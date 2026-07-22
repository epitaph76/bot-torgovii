from __future__ import annotations

import calendar
import io
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas as pd
import requests


BINANCE_VISION_BASE = "https://data.binance.vision/data/spot"
BINANCE_API_BASE = "https://api.binance.com"
KLINE_COLUMNS = ["open", "high", "low", "close", "volume", "amount"]
OUTPUT_COLUMNS = ["timestamps", "end", *KLINE_COLUMNS]
ARCHIVE_KLINE_COLUMNS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trade_count",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
    "ignore",
]

INTERVAL_MS = {
    "1m": 60_000,
    "3m": 3 * 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
    "2h": 2 * 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "6h": 6 * 60 * 60_000,
    "8h": 8 * 60 * 60_000,
    "12h": 12 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}


@dataclass(frozen=True)
class CoverageSummary:
    symbol: str
    rows: int
    expected_rows: int
    coverage_ratio: float
    first_timestamp: str
    last_timestamp: str
    missing_rows: int


def download_candles(
    *,
    symbols: Iterable[str],
    out_dir: str | Path,
    from_date: str,
    till_date: str,
    interval: str = "1h",
    timestamp_offset_hours: int = 3,
    source: str = "archive",
    sleep_seconds: float = 0.1,
) -> dict[str, int]:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for symbol in symbols:
        secid = str(symbol).upper()
        if source == "api":
            frame = fetch_api_klines(
                symbol=secid,
                from_date=from_date,
                till_date=till_date,
                interval=interval,
                timestamp_offset_hours=timestamp_offset_hours,
            )
        else:
            frame = fetch_archive_klines(
                symbol=secid,
                from_date=from_date,
                till_date=till_date,
                interval=interval,
                timestamp_offset_hours=timestamp_offset_hours,
            )
        frame = trim_candles(frame, from_date=from_date, till_date=till_date)
        if not frame.empty:
            frame.to_csv(out_path / f"candles_{secid}.csv", index=False)
        counts[secid] = len(frame)
        time.sleep(max(float(sleep_seconds), 0.0))
    return counts


def fetch_archive_klines(
    *,
    symbol: str,
    from_date: str,
    till_date: str,
    interval: str = "1h",
    timestamp_offset_hours: int = 3,
) -> pd.DataFrame:
    interval = _validate_interval(interval)
    frames: list[pd.DataFrame] = []
    local_start = pd.Timestamp(from_date)
    local_end = pd.Timestamp(till_date)
    if _date_only(till_date):
        local_end += pd.Timedelta(days=1)
    archive_start = (local_start - pd.Timedelta(hours=int(timestamp_offset_hours))).date()
    archive_end = (local_end - pd.Timedelta(microseconds=1) - pd.Timedelta(hours=int(timestamp_offset_hours))).date()

    for month_start in _month_starts(archive_start, archive_end):
        month_end = _month_end(month_start)
        month_frame = _download_archive_zip(
            _monthly_url(str(symbol).upper(), interval, month_start),
            timestamp_offset_hours=timestamp_offset_hours,
        )
        if not month_frame.empty:
            frames.append(month_frame)
            continue
        for day in _days(max(archive_start, month_start), min(archive_end, month_end)):
            day_frame = _download_archive_zip(
                _daily_url(str(symbol).upper(), interval, day),
                timestamp_offset_hours=timestamp_offset_hours,
            )
            if not day_frame.empty:
                frames.append(day_frame)

    if not frames:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    return _normalize_candles(pd.concat(frames, ignore_index=True))


