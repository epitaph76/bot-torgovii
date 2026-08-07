from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from ..binance_archive import KLINE_COLUMNS
from ..kronos_labeler import KronosPaths, LabelConfig, _load_predictor, _predict_one
from .client import quantize_down
from .config import BybitDemoBotConfig, StrategyConfig, SymbolStrategyFilter
from .market import InstrumentSpec, MarketBundle


@dataclass(frozen=True)
class ForecastRow:
    secid: str
    pred_open: float
    pred_high: float
    pred_low: float
    pred_close: float
    pred_volume: float
    pred_amount: float


@dataclass(frozen=True)
class StrategyDecision:
    as_of: str
    target_timestamp: str
    ranked_rows: list[dict[str, Any]]
    selected: dict[str, Any] | None
    status: str
    reason: str


class KronosLiveForecaster:
    def __init__(self, *, config: BybitDemoBotConfig):
        self.config = config
        self._predictor = None

    def forecast(self, bundle: MarketBundle) -> dict[str, ForecastRow]:
        predictor = self._ensure_predictor()
        out: dict[str, ForecastRow] = {}
        kronos_cfg = self.config.kronos
        label_cfg = LabelConfig(
            context_rows=int(kronos_cfg.context_rows),
            pred_len=int(kronos_cfg.pred_len),
            sample_count=int(kronos_cfg.sample_count),
            temperature=float(kronos_cfg.temperature),
            top_p=float(kronos_cfg.top_p),
            device=str(kronos_cfg.device),
        )
        for symbol in self.config.strategy.symbols:
            history = bundle.candles[symbol].tail(max(int(kronos_cfg.context_rows), 2)).reset_index(drop=True)
            pred = _predict_one(history=history, predictor=predictor, config=label_cfg)
            if pred is None:
                continue
            out[symbol] = ForecastRow(
                secid=symbol,
                pred_open=float(pred["open"]),
                pred_high=float(pred["high"]),
                pred_low=float(pred["low"]),
                pred_close=float(pred["close"]),
                pred_volume=float(pred["volume"]),
                pred_amount=float(pred["amount"]),
            )
        return out

    def _ensure_predictor(self):
        if self._predictor is None:
            kronos_cfg = self.config.kronos
            self._predictor = _load_predictor(
                KronosPaths(
                    code_dir=Path(kronos_cfg.code_dir),
                    weights_dir=Path(kronos_cfg.weights_dir),
                    model_name=str(kronos_cfg.model),
                ),
                config=LabelConfig(
                    context_rows=int(kronos_cfg.context_rows),
                    pred_len=int(kronos_cfg.pred_len),
                    sample_count=int(kronos_cfg.sample_count),
                    temperature=float(kronos_cfg.temperature),
                    top_p=float(kronos_cfg.top_p),
                    device=str(kronos_cfg.device),
                ),
            )
        return self._predictor


def build_strategy_decision(
    *,
    bundle: MarketBundle,
    forecasts: Mapping[str, ForecastRow],
    config: BybitDemoBotConfig,
) -> StrategyDecision:
    rows = []
    for symbol in config.strategy.symbols:
        forecast = forecasts.get(symbol)
        if forecast is None:
            continue
        rows.append(
            _build_feature_row(
                symbol=symbol,
                bundle=bundle,
                forecast=forecast,
                strategy=config.strategy,
                symbol_filter=config.strategy.filter_for(symbol),
            )
        )
    if not rows:
        return StrategyDecision(
            as_of=bundle.decision_as_of.isoformat(),
            target_timestamp=bundle.target_timestamp.isoformat(),
            ranked_rows=[],
            selected=None,
            status="blocked",
            reason="no_kronos_forecasts",
        )
    frame = pd.DataFrame(rows).sort_values(["net_pred_move_pct", "secid"], ascending=[False, True]).reset_index(drop=True)
    frame["rank"] = np.arange(1, len(frame) + 1)
    if config.strategy.ranking_mode == "rank_cascade":
        selected, status, reason = _select_rank_cascade(frame)
    else:
        selected, status, reason = _select_rank1_only(frame)
    return StrategyDecision(
        as_of=bundle.decision_as_of.isoformat(),
        target_timestamp=bundle.target_timestamp.isoformat(),
        ranked_rows=_json_rows(frame),
        selected=_json_row(selected) if selected is not None else None,
        status=status,
        reason=reason,
    )


