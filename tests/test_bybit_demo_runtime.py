from __future__ import annotations

import sys
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
    def __init__(self, equity: str = "5000"):
        self.equity = Decimal(equity)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def ensure_can_submit(self, *, environment: str, dry_run: bool) -> None:
        return None

    def wallet_balance(self, *, account_type: str = "UNIFIED", coin: str = "USDT") -> Mapping[str, Any]:
        return {"result": {"list": [{"coin": [{"coin": coin, "equity": str(self.equity)}]}]}}

    def positions(self, *, category: str, symbol: str) -> Mapping[str, Any]:
        return {"result": {"list": []}}

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


if __name__ == "__main__":
    unittest.main()
