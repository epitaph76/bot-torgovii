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
class BybitErrorPolicy:
    action: str
    reason: str
    retryable: bool
    blocks_trading: bool
    needs_reconciliation: bool


class BybitAPIError(BybitDemoError):
    def __init__(
        self,
        *,
        method: str,
        path: str,
        ret_code: int | None = None,
        ret_msg: str = "",
        status_code: int | None = None,
        response_text: str = "",
    ):
        self.method = method
        self.path = path
        self.ret_code = ret_code
        self.ret_msg = ret_msg
        self.status_code = status_code
        self.response_text = response_text
        self.policy = classify_bybit_error(ret_code=ret_code, ret_msg=ret_msg, status_code=status_code)
        if ret_code is not None:
            message = f"Bybit retCode={ret_code} retMsg={ret_msg} action={self.policy.action} reason={self.policy.reason}"
        else:
            message = f"Bybit HTTP {status_code} action={self.policy.action} reason={self.policy.reason}: {response_text[:500]}"
        super().__init__(message)


class BybitTransportError(BybitDemoError):
    def __init__(self, *, method: str, path: str, url: str, original: Exception):
        self.method = method
        self.path = path
        self.url = url
        self.original = original
        needs_reconciliation = method.upper() == "POST"
        self.policy = BybitErrorPolicy(
            action="retry_exhausted",
            reason="transport_error",
            retryable=True,
            blocks_trading=True,
            needs_reconciliation=needs_reconciliation,
        )
        super().__init__(f"Bybit transport error {method} {path}: {original}")


