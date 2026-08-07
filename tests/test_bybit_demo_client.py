from __future__ import annotations

import hashlib
import hmac
import json
import sys
import unittest
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kronosobuchalka.bybit_demo.client import (  # noqa: E402
    DEMO_BASE_URL,
    BybitAPIError,
    BybitCredentials,
    BybitDemoError,
    BybitV5Client,
    _auth_headers,
    classify_bybit_error,
    _server_time_ms,
    quantize_down,
)
from decimal import Decimal  # noqa: E402


class FakeResponse:
    def __init__(self, payload: Mapping[str, Any], status_code: int = 200):
        self._payload = dict(payload)
        self.status_code = status_code
        self.text = str(payload)

    def json(self) -> Mapping[str, Any]:
        return self._payload


class BybitClientTests(unittest.TestCase):
    def test_v5_signature_headers(self) -> None:
        credentials = BybitCredentials("key", "secret")
        headers = _auth_headers(credentials, "1234567890", 5000, "category=linear&symbol=GRAMUSDT")
        expected = hmac.new(
            b"secret",
            b"1234567890key5000category=linear&symbol=GRAMUSDT",
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(headers["X-BAPI-SIGN"], expected)
        self.assertEqual(headers["X-BAPI-SIGN-TYPE"], "2")
        self.assertEqual(headers["X-BAPI-API-KEY"], "key")

    def test_rejects_non_demo_submission(self) -> None:
        client = BybitV5Client(credentials=BybitCredentials("k", "s"), base_url="https://api.bybit.com")
        with self.assertRaises(BybitDemoError):
            client.ensure_can_submit(environment="bybit_demo", dry_run=False)

    def test_rejects_non_demo_environment(self) -> None:
        client = BybitV5Client(credentials=BybitCredentials("k", "s"), base_url=DEMO_BASE_URL)
        with self.assertRaises(BybitDemoError):
            client.ensure_can_submit(environment="mainnet", dry_run=False)

    def test_dry_run_allows_missing_credentials(self) -> None:
        client = BybitV5Client(base_url=DEMO_BASE_URL)
        client.ensure_can_submit(environment="bybit_demo", dry_run=True)

    def test_quantize_down(self) -> None:
        self.assertEqual(quantize_down(Decimal("1.2349"), Decimal("0.01")), Decimal("1.23"))
        self.assertEqual(quantize_down(Decimal("12.39"), Decimal("0.1")), Decimal("12.3"))

    def test_server_time_parser_accepts_bybit_shapes(self) -> None:
        self.assertEqual(_server_time_ms({"result": {"timeSecond": "2"}}), 2000)
        self.assertEqual(_server_time_ms({"result": {"timeNano": "2500000000"}}), 2500)
        self.assertEqual(_server_time_ms({"time": "3000"}), 3000)

    def test_error_policy_classifies_strategy_relevant_codes(self) -> None:
        self.assertEqual(classify_bybit_error(ret_code=10002).action, "resync_clock")
        self.assertEqual(classify_bybit_error(ret_code=10006).action, "retry")
        self.assertEqual(classify_bybit_error(ret_code=110043).action, "no_op_candidate")
        self.assertEqual(classify_bybit_error(ret_code=34040).action, "no_op_candidate")
        self.assertEqual(classify_bybit_error(ret_code=110004).reason, "margin_balance_or_risk_limit")
        self.assertEqual(classify_bybit_error(ret_code=110072).reason, "position_order_state")
        self.assertEqual(classify_bybit_error(status_code=429).action, "retry")

    def test_private_request_resyncs_timestamp_after_retcode_10002(self) -> None:
        class FakeSession:
            def __init__(self) -> None:
                self.wallet_headers: list[Mapping[str, str]] = []

            def get(self, url: str, *, headers: Mapping[str, str], timeout: float) -> FakeResponse:
                if "/v5/market/time" in url:
                    return FakeResponse({"retCode": 0, "retMsg": "OK", "result": {"timeSecond": "2"}, "time": 2000})
                if "/v5/account/wallet-balance" in url:
                    self.wallet_headers.append(dict(headers))
                    if len(self.wallet_headers) == 1:
                        return FakeResponse({"retCode": 10002, "retMsg": "invalid timestamp"})
                    return FakeResponse({"retCode": 0, "retMsg": "OK", "result": {"list": []}})
                raise AssertionError(f"unexpected URL: {url}")

        session = FakeSession()
        client = BybitV5Client(credentials=BybitCredentials("key", "secret"), session=session)
        with patch("kronosobuchalka.bybit_demo.client._timestamp_ms", side_effect=[1000, 1000, 1100]):
            payload = client.wallet_balance()

        self.assertEqual(payload["retCode"], 0)
        self.assertEqual(len(session.wallet_headers), 2)
        self.assertEqual(session.wallet_headers[0]["X-BAPI-TIMESTAMP"], "1000")
        self.assertEqual(session.wallet_headers[1]["X-BAPI-TIMESTAMP"], "2100")

    def test_set_leverage_accepts_already_set_retcode(self) -> None:
        class FakeSession:
            def post(self, url: str, *, headers: Mapping[str, str], data: str, timeout: float) -> FakeResponse:
                if "/v5/position/set-leverage" not in url:
                    raise AssertionError(f"unexpected URL: {url}")
                return FakeResponse({"retCode": 110043, "retMsg": "leverage not modified", "result": {}})

        client = BybitV5Client(credentials=BybitCredentials("key", "secret"), session=FakeSession())  # type: ignore[arg-type]
        payload = client.set_leverage(category="linear", symbol="ZECUSDT", leverage=1, environment="bybit_demo", dry_run=False)

        self.assertEqual(payload["retCode"], 0)
        self.assertEqual(payload["retExtInfo"]["ignoredRetCode"], 110043)

    def test_set_trading_stop_accepts_not_modified_retcode(self) -> None:
        class FakeSession:
            def post(self, url: str, *, headers: Mapping[str, str], data: str, timeout: float) -> FakeResponse:
                if "/v5/position/trading-stop" not in url:
                    raise AssertionError(f"unexpected URL: {url}")
                return FakeResponse({"retCode": 34040, "retMsg": "not modified", "result": {}})

        client = BybitV5Client(credentials=BybitCredentials("key", "secret"), session=FakeSession())  # type: ignore[arg-type]
        payload = client.set_trading_stop(
            category="linear",
            symbol="ZECUSDT",
            stop_loss=Decimal("536.55"),
            trigger_by="LastPrice",
            environment="bybit_demo",
            dry_run=False,
        )

        self.assertEqual(payload["retCode"], 0)
        self.assertEqual(payload["retExtInfo"]["ignoredRetCode"], 34040)

    def test_safe_post_retries_transient_retcode(self) -> None:
        class FakeSession:
            def __init__(self) -> None:
                self.posts = 0

            def post(self, url: str, *, headers: Mapping[str, str], data: str, timeout: float) -> FakeResponse:
                self.posts += 1
                if self.posts == 1:
                    return FakeResponse({"retCode": 10006, "retMsg": "Too many visits"})
                return FakeResponse({"retCode": 0, "retMsg": "OK", "result": {}})

        session = FakeSession()
        client = BybitV5Client(credentials=BybitCredentials("key", "secret"), session=session, retries=1)  # type: ignore[arg-type]
        payload = client.set_trading_stop(
            category="linear",
            symbol="ZECUSDT",
            stop_loss=Decimal("474.56"),
            trigger_by="LastPrice",
            environment="bybit_demo",
            dry_run=False,
        )

        self.assertEqual(payload["retCode"], 0)
        self.assertEqual(session.posts, 2)

    def test_market_order_does_not_retry_transient_retcode_without_reconciliation(self) -> None:
        class FakeSession:
            def __init__(self) -> None:
                self.posts = 0

            def post(self, url: str, *, headers: Mapping[str, str], data: str, timeout: float) -> FakeResponse:
                self.posts += 1
                return FakeResponse({"retCode": 10006, "retMsg": "Too many visits"})

        session = FakeSession()
        client = BybitV5Client(credentials=BybitCredentials("key", "secret"), session=session, retries=2)  # type: ignore[arg-type]
        with self.assertRaises(BybitAPIError) as raised:
            client.create_market_order(
                category="linear",
                symbol="ZECUSDT",
                side="Buy",
                qty=Decimal("0.2"),
                reduce_only=False,
                environment="bybit_demo",
                dry_run=False,
                order_link_id="krn-test",
            )

        self.assertEqual(session.posts, 1)
        self.assertEqual(raised.exception.policy.action, "retry")

    def test_market_order_sends_order_link_id(self) -> None:
        class FakeSession:
            def __init__(self) -> None:
                self.body: Mapping[str, Any] = {}

            def post(self, url: str, *, headers: Mapping[str, str], data: str, timeout: float) -> FakeResponse:
                self.body = json.loads(data)
                return FakeResponse({"retCode": 0, "retMsg": "OK", "result": {"orderId": "abc"}})

        session = FakeSession()
        client = BybitV5Client(credentials=BybitCredentials("key", "secret"), session=session)  # type: ignore[arg-type]
        client.create_market_order(
            category="linear",
            symbol="ZECUSDT",
            side="Buy",
            qty=Decimal("0.2"),
            reduce_only=False,
            environment="bybit_demo",
            dry_run=False,
            order_link_id="krn-test",
        )

        self.assertEqual(session.body["orderLinkId"], "krn-test")

    def test_closed_pnl_sends_expected_query_params(self) -> None:
        class FakeSession:
            def __init__(self) -> None:
                self.url = ""

            def get(self, url: str, *, headers: Mapping[str, str], timeout: float) -> FakeResponse:
                self.url = url
                return FakeResponse({"retCode": 0, "retMsg": "OK", "result": {"list": []}})

        session = FakeSession()
        client = BybitV5Client(credentials=BybitCredentials("key", "secret"), session=session)  # type: ignore[arg-type]
        client.closed_pnl(category="linear", symbol="BTCUSDT", start_time_ms=1, end_time_ms=2, limit=25)

        self.assertIn("/v5/position/closed-pnl", session.url)
        self.assertIn("category=linear", session.url)
        self.assertIn("symbol=BTCUSDT", session.url)
        self.assertIn("startTime=1", session.url)
        self.assertIn("endTime=2", session.url)
        self.assertIn("limit=25", session.url)

    def test_executions_sends_expected_query_params(self) -> None:
        class FakeSession:
            def __init__(self) -> None:
                self.url = ""

            def get(self, url: str, *, headers: Mapping[str, str], timeout: float) -> FakeResponse:
                self.url = url
                return FakeResponse({"retCode": 0, "retMsg": "OK", "result": {"list": []}})

        session = FakeSession()
        client = BybitV5Client(credentials=BybitCredentials("key", "secret"), session=session)  # type: ignore[arg-type]
        client.executions(category="linear", symbol="SOLUSDT", start_time_ms=10, end_time_ms=20, limit=30, cursor="abc")

        self.assertIn("/v5/execution/list", session.url)
        self.assertIn("category=linear", session.url)
        self.assertIn("symbol=SOLUSDT", session.url)
        self.assertIn("startTime=10", session.url)
        self.assertIn("endTime=20", session.url)
        self.assertIn("limit=30", session.url)
        self.assertIn("cursor=abc", session.url)


if __name__ == "__main__":
    unittest.main()