def active_can_hold(
    *,
    active: Mapping[str, Any] | None,
    decision: StrategyDecision,
    config: BybitDemoBotConfig,
) -> tuple[bool, str]:
    if not active:
        return False, "no_active_episode"
    expected_as_of = active.get("target_timestamp")
    if expected_as_of and pd.Timestamp(expected_as_of) != pd.Timestamp(decision.as_of):
        return False, "episode_time_gap"
    active_symbol = str(active.get("symbol"))
    ranked_rows = _ranked_decision_rows(decision)
    if not ranked_rows:
        return False, "no_ranked_candidates"
    if config.strategy.ranking_mode == "rank1_only":
        return _active_can_hold_rank1_only(active_symbol=active_symbol, ranked_rows=ranked_rows, config=config)
    active_row = None
    for row in ranked_rows:
        if str(row.get("secid")) == active_symbol:
            active_row = row
            break
        if bool(row.get("entry_selected")):
            return False, "higher_rank_entry_candidate_selected"
    if active_row is None:
        return False, "active_symbol_not_in_ranked_candidates"
    if str(active_row.get("final_side")) != str(active.get("side")):
        return False, "selected_side_changed"
    symbol_filter = config.strategy.filter_for(active_symbol)
    net = _finite(active_row.get("net_pred_move_bps"))
    vol = _finite(active_row.get("vol_ratio_168"))
    if net < float(symbol_filter.exit_net_pred_move_bps_min):
        return False, "hold_net_pred_move_bps_below_exit_threshold"
    if not math.isfinite(vol) or vol < float(symbol_filter.exit_vol_ratio_168_min):
        return False, "hold_vol_ratio_168_below_exit_threshold"
    return True, "hold_decay_passed"


def _select_rank1_only(frame: pd.DataFrame) -> tuple[dict[str, Any] | None, str, str]:
    rows = frame.to_dict(orient="records")
    if not rows:
        return None, "cash", "no_ranked_candidates"
    rank1 = rows[0]
    if bool(rank1.get("entry_selected")):
        return rank1, "selected", "rank1_passed_entry_filter"
    return None, "cash", "rank1_failed_entry_filter"


def _select_rank_cascade(frame: pd.DataFrame) -> tuple[dict[str, Any] | None, str, str]:
    for row in frame.to_dict(orient="records"):
        if bool(row.get("entry_selected")):
            return row, "selected", f"rank{int(row['rank'])}_passed_entry_filter"
    return None, "cash", "no_ranked_candidate_passed_entry_filter"


def _active_can_hold_rank1_only(
    *,
    active_symbol: str,
    ranked_rows: list[dict[str, Any]],
    config: BybitDemoBotConfig,
) -> tuple[bool, str]:
    active_row = None
    for row in ranked_rows:
        if str(row.get("secid")) == active_symbol:
            active_row = row
            break
    if active_row is None:
        return False, "active_symbol_not_in_ranked_candidates"
    symbol_filter = config.strategy.filter_for(active_symbol)
    net = _finite(active_row.get("net_pred_move_bps"))
    vol = _finite(active_row.get("vol_ratio_168"))
    if net < float(symbol_filter.exit_net_pred_move_bps_min):
        return False, "hold_net_pred_move_bps_below_exit_threshold"
    if not math.isfinite(vol) or vol < float(symbol_filter.exit_vol_ratio_168_min):
        return False, "hold_vol_ratio_168_below_exit_threshold"
    return True, "hold_decay_passed"


def active_decision_row(*, active: Mapping[str, Any] | None, decision: StrategyDecision) -> dict[str, Any] | None:
    if not active:
        return None
    active_symbol = str(active.get("symbol"))
    for row in _ranked_decision_rows(decision):
        if str(row.get("secid")) == active_symbol:
            return row
    return None


def stop_price(*, side: str, entry_price: Decimal, stop_loss_pct: float, spec: InstrumentSpec) -> Decimal:
    if side == "long":
        raw = entry_price * (Decimal("1") - Decimal(str(stop_loss_pct)))
    else:
        raw = entry_price * (Decimal("1") + Decimal(str(stop_loss_pct)))
    return quantize_down(raw, spec.tick_size)


def order_qty(
    *,
    equity: Decimal,
    leverage: float,
    price: Decimal,
    spec: InstrumentSpec,
    max_notional_usdt: Decimal | float | None = None,
) -> Decimal:
    if price <= 0:
        return Decimal("0")
    notional = equity * Decimal(str(leverage))
    if max_notional_usdt not in (None, ""):
        cap = Decimal(str(max_notional_usdt))
        if cap > 0:
            notional = min(notional, cap)
    qty = quantize_down(notional / price, spec.qty_step)
    if qty < spec.min_order_qty:
        return Decimal("0")
    if spec.min_notional > 0 and qty * price < spec.min_notional:
        return Decimal("0")
    if spec.max_market_qty > 0:
        qty = min(qty, spec.max_market_qty)
    return quantize_down(qty, spec.qty_step)


