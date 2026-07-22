from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

import pandas as pd

from .client import OkxDemoError, OkxV5Client
from .config import OkxDemoBotConfig


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str
    tick_size: Decimal
    qty_step: Decimal
    min_order_qty: Decimal
    min_notional: Decimal
    max_market_qty: Decimal
    max_leverage: Decimal
    contract_value: Decimal


@dataclass(frozen=True)
class Ticker:
    symbol: str
    last_price: Decimal
    bid1_price: Decimal
    ask1_price: Decimal


@dataclass(frozen=True)
class MarketBundle:
    candles: dict[str, pd.DataFrame]
    forming_open: dict[str, Decimal]
    tickers: dict[str, Ticker]
    specs: dict[str, InstrumentSpec]
    decision_as_of: pd.Timestamp
    target_timestamp: pd.Timestamp


class OkxMarketData:
    def __init__(self, *, client: OkxV5Client, config: OkxDemoBotConfig):
        self.client = client
        self.config = config

    def load_bundle(self) -> MarketBundle:
        strategy = self.config.strategy
        candles: dict[str, pd.DataFrame] = {}
        forming_open: dict[str, Decimal] = {}
        tickers: dict[str, Ticker] = {}
        specs: dict[str, InstrumentSpec] = {}
        target_candidates: list[pd.Timestamp] = []
        for symbol in strategy.symbols:
            specs[symbol] = self.instrument_spec(symbol)
            tickers[symbol] = self.ticker(symbol)
            frame = self.klines(symbol, limit=int(strategy.history_limit))
            completed, current_open, target_timestamp = split_completed_and_forming(frame, fallback_open=tickers[symbol].last_price)
            if completed.empty:
                raise RuntimeError(f"no completed candles for {symbol}")
            candles[symbol] = completed
            forming_open[symbol] = current_open
            target_candidates.append(target_timestamp)
        target_timestamp = max(target_candidates)
        decision_as_of = target_timestamp - pd.Timedelta(hours=1)
        return MarketBundle(
            candles=candles,
            forming_open=forming_open,
            tickers=tickers,
            specs=specs,
            decision_as_of=decision_as_of,
            target_timestamp=target_timestamp,
        )

    def instrument_spec(self, symbol: str) -> InstrumentSpec:
        try:
            payload = self.client.instruments(inst_type=self.config.strategy.inst_type, inst_id=symbol)
        except OkxDemoError as exc:
            raise RuntimeError(f"instrument not found on OKX simulated trading: {symbol}") from exc
        rows = _data_list(payload)
        if not rows:
            raise RuntimeError(f"instrument not found on OKX Demo: {symbol}")
        row = rows[0]
        if str(row.get("state")) != "live":
            raise RuntimeError(f"instrument is not live: {symbol} state={row.get('state')}")
        return InstrumentSpec(
            symbol=symbol,
            tick_size=_decimal(row.get("tickSz"), "0.0001"),
            qty_step=_decimal(row.get("lotSz"), "1"),
            min_order_qty=_decimal(row.get("minSz"), "0"),
            min_notional=Decimal("0"),
            max_market_qty=_decimal(row.get("maxMktSz"), row.get("maxLmtSz", "0")),
            max_leverage=_decimal(row.get("lever"), "1"),
            contract_value=_decimal(row.get("ctVal"), "1"),
        )

    def ticker(self, symbol: str) -> Ticker:
        payload = self.client.ticker(inst_id=symbol)
        rows = _data_list(payload)
        if not rows:
            raise RuntimeError(f"ticker not found: {symbol}")
        row = rows[0]
        return Ticker(
            symbol=symbol,
            last_price=_decimal(row.get("last"), "0"),
            bid1_price=_decimal(row.get("bidPx"), row.get("last", "0")),
            ask1_price=_decimal(row.get("askPx"), row.get("last", "0")),
        )

    def klines(self, symbol: str, *, limit: int) -> pd.DataFrame:
        payload = self.client.candles(inst_id=symbol, bar=self.config.strategy.bar, limit=limit)
        rows = payload.get("data", [])
        out_rows = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 8:
                continue
            ts = pd.to_datetime(int(row[0]), unit="ms", utc=True).tz_localize(None)
            out_rows.append(
                {
                    "timestamps": ts,
                    "end": ts + pd.Timedelta(hours=1) - pd.Timedelta(milliseconds=1),
                    "open": _float(row[1]),
                    "high": _float(row[2]),
                    "low": _float(row[3]),
                    "close": _float(row[4]),
                    "volume": _float(row[6]),
                    "amount": _float(row[7]),
                    "confirm": str(row[8]) if len(row) > 8 else "",
                }
            )
        frame = pd.DataFrame(out_rows)
        columns = ["timestamps", "end", "open", "high", "low", "close", "volume", "amount", "confirm"]
        if frame.empty:
            return pd.DataFrame(columns=columns)
        return frame.sort_values("timestamps").drop_duplicates("timestamps").reset_index(drop=True)


def split_completed_and_forming(frame: pd.DataFrame, *, fallback_open: Decimal) -> tuple[pd.DataFrame, Decimal, pd.Timestamp]:
    if frame.empty:
        raise RuntimeError("empty kline frame")
    now = pd.Timestamp.utcnow().tz_localize(None)
    sorted_frame = frame.sort_values("timestamps")
    latest = sorted_frame.iloc[-1]
    latest_ts = pd.Timestamp(latest["timestamps"])
    latest_end = latest_ts + pd.Timedelta(hours=1)
    is_forming = str(latest.get("confirm", "")) == "0" or latest_ts <= now < latest_end
    if is_forming:
        completed = sorted_frame[sorted_frame["timestamps"] < latest_ts].copy()
        current_open = _decimal(latest["open"], str(fallback_open))
        target_timestamp = latest_ts
    else:
        completed = sorted_frame.copy()
        current_open = fallback_open
        target_timestamp = latest_ts + pd.Timedelta(hours=1)
    if "confirm" in completed.columns:
        completed = completed.drop(columns=["confirm"])
    return completed.reset_index(drop=True), current_open, target_timestamp


def _data_list(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = payload.get("data")
    return [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


def _decimal(value: Any, fallback: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(str(fallback))


def _float(value: Any) -> float:
    try:
        out = float(value)
    except Exception:
        return math.nan
    return out if math.isfinite(out) else math.nan
