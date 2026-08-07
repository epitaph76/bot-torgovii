from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Sequence

import pandas as pd
import requests

from .binance_archive import (
    BINANCE_VISION_BASE,
    coverage_for_file,
    download_candles,
)
from .kronos_labeler import (
    KronosPaths,
    LabelConfig,
    discover_candle_paths,
    label_candle_files,
)
from .bybit_demo.commands import add_bybit_demo_parser, handle_bybit_demo
from .okx_demo.commands import add_okx_demo_parser, handle_okx_demo
from .bybit_demo.commands import DEFAULT_CONFIG_PATH


DEFAULT_SYMBOLS = ("TONUSDT", "ZECUSDT")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "download":
        return _cmd_download(args)
    if args.command == "coverage":
        return _cmd_coverage(args)
    if args.command == "label":
        return _cmd_label(args)
    if args.command == "check-remote":
        return _cmd_check_remote(args)
    if args.command == "bybit-demo":
        return handle_bybit_demo(args)
    if args.command == "okx-demo":
        return handle_okx_demo(args)
    if args.command == "tui":
        return _cmd_tui(args)
    parser.print_help()
    return 2


def _cmd_download(args: argparse.Namespace) -> int:
    counts = download_candles(
        symbols=_parse_symbols(args.symbols),
        out_dir=args.out_dir,
        from_date=args.from_date,
        till_date=args.till_date,
        interval=args.interval,
        timestamp_offset_hours=int(args.timestamp_offset_hours),
        source=args.source,
        sleep_seconds=float(args.sleep_seconds),
    )
    for symbol, count in counts.items():
        print(f"{symbol}: {count} rows -> {Path(args.out_dir) / f'candles_{symbol}.csv'}")
    return 0


def _cmd_coverage(args: argparse.Namespace) -> int:
    rows = []
    for symbol in _parse_symbols(args.symbols):
        path = Path(args.candles_dir) / f"candles_{symbol}.csv"
        item = coverage_for_file(path, symbol=symbol, from_date=args.from_date, till_date=args.till_date, interval=args.interval)
        rows.append(item.__dict__)
    frame = pd.DataFrame(rows)
    print(frame.to_string(index=False))
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(out, index=False)
    return 0


def _cmd_label(args: argparse.Namespace) -> int:
    symbols = _parse_symbols(args.symbols)
    candle_paths = discover_candle_paths(args.candles_dir, symbols)
    labels = label_candle_files(
        candle_paths=candle_paths,
        output_dir=args.output_dir,
        kronos_paths=KronosPaths(
            code_dir=Path(args.kronos_code_dir),
            weights_dir=Path(args.kronos_weights_dir),
            model_name=str(args.model),
        ),
        config=LabelConfig(
            context_rows=int(args.context_rows),
            pred_len=int(args.pred_len),
            sample_count=int(args.sample_count),
            temperature=float(args.temperature),
            top_p=float(args.top_p),
            device=str(args.device),
            checkpoint_every=int(args.checkpoint_every),
        ),
        from_time=args.from_time,
        till_time=args.till_time,
        overwrite=bool(args.overwrite),
    )
    print(f"rows: {len(labels)}")
    print(f"output: {args.output_dir}")
    if not labels.empty:
        print(json.dumps(_compact_summary(labels), ensure_ascii=False, indent=2))
    return 0