def _build_feature_row(
    *,
    symbol: str,
    bundle: MarketBundle,
    forecast: ForecastRow,
    strategy: StrategyConfig,
    symbol_filter: SymbolStrategyFilter,
) -> dict[str, Any]:
    history = bundle.candles[symbol].copy()
    for column in KLINE_COLUMNS:
        history[column] = pd.to_numeric(history[column], errors="coerce")
    forming_open = float(bundle.forming_open[symbol])
    if forming_open <= 0:
        forming_open = float(history["close"].iloc[-1])

    actual_body_return = history["close"] / history["open"] - 1.0
    pred_close = float(forecast.pred_close)
    raw_pred_move_pct = pred_close / forming_open - 1.0 if forming_open > 0 else math.nan
    pred_abs_move_pct = abs(raw_pred_move_pct) if math.isfinite(raw_pred_move_pct) else math.nan
    pred_direction = 1 if raw_pred_move_pct > 0 else -1
    pred_side = "long" if pred_direction > 0 else "short"

    cost = _live_execution_cost(history, strategy)
    vol_168 = _tail_std(actual_body_return, 168, _auto_min_periods(168))
    vol_ratio_168 = pred_abs_move_pct / vol_168 if math.isfinite(vol_168) and vol_168 > 1e-12 else math.nan
    entry_vol_lookback = max(int(symbol_filter.entry_vol_ratio_lookback_hours), 1)
    entry_vol = _tail_std(actual_body_return, entry_vol_lookback, _auto_min_periods(entry_vol_lookback))
    entry_vol_ratio = pred_abs_move_pct / entry_vol if math.isfinite(entry_vol) and entry_vol > 1e-12 else math.nan

    hl = _live_hl_metrics(history, forecast, forming_open, pred_direction)
    is_extreme = (
        bool(symbol_filter.flip_enabled)
        and _finite(hl["hl_relevant_wick_ratio_168"]) >= float(symbol_filter.hl_relevant_wick_ratio_168_min)
        and _finite(hl["hl_range_ratio_168"]) >= float(symbol_filter.hl_range_ratio_168_min)
        and _finite(hl["pred_relevant_wick_bps"]) >= float(symbol_filter.pred_relevant_wick_bps_min)
        and _finite(hl["pred_range_bps"]) >= float(symbol_filter.pred_range_bps_min)
    )
    final_direction = -pred_direction if is_extreme else pred_direction
    final_side = "long" if final_direction > 0 else "short"
    net_pred_move_pct = pred_abs_move_pct - cost["execution_cost_pct"]
    net_pred_move_bps = net_pred_move_pct * 10_000.0
    execution_cost_bps = cost["execution_cost_pct"] * 10_000.0
    cost_cap = symbol_filter.entry_execution_cost_bps_max
    entry_selected = bool(
        math.isfinite(entry_vol_ratio)
        and entry_vol_ratio >= float(symbol_filter.entry_vol_ratio_min)
        and math.isfinite(net_pred_move_bps)
        and net_pred_move_bps >= float(symbol_filter.entry_net_pred_move_bps_min)
        and (cost_cap is None or (math.isfinite(execution_cost_bps) and execution_cost_bps <= float(cost_cap)))
    )
    return {
        "secid": symbol,
        "as_of": bundle.decision_as_of.isoformat(),
        "target_timestamp": bundle.target_timestamp.isoformat(),
        "actual_open": forming_open,
        **asdict(forecast),
        "raw_pred_move_pct": raw_pred_move_pct,
        "pred_abs_move_pct": pred_abs_move_pct,
        "pred_abs_move_bps": pred_abs_move_pct * 10_000.0,
        "pred_direction": pred_direction,
        "pred_side": pred_side,
        "execution_cost_pct": cost["execution_cost_pct"],
        "execution_cost_bps": execution_cost_bps,
        "estimated_spread_pct": cost["estimated_spread_pct"],
        "liquidity_penalty": cost["liquidity_penalty"],
        "slippage_one_way_pct": cost["slippage_one_way_pct"],
        "corwin_schultz_spread_pct": cost["corwin_schultz_spread_pct"],
        "range_spread_fallback_pct": cost["range_spread_fallback_pct"],
        "net_pred_move_pct": net_pred_move_pct,
        "net_pred_move_bps": net_pred_move_bps,
        "rolling_vol_168_pct": vol_168,
        "vol_ratio_168": vol_ratio_168,
        "entry_vol_lookback_hours": entry_vol_lookback,
        "rolling_vol_entry_pct": entry_vol,
        "entry_vol_ratio": entry_vol_ratio,
        "entry_vol_ratio_min": float(symbol_filter.entry_vol_ratio_min),
        "entry_net_pred_move_bps_min": float(symbol_filter.entry_net_pred_move_bps_min),
        "entry_execution_cost_bps_max": cost_cap,
        "exit_net_pred_move_bps_min": float(symbol_filter.exit_net_pred_move_bps_min),
        "exit_vol_ratio_168_min": float(symbol_filter.exit_vol_ratio_168_min),
        "flip_enabled": bool(symbol_filter.flip_enabled),
        "flip_hl_relevant_wick_ratio_168_min": float(symbol_filter.hl_relevant_wick_ratio_168_min),
        "flip_hl_range_ratio_168_min": float(symbol_filter.hl_range_ratio_168_min),
        "flip_pred_relevant_wick_bps_min": float(symbol_filter.pred_relevant_wick_bps_min),
        "flip_pred_range_bps_min": float(symbol_filter.pred_range_bps_min),
        **hl,
        "is_extreme": is_extreme,
        "flip_applied": is_extreme,
        "final_direction": final_direction,
        "final_side": final_side,
        "entry_selected": entry_selected,
    }