def fetch_api_klines(
    *,
    symbol: str,
    from_date: str,
    till_date: str,
    interval: str = "1h",
    timestamp_offset_hours: int = 3,
    limit: int = 1000,
) -> pd.DataFrame:
    interval = _validate_interval(interval)
    start_ms, end_ms = _date_range_to_utc_ms(from_date, till_date, timestamp_offset_hours=timestamp_offset_hours)
    rows: list[Sequence[Any]] = []
    cursor = start_ms
    url = f"{BINANCE_API_BASE}/api/v3/klines"
    request_limit = max(min(int(limit), 1000), 1)
    while cursor < end_ms:
        response = requests.get(
            url,
            params={
                "symbol": str(symbol).upper(),
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms - 1,
                "limit": request_limit,
            },
            timeout=30,
        )
        response.raise_for_status()
        chunk = response.json()
        if not chunk:
            break
        rows.extend(chunk)
        last_open = int(chunk[-1][0])
        next_cursor = last_open + INTERVAL_MS[interval]
        if next_cursor <= cursor or len(chunk) < request_limit:
            break
        cursor = next_cursor
    return archive_rows_to_candles(rows, timestamp_offset_hours=timestamp_offset_hours)


def archive_rows_to_candles(rows: Sequence[Sequence[Any]], *, timestamp_offset_hours: int = 3) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    raw = pd.DataFrame(rows)
    if raw.shape[1] < 8:
        raise ValueError("kline rows must contain at least 8 columns")
    raw = raw.iloc[:, : min(raw.shape[1], len(ARCHIVE_KLINE_COLUMNS))]
    raw.columns = ARCHIVE_KLINE_COLUMNS[: raw.shape[1]]
    offset = pd.Timedelta(hours=int(timestamp_offset_hours))
    out = pd.DataFrame(
        {
            "timestamps": _epoch_to_timestamp(raw["open_time"]) + offset,
            "end": _epoch_to_timestamp(raw["close_time"]) + offset,
            "open": pd.to_numeric(raw["open"], errors="coerce"),
            "high": pd.to_numeric(raw["high"], errors="coerce"),
            "low": pd.to_numeric(raw["low"], errors="coerce"),
            "close": pd.to_numeric(raw["close"], errors="coerce"),
            "volume": pd.to_numeric(raw["volume"], errors="coerce"),
            "amount": pd.to_numeric(raw["quote_volume"], errors="coerce"),
        }
    )
    return _normalize_candles(out)


