from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kronosobuchalka.bybit_demo.config import BybitDemoBotConfig, StrategyConfig, parse_config  # noqa: E402
from kronosobuchalka.bybit_demo.market import InstrumentSpec, MarketBundle, Ticker  # noqa: E402
from kronosobuchalka.bybit_demo.strategy import (  # noqa: E402
    ForecastRow,
    StrategyDecision,
    active_can_hold,
    build_strategy_decision,
    order_qty,
    stop_price,
)


def candles(rows: int = 220, *, start: str = "2026-07-01 00:00:00", price: float = 100.0) -> pd.DataFrame:
    timestamps = pd.date_range(start, periods=rows, freq="h")
    values = []
    current = price
    for idx, ts in enumerate(timestamps):
        open_price = current
        close = open_price * (1.001 if idx % 2 == 0 else 0.999)
        high = max(open_price, close) * 1.003
        low = min(open_price, close) * 0.997
        values.append(
            {
                "timestamps": ts,
                "end": ts + pd.Timedelta(hours=1) - pd.Timedelta(milliseconds=1),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": 1000 + idx,
                "amount": (1000 + idx) * close,
            }
        )
        current = close
    return pd.DataFrame(values)


def spec(symbol: str, tick: str = "0.01", qty_step: str = "0.1") -> InstrumentSpec:
    return InstrumentSpec(
        symbol=symbol,
        tick_size=Decimal(tick),
        qty_step=Decimal(qty_step),
        min_order_qty=Decimal(qty_step),
        min_notional=Decimal("5"),
        max_market_qty=Decimal("100000"),
        max_leverage=Decimal("50"),
    )


