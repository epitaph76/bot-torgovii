from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any, Mapping
from urllib.parse import urlencode

import requests


DEMO_BASE_URL = "https://api-demo.bybit.com"


class BybitDemoError(RuntimeError):
    pass


@dataclass(frozen=True)
class BybitCredentials:
    api_key: str
    api_secret: str

    @property
    def present(self) -> bool:
        return bool(self.api_key and self.api_secret)


class BybitV5Client:
    def __init__(
        self,
        *,
        credentials: BybitCredentials | None = None,
        base_url: str = DEMO_BASE_URL,
        recv_window: int = 5000,
        timeout: float = 15.0,
        retries: int = 2,
        session: requests.Session | None = None,
    ):
        self.credentials = credentials or BybitCredentials("", "")
        self.base_url = str(base_url).rstrip("/")
        self.recv_window = int(recv_window)
        self.timeout = float(timeout)
        self.retries = max(int(retries), 0)
        self.session = session or requests.Session()

    @property
    def is_demo_endpoint(self) -> bool:
        return self.base_url == DEMO_BASE_URL

    def ensure_can_submit(self, *, environment: str, dry_run: bool) -> None:
        if dry_run:
            return
        if str(environment) != "bybit_demo":
            raise BybitDemoError("live submission requires environment='bybit_demo'")
        if not self.is_demo_endpoint:
            raise BybitDemoError(f"live submission requires {DEMO_BASE_URL}, got {self.base_url}")
        if not self.credentials.present:
            raise BybitDemoError("live submission requires Bybit Demo API credentials")

    def public_get(self, path: str, params: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        return self._request("GET", path, params=params, private=False)

    def private_get(self, path: str, params: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        return self._request("GET", path, params=params, private=True)

    def private_post(
        self,
        path: str,
        body: Mapping[str, Any],
        *,
        environment: str,
        dry_run: bool,
    ) -> Mapping[str, Any]:
        self.ensure_can_submit(environment=environment, dry_run=dry_run)
        if dry_run:
            return {"retCode": 0, "retMsg": "dry_run", "result": dict(body), "retExtInfo": {}, "time": _timestamp_ms()}
        return self._request("POST", path, body=body, private=True)

    def instruments_info(self, *, category: str, symbol: str) -> Mapping[str, Any]:
        return self.public_get("/v5/market/instruments-info", {"category": category, "symbol": symbol})

    def klines(self, *, category: str, symbol: str, interval: str, limit: int) -> Mapping[str, Any]:
        return self.public_get("/v5/market/kline", {"category": category, "symbol": symbol, "interval": interval, "limit": int(limit)})

    def ticker(self, *, category: str, symbol: str) -> Mapping[str, Any]:
        return self.public_get("/v5/market/tickers", {"category": category, "symbol": symbol})

    def wallet_balance(self, *, account_type: str = "UNIFIED", coin: str = "USDT") -> Mapping[str, Any]:
        return self.private_get("/v5/account/wallet-balance", {"accountType": account_type, "coin": coin})

    def positions(self, *, category: str, symbol: str) -> Mapping[str, Any]:
        return self.private_get("/v5/position/list", {"category": category, "symbol": symbol})

    def set_leverage(
        self,
        *,
        category: str,
        symbol: str,
        leverage: float,
        environment: str,
        dry_run: bool,
    ) -> Mapping[str, Any]:
        leverage_s = _decimal_text(Decimal(str(leverage)))
        return self.private_post(
            "/v5/position/set-leverage",
            {
                "category": category,
                "symbol": symbol,
                "buyLeverage": leverage_s,
                "sellLeverage": leverage_s,
            },
            environment=environment,
            dry_run=dry_run,
        )

    def create_market_order(
        self,
        *,
        category: str,
        symbol: str,
        side: str,
        qty: Decimal,
        reduce_only: bool,
        environment: str,
        dry_run: bool,
    ) -> Mapping[str, Any]:
        body: dict[str, Any] = {
            "category": category,
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": _decimal_text(qty),
            "reduceOnly": bool(reduce_only),
            "positionIdx": 0,
        }
        return self.private_post("/v5/order/create", body, environment=environment, dry_run=dry_run)

    def set_trading_stop(
        self,
        *,
        category: str,
        symbol: str,
        stop_loss: Decimal,
        trigger_by: str,
        environment: str,
        dry_run: bool,
    ) -> Mapping[str, Any]:
        return self.private_post(
            "/v5/position/trading-stop",
            {
                "category": category,
                "symbol": symbol,
                "tpslMode": "Full",
                "stopLoss": _decimal_text(stop_loss),
                "slTriggerBy": trigger_by,
                "positionIdx": 0,
            },
            environment=environment,
            dry_run=dry_run,
        )

    def universal_transfer(
        self,
        *,
        from_member_id: str,
        to_member_id: str,
        from_account_type: str,
        to_account_type: str,
        coin: str,
        amount: Decimal,
        environment: str,
        dry_run: bool,
    ) -> Mapping[str, Any]:
        return self.private_post(
            "/v5/asset/transfer/universal-transfer",
            {
                "transferId": str(uuid.uuid4()),
                "coin": coin,
                "amount": _decimal_text(amount),
                "fromMemberId": from_member_id,
                "toMemberId": to_member_id,
                "fromAccountType": from_account_type,
                "toAccountType": to_account_type,
            },
            environment=environment,
            dry_run=dry_run,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        private: bool,
    ) -> Mapping[str, Any]:
        method = method.upper()
        payload = ""
        query = _query_string(params or {})
        url = self.base_url + path
        if method == "GET" and query:
            url += "?" + query
            payload = query
        elif method == "POST":
            payload = _json_body(body or {})

        headers = {"Content-Type": "application/json"}
        if private:
            if not self.credentials.present:
                raise BybitDemoError("private Bybit request requires credentials")
            timestamp = str(_timestamp_ms())
            headers.update(_auth_headers(self.credentials, timestamp, self.recv_window, payload))

        if method not in {"GET", "POST"}:
            raise ValueError(f"unsupported method: {method}")
        attempts = self.retries + 1 if method == "GET" else 1
        last_exc: Exception | None = None
        response = None
        for attempt in range(attempts):
            try:
                if method == "GET":
                    response = self.session.get(url, headers=headers, timeout=self.timeout)
                else:
                    response = self.session.post(url, headers=headers, data=payload, timeout=self.timeout)
                break
            except requests.RequestException as exc:
                last_exc = exc
                if attempt + 1 >= attempts:
                    raise
                time.sleep(0.5 * (attempt + 1))
        if response is None:
            if last_exc is not None:
                raise last_exc
            raise BybitDemoError("Bybit request did not return a response")
        if response.status_code >= 400:
            raise BybitDemoError(f"Bybit HTTP {response.status_code}: {response.text[:500]}")
        data = response.json()
        if not isinstance(data, Mapping):
            raise BybitDemoError("Bybit response is not an object")
        if int(data.get("retCode", 0) or 0) != 0:
            raise BybitDemoError(f"Bybit retCode={data.get('retCode')} retMsg={data.get('retMsg')}")
        return data


def _auth_headers(credentials: BybitCredentials, timestamp: str, recv_window: int, payload: str) -> dict[str, str]:
    prehash = f"{timestamp}{credentials.api_key}{int(recv_window)}{payload}"
    signature = hmac.new(credentials.api_secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "X-BAPI-API-KEY": credentials.api_key,
        "X-BAPI-SIGN": signature,
        "X-BAPI-SIGN-TYPE": "2",
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": str(int(recv_window)),
    }


def _timestamp_ms() -> int:
    return int(time.time() * 1000)


def _query_string(params: Mapping[str, Any]) -> str:
    clean = {key: value for key, value in params.items() if value is not None and value != ""}
    return urlencode(sorted(clean.items()), doseq=True)


def _json_body(body: Mapping[str, Any]) -> str:
    return json.dumps(dict(body), separators=(",", ":"), sort_keys=False)


def quantize_down(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"
