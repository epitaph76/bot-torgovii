from __future__ import annotations

import hashlib
import hmac
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kronosobuchalka.bybit_demo.client import (  # noqa: E402
    DEMO_BASE_URL,
    BybitCredentials,
    BybitDemoError,
    BybitV5Client,
    _auth_headers,
    quantize_down,
)
from decimal import Decimal  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
