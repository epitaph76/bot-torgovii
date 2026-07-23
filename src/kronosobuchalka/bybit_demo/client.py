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
        recv_window: int = 15000,
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
        self._server_time_offset_ms = 0

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

    def server_time_ms(self) -> int:
        payload = self.public_get("/v5/market/time")
        server_ms = _server_time_ms(payload)
        if server_ms <= 0:
            raise BybitDemoError(f"Bybit server time response is not usable: {payload}")
        return server_ms

    def sync_server_time(self) -> int:
        server_ms = self.server_time_ms()
        self._server_time_offset_ms = server_ms - _timestamp_ms()
        return self._server_time_offset_ms

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
        body = {
            "category": category,
            "symbol": symbol,
            "buyLeverage": leverage_s,
            "sellLeverage": leverage_s,
        }
        try:
            return self.private_post("/v5/position/set-leverage", body, environment=environment, dry_run=dry_run)
        except BybitDemoError as exc:
            message = str(exc)
            if "retCode=110043" in message and "leverage not modified" in message:
                return {
                    "retCode": 0,
                    "retMsg": "leverage not modified",
                    "result": body,
                    "retExtInfo": {"ignoredRetCode": 110043},
                    "time": _timestamp_ms(),
                }
            raise

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

        if method not in {"GET", "POST"}:
            raise ValueError(f"unsupported method: {method}")
        attempts = self.retries + 1 if method == "GET" else 1
        last_exc: Exception | None = None
        response = None
        timestamp_resynced = False
        attempt = 0
        while True:
            headers = self._headers(private=private, payload=payload)
            try:
                if method == "GET":
                    response = self.session.get(url, headers=headers, timeout=self.timeout)
                else:
                    response = self.session.post(url, headers=headers, data=payload, timeout=self.timeout)
            except requests.RequestException as exc:
                last_exc = exc
                attempt += 1
                if attempt >= attempts:
                    raise
                time.sleep(min(2.0, 0.5 * attempt))
                continue
            if response is None:
                if last_exc is not None:
                    raise last_exc
                raise BybitDemoError("Bybit request did not return a response")
            if response.status_code >= 400:
                raise BybitDemoError(f"Bybit HTTP {response.status_code}: {response.text[:500]}")
            data = response.json()
            if not isinstance(data, Mapping):
                raise BybitDemoError("Bybit response is not an object")
            ret_code = int(data.get("retCode", 0) or 0)
            if ret_code == 0:
                return data
            if private and ret_code == 10002 and not timestamp_resynced:
                self.sync_server_time()
                timestamp_resynced = True
                continue
            raise BybitDemoError(f"Bybit retCode={data.get('retCode')} retMsg={data.get('retMsg')}")

    def _headers(self, *, private: bool, payload: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if private:
            if not self.credentials.present:
                raise BybitDemoError("private Bybit request requires credentials")
            timestamp = str(_timestamp_ms() + int(self._server_time_offset_ms))
            headers.update(_auth_headers(self.credentials, timestamp, self.recv_window, payload))
        return headers


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


def _server_time_ms(payload: Mapping[str, Any]) -> int:
    result = payload.get("result") if isinstance(payload, Mapping) else {}
    if isinstance(result, Mapping):
        time_nano = result.get("timeNano")
        if time_nano not in (None, ""):
            return int(Decimal(str(time_nano)) / Decimal("1000000"))
        time_second = result.get("timeSecond")
        if time_second not in (None, ""):
            return int(Decimal(str(time_second)) * Decimal("1000"))
    response_time = payload.get("time") if isinstance(payload, Mapping) else None
    if response_time not in (None, ""):
        return int(Decimal(str(response_time)))
    return 0


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