def _cmd_check_remote(args: argparse.Namespace) -> int:
    symbols = _parse_symbols(args.symbols)
    start = pd.Timestamp(args.from_date).date()
    end = pd.Timestamp(args.till_date).date()
    rows = []
    for symbol in symbols:
        for month in _months(start, end):
            status, size = _head(_monthly_url(symbol, args.interval, month))
            rows.append(
                {
                    "symbol": symbol,
                    "scope": "monthly",
                    "period": f"{month.year:04d}-{month.month:02d}",
                    "status": status,
                    "size_bytes": size,
                }
            )
        if bool(args.daily):
            for day in _days(start, end):
                status, size = _head(_daily_url(symbol, args.interval, day))
                rows.append(
                    {
                        "symbol": symbol,
                        "scope": "daily",
                        "period": day.isoformat(),
                        "status": status,
                        "size_bytes": size,
                    }
                )
    frame = pd.DataFrame(rows)
    print(frame.to_string(index=False))
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(out, index=False)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kronosobuchalka")
    sub = parser.add_subparsers(dest="command")

    download = sub.add_parser("download", help="Download hourly candles from Binance Vision archive.")
    download.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    download.add_argument("--from", dest="from_date", required=True)
    download.add_argument("--till", dest="till_date", required=True)
    download.add_argument("--interval", default="1h")
    download.add_argument("--out-dir", default="data/candles")
    download.add_argument("--source", choices=("archive", "api"), default="archive")
    download.add_argument("--timestamp-offset-hours", type=int, default=3)
    download.add_argument("--sleep-seconds", type=float, default=0.1)

    coverage = sub.add_parser("coverage", help="Check local candle file coverage.")
    coverage.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    coverage.add_argument("--from", dest="from_date", required=True)
    coverage.add_argument("--till", dest="till_date", required=True)
    coverage.add_argument("--interval", default="1h")
    coverage.add_argument("--candles-dir", default="data/candles")
    coverage.add_argument("--output", default="")

    label = sub.add_parser("label", help="Run Kronos over candle files and write labels.")
    label.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    label.add_argument("--candles-dir", default="data/candles")
    label.add_argument("--output-dir", default="labels/kronos")
    label.add_argument("--kronos-code-dir", default="kronos/model")
    label.add_argument("--kronos-weights-dir", default="kronos/weights")
    label.add_argument("--model", default="base")
    label.add_argument("--context-rows", type=int, default=512)
    label.add_argument("--pred-len", type=int, default=1)
    label.add_argument("--sample-count", type=int, default=10)
    label.add_argument("--temperature", type=float, default=0.6)
    label.add_argument("--top-p", type=float, default=0.9)
    label.add_argument("--device", default="auto")
    label.add_argument("--checkpoint-every", type=int, default=0)
    label.add_argument("--from", dest="from_time", default="")
    label.add_argument("--till", dest="till_time", default="")
    label.add_argument("--overwrite", action="store_true")

    check = sub.add_parser("check-remote", help="Check Binance Vision archive availability.")
    check.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    check.add_argument("--from", dest="from_date", required=True)
    check.add_argument("--till", dest="till_date", required=True)
    check.add_argument("--interval", default="1h")
    check.add_argument("--daily", action="store_true", help="Also check daily archives.")
    check.add_argument("--output", default="")

    add_bybit_demo_parser(sub)
    add_okx_demo_parser(sub)

    tui = sub.add_parser("tui", help="Открыть терминальный интерфейс Bybit Demo бота.")
    tui.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    tui.set_defaults(func=_cmd_tui)

    return parser


def _parse_symbols(value: str) -> tuple[str, ...]:
    symbols = tuple(part.strip().upper() for part in str(value).split(",") if part.strip())
    if not symbols:
        raise SystemExit("--symbols must not be empty")
    return symbols


def _compact_summary(frame: pd.DataFrame) -> dict[str, object]:
    return {
        "symbols": sorted(str(value) for value in frame["secid"].dropna().unique()),
        "rows": int(len(frame)),
        "as_of_min": str(frame["as_of"].min()),
        "as_of_max": str(frame["as_of"].max()),
        "direction_hit_rate": float(frame["direction_hit"].astype(bool).mean()) if len(frame) else None,
    }


def _head(url: str) -> tuple[int | str, int]:
    try:
        response = requests.head(url, timeout=15, allow_redirects=True)
        return response.status_code, int(response.headers.get("content-length") or 0)
    except Exception as exc:
        return type(exc).__name__, 0


def _monthly_url(symbol: str, interval: str, month: date) -> str:
    ym = f"{month.year:04d}-{month.month:02d}"
    return f"{BINANCE_VISION_BASE}/monthly/klines/{symbol}/{interval}/{symbol}-{interval}-{ym}.zip"


def _daily_url(symbol: str, interval: str, day: date) -> str:
    ds = day.isoformat()
    return f"{BINANCE_VISION_BASE}/daily/klines/{symbol}/{interval}/{symbol}-{interval}-{ds}.zip"


def _months(start: date, end: date):
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        yield cursor
        year = cursor.year + (1 if cursor.month == 12 else 0)
        month = 1 if cursor.month == 12 else cursor.month + 1
        cursor = date(year, month, 1)


def _days(start: date, end: date):
    cursor = start
    while cursor <= end:
        yield cursor
        cursor += timedelta(days=1)


def _cmd_tui(args: argparse.Namespace) -> int:
    from .bybit_demo.tui import run_tui

    return run_tui(config_path=args.config)


if __name__ == "__main__":
    raise SystemExit(main())
