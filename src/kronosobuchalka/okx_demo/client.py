from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Mapping
from urllib.parse import urlencode

import requests


OKX_BASE_URL = "https://www.okx.com"


class OkxDemoError(RuntimeError):
    pass


@dataclass(frozen=True)
class OkxCredentials:
    api_key: str
    api_secret: str
    passphrase: str

    @property
    def present(self) -> bool:
        return bool(self.api_key and self.api_secret and self.passphrase)


class OkxV5Client:
    def __init__(
        self,
        *,
        credentials: OkxCredentials | None = None,
        base_url: str = OKX_BASE_URL,
        simulated_trading: bool = True,
        timeout: float = 15.0,
        retries: int = 2,
        session: requests.Session | None = None,
    ):
        self.credentials = credentials or OkxCredentials("", "", "")
        self.base_url = str(base_url).rstrip("/")
        self.simulated_trading = bool(simulated_trading)
        self.timeout = float(timeout)
        self.retries = max(int(retries), 0)
        self.session = session or requests.Session()

    @property
    def is_okx_endpoint(self) -> bool:
        return self.base_url == OKX_BASE_URL

    def ensure_can_submit(self, *, environment: str, dry_run: bool) -> None:
        if dry_run:
            return
        if str(environment) != "okx_demo":
            raise OkxDemoError("live submission requires environment='okx_demo'")
        if not self.is_okx_endpoint:
            raise OkxDemoError(f"live submission requires {OKX_BASE_URL}, got {self.base_url}")
        if not self.simulated_trading:
            raise OkxDemoError("live submission requires OKX simulated trading header")
        if not self.credentials.present:
            raise OkxDemoError("live submission requires OKX Demo API credentials and passphrase")

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
            return {"code": "0", "msg": "dry_run", "data": [dict(body)]}
        return self._request("POST", path, body=body, private=True)

    def instruments(self, *, inst_type: str, inst_id: str) -> Mapping[str, Any]:
        return self.public_get("/api/v5/public/instruments", {"instType": inst_type, "instId": inst_id})

    def candles(self, *, inst_id: str, bar: str, limit: int) -> Mapping[str, Any]:
        return self.public_get("/api/v5/market/candles", {"instId": inst_id, "bar": bar, "limit": int(limit)})

    def ticker(self, *, inst_id: str) -> Mapping[str, Any]:
        return self.public_get("/api/v5/market/ticker", {"instId": inst_id})

    def account_balance(self, *, coin: str = "USDT") -> Mapping[str, Any]:
        return self.private_get("/api/v5/account/balance", {"ccy": coin})

    def positions(self, *, inst_type: str, inst_id: str) -> Mapping[str, Any]:
        return self.private_get("/api/v5/account/positions", {"instType": inst_type, "instId": inst_id})

    def set_leverage(
        self,
        *,
        inst_id: str,
        leverage: float,
        margin_mode: str,
        environment: str,
        dry_run: bool,
    ) -> Mapping[str, Any]:
        return self.private_post(
            "/api/v5/account/set-leverage",
            {
                "instId": inst_id,
                "lever": _decimal_text(Decimal(str(leverage))),
                "mgnMode": margin_mode,
            },
            environment=environment,
            dry_run=dry_run,
        )

    def create_market_order(
        self,
        *,
        inst_id: str,
        side: str,
        size: Decimal,
        td_mode: str,
        reduce_only: bool,
        environment: str,
        dry_run: bool,
    ) -> Mapping[str, Any]:
        body: dict[str, Any] = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": str(side).lower(),
            "ordType": "market",
            "sz": _decimal_text(size),
            "reduceOnly": "true" if reduce_only else "false",
        }
        return self.private_post("/api/v5/trade/order", body, environment=environment, dry_run=dry_run)

    def place_stop_loss(
        self,
        *,
        inst_id: str,
        close_side: str,
        size: Decimal,
        td_mode: str,
        stop_loss: Decimal,
        trigger_px_type: str,
        environment: str,
        dry_run: bool,
    ) -> Mapping[str, Any]:
        return self.private_post(
            "/api/v5/trade/order-algo",
            {
                "instId": inst_id,
                "tdMode": td_mode,
                "side": str(close_side).lower(),
                "ordType": "conditional",
                "sz": _decimal_text(size),
                "slTriggerPx": _decimal_text(stop_loss),
                "slOrdPx": "-1",
                "slTriggerPxType": str(trigger_px_type).lower(),
                "reduceOnly": "true",
            },
            environment=environment,
            dry_run=dry_run,
        )

    def asset_transfer(
        self,
        *,
        coin: str,
        amount: Decimal,
        from_account: str,
        to_account: str,
        transfer_type: str,
        sub_account: str,
        environment: str,
        dry_run: bool,
    ) -> Mapping[str, Any]:
        body: dict[str, Any] = {
            "ccy": coin,
            "amt": _decimal_text(amount),
            "from": from_account,
            "to": to_account,
            "type": transfer_type,
            "clientId": uuid.uuid4().hex[:32],
        }
        if sub_account:
            body["subAcct"] = sub_account
        return self.private_post("/api/v5/asset/transfer", body, environment=environment, dry_run=dry_run)

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
        if method not in {"GET", "POST"}:
            raise ValueError(f"unsupported method: {method}")

        query = _query_string(params or {})
        body_payload = _json_body(body or {}) if method == "POST" else ""
        request_path = path + (f"?{query}" if method == "GET" and query else "")
        url = self.base_url + request_path

        headers = {"Content-Type": "application/json"}
        if self.simulated_trading:
            headers["x-simulated-trading"] = "1"
        if private:
            if not self.credentials.present:
                raise OkxDemoError("private OKX request requires credentials and passphrase")
            timestamp = _timestamp_iso()
            headers.update(_auth_headers(self.credentials, timestamp, method, request_path, body_payload, simulated=self.simulated_trading))

        attempts = self.retries + 1 if method == "GET" else 1
        last_exc: Exception | None = None
        response = None
        for attempt in range(attempts):
            try:
                if method == "GET":
                    response = self.session.get(url, headers=headers, timeout=self.timeout)
                else:
                    response = self.session.post(url, headers=headers, data=body_payload, timeout=self.timeout)
                break
            except requests.RequestException as exc:
                last_exc = exc
                if attempt + 1 >= attempts:
                    raise
                time.sleep(0.5 * (attempt + 1))
        if response is None:
            if last_exc is not None:
                raise last_exc
            raise OkxDemoError("OKX request did not return a response")
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, Mapping):
            raise OkxDemoError("OKX response is not an object")
        if str(data.get("code", "0")) != "0":
            raise OkxDemoError(f"OKX code={data.get('code')} msg={data.get('msg')}")
        return data


def _auth_headers(
    credentials: OkxCredentials,
    timestamp: str,
    method: str,
    request_path: str,
    body: str,
    *,
    simulated: bool = True,
) -> dict[str, str]:
    prehash = f"{timestamp}{method.upper()}{request_path}{body}"
    digest = hmac.new(credentials.api_secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
    headers = {
        "OK-ACCESS-KEY": credentials.api_key,
        "OK-ACCESS-SIGN": base64.b64encode(digest).decode("ascii"),
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": credentials.passphrase,
    }
    if simulated:
        headers["x-simulated-trading"] = "1"
    return headers


def _timestamp_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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