_RETRYABLE_HTTP_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
_AUTH_RET_CODES = {-2015, 33004, 10003, 10004, 10005, 10007, 10008, 10009, 10010, 10024, 10027}
_CONFIG_RET_CODES = {
    10001,
    10017,
    10028,
    100028,
    10029,
    110003,
    110017,
    110029,
    110031,
    110037,
    110066,
    110067,
    110068,
    110074,
    110086,
    110088,
    110094,
    110100,
    110123,
    110125,
    110132,
    131002,
    131203,
}
_RETRYABLE_RET_CODES = {
    -1,
    429,
    10000,
    10006,
    10016,
    30035,
    40004,
    110079,
    3400214,
    131001,
    131003,
    131200,
    131201,
    131205,
    131216,
    170007,
    170032,
    170146,
    170147,
    170191,
    170212,
}
_NO_OP_CANDIDATE_RET_CODES = {110025, 110026, 110027, 110043, 34040}
_MARGIN_RISK_RET_CODES = {
    110004,
    110006,
    110007,
    110011,
    110012,
    110013,
    110014,
    110021,
    110023,
    110044,
    110045,
    110047,
    110048,
    110051,
    110052,
    110053,
    110080,
    110085,
    110089,
    110090,
    110131,
    131212,
    131217,
}
_POSITION_STATE_RET_CODES = {
    110001,
    110005,
    110008,
    110010,
    110024,
    110028,
    110033,
    110034,
    110035,
    110039,
    110040,
    110058,
    110060,
    110063,
    110072,
    110095,
    110096,
    110099,
    110118,
}
_POSITION_ALREADY_FLAT_RET_CODES = {110005, 110008, 110010, 110034}
_DUPLICATE_ORDER_LINK_RET_CODES = {110072}


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
        safe_to_retry: bool = False,
    ) -> Mapping[str, Any]:
        self.ensure_can_submit(environment=environment, dry_run=dry_run)
        if dry_run:
            return {"retCode": 0, "retMsg": "dry_run", "result": dict(body), "retExtInfo": {}, "time": _timestamp_ms()}
        return self._request("POST", path, body=body, private=True, safe_to_retry=safe_to_retry)

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
            return self.private_post("/v5/position/set-leverage", body, environment=environment, dry_run=dry_run, safe_to_retry=True)
        except BybitAPIError as exc:
            if exc.ret_code == 110043:
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
        order_link_id: str | None = None,
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
        if order_link_id:
            body["orderLinkId"] = str(order_link_id)
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
        body = {
            "category": category,
            "symbol": symbol,
            "tpslMode": "Full",
            "stopLoss": _decimal_text(stop_loss),
            "slTriggerBy": trigger_by,
            "positionIdx": 0,
        }
        try:
            return self.private_post("/v5/position/trading-stop", body, environment=environment, dry_run=dry_run, safe_to_retry=True)
        except BybitAPIError as exc:
            if exc.ret_code == 34040:
                return {
                    "retCode": 0,
                    "retMsg": "trading stop not modified",
                    "result": body,
                    "retExtInfo": {"ignoredRetCode": 34040},
                    "time": _timestamp_ms(),
                }
            raise

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
        safe_to_retry: bool = False,
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
        attempts = self.retries + 1 if method == "GET" or safe_to_retry else 1
        timestamp_resynced = False
        attempt = 0
        while True:
            attempt += 1
            headers = self._headers(private=private, payload=payload)
            try:
                if method == "GET":
                    response = self.session.get(url, headers=headers, timeout=self.timeout)
                else:
                    response = self.session.post(url, headers=headers, data=payload, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt >= attempts:
                    raise BybitTransportError(method=method, path=path, url=url, original=exc) from exc
                time.sleep(_retry_delay_seconds(attempt))
                continue
            if response.status_code >= 400:
                error = BybitAPIError(method=method, path=path, status_code=response.status_code, response_text=response.text)
                if _should_retry_bybit_error(error, attempt=attempt, attempts=attempts):
                    time.sleep(_retry_delay_seconds(attempt))
                    continue
                raise error
            data = response.json()
            if not isinstance(data, Mapping):
                raise BybitDemoError("Bybit response is not an object")
            ret_code = int(data.get("retCode", 0) or 0)
            if ret_code == 0:
                return data
            if private and ret_code == 10002 and not timestamp_resynced:
                self.sync_server_time()
                timestamp_resynced = True
                if attempt >= attempts:
                    attempts += 1
                continue
            error = BybitAPIError(method=method, path=path, ret_code=ret_code, ret_msg=str(data.get("retMsg") or ""))
            if _should_retry_bybit_error(error, attempt=attempt, attempts=attempts):
                time.sleep(_retry_delay_seconds(attempt))
                continue
            raise error

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


def classify_bybit_error(*, ret_code: int | None = None, ret_msg: str = "", status_code: int | None = None) -> BybitErrorPolicy:
    if status_code is not None:
        if status_code in _RETRYABLE_HTTP_STATUS:
            return BybitErrorPolicy("retry", "http_transient", True, True, True)
        if status_code in {401, 403}:
            return BybitErrorPolicy("block", "auth_or_ip_permission", False, True, False)
        if status_code in {400, 404}:
            return BybitErrorPolicy("block", "bad_request_or_route", False, True, False)
        return BybitErrorPolicy("block", "http_error", False, True, True)

    if ret_code is None:
        return BybitErrorPolicy("block", "unknown_error", False, True, True)
    code = int(ret_code)
    if code == 0:
        return BybitErrorPolicy("ok", "ok", False, False, False)
    if code == 10002:
        return BybitErrorPolicy("resync_clock", "timestamp_or_recv_window", True, True, False)
    if code in _NO_OP_CANDIDATE_RET_CODES:
        return BybitErrorPolicy("no_op_candidate", "already_in_requested_state", False, False, False)
    if code in _RETRYABLE_RET_CODES:
        return BybitErrorPolicy("retry", "exchange_transient_or_rate_limit", True, True, True)
    if code in _AUTH_RET_CODES:
        return BybitErrorPolicy("block", "auth_ip_permission_or_compliance", False, True, False)
    if code in _CONFIG_RET_CODES:
        return BybitErrorPolicy("block", "request_or_symbol_config", False, True, False)
    if code in _MARGIN_RISK_RET_CODES:
        return BybitErrorPolicy("block", "margin_balance_or_risk_limit", False, True, True)
    if code in _POSITION_STATE_RET_CODES or 110000 <= code < 111000:
        return BybitErrorPolicy("reconcile", "position_order_state", False, True, True)
    if 131000 <= code < 132000:
        return BybitErrorPolicy("reconcile", "asset_transfer_state", False, True, True)
    return BybitErrorPolicy("block", "unclassified_bybit_error", False, True, True)


def is_duplicate_order_link_error(exc: Exception) -> bool:
    return isinstance(exc, BybitAPIError) and exc.ret_code in _DUPLICATE_ORDER_LINK_RET_CODES


def is_position_already_flat_error(exc: Exception) -> bool:
    return isinstance(exc, BybitAPIError) and exc.ret_code in _POSITION_ALREADY_FLAT_RET_CODES


def handled_bybit_error_response(exc: BybitAPIError, *, reason: str) -> dict[str, Any]:
    return {
        "retCode": 0,
        "retMsg": reason,
        "result": {},
        "retExtInfo": {
            "handledRetCode": exc.ret_code,
            "handledRetMsg": exc.ret_msg,
            "policy": exc.policy.action,
            "policyReason": exc.policy.reason,
        },
        "time": _timestamp_ms(),
    }


def _should_retry_bybit_error(error: BybitAPIError, *, attempt: int, attempts: int) -> bool:
    return bool(error.policy.retryable and attempt < attempts)


def _retry_delay_seconds(attempt: int) -> float:
    return min(5.0, 0.5 * (2 ** max(attempt - 1, 0)))


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
