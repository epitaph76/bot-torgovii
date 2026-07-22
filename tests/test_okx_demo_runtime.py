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

from kronosobuchalka.okx_demo.config import OkxDemoBotConfig, parse_config  # noqa: E402
from kronosobuchalka.okx_demo.client import OkxDemoError  # noqa: E402
from kronosobuchalka.okx_demo.market import MarketBundle, Ticker  # noqa: E402
from kronosobuchalka.okx_demo.runtime import OkxDemoRuntime  # noqa: E402
from kronosobuchalka.okx_demo.state import BotStateStore  # noqa: E402
from kronosobuchalka.okx_demo.strategy import ForecastRow  # noqa: E402
from test_okx_demo_strategy import candles, spec  # noqa: E402


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

    def account_balance(self, *, coin: str = "USDT") -> Mapping[str, Any]:
        return {"data": [{"details": [{"ccy": coin, "eq": str(self.equity)}]}]}

    def positions(self, *, inst_type: str, inst_id: str) -> Mapping[str, Any]:
        return {"data": []}

    def set_leverage(self, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(("set_leverage", kwargs))
        return {"ok": True}

    def create_market_order(self, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(("create_market_order", kwargs))
        return {"ok": True, "kwargs": kwargs}

    def place_stop_loss(self, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(("place_stop_loss", kwargs))
        return {"ok": True, "kwargs": kwargs}

    def asset_transfer(self, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(("asset_transfer", kwargs))
        return {"ok": True, "kwargs": kwargs}


def bundle() -> MarketBundle:
    base = candles(price=100)
    return MarketBundle(
        candles={"GRAM-USDT-SWAP": base, "ZEC-USDT-SWAP": base.copy()},
        forming_open={"GRAM-USDT-SWAP": Decimal("100"), "ZEC-USDT-SWAP": Decimal("100")},
        tickers={
            "GRAM-USDT-SWAP": Ticker("GRAM-USDT-SWAP", Decimal("100"), Decimal("99.9"), Decimal("100.1")),
            "ZEC-USDT-SWAP": Ticker("ZEC-USDT-SWAP", Decimal("100"), Decimal("99.9"), Decimal("100.1")),
        },
        specs={"GRAM-USDT-SWAP": spec("GRAM-USDT-SWAP"), "ZEC-USDT-SWAP": spec("ZEC-USDT-SWAP", contract_value="0.01")},
        decision_as_of=pd.Timestamp("2026-07-10 00:00:00"),
        target_timestamp=pd.Timestamp("2026-07-10 01:00:00"),
    )


class OkxRuntimeTests(unittest.TestCase):
    def test_dry_run_opens_selected_sleeves_and_persists_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = OkxDemoBotConfig(state_path=str(Path(tmp) / "state.sqlite3"))
            forecasts = {
                "GRAM-USDT-SWAP": ForecastRow("GRAM-USDT-SWAP", 100, 180, 99, 120, 0, 0),
                "ZEC-USDT-SWAP": ForecastRow("ZEC-USDT-SWAP", 100, 100.5, 99.5, 101, 0, 0),
            }
            state = BotStateStore(cfg.state_path)
            runtime = OkxDemoRuntime(config=cfg, market=FakeMarket(bundle()), forecaster=FakeForecaster(forecasts), state=state)
            result = runtime.run_once(dry_run=True)
            self.assertIn(result["action"], {"open", "cash"})
            self.assertTrue(result["orders"])
            active = state.load_active_episode()
            self.assertIsNotNone(active)
            self.assertEqual(active["symbol"], "GRAM-USDT-SWAP")

    def test_rebalance_threshold_submits_okx_transfer_legs_when_flat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = parse_config(
                {
                    "state_path": str(Path(tmp) / "state.sqlite3"),
                    "split": {
                        "sleeves": [
                            {"sleeve_id": "cash", "kind": "cash", "target_weight": 0.2, "leverage": 0},
                            {"sleeve_id": "sleeve_3x", "kind": "trade", "target_weight": 0.5, "leverage": 3, "sub_account": "sub3"},
                            {"sleeve_id": "sleeve_5x", "kind": "trade", "target_weight": 0.3, "leverage": 5, "sub_account": "sub5"},
                        ]
                    },
                }
            )
            master = FakeClient("1000")
            sleeve3 = FakeClient("9000")
            sleeve5 = FakeClient("0")
            runtime = OkxDemoRuntime(
                config=cfg,
                market=FakeMarket(bundle()),
                forecaster=FakeForecaster({}),
                state=BotStateStore(cfg.state_path),
                master_client=master,  # type: ignore[arg-type]
                sleeve_clients={"sleeve_3x": sleeve3, "sleeve_5x": sleeve5},  # type: ignore[arg-type]
            )
            result = runtime.run_once(dry_run=False, rebalance_only=True)
            self.assertEqual(result["rebalance"]["status"], "submitted")
            transfer_calls = [call for call in master.calls if call[0] == "asset_transfer"]
            self.assertTrue(transfer_calls)
            transfer_types = {call[1]["transfer_type"] for call in transfer_calls}
            self.assertIn("2", transfer_types)
            self.assertIn("1", transfer_types)

    def test_rebalance_blocks_missing_sub_account_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = parse_config(
                {
                    "state_path": str(Path(tmp) / "state.sqlite3"),
                    "split": {
                        "sleeves": [
                            {"sleeve_id": "cash", "kind": "cash", "target_weight": 0.2, "leverage": 0},
                            {"sleeve_id": "sleeve_3x", "kind": "trade", "target_weight": 0.5, "leverage": 3},
                            {"sleeve_id": "sleeve_5x", "kind": "trade", "target_weight": 0.3, "leverage": 5, "sub_account": "sub5"},
                        ]
                    },
                }
            )
            runtime = OkxDemoRuntime(
                config=cfg,
                market=FakeMarket(bundle()),
                forecaster=FakeForecaster({}),
                state=BotStateStore(cfg.state_path),
                master_client=FakeClient("1000"),  # type: ignore[arg-type]
                sleeve_clients={"sleeve_3x": FakeClient("9000"), "sleeve_5x": FakeClient("0")},  # type: ignore[arg-type]
            )
            with self.assertRaises(OkxDemoError):
                runtime.run_once(dry_run=False, rebalance_only=True)


if __name__ == "__main__":
    unittest.main()
