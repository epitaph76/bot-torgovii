from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from .client import BybitV5Client
from .config import load_config
from .env_store import EnvStore
from .process_manager import project_root_for_config
from .state import BotStateStore


class BybitAccountService:
    def __init__(self, *, config_path: str | Path, env_path: str | Path = ".env"):
        self.config_path = Path(config_path)
        self.root = project_root_for_config(self.config_path)
        if not self.config_path.is_absolute():
            self.config_path = self.root / self.config_path
        env_path = Path(env_path)
        self.env_store = EnvStore(env_path if env_path.is_absolute() else self.root / env_path)
        self.env_store.apply_to_environ()
        self.config = load_config(self.config_path)
        self.client = BybitV5Client(
            credentials=self.config.master_credentials(),
            base_url=self.config.base_url,
            recv_window=self.config.recv_window,
            timeout=self.config.request_timeout_seconds,
            retries=self.config.request_retries,
        )
        state_path = Path(self.config.state_path)
        self.state = BotStateStore(state_path if state_path.is_absolute() else self.root / state_path)

    def snapshot(self, *, history_limit: int = 50) -> dict[str, Any]:
        wallet = _safe_section(self.wallet)
        positions = _safe_section(self.positions)
        closed_pnl = _safe_section(lambda: self.closed_pnl(limit=history_limit))
        executions = _safe_section(lambda: self.executions(limit=history_limit))
        local_history = {
            "decisions": self.state.latest_decisions(limit=20),
            "order_events": self.state.latest_order_events(limit=50),
        }
        return {
            "as_of_utc": utcnow_iso(),
            "environment": self.config.environment,
            "base_url": self.config.base_url,
            "wallet": wallet,
            "positions": positions,
            "closed_pnl": closed_pnl,
            "executions": executions,
            "local_history": local_history,
        }

    def wallet(self) -> dict[str, Any]:
        payload = self.client.wallet_balance(account_type=self.config.master_account.account_type, coin=self.config.split.coin)
        return {
            "raw": payload,
            "coins": compact_wallet(payload, coin=self.config.split.coin),
        }

    def positions(self) -> dict[str, Any]:
        rows = []
        raw: dict[str, Any] = {}
        for symbol in self.config.strategy.symbols:
            payload = self.client.positions(category=self.config.strategy.category, symbol=symbol)
            raw[symbol] = payload
            rows.extend(compact_positions(payload, fallback_symbol=symbol))
        return {"rows": rows, "raw": raw}

    def closed_pnl(self, *, limit: int = 50, symbol: str | None = None, days: int = 6) -> dict[str, Any]:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=max(int(days), 1))
        payload = self.client.closed_pnl(
            category=self.config.strategy.category,
            symbol=symbol,
            start_time_ms=int(start.timestamp() * 1000),
            end_time_ms=int(end.timestamp() * 1000),
            limit=limit,
        )
        return {"rows": compact_list_result(payload), "raw": payload}

    def executions(self, *, limit: int = 50, symbol: str | None = None, days: int = 6) -> dict[str, Any]:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=max(int(days), 1))
        payload = self.client.executions(
            category=self.config.strategy.category,
            symbol=symbol,
            start_time_ms=int(start.timestamp() * 1000),
            end_time_ms=int(end.timestamp() * 1000),
            limit=limit,
        )
        return {"rows": compact_list_result(payload), "raw": payload}


def compact_wallet(payload: Mapping[str, Any], *, coin: str) -> list[dict[str, Any]]:
    result = payload.get("result") if isinstance(payload, Mapping) else {}
    accounts = result.get("list", []) if isinstance(result, Mapping) else []
    out = []
    for account in accounts:
        coins = account.get("coin", []) if isinstance(account, Mapping) else []
        for item in coins:
            if str(item.get("coin")) == coin:
                out.append(
                    {
                        "coin": item.get("coin"),
                        "equity": item.get("equity"),
                        "walletBalance": item.get("walletBalance"),
                        "usdValue": item.get("usdValue"),
                        "availableToWithdraw": item.get("availableToWithdraw"),
                        "availableToBorrow": item.get("availableToBorrow"),
                    }
                )
    return out


def _safe_section(fn: Any) -> dict[str, Any]:
    try:
        payload = fn()
    except Exception as exc:
        return {"ok": False, "error_type": type(exc).__name__, "error": str(exc), "rows": []}
    if isinstance(payload, dict):
        payload.setdefault("ok", True)
        return payload
    return {"ok": True, "raw": payload}


def compact_positions(payload: Mapping[str, Any], *, fallback_symbol: str = "") -> list[dict[str, Any]]:
    out = []
    for row in compact_list_result(payload):
        size = row.get("size") or "0"
        out.append(
            {
                "symbol": row.get("symbol") or fallback_symbol,
                "side": row.get("side") or "",
                "size": size,
                "avgPrice": row.get("avgPrice") or "0",
                "markPrice": row.get("markPrice") or "",
                "unrealisedPnl": row.get("unrealisedPnl") or row.get("unrealizedPnl") or "",
                "liqPrice": row.get("liqPrice") or "",
                "leverage": row.get("leverage") or "",
            }
        )
    if not out and fallback_symbol:
        out.append({"symbol": fallback_symbol, "side": "", "size": "0", "avgPrice": "0", "markPrice": "", "unrealisedPnl": "", "liqPrice": "", "leverage": ""})
    return out


def compact_list_result(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    result = payload.get("result") if isinstance(payload, Mapping) else {}
    rows = result.get("list", []) if isinstance(result, Mapping) else []
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