class StrategyTests(unittest.TestCase):
    def test_soft_filters_select_rank1_even_when_signal_is_weak(self) -> None:
        cfg = parse_config(
            {
                "strategy": {
                    "symbols": ["GRAMUSDT", "ZECUSDT"],
                    "symbol_filters": {
                        "GRAMUSDT": {"entry": {"net_pred_move_bps_min": -10000, "vol_ratio_min": 0.0}, "flip": {"enabled": False}},
                        "ZECUSDT": {"entry": {"net_pred_move_bps_min": -10000, "vol_ratio_min": 0.0}, "flip": {"enabled": False}},
                    },
                }
            }
        )
        base = candles(price=100)
        bundle = MarketBundle(
            candles={"GRAMUSDT": base, "ZECUSDT": base.copy()},
            forming_open={"GRAMUSDT": Decimal("100"), "ZECUSDT": Decimal("100")},
            tickers={
                "GRAMUSDT": Ticker("GRAMUSDT", Decimal("100"), Decimal("99.9"), Decimal("100.1")),
                "ZECUSDT": Ticker("ZECUSDT", Decimal("100"), Decimal("99.9"), Decimal("100.1")),
            },
            specs={"GRAMUSDT": spec("GRAMUSDT"), "ZECUSDT": spec("ZECUSDT")},
            decision_as_of=pd.Timestamp("2026-07-10 00:00:00"),
            target_timestamp=pd.Timestamp("2026-07-10 01:00:00"),
        )
        forecasts = {
            "GRAMUSDT": ForecastRow("GRAMUSDT", 100, 100.1, 99.9, 100.0, 0, 0),
            "ZECUSDT": ForecastRow("ZECUSDT", 100, 100.1, 99.9, 100.0, 0, 0),
        }

        decision = build_strategy_decision(bundle=bundle, forecasts=forecasts, config=cfg)

        self.assertEqual(decision.status, "selected")
        self.assertIsNotNone(decision.selected)
        self.assertTrue(decision.ranked_rows[0]["entry_selected"])

    def test_rank_recomputed_after_dynamic_cost(self) -> None:
        cfg = BybitDemoBotConfig()
        gram = candles(price=10)
        zec = candles(price=500)
        bundle = MarketBundle(
            candles={"GRAMUSDT": gram, "ZECUSDT": zec},
            forming_open={"GRAMUSDT": Decimal("10"), "ZECUSDT": Decimal("500")},
            tickers={
                "GRAMUSDT": Ticker("GRAMUSDT", Decimal("10"), Decimal("9.99"), Decimal("10.01")),
                "ZECUSDT": Ticker("ZECUSDT", Decimal("500"), Decimal("499.9"), Decimal("500.1")),
            },
            specs={"GRAMUSDT": spec("GRAMUSDT", "0.0001", "0.1"), "ZECUSDT": spec("ZECUSDT")},
            decision_as_of=pd.Timestamp("2026-07-10 00:00:00"),
            target_timestamp=pd.Timestamp("2026-07-10 01:00:00"),
        )
        forecasts = {
            "GRAMUSDT": ForecastRow("GRAMUSDT", 10, 20, 9.5, 10.30, 0, 0),
            "ZECUSDT": ForecastRow("ZECUSDT", 500, 505, 495, 509, 0, 0),
        }
        decision = build_strategy_decision(bundle=bundle, forecasts=forecasts, config=cfg)
        self.assertEqual(decision.ranked_rows[0]["rank"], 1)
        nets = [row["net_pred_move_pct"] for row in decision.ranked_rows]
        self.assertGreaterEqual(nets[0], nets[1])

    def test_selection_falls_through_to_next_rank_when_top_fails_symbol_filter(self) -> None:
        cfg = BybitDemoBotConfig(
            strategy=StrategyConfig(
                symbols=("GRAMUSDT", "ZECUSDT"),
                ranking_mode="rank_cascade",
                symbol_filters={
                    "GRAMUSDT": {
                        "entry": {"net_pred_move_bps_min": 5000.0, "vol_ratio_min": 0.0},
                        "flip": {"enabled": False},
                    },
                    "ZECUSDT": {
                        "entry": {"net_pred_move_bps_min": 20.0, "vol_ratio_min": 0.0},
                        "flip": {"enabled": False},
                    },
                },
            )
        )
        base = candles(price=100)
        bundle = MarketBundle(
            candles={"GRAMUSDT": base, "ZECUSDT": base.copy()},
            forming_open={"GRAMUSDT": Decimal("100"), "ZECUSDT": Decimal("100")},
            tickers={
                "GRAMUSDT": Ticker("GRAMUSDT", Decimal("100"), Decimal("99.9"), Decimal("100.1")),
                "ZECUSDT": Ticker("ZECUSDT", Decimal("100"), Decimal("99.9"), Decimal("100.1")),
            },
            specs={"GRAMUSDT": spec("GRAMUSDT"), "ZECUSDT": spec("ZECUSDT")},
            decision_as_of=pd.Timestamp("2026-07-10 00:00:00"),
            target_timestamp=pd.Timestamp("2026-07-10 01:00:00"),
        )
        forecasts = {
            "GRAMUSDT": ForecastRow("GRAMUSDT", 100, 125, 99, 120, 0, 0),
            "ZECUSDT": ForecastRow("ZECUSDT", 100, 104, 99, 103, 0, 0),
        }
        decision = build_strategy_decision(bundle=bundle, forecasts=forecasts, config=cfg)
        self.assertEqual(decision.ranked_rows[0]["secid"], "GRAMUSDT")
        self.assertFalse(decision.ranked_rows[0]["entry_selected"])
        self.assertIsNotNone(decision.selected)
        self.assertEqual(decision.selected["secid"], "ZECUSDT")
        self.assertEqual(decision.selected["rank"], 2)
        self.assertEqual(decision.reason, "rank2_passed_entry_filter")

    def test_rank1_only_does_not_fall_through_to_next_rank(self) -> None:
        cfg = BybitDemoBotConfig(
            strategy=StrategyConfig(
                symbols=("GRAMUSDT", "ZECUSDT"),
                ranking_mode="rank1_only",
                symbol_filters={
                    "GRAMUSDT": {
                        "entry": {"net_pred_move_bps_min": 5000.0, "vol_ratio_min": 0.0},
                        "flip": {"enabled": False},
                    },
                    "ZECUSDT": {
                        "entry": {"net_pred_move_bps_min": 20.0, "vol_ratio_min": 0.0},
                        "flip": {"enabled": False},
                    },
                },
            )
        )
        base = candles(price=100)
        bundle = MarketBundle(
            candles={"GRAMUSDT": base, "ZECUSDT": base.copy()},
            forming_open={"GRAMUSDT": Decimal("100"), "ZECUSDT": Decimal("100")},
            tickers={
                "GRAMUSDT": Ticker("GRAMUSDT", Decimal("100"), Decimal("99.9"), Decimal("100.1")),
                "ZECUSDT": Ticker("ZECUSDT", Decimal("100"), Decimal("99.9"), Decimal("100.1")),
            },
            specs={"GRAMUSDT": spec("GRAMUSDT"), "ZECUSDT": spec("ZECUSDT")},
            decision_as_of=pd.Timestamp("2026-07-10 00:00:00"),
            target_timestamp=pd.Timestamp("2026-07-10 01:00:00"),
        )
        forecasts = {
            "GRAMUSDT": ForecastRow("GRAMUSDT", 100, 125, 99, 120, 0, 0),
            "ZECUSDT": ForecastRow("ZECUSDT", 100, 104, 99, 103, 0, 0),
        }
        decision = build_strategy_decision(bundle=bundle, forecasts=forecasts, config=cfg)
        self.assertEqual(decision.ranked_rows[0]["secid"], "GRAMUSDT")
        self.assertFalse(decision.ranked_rows[0]["entry_selected"])
        self.assertIsNone(decision.selected)
        self.assertEqual(decision.reason, "rank1_failed_entry_filter")

    def test_btc_symbol_filter_can_disable_flip(self) -> None:
        cfg = BybitDemoBotConfig()
        btc_filter = cfg.strategy.filter_for("btcusdt")
        self.assertIn("BTCUSDT", cfg.strategy.symbols)
        self.assertFalse(btc_filter.flip_enabled)
        self.assertAlmostEqual(btc_filter.entry_net_pred_move_bps_min, 80.0)
        self.assertEqual(btc_filter.entry_vol_ratio_lookback_hours, 72)

    def test_hl_flip_rule(self) -> None:
        cfg = BybitDemoBotConfig()
        base = candles(price=100)
        bundle = MarketBundle(
            candles={"GRAMUSDT": base, "ZECUSDT": base.copy()},
            forming_open={"GRAMUSDT": Decimal("100"), "ZECUSDT": Decimal("100")},
            tickers={
                "GRAMUSDT": Ticker("GRAMUSDT", Decimal("100"), Decimal("99.9"), Decimal("100.1")),
                "ZECUSDT": Ticker("ZECUSDT", Decimal("100"), Decimal("99.9"), Decimal("100.1")),
            },
            specs={"GRAMUSDT": spec("GRAMUSDT"), "ZECUSDT": spec("ZECUSDT")},
            decision_as_of=pd.Timestamp("2026-07-10 00:00:00"),
            target_timestamp=pd.Timestamp("2026-07-10 01:00:00"),
        )
        forecasts = {
            "GRAMUSDT": ForecastRow("GRAMUSDT", 100, 180, 99, 120, 0, 0),
            "ZECUSDT": ForecastRow("ZECUSDT", 100, 100.5, 99.5, 101, 0, 0),
        }
        decision = build_strategy_decision(bundle=bundle, forecasts=forecasts, config=cfg)
        row = next(item for item in decision.ranked_rows if item["secid"] == "GRAMUSDT")
        self.assertTrue(row["is_extreme"])
        self.assertEqual(row["pred_side"], "long")
        self.assertEqual(row["final_side"], "short")

    def test_hold_decay_requires_same_symbol_side_and_metrics(self) -> None:
        cfg = BybitDemoBotConfig(strategy=StrategyConfig(symbols=("GRAMUSDT", "ZECUSDT"), ranking_mode="rank_cascade"))
        active = {"symbol": "GRAMUSDT", "side": "long", "target_timestamp": "2026-07-10T00:00:00"}
        decision = type(
            "D",
            (),
            {
                "as_of": "2026-07-10T00:00:00",
                "ranked_rows": [],
                "selected": {
                    "secid": "GRAMUSDT",
                    "final_side": "long",
                    "net_pred_move_bps": 51.0,
                    "vol_ratio_168": 0.8,
                },
            },
        )()
        can_hold, reason = active_can_hold(active=active, decision=decision, config=cfg)
        self.assertTrue(can_hold, reason)
        decision.selected["final_side"] = "short"
        can_hold, reason = active_can_hold(active=active, decision=decision, config=cfg)
        self.assertFalse(can_hold)
        self.assertEqual(reason, "selected_side_changed")

    def test_rank1_only_hold_preserves_active_side_when_rank1_changes(self) -> None:
        cfg = BybitDemoBotConfig(strategy=StrategyConfig(symbols=("GRAMUSDT", "ZECUSDT"), ranking_mode="rank1_only"))
        active = {"symbol": "GRAMUSDT", "side": "long", "target_timestamp": "2026-07-10T00:00:00"}
        decision = StrategyDecision(
            as_of="2026-07-10T00:00:00",
            target_timestamp="2026-07-10T01:00:00",
            ranked_rows=[
                {"rank": 1, "secid": "ZECUSDT", "final_side": "short", "entry_selected": True, "net_pred_move_bps": 200.0, "vol_ratio_168": 2.0},
                {"rank": 2, "secid": "GRAMUSDT", "final_side": "short", "entry_selected": False, "net_pred_move_bps": 55.0, "vol_ratio_168": 0.8},
            ],
            selected={"rank": 1, "secid": "ZECUSDT", "final_side": "short"},
            status="selected",
            reason="rank1_passed_entry_filter",
        )
        can_hold, reason = active_can_hold(active=active, decision=decision, config=cfg)
        self.assertTrue(can_hold, reason)

    def test_hold_uses_active_symbol_hold_filter_not_entry_filter(self) -> None:
        cfg = BybitDemoBotConfig(
            strategy=StrategyConfig(
                symbols=("GRAMUSDT",),
                symbol_filters={
                    "GRAMUSDT": {
                        "entry": {"net_pred_move_bps_min": 5000.0, "vol_ratio_min": 10.0},
                        "hold": {"net_pred_move_bps_min": 50.0, "vol_ratio_168_min": 0.75},
                        "flip": {"enabled": False},
                    }
                },
            )
        )
        active = {"symbol": "GRAMUSDT", "side": "long", "target_timestamp": "2026-07-10T00:00:00"}
        decision = StrategyDecision(
            as_of="2026-07-10T00:00:00",
            target_timestamp="2026-07-10T01:00:00",
            ranked_rows=[
                {
                    "rank": 1,
                    "secid": "GRAMUSDT",
                    "final_side": "long",
                    "entry_selected": False,
                    "net_pred_move_bps": 55.0,
                    "vol_ratio_168": 0.8,
                }
            ],
            selected=None,
            status="cash",
            reason="rank1_failed_entry_filter",
        )
        can_hold, reason = active_can_hold(active=active, decision=decision, config=cfg)
        self.assertTrue(can_hold, reason)

    def test_soft_hold_thresholds_allow_weak_same_signal(self) -> None:
        cfg = parse_config(
            {
                "strategy": {
                    "symbols": ["GRAMUSDT"],
                    "symbol_filters": {
                        "GRAMUSDT": {
                            "entry": {"net_pred_move_bps_min": -10000, "vol_ratio_min": 0.0},
                            "hold": {"net_pred_move_bps_min": -10000, "vol_ratio_168_min": 0.0},
                            "flip": {"enabled": False},
                        }
                    },
                }
            }
        )
        active = {"symbol": "GRAMUSDT", "side": "short", "target_timestamp": "2026-07-10T00:00:00"}
        decision = type(
            "D",
            (),
            {
                "as_of": "2026-07-10T00:00:00",
                "ranked_rows": [],
                "selected": {
                    "secid": "GRAMUSDT",
                    "final_side": "short",
                    "net_pred_move_bps": -500.0,
                    "vol_ratio_168": 0.0,
                },
            },
        )()

        can_hold, reason = active_can_hold(active=active, decision=decision, config=cfg)

        self.assertTrue(can_hold, reason)

    def test_stop_price_and_qty(self) -> None:
        instrument = spec("GRAMUSDT", "0.0001", "0.1")
        self.assertEqual(stop_price(side="long", entry_price=Decimal("1.2345"), stop_loss_pct=0.05, spec=instrument), Decimal("1.1727"))
        self.assertEqual(stop_price(side="short", entry_price=Decimal("1.2345"), stop_loss_pct=0.05, spec=instrument), Decimal("1.2962"))
        qty = order_qty(equity=Decimal("5000"), leverage=3, price=Decimal("1.25"), spec=instrument)
        self.assertEqual(qty, Decimal("12000.0"))
        capped = order_qty(equity=Decimal("5000"), leverage=3, price=Decimal("1.25"), spec=instrument, max_notional_usdt=Decimal("100"))
        self.assertEqual(capped, Decimal("80.0"))


if __name__ == "__main__":
    unittest.main()
