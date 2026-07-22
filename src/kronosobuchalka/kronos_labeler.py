from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from .binance_archive import KLINE_COLUMNS, load_candle_file


@dataclass(frozen=True)
class KronosPaths:
    code_dir: Path
    weights_dir: Path
    model_name: str = "base"


@dataclass(frozen=True)
class LabelConfig:
    context_rows: int = 512
    pred_len: int = 1
    sample_count: int = 10
    temperature: float = 0.6
    top_p: float = 0.9
    device: str = "auto"
    checkpoint_every: int = 0


def label_candle_files(
    *,
    candle_paths: Mapping[str, str | Path],
    output_dir: str | Path,
    kronos_paths: KronosPaths,
    config: LabelConfig,
    from_time: str | None = None,
    till_time: str | None = None,
    overwrite: bool = False,
) -> pd.DataFrame:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    predictor = _load_predictor(kronos_paths, config=config)
    all_rows: list[pd.DataFrame] = []
    checkpoint_dir = output_path / "checkpoints" if int(config.checkpoint_every) > 0 else None
    if checkpoint_dir is not None:
        checkpoint_dir.mkdir(parents=True, exist_ok=True)

    for symbol, path in candle_paths.items():
        secid = str(symbol).upper()
        candles = load_candle_file(path)
        rows = label_symbol_frame(
            secid=secid,
            candles=candles,
            predictor=predictor,
            kronos_paths=kronos_paths,
            config=config,
            from_time=from_time,
            till_time=till_time,
            checkpoint_path=(checkpoint_dir / f"labels_{secid}.partial.csv") if checkpoint_dir is not None else None,
        )
        out_file = output_path / f"labels_{secid}.csv"
        if out_file.exists() and not overwrite:
            raise FileExistsError(f"output exists: {out_file}; pass overwrite=True or --overwrite")
        rows.to_csv(out_file, index=False)
        all_rows.append(rows)

    combined = pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()
    combined = combined.sort_values(["as_of", "secid"]).reset_index(drop=True) if not combined.empty else combined
    combined_file = output_path / "labels_all.csv"
    if combined_file.exists() and not overwrite:
        raise FileExistsError(f"output exists: {combined_file}; pass overwrite=True or --overwrite")
    combined.to_csv(combined_file, index=False)
    (output_path / "summary.json").write_text(
        json.dumps(build_label_summary(combined), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return combined


def label_symbol_frame(
    *,
    secid: str,
    candles: pd.DataFrame,
    predictor: Any,
    kronos_paths: KronosPaths,
    config: LabelConfig,
    from_time: str | None = None,
    till_time: str | None = None,
    checkpoint_path: str | Path | None = None,
) -> pd.DataFrame:
    frame = candles.copy()
    frame["timestamps"] = pd.to_datetime(frame["timestamps"], errors="coerce")
    frame = frame.dropna(subset=["timestamps"]).sort_values("timestamps").reset_index(drop=True)
    for col in KLINE_COLUMNS:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)

    start_idx = max(int(config.context_rows), 2) - 1
    if from_time:
        start_ts = pd.Timestamp(from_time)
        candidates = frame.index[frame["timestamps"] >= start_ts].tolist()
        if candidates:
            start_idx = max(start_idx, int(candidates[0]))
    end_idx = len(frame) - max(int(config.pred_len), 1) - 1
    if till_time:
        till_ts = pd.Timestamp(till_time)
        candidates = frame.index[frame["timestamps"] <= till_ts].tolist()
        if candidates:
            end_idx = min(end_idx, int(candidates[-1]))

    rows: list[dict[str, Any]] = []
    checkpoint_interval = max(int(config.checkpoint_every), 0)
    total = max(end_idx - start_idx + 1, 0)
    for idx in range(start_idx, max(end_idx + 1, start_idx)):
        history = frame.iloc[: idx + 1].tail(max(int(config.context_rows), 2)).reset_index(drop=True)
        if len(history) < 2:
            continue
        target_idx = idx + max(int(config.pred_len), 1)
        if target_idx >= len(frame):
            break
        target = frame.iloc[target_idx]
        pred = _predict_one(history=history, predictor=predictor, config=config)
        if pred is None:
            continue
        last_close = _safe_float(history["close"].iloc[-1])
        actual_open = _safe_float(target["open"])
        actual_close = _safe_float(target["close"])
        raw_pred_move_pct = pred["close"] / actual_open - 1.0 if actual_open > 0 else math.nan
        actual_body_return_pct = actual_close / actual_open - 1.0 if actual_open > 0 else math.nan
        rows.append(
            {
                "secid": secid,
                "as_of": pd.Timestamp(history["timestamps"].iloc[-1]).isoformat(),
                "target_timestamp": pd.Timestamp(target["timestamps"]).isoformat(),
                "last_close": last_close,
                "actual_open": actual_open,
                "actual_high": _safe_float(target["high"]),
                "actual_low": _safe_float(target["low"]),
                "actual_close": actual_close,
                "actual_volume": _safe_float(target.get("volume", 0.0)),
                "actual_amount": _safe_float(target.get("amount", 0.0)),
                "pred_open": pred["open"],
                "pred_high": pred["high"],
                "pred_low": pred["low"],
                "pred_close": pred["close"],
                "pred_volume": pred["volume"],
                "pred_amount": pred["amount"],
                "pred_return_vs_last_close": pred["close"] / last_close - 1.0 if last_close > 0 else math.nan,
                "raw_pred_move_pct": raw_pred_move_pct,
                "pred_abs_move_pct": abs(raw_pred_move_pct) if math.isfinite(raw_pred_move_pct) else math.nan,
                "pred_side": "long" if raw_pred_move_pct > 0 else "short",
                "actual_body_return_pct": actual_body_return_pct,
                "actual_direction": 1 if actual_body_return_pct > 0 else (-1 if actual_body_return_pct < 0 else 0),
                "direction_hit": bool((raw_pred_move_pct > 0 and actual_body_return_pct > 0) or (raw_pred_move_pct < 0 and actual_body_return_pct < 0)),
                "model": kronos_paths.model_name,
                "context_rows": int(config.context_rows),
                "pred_len": int(config.pred_len),
                "sample_count": int(config.sample_count),
                "temperature": float(config.temperature),
                "top_p": float(config.top_p),
            }
        )
        if checkpoint_path is not None and checkpoint_interval > 0 and len(rows) % checkpoint_interval == 0:
            _write_checkpoint(rows, checkpoint_path)
            print(f"{secid}: checkpoint {len(rows)}/{total} -> {checkpoint_path}", flush=True)
    if checkpoint_path is not None and checkpoint_interval > 0:
        _write_checkpoint(rows, checkpoint_path)
        print(f"{secid}: checkpoint {len(rows)}/{total} -> {checkpoint_path}", flush=True)
    return pd.DataFrame(rows)


def _write_checkpoint(rows: Sequence[Mapping[str, Any]], path: str | Path) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out, index=False)