def _live_execution_cost(history: pd.DataFrame, strategy: StrategyConfig) -> dict[str, float]:
    spread_cs = _corwin_schultz_last(history)
    fallback = _range_fallback_last(history, strategy)
    estimated_spread = spread_cs if math.isfinite(spread_cs) else fallback
    if not math.isfinite(estimated_spread):
        estimated_spread = float(strategy.minimum_spread_pct)
    estimated_spread = min(max(estimated_spread, float(strategy.minimum_spread_pct)), float(strategy.maximum_spread_pct))
    penalty = _liquidity_penalty_last(history, strategy)
    slippage = estimated_spread * float(strategy.slippage_spread_multiplier) * penalty
    execution_cost = estimated_spread + 2.0 * float(strategy.commission_rate) + 2.0 * slippage
    return {
        "corwin_schultz_spread_pct": spread_cs,
        "range_spread_fallback_pct": fallback,
        "estimated_spread_pct": estimated_spread,
        "liquidity_penalty": penalty,
        "slippage_one_way_pct": slippage,
        "execution_cost_pct": execution_cost,
    }


def _corwin_schultz_last(history: pd.DataFrame) -> float:
    tail = history.dropna(subset=["high", "low"]).tail(2)
    if len(tail) < 2:
        return math.nan
    high = pd.to_numeric(tail["high"], errors="coerce")
    low = pd.to_numeric(tail["low"], errors="coerce")
    if (high <= 0).any() or (low <= 0).any() or (high < low).any():
        return math.nan
    log_hl = np.log(high / low)
    beta = float((log_hl.pow(2.0)).sum())
    two_high = float(high.max())
    two_low = float(low.min())
    gamma = math.log(two_high / two_low) ** 2 if two_low > 0 and two_high >= two_low else math.nan
    denom = 3.0 - 2.0 * math.sqrt(2.0)
    try:
        alpha = ((math.sqrt(2.0 * beta) - math.sqrt(beta)) / denom) - math.sqrt(gamma / denom)
        alpha = max(alpha, 0.0)
        spread = 2.0 * (math.exp(alpha) - 1.0) / (1.0 + math.exp(alpha))
    except Exception:
        return math.nan
    return spread if math.isfinite(spread) else math.nan


def _range_fallback_last(history: pd.DataFrame, strategy: StrategyConfig) -> float:
    if history.empty:
        return math.nan
    row = history.iloc[-1]
    high = _finite(row.get("high"))
    low = _finite(row.get("low"))
    mid = (high + low) / 2.0
    if mid <= 0 or high < low:
        return math.nan
    return ((high - low) / mid) * float(strategy.range_fallback_fraction)


