from __future__ import annotations

import sys
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kronosobuchalka.bybit_demo.client import BybitAPIError  # noqa: E402
from kronosobuchalka.bybit_demo.config import BybitDemoBotConfig, parse_config  # noqa: E402
from kronosobuchalka.bybit_demo.market import InstrumentSpec, MarketBundle, Ticker  # noqa: E402
from kronosobuchalka.bybit_demo.runtime import BybitDemoRuntime  # noqa: E402
from kronosobuchalka.bybit_demo.state import BotStateStore  # noqa: E402
from kronosobuchalka.bybit_demo.strategy import ForecastRow  # noqa: E402
from test_bybit_demo_strategy import candles, spec  # noqa: E402


class FakeMarket:
    def __init__(self, bundle: MarketBundle):
        self.bundle = bundle

    def load_bundle(self) -> MarketBundle:
        return self.bundle


class FakeForecaster:
    def __init__(self, forecasts: Mapping[str, ForecastRow]):
        self.forecasts = dict(forecasts)

    def forecast(self, bundle: MarketBundle) -> dict[str, ForecastRow]:
        return dict(self.forecasts)


class FakeClient:
    def __init__(self, equity: str = "5000", positions: Mapping[str, Mapping[str, str]] | None = None):
        self.equity = Decimal(equity)
        self.position_rows = dict(positions or {})
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def ensure_can_submit(self, *, environment: str, dry_run: bool) -> None:
        return None

    def wallet_balance(self, *, account_type: str = "UNIFIED", coin: str = "USDT") -> Mapping[str, Any]:
        return {"result": {"list": [{"coin": [{"coin": coin, "equity": str(self.equity)}]}]}}

    def positions(self, *, category: str, symbol: str) -> Mapping[str, Any]:
        row = self.position_rows.get(symbol)
        if row is None:
            return {"result": {"list": []}}
        return {
            "result": {
                "list": [
                    {
                        "symbol": symbol,
                        "size": row.get("size", "0"),
                        "side": row.get("side", ""),
                        "avgPrice": row.get("avgPrice", "0"),
                    }
                ]
            }
        }

    def set_leverage(self, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(("set_leverage", kwargs))
        return {"ok": True}

    def create_market_order(self, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(("create_market_order", kwargs))
        return {"ok": True, "kwargs": kwargs}

    def set_trading_stop(self, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(("set_trading_stop", kwargs))
        return {"ok": True, "kwargs": kwargs}

    def universal_transfer(self, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(("universal_transfer", kwargs))
        return {"ok": True, "kwargs": kwargs}


class DuplicateOpenClient(FakeClient):
    def __init__(self, equity: str, verified_positions: Mapping[str, Mapping[str, str]]):
        super().__init__(equity)
        self.verified_positions = dict(verified_positions)

    def create_market_order(self, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(("create_market_order", kwargs))
        if not bool(kwargs.get("reduce_only")):
            self.position_rows = dict(self.verified_positions)
            raise BybitAPIError(method="POST", path="/v5/order/create", ret_code=110072, ret_msg="OrderLinkedID is duplicate")
        return {"ok": True, "kwargs": kwargs}


def bundle() -> MarketBundle:
    base = candles(price=100)
    return MarketBundle(
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


def probe_config(tmp: str) -> BybitDemoBotConfig:
    return parse_config(
        {
            "state_path": str(Path(tmp) / "state.sqlite3"),
            "split": {
                "sleeves": [
                    {
                        "sleeve_id": "trade_1x",
                        "kind": "trade",
                        "target_weight": 1.0,
                        "leverage": 1,
                        "max_notional_usdt": 100,
                        "api_key_env": "BYBIT_DEMO_API_KEY",
                        "api_secret_env": "BYBIT_DEMO_API_SECRET",
                        "dry_run_equity_usdt": 1000,
                    }
                ]
            },
            "strategy": {
                "entry_vol_ratio_168_min": 0.0,
                "entry_net_pred_move_bps_min": -10000,
                "exit_net_pred_move_bps_min": -10000,
                "exit_vol_ratio_168_min": 0.0,
            },
        }
    )


class RuntimeTests(unittest.TestCase):
    def test_dry_run_opens_selected_sleeves_and_persists_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = BybitDemoBotConfig(state_path=str(Path(tmp) / "state.sqlite3"))
            forecasts = {
                "GRAMUSDT": ForecastRow("GRAMUSDT", 100, 180, 99, 120, 0, 0),
                "ZECUSDT": ForecastRow("ZECUSDT", 100, 100.5, 99.5, 101, 0, 0),
            }
            state = BotStateStore(cfg.state_path)
            runtime = BybitDemoRuntime(config=cfg, market=FakeMarket(bundle()), forecaster=FakeForecaster(forecasts), state=state)
            result = runtime.run_once(dry_run=True)
            self.assertIn(result["action"], {"open", "cash"})
            self.assertTrue(result["orders"])
            active = state.load_active_episode()
            self.assertIsNotNone(active)
            self.assertEqual(active["symbol"], "GRAMUSDT")

    def test_successful_run_clears_stale_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = BybitDemoBotConfig(state_path=str(Path(tmp) / "state.sqlite3"))
            forecasts = {
                "GRAMUSDT": ForecastRow("GRAMUSDT", 100, 180, 99, 120, 0, 0),
                "ZECUSDT": ForecastRow("ZECUSDT", 100, 100.5, 99.5, 101, 0, 0),
            }
            state = BotStateStore(cfg.state_path)
            state.set_reconciliation("blocked", "runtime_error:BybitDemoError", {"error": "timestamp drift"})
            runtime = BybitDemoRuntime(config=cfg, market=FakeMarket(bundle()), forecaster=FakeForecaster(forecasts), state=state)

            runtime.run_once(dry_run=True)

            self.assertIsNone(state.reconciliation())

    def test_rebalance_threshold_submits_transfer_when_flat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = parse_config(
                {
                    "state_path": str(Path(tmp) / "state.sqlite3"),
                    "master_account": {"member_id": "master"},
                    "split": {
                        "sleeves": [
                            {"sleeve_id": "cash", "kind": "cash", "target_weight": 0.2, "leverage": 0, "member_id": "master"},
                            {"sleeve_id": "sleeve_3x", "kind": "trade", "target_weight": 0.5, "leverage": 3, "member_id": "sub3"},
                            {"sleeve_id": "sleeve_5x", "kind": "trade", "target_weight": 0.3, "leverage": 5, "member_id": "sub5"},
                        ]
                    },
                }
            )
            master = FakeClient("1000")
            sleeve3 = FakeClient("9000")
            sleeve5 = FakeClient("0")
            runtime = BybitDemoRuntime(
                config=cfg,
                market=FakeMarket(bundle()),
                forecaster=FakeForecaster({}),
                state=BotStateStore(cfg.state_path),
                master_client=master,  # type: ignore[arg-type]
                sleeve_clients={"sleeve_3x": sleeve3, "sleeve_5x": sleeve5},  # type: ignore[arg-type]
            )
            result = runtime.run_once(dry_run=False, rebalance_only=True)
            self.assertEqual(result["rebalance"]["status"], "submitted")
            transfer_calls = [call for call in master.calls if call[0] == "universal_transfer"]
            self.assertTrue(transfer_calls)

    def test_live_open_caps_notional_and_persists_spread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = probe_config(tmp)
            forecasts = {
                "GRAMUSDT": ForecastRow("GRAMUSDT", 100, 100.1, 99.9, 99.7, 0, 0),
                "ZECUSDT": ForecastRow("ZECUSDT", 100, 100.1, 99.9, 100.1, 0, 0),
            }
            client = FakeClient("5000")
            state = BotStateStore(cfg.state_path)
            runtime = BybitDemoRuntime(
                config=cfg,
                market=FakeMarket(bundle()),
                forecaster=FakeForecaster(forecasts),
                state=state,
                master_client=FakeClient("5000"),  # type: ignore[arg-type]
                sleeve_clients={"trade_1x": client},  # type: ignore[arg-type]
            )

            result = runtime.run_once(dry_run=False)

            self.assertEqual(result["action"], "open")
            open_events = [event for event in result["orders"] if event["action"] == "open"]
            self.assertEqual(len(open_events), 1)
            self.assertEqual(open_events[0]["symbol"], "GRAMUSDT")
            self.assertEqual(open_events[0]["max_notional_usdt"], "100")
            self.assertLessEqual(Decimal(open_events[0]["order_notional_usdt"]), Decimal("100"))
            create_calls = [call for call in client.calls if call[0] == "create_market_order"]
            self.assertEqual(len(create_calls), 1)
            self.assertLessEqual(Decimal(str(create_calls[0][1]["qty"])) * Decimal("99.9"), Decimal("100"))
            self.assertTrue(str(create_calls[0][1]["order_link_id"]).startswith("krn-"))
            self.assertEqual(len(result["spread_snapshots"]), 2)
            conn = sqlite3.connect(cfg.state_path)
            try:
                row = conn.execute(
                    "select bid1_price, ask1_price, bid_ask_spread_pct, execution_cost_bps from spread_snapshots where symbol = ?",
                    ("GRAMUSDT",),
                ).fetchone()
            finally:
                conn.close()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "99.9")
            self.assertEqual(row[1], "100.1")
            self.assertIsNotNone(row[2])
            self.assertIsNotNone(row[3])

    def test_duplicate_open_order_is_verified_before_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = probe_config(tmp)
            forecasts = {
                "GRAMUSDT": ForecastRow("GRAMUSDT", 100, 100.1, 99.9, 99.7, 0, 0),
                "ZECUSDT": ForecastRow("ZECUSDT", 100, 100.1, 99.9, 100.1, 0, 0),
            }
            client = DuplicateOpenClient("5000", verified_positions={"GRAMUSDT": {"size": "1.0", "side": "Sell", "avgPrice": "99.9"}})
            runtime = BybitDemoRuntime(
                config=cfg,
                market=FakeMarket(bundle()),
                forecaster=FakeForecaster(forecasts),
                state=BotStateStore(cfg.state_path),
                master_client=FakeClient("5000"),  # type: ignore[arg-type]
                sleeve_clients={"trade_1x": client},  # type: ignore[arg-type]
            )

            result = runtime.run_once(dry_run=False)

            self.assertEqual(result["action"], "open")
            open_event = [event for event in result["orders"] if event["action"] == "open"][0]
            self.assertEqual(open_event["order_response"]["retExtInfo"]["handledRetCode"], 110072)
            self.assertEqual(open_event["qty"], "1.0")
            self.assertEqual(open_event["stop_entry_price"], "99.9")
            self.assertTrue([call for call in client.calls if call[0] == "set_trading_stop"])

    def test_exchange_position_without_active_episode_blocks_new_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = probe_config(tmp)
            forecasts = {
                "GRAMUSDT": ForecastRow("GRAMUSDT", 100, 100.1, 99.9, 99.7, 0, 0),
                "ZECUSDT": ForecastRow("ZECUSDT", 100, 100.1, 99.9, 100.1, 0, 0),
            }
            state = BotStateStore(cfg.state_path)
            client = FakeClient("5000", positions={"ZECUSDT": {"size": "0.2", "side": "Buy", "avgPrice": "100.0"}})
            runtime = BybitDemoRuntime(
                config=cfg,
                market=FakeMarket(bundle()),
                forecaster=FakeForecaster(forecasts),
                state=state,
                master_client=FakeClient("5000"),  # type: ignore[arg-type]
                sleeve_clients={"trade_1x": client},  # type: ignore[arg-type]
            )

            result = runtime.run_once(dry_run=False)

            self.assertEqual(result["action"], "blocked")
            self.assertEqual(result["reason"], "exchange_position_without_active_episode")
            self.assertFalse([call for call in client.calls if call[0] == "create_market_order"])
            self.assertEqual(state.reconciliation()["reason"], "exchange_position_without_active_episode")  # type: ignore[index]

    def test_hold_refreshes_stop_without_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = probe_config(tmp)
            forecasts = {
                "GRAMUSDT": ForecastRow("GRAMUSDT", 100, 100.1, 99.9, 99.7, 0, 0),
                "ZECUSDT": ForecastRow("ZECUSDT", 100, 100.1, 99.9, 100.1, 0, 0),
            }
            state = BotStateStore(cfg.state_path)
            state.set_active_episode(
                {
                    "symbol": "GRAMUSDT",
                    "side": "short",
                    "as_of": "2026-07-10T00:00:00",
                    "target_timestamp": "2026-07-10T00:00:00",
                    "entry_decision_id": "old",
                    "entry_price": 99.9,
                    "selected": {},
                    "sleeves": {"trade_1x": {"qty": "1.0", "entry_price": "99.9"}},
                }
            )
            client = FakeClient("5000", positions={"GRAMUSDT": {"size": "1.0", "side": "Sell", "avgPrice": "99.9"}})
            runtime = BybitDemoRuntime(
                config=cfg,
                market=FakeMarket(bundle()),
                forecaster=FakeForecaster(forecasts),
                state=state,
                master_client=FakeClient("5000"),  # type: ignore[arg-type]
                sleeve_clients={"trade_1x": client},  # type: ignore[arg-type]
            )

            result = runtime.run_once(dry_run=False)

            self.assertEqual(result["action"], "hold")
            self.assertFalse([call for call in client.calls if call[0] == "create_market_order"])
            stop_calls = [call for call in client.calls if call[0] == "set_trading_stop"]
            self.assertEqual(len(stop_calls), 1)
            self.assertEqual(result["orders"][0]["action"], "refresh_stop")

    def test_side_or_symbol_change_closes_then_opens_in_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = probe_config(tmp)
            forecasts = {
                "GRAMUSDT": ForecastRow("GRAMUSDT", 100, 100.1, 99.9, 99.9, 0, 0),
                "ZECUSDT": ForecastRow("ZECUSDT", 100, 100.1, 99.9, 100.4, 0, 0),
            }
            state = BotStateStore(cfg.state_path)
            state.set_active_episode(
                {
                    "symbol": "GRAMUSDT",
                    "side": "short",
                    "as_of": "2026-07-10T00:00:00",
                    "target_timestamp": "2026-07-10T00:00:00",
                    "entry_decision_id": "old",
                    "entry_price": 99.9,
                    "selected": {},
                    "sleeves": {"trade_1x": {"qty": "1.0", "entry_price": "99.9"}},
                }
            )
            runtime = BybitDemoRuntime(
                config=cfg,
                market=FakeMarket(bundle()),
                forecaster=FakeForecaster(forecasts),
                state=state,
            )

            result = runtime.run_once(dry_run=True)

            self.assertEqual(result["action"], "close_then_open")
            self.assertEqual([event["action"] for event in result["orders"]], ["close", "open"])
            self.assertEqual(result["orders"][1]["symbol"], "ZECUSDT")


if __name__ == "__main__":
    unittest.main()
