from __future__ import annotations

import base64
import hashlib
import hmac
import sys
import unittest
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kronosobuchalka.okx_demo.client import (  # noqa: E402
    OKX_BASE_URL,
    OkxCredentials,
    OkxDemoError,
    OkxV5Client,
    _auth_headers,
    quantize_down,
)


class OkxClientTests(unittest.TestCase):
    def test_v5_signature_headers(self) -> None:
        credentials = OkxCredentials("key", "secret", "passphrase")
        timestamp = "2020-12-08T09:08:57.715Z"
        request_path = "/api/v5/account/balance?ccy=USDT"
        headers = _auth_headers(credentials, timestamp, "GET", request_path, "", simulated=True)
        expected = base64.b64encode(
            hmac.new(
                b"secret",
                f"{timestamp}GET{request_path}".encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("ascii")
        self.assertEqual(headers["OK-ACCESS-SIGN"], expected)
        self.assertEqual(headers["OK-ACCESS-KEY"], "key")
        self.assertEqual(headers["OK-ACCESS-PASSPHRASE"], "passphrase")
        self.assertEqual(headers["x-simulated-trading"], "1")

    def test_rejects_disabled_simulated_trading_submission(self) -> None:
        client = OkxV5Client(credentials=OkxCredentials("k", "s", "p"), base_url=OKX_BASE_URL, simulated_trading=False)
        with self.assertRaises(OkxDemoError):
            client.ensure_can_submit(environment="okx_demo", dry_run=False)

    def test_rejects_non_demo_environment(self) -> None:
        client = OkxV5Client(credentials=OkxCredentials("k", "s", "p"), base_url=OKX_BASE_URL)
        with self.assertRaises(OkxDemoError):
            client.ensure_can_submit(environment="mainnet", dry_run=False)

    def test_dry_run_allows_missing_credentials(self) -> None:
        client = OkxV5Client(base_url=OKX_BASE_URL)
        client.ensure_can_submit(environment="okx_demo", dry_run=True)

    def test_quantize_down(self) -> None:
        self.assertEqual(quantize_down(Decimal("1.2349"), Decimal("0.01")), Decimal("1.23"))
        self.assertEqual(quantize_down(Decimal("12.39"), Decimal("1")), Decimal("12"))


if __name__ == "__main__":
    unittest.main()