def _liquidity_penalty_last(history: pd.DataFrame, strategy: StrategyConfig) -> float:
    volumes = pd.to_numeric(history.get("volume", pd.Series(dtype=float)), errors="coerce").dropna()
    if volumes.empty:
        return 1.0
    previous = float(volumes.iloc[-1])
    median_window = volumes.tail(max(int(strategy.liquidity_lookback), 1))
    if len(median_window) < int(strategy.liquidity_min_periods) or previous <= 0:
        return 1.0
    penalty = math.sqrt(float(median_window.median()) / max(previous, 1e-12))
    return min(max(penalty, float(strategy.liquidity_penalty_min)), float(strategy.liquidity_penalty_max))


def _live_hl_metrics(history: pd.DataFrame, forecast: ForecastRow, actual_open: float, pred_direction: int) -> dict[str, float | bool]:
    actual_up_bps = (history["high"] / history["open"] - 1.0) * 10_000.0
    actual_down_bps = (history["open"] / history["low"] - 1.0) * 10_000.0
    actual_range_bps = actual_up_bps + actual_down_bps
    roll_up = _tail_mean(actual_up_bps, 168, 24)
    roll_down = _tail_mean(actual_down_bps, 168, 24)
    roll_range = _tail_mean(actual_range_bps, 168, 24)
    pred_up_raw = (float(forecast.pred_high) / actual_open - 1.0) * 10_000.0 if actual_open > 0 else math.nan
    pred_down_raw = (actual_open / float(forecast.pred_low) - 1.0) * 10_000.0 if float(forecast.pred_low) > 0 else math.nan
    pred_up = max(pred_up_raw, 0.0) if math.isfinite(pred_up_raw) else math.nan
    pred_down = max(pred_down_raw, 0.0) if math.isfinite(pred_down_raw) else math.nan
    pred_range = pred_up + pred_down if math.isfinite(pred_up) and math.isfinite(pred_down) else math.nan
    hl_skew = pred_up - pred_down if math.isfinite(pred_range) else math.nan
    relevant_wick = pred_up if pred_direction >= 0 else pred_down
    opposite_wick = pred_down if pred_direction >= 0 else pred_up
    roll_relevant = roll_up if pred_direction >= 0 else roll_down
    return {
        "actual_up_bps_last_mean_168": roll_up,
        "actual_down_bps_last_mean_168": roll_down,
        "actual_range_bps_last_mean_168": roll_range,
        "pred_up_bps": pred_up,
        "pred_down_bps": pred_down,
        "pred_range_bps": pred_range,
        "pred_relevant_wick_bps": relevant_wick,
        "pred_opposite_wick_bps": opposite_wick,
        "hl_skew_bps": hl_skew,
        "hl_abs_skew_bps": abs(hl_skew) if math.isfinite(hl_skew) else math.nan,
        "hl_direction_agrees_close": bool(hl_skew > 0) if pred_direction >= 0 else bool(hl_skew < 0),
        "hl_relevant_wick_ratio_168": relevant_wick / roll_relevant if math.isfinite(roll_relevant) and roll_relevant > 0 else math.nan,
        "hl_range_ratio_168": pred_range / roll_range if math.isfinite(roll_range) and roll_range > 0 else math.nan,
    }


def _tail_mean(series: pd.Series, lookback: int, min_periods: int) -> float:
    clean = pd.to_numeric(series, errors="coerce").dropna().tail(int(lookback))
    if len(clean) < int(min_periods):
        return math.nan
    return float(clean.mean())


def _tail_std(series: pd.Series, lookback: int, min_periods: int) -> float:
    clean = pd.to_numeric(series, errors="coerce").dropna().tail(int(lookback))
    if len(clean) < int(min_periods):
        return math.nan
    return float(clean.std(ddof=0))


def _auto_min_periods(lookback: int) -> int:
    return min(int(lookback), max(6, int(lookback) // 3))


def _finite(value: Any) -> float:
    try:
        out = float(value)
    except Exception:
        return math.nan
    return out if math.isfinite(out) else math.nan


def _json_rows(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [_json_row(row) for row in frame.to_dict(orient="records")]


def _ranked_decision_rows(decision: StrategyDecision) -> list[dict[str, Any]]:
    rows = list(getattr(decision, "ranked_rows", []) or [])
    selected = getattr(decision, "selected", None)
    if not rows and selected:
        rows = [selected]
    return sorted(rows, key=lambda row: int(row.get("rank") or 10**9))


def _json_row(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, np.generic):
            value = value.item()
        if isinstance(value, pd.Timestamp):
            value = value.isoformat()
        if isinstance(value, float) and not math.isfinite(value):
            value = None
        out[str(key)] = value
    return out
