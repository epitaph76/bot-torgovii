from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kronosobuchalka.bybit_demo.config import BybitDemoBotConfig  # noqa: E402
from kronosobuchalka.bybit_demo.market import BybitMarketData  # noqa: E402


class FakeClient:
    def __init__(self) -> None:
        self.calls = 0

    def instruments_info(self, *, category: str, symbol: str) -> Mapping[str, Any]:
        self.calls += 1
        return {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "list": [
                    {
                        "symbol": symbol,
                        "status": "Trading",
                        "priceFilter": {"tickSize": "0.01"},
                        "lotSizeFilter": {
                            "qtyStep": "0.1",
                            "minOrderQty": "0.1",
                            "minNotionalValue": "5",
                            "maxMktOrderQty": "1000",
                        },
                        "leverageFilter": {"maxLeverage": "50"},
                    }
                ]
            },
        }


class BybitMarketDataTests(unittest.TestCase):
    def test_instrument_spec_is_cached_after_first_fetch(self) -> None:
        client = FakeClient()
        market = BybitMarketData(client=client, config=BybitDemoBotConfig())  # type: ignore[arg-type]

        first = market.instrument_spec("ZECUSDT")
        second = market.instrument_spec("ZECUSDT")

        self.assertIs(first, second)
        self.assertEqual(client.calls, 1)
        self.assertEqual(first.qty_step, second.qty_step)


if __name__ == "__main__":
    unittest.main()