def build_label_summary(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {"rows": 0, "symbols": []}
    out: dict[str, Any] = {
        "rows": int(len(frame)),
        "symbols": sorted(str(value) for value in frame["secid"].dropna().unique()),
        "as_of_min": str(frame["as_of"].min()),
        "as_of_max": str(frame["as_of"].max()),
        "direction_hit_rate": _mean_bool(frame["direction_hit"]),
        "pred_abs_move_mean_bps": _mean(frame["pred_abs_move_pct"]) * 10_000.0,
        "actual_abs_move_mean_bps": _mean(frame["actual_body_return_pct"].abs()) * 10_000.0,
    }
    by_symbol = []
    for secid, group in frame.groupby("secid"):
        by_symbol.append(
            {
                "secid": secid,
                "rows": int(len(group)),
                "direction_hit_rate": _mean_bool(group["direction_hit"]),
                "pred_abs_move_mean_bps": _mean(group["pred_abs_move_pct"]) * 10_000.0,
                "actual_abs_move_mean_bps": _mean(group["actual_body_return_pct"].abs()) * 10_000.0,
            }
        )
    out["by_symbol"] = by_symbol
    return out


def discover_candle_paths(candles_dir: str | Path, symbols: Sequence[str]) -> dict[str, Path]:
    root = Path(candles_dir)
    out: dict[str, Path] = {}
    for symbol in symbols:
        secid = str(symbol).upper()
        for name in (f"candles_{secid}.csv", f"candles_1h_{secid}.csv"):
            path = root / name
            if path.exists():
                out[secid] = path
                break
    missing = [str(symbol).upper() for symbol in symbols if str(symbol).upper() not in out]
    if missing:
        raise FileNotFoundError(f"missing candle files for: {', '.join(missing)} in {root}")
    return out


def _predict_one(*, history: pd.DataFrame, predictor: Any, config: LabelConfig) -> dict[str, float] | None:
    pred_len = max(int(config.pred_len), 1)
    y_timestamp = _future_timestamps(history, pred_len=pred_len)
    try:
        if hasattr(predictor, "predict_samples") and int(config.sample_count) > 1:
            samples = predictor.predict_samples(
                df=history[KLINE_COLUMNS],
                x_timestamp=history["timestamps"],
                y_timestamp=y_timestamp,
                pred_len=pred_len,
                T=float(config.temperature),
                top_p=float(config.top_p),
                sample_count=max(int(config.sample_count), 1),
                verbose=False,
            )
            return {
                col: float(np.nanmean(samples[:, pred_len - 1, idx]))
                for idx, col in enumerate(KLINE_COLUMNS)
            }
        pred = predictor.predict(
            df=history[KLINE_COLUMNS],
            x_timestamp=history["timestamps"],
            y_timestamp=y_timestamp,
            pred_len=pred_len,
            T=float(config.temperature),
            top_p=float(config.top_p),
            sample_count=max(int(config.sample_count), 1),
            verbose=False,
        )
        last_pred = pred.iloc[-1]
        return {col: _safe_float(last_pred[col]) if col in pred.columns else 0.0 for col in KLINE_COLUMNS}
    except Exception:
        return None


def _load_predictor(paths: KronosPaths, *, config: LabelConfig) -> Any:
    code_dir = Path(paths.code_dir)
    repo_root = code_dir.parent if code_dir.name == "model" else code_dir
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from model import Kronos, KronosPredictor, KronosTokenizer  # type: ignore

    model_dir = _model_dir(Path(paths.weights_dir), paths.model_name)
    tokenizer_dir = _tokenizer_dir(Path(paths.weights_dir), paths.model_name)
    tokenizer = KronosTokenizer.from_pretrained(str(tokenizer_dir))
    model = Kronos.from_pretrained(str(model_dir))
    tokenizer.eval()
    model.eval()
    return KronosPredictor(
        model,
        tokenizer,
        device=_resolve_device(config.device),
        max_context=min(_native_context(paths.model_name), max(int(config.context_rows), 2)),
    )


def _future_timestamps(history: pd.DataFrame, *, pred_len: int) -> pd.Series:
    timestamps = pd.to_datetime(history["timestamps"], errors="coerce").dropna().reset_index(drop=True)
    if len(timestamps) >= 2:
        delta = timestamps.iloc[-1] - timestamps.iloc[-2]
        if pd.notna(delta) and delta.total_seconds() > 0:
            first = timestamps.iloc[-1] + delta
            return pd.Series([first + idx * delta for idx in range(max(int(pred_len), 1))])
    first = timestamps.iloc[-1] + pd.Timedelta(hours=1)
    return pd.Series([first + pd.Timedelta(hours=idx) for idx in range(max(int(pred_len), 1))])


def _model_dir(weights_dir: Path, model_name: str) -> Path:
    candidates = [
        weights_dir / f"NeoQuasar__Kronos-{model_name}",
        weights_dir / f"Kronos-{model_name}",
        weights_dir / model_name,
    ]
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def _tokenizer_dir(weights_dir: Path, model_name: str) -> Path:
    candidates = [
        weights_dir / f"NeoQuasar__Kronos-Tokenizer-{model_name}",
        weights_dir / f"Kronos-Tokenizer-{model_name}",
        weights_dir / f"Tokenizer-{model_name}",
    ]
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def _native_context(model_name: str) -> int:
    name = str(model_name).lower()
    if "mini" in name:
        return 512
    if "small" in name:
        return 512
    return 512


def _resolve_device(device: str) -> str:
    value = str(device or "auto").lower()
    if value != "auto":
        return value
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def _safe_float(value: Any) -> float:
    try:
        out = float(value)
    except Exception:
        return math.nan
    return out if math.isfinite(out) else math.nan


def _mean(values: Iterable[Any]) -> float:
    series = pd.to_numeric(pd.Series(values), errors="coerce").dropna()
    return float(series.mean()) if len(series) else math.nan


def _mean_bool(values: Iterable[Any]) -> float:
    series = pd.Series(values).dropna()
    return float(series.astype(bool).mean()) if len(series) else math.nan
