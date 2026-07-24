from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

import pandas as pd

from .client import BybitV5Client
from .config import BybitDemoBotConfig


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str
    tick_size: Decimal
    qty_step: Decimal
    min_order_qty: Decimal
    min_notional: Decimal
    max_market_qty: Decimal
    max_leverage: Decimal


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


class BybitMarketData:
    def __init__(self, *, client: BybitV5Client, config: BybitDemoBotConfig):
        self.client = client
        self.config = config
        self._spec_cache: dict[str, InstrumentSpec] = {}

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
        cached = self._spec_cache.get(symbol)
        if cached is not None:
            return cached
        payload = self.client.instruments_info(category=self.config.strategy.category, symbol=symbol)
        rows = _result_list(payload)
        if not rows:
            raise RuntimeError(f"instrument not found on Bybit Demo: {symbol}")
        row = rows[0]
        lot = row.get("lotSizeFilter") or {}
        price = row.get("priceFilter") or {}
        leverage = row.get("leverageFilter") or {}
        if str(row.get("status")) != "Trading":
            raise RuntimeError(f"instrument is not trading: {symbol} status={row.get('status')}")
        spec = InstrumentSpec(
            symbol=symbol,
            tick_size=_decimal(price.get("tickSize"), "0.0001"),
            qty_step=_decimal(lot.get("qtyStep"), "0.001"),
            min_order_qty=_decimal(lot.get("minOrderQty"), "0"),
            min_notional=_decimal(lot.get("minNotionalValue"), "5"),
            max_market_qty=_decimal(lot.get("maxMktOrderQty"), lot.get("maxOrderQty", "0")),
            max_leverage=_decimal(leverage.get("maxLeverage"), "1"),
        )
        self._spec_cache[symbol] = spec
        return spec

    def ticker(self, symbol: str) -> Ticker:
        payload = self.client.ticker(category=self.config.strategy.category, symbol=symbol)
        rows = _result_list(payload)
        if not rows:
            raise RuntimeError(f"ticker not found: {symbol}")
        row = rows[0]
        return Ticker(
            symbol=symbol,
            last_price=_decimal(row.get("lastPrice"), "0"),
            bid1_price=_decimal(row.get("bid1Price"), row.get("lastPrice", "0")),
            ask1_price=_decimal(row.get("ask1Price"), row.get("lastPrice", "0")),
        )

    def klines(self, symbol: str, *, limit: int) -> pd.DataFrame:
        payload = self.client.klines(category=self.config.strategy.category, symbol=symbol, interval=self.config.strategy.interval, limit=limit)
        rows = payload.get("result", {}).get("list", []) if isinstance(payload.get("result"), Mapping) else []
        out_rows = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 7:
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
                    "volume": _float(row[5]),
                    "amount": _float(row[6]),
                }
            )
        frame = pd.DataFrame(out_rows)
        if frame.empty:
            return pd.DataFrame(columns=["timestamps", "end", "open", "high", "low", "close", "volume", "amount"])
        return frame.sort_values("timestamps").drop_duplicates("timestamps").reset_index(drop=True)


def split_completed_and_forming(frame: pd.DataFrame, *, fallback_open: Decimal) -> tuple[pd.DataFrame, Decimal, pd.Timestamp]:
    if frame.empty:
        raise RuntimeError("empty kline frame")
    now = pd.Timestamp.utcnow().tz_localize(None)
    latest = frame.sort_values("timestamps").iloc[-1]
    latest_ts = pd.Timestamp(latest["timestamps"])
    latest_end = latest_ts + pd.Timedelta(hours=1)
    if latest_ts <= now < latest_end:
        completed = frame[frame["timestamps"] < latest_ts].copy()
        current_open = _decimal(latest["open"], str(fallback_open))
        target_timestamp = latest_ts
    else:
        completed = frame.copy()
        current_open = fallback_open
        target_timestamp = latest_ts + pd.Timedelta(hours=1)
    return completed.reset_index(drop=True), current_open, target_timestamp


def _result_list(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    result = payload.get("result")
    if not isinstance(result, Mapping):
        return []
    rows = result.get("list")
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