def load_candle_file(path: str | Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    missing = [col for col in OUTPUT_COLUMNS if col not in frame.columns]
    if missing:
        raise ValueError(f"{path} is missing columns: {', '.join(missing)}")
    return _normalize_candles(frame)


def trim_candles(frame: pd.DataFrame, *, from_date: str, till_date: str) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    out = frame.copy()
    out["timestamps"] = pd.to_datetime(out["timestamps"], errors="coerce")
    start = pd.Timestamp(from_date)
    end = pd.Timestamp(till_date)
    if _date_only(till_date):
        end += pd.Timedelta(days=1)
    return out[(out["timestamps"] >= start) & (out["timestamps"] < end)].reset_index(drop=True)


def coverage_for_file(path: str | Path, *, symbol: str, from_date: str, till_date: str, interval: str = "1h") -> CoverageSummary:
    frame = load_candle_file(path)
    frame = trim_candles(frame, from_date=from_date, till_date=till_date)
    expected = expected_row_count(from_date=from_date, till_date=till_date, interval=interval)
    rows = int(len(frame))
    first = frame["timestamps"].min().isoformat() if rows else ""
    last = frame["timestamps"].max().isoformat() if rows else ""
    return CoverageSummary(
        symbol=str(symbol).upper(),
        rows=rows,
        expected_rows=expected,
        coverage_ratio=rows / expected if expected > 0 else 0.0,
        first_timestamp=first,
        last_timestamp=last,
        missing_rows=max(expected - rows, 0),
    )


def expected_row_count(*, from_date: str, till_date: str, interval: str = "1h") -> int:
    interval = _validate_interval(interval)
    start = pd.Timestamp(from_date)
    end = pd.Timestamp(till_date)
    if _date_only(till_date):
        end += pd.Timedelta(days=1)
    delta_ms = max((end - start).total_seconds() * 1000.0, 0.0)
    return int(delta_ms // INTERVAL_MS[interval])


def _download_archive_zip(url: str, *, timestamp_offset_hours: int) -> pd.DataFrame:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = requests.get(url, timeout=60)
            if response.status_code == 404:
                return pd.DataFrame(columns=OUTPUT_COLUMNS)
            response.raise_for_status()
            break
        except requests.RequestException as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            return pd.DataFrame(columns=OUTPUT_COLUMNS)
    else:
        if last_error is not None:
            return pd.DataFrame(columns=OUTPUT_COLUMNS)
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        names = [name for name in zf.namelist() if name.endswith(".csv")]
        if not names:
            return pd.DataFrame(columns=OUTPUT_COLUMNS)
        with zf.open(names[0]) as handle:
            rows = pd.read_csv(handle, header=None)
    return archive_rows_to_candles(rows.values.tolist(), timestamp_offset_hours=timestamp_offset_hours)


def _normalize_candles(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    out = frame.copy()
    out["timestamps"] = pd.to_datetime(out["timestamps"], errors="coerce")
    out["end"] = pd.to_datetime(out["end"], errors="coerce")
    for col in KLINE_COLUMNS:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["timestamps", "end", "open", "high", "low", "close"])
    out["volume"] = out["volume"].fillna(0.0)
    out["amount"] = out["amount"].fillna(0.0)
    return out[OUTPUT_COLUMNS].sort_values("timestamps").drop_duplicates("timestamps").reset_index(drop=True)


def _epoch_to_timestamp(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    finite = numeric.dropna()
    if finite.empty:
        return pd.to_datetime(numeric, unit="ms", utc=True, errors="coerce").dt.tz_localize(None)
    median_abs = float(finite.abs().median())
    if median_abs >= 1e15:
        unit = "us"
    elif median_abs >= 1e12:
        unit = "ms"
    elif median_abs >= 1e9:
        unit = "s"
    else:
        unit = "ms"
    return pd.to_datetime(numeric, unit=unit, utc=True, errors="coerce").dt.tz_localize(None)


def _monthly_url(symbol: str, interval: str, month_start: date) -> str:
    ym = f"{month_start.year:04d}-{month_start.month:02d}"
    return f"{BINANCE_VISION_BASE}/monthly/klines/{symbol}/{interval}/{symbol}-{interval}-{ym}.zip"


def _daily_url(symbol: str, interval: str, day: date) -> str:
    ds = day.isoformat()
    return f"{BINANCE_VISION_BASE}/daily/klines/{symbol}/{interval}/{symbol}-{interval}-{ds}.zip"


def _month_starts(start: date, end: date) -> Iterable[date]:
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        yield cursor
        year = cursor.year + (1 if cursor.month == 12 else 0)
        month = 1 if cursor.month == 12 else cursor.month + 1
        cursor = date(year, month, 1)


def _month_end(month_start: date) -> date:
    return date(month_start.year, month_start.month, calendar.monthrange(month_start.year, month_start.month)[1])


def _days(start: date, end: date) -> Iterable[date]:
    cursor = start
    while cursor <= end:
        yield cursor
        cursor += timedelta(days=1)


def _inclusive_end_date(till_date: str) -> date:
    timestamp = pd.Timestamp(till_date)
    if _date_only(till_date):
        return timestamp.date()
    return (timestamp - pd.Timedelta(milliseconds=1)).date()


def _validate_interval(interval: str) -> str:
    interval = str(interval)
    if interval not in INTERVAL_MS:
        raise ValueError(f"unsupported interval: {interval}")
    return interval


def _date_range_to_utc_ms(from_date: str, till_date: str, *, timestamp_offset_hours: int) -> tuple[int, int]:
    start_local = pd.Timestamp(from_date)
    end_local = pd.Timestamp(till_date)
    if _date_only(till_date):
        end_local += pd.Timedelta(days=1)
    start_utc = start_local.to_pydatetime() - timedelta(hours=int(timestamp_offset_hours))
    end_utc = end_local.to_pydatetime() - timedelta(hours=int(timestamp_offset_hours))
    return _naive_utc_to_ms(start_utc), _naive_utc_to_ms(end_utc)


def _date_only(value: str) -> bool:
    stripped = str(value).strip()
    return len(stripped) == 10 and stripped[4] == "-" and stripped[7] == "-"


def _naive_utc_to_ms(value: datetime) -> int:
    return int(calendar.timegm(value.timetuple()) * 1000 + value.microsecond / 1000)
