from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping

from .client import DEMO_BASE_URL, BybitCredentials, BybitDemoError, BybitV5Client, quantize_down
from .config import BybitDemoBotConfig, SleeveConfig
from .market import BybitMarketData, MarketBundle
from .state import BotStateStore
from .strategy import (
    KronosLiveForecaster,
    active_can_hold,
    build_strategy_decision,
    order_qty,
    stop_price,
)


class BybitDemoRuntime:
    def __init__(
        self,
        *,
        config: BybitDemoBotConfig,
        market: BybitMarketData | None = None,
        forecaster: Any | None = None,
        state: BotStateStore | None = None,
        master_client: BybitV5Client | None = None,
        sleeve_clients: Mapping[str, BybitV5Client] | None = None,
    ):
        config.validate()
        self.config = config
        public_client = BybitV5Client(
            base_url=config.public_base_url,
            recv_window=config.recv_window,
            timeout=config.request_timeout_seconds,
            retries=config.request_retries,
        )
        self.market = market or BybitMarketData(client=public_client, config=config)
        self.forecaster = forecaster or KronosLiveForecaster(config=config)
        self.state = state or BotStateStore(config.state_path)
        self.master_client = master_client or BybitV5Client(
            credentials=config.master_credentials(),
            base_url=config.base_url,
            recv_window=config.recv_window,
            timeout=config.request_timeout_seconds,
            retries=config.request_retries,
        )
        self.sleeve_clients = dict(sleeve_clients or self._build_sleeve_clients())

    def run_once(self, *, dry_run: bool = False, rebalance_only: bool = False) -> dict[str, Any]:
        self._assert_safe(dry_run=dry_run)
        bundle = self.market.load_bundle()
        forecasts = self.forecaster.forecast(bundle)
        decision = build_strategy_decision(bundle=bundle, forecasts=forecasts, config=self.config)
        decision_id = _decision_id(decision.as_of, decision.selected, rebalance_only=rebalance_only)
        active = self.state.load_active_episode()

        flat, flat_payload = self._all_flat(dry_run=dry_run, active=active)
        rebalance = self._rebalance_if_needed(bundle=bundle, dry_run=dry_run, flat=flat)

        orders: list[dict[str, Any]] = []
        action = "rebalance_only" if rebalance_only else "none"
        reason = decision.reason
        if rebalance_only:
            reason = "rebalance_only"
        else:
            can_hold, hold_reason = active_can_hold(active=active, decision=decision, config=self.config)
            if active and can_hold:
                action = "hold"
                reason = hold_reason
                updated = dict(active)
                updated["as_of"] = decision.as_of
                updated["target_timestamp"] = decision.target_timestamp
                updated["last_decision"] = decision.selected
                self.state.set_active_episode(updated)
            else:
                if active:
                    close_orders = self._close_all_sleeves(
                        decision_id=decision_id,
                        bundle=bundle,
                        active=active,
                        dry_run=dry_run,
                        reason=hold_reason,
                    )
                    orders.extend(close_orders)
                    self.state.clear_active_episode()
                    action = "close"
                if decision.selected is not None:
                    open_orders = self._open_selected(
                        decision_id=decision_id,
                        bundle=bundle,
                        selected=decision.selected,
                        dry_run=dry_run,
                    )
                    orders.extend(open_orders)
                    self.state.set_active_episode(
                        {
                            "symbol": decision.selected["secid"],
                            "side": decision.selected["final_side"],
                            "as_of": decision.as_of,
                            "target_timestamp": decision.target_timestamp,
                            "entry_decision_id": decision_id,
                            "entry_price": _entry_price(bundle, decision.selected),
                            "selected": decision.selected,
                            "sleeves": _active_sleeves_from_orders(open_orders),
                        }
                    )
                    action = "open" if action != "close" else "close_then_open"
                elif action == "none":
                    action = "cash"

        payload = {
            "decision_id": decision_id,
            "dry_run": bool(dry_run),
            "rebalance_only": bool(rebalance_only),
            "environment": self.config.environment,
            "base_url": self.config.base_url,
            "as_of": decision.as_of,
            "target_timestamp": decision.target_timestamp,
            "action": action,
            "reason": reason,
            "decision": asdict(decision),
            "active_before": active,
            "flat": flat_payload,
            "rebalance": rebalance,
            "orders": orders,
        }
        self.state.save_decision(decision_id, decision.as_of, payload)
        return payload

    def run_forever(self, *, dry_run: bool = False) -> None:
        while True:
            wait = seconds_until_next_decision(delay_seconds=int(self.config.decision_delay_seconds))
            time.sleep(wait)
            try:
                result = self.run_once(dry_run=dry_run)
                print(json.dumps({"decision_id": result["decision_id"], "action": result["action"], "reason": result["reason"]}, ensure_ascii=False), flush=True)
            except Exception as exc:
                self.state.set_reconciliation("blocked", f"runtime_error:{type(exc).__name__}", {"error": str(exc)})
                print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False), flush=True)
                time.sleep(max(int(self.config.loop_sleep_seconds), 1))

    def status(self) -> dict[str, Any]:
        return self.state.status()

    def preflight(self) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        payload: dict[str, Any] = {
            "environment": self.config.environment,
            "base_url": self.config.base_url,
            "public_base_url": self.config.public_base_url,
            "symbols": list(self.config.strategy.symbols),
            "checks": checks,
        }
        try:
            self._assert_safe(dry_run=True)
            checks.append({"name": "config_safety", "ok": True})
        except Exception as exc:
            checks.append({"name": "config_safety", "ok": False, "error": str(exc)})

        for symbol in self.config.strategy.symbols:
            try:
                spec = self.market.instrument_spec(symbol)
                ticker = self.market.ticker(symbol)
                checks.append(
                    {
                        "name": f"public_market:{symbol}",
                        "ok": True,
                        "tick_size": str(spec.tick_size),
                        "qty_step": str(spec.qty_step),
                        "min_order_qty": str(spec.min_order_qty),
                        "max_leverage": str(spec.max_leverage),
                        "last_price": str(ticker.last_price),
                    }
                )
            except Exception as exc:
                checks.append({"name": f"public_market:{symbol}", "ok": False, "error": str(exc)})

        account_checks = [("master", self.master_client, None)]
        account_checks.extend((sleeve.sleeve_id, self.sleeve_clients[sleeve.sleeve_id], sleeve) for sleeve in self._trade_sleeves())
        for name, client, sleeve in account_checks:
            if not client.credentials.present:
                checks.append({"name": f"private_account:{name}", "ok": False, "error": "missing API key/secret env vars"})
                continue
            try:
                wallet = client.wallet_balance(account_type=(sleeve.account_type if sleeve is not None else self.config.master_account.account_type), coin=self.config.split.coin)
                checks.append({"name": f"private_wallet:{name}", "ok": True, "wallet": _wallet_compact(wallet, coin=self.config.split.coin)})
            except Exception as exc:
                checks.append({"name": f"private_wallet:{name}", "ok": False, "error": str(exc)})
            for symbol in self.config.strategy.symbols:
                try:
                    qty, side, avg_price = _position(client.positions(category=self.config.strategy.category, symbol=symbol))
                    checks.append({"name": f"private_position:{name}:{symbol}", "ok": True, "qty": str(qty), "side": side, "avg_price": str(avg_price)})
                except Exception as exc:
                    checks.append({"name": f"private_position:{name}:{symbol}", "ok": False, "error": str(exc)})

        payload["ok"] = all(bool(row.get("ok")) for row in checks)
        return payload

    def _assert_safe(self, *, dry_run: bool) -> None:
        if self.config.environment != "bybit_demo":
            raise BybitDemoError("environment must be bybit_demo")
        if self.config.base_url.rstrip("/") != DEMO_BASE_URL:
            raise BybitDemoError(f"base_url must be {DEMO_BASE_URL}")
        if self.config.public_base_url.rstrip("/") != DEMO_BASE_URL:
            raise BybitDemoError(f"public_base_url must be {DEMO_BASE_URL}")
        if not dry_run:
            for client in [self.master_client, *self.sleeve_clients.values()]:
                client.ensure_can_submit(environment=self.config.environment, dry_run=False)

    def _build_sleeve_clients(self) -> dict[str, BybitV5Client]:
        out: dict[str, BybitV5Client] = {}
        for sleeve in self.config.split.sleeves:
            if sleeve.is_cash:
                continue
            out[sleeve.sleeve_id] = BybitV5Client(
                credentials=self.config.sleeve_credentials(sleeve),
                base_url=self.config.base_url,
                recv_window=self.config.recv_window,
                timeout=self.config.request_timeout_seconds,
                retries=self.config.request_retries,
            )
        return out

    def _all_flat(self, *, dry_run: bool, active: Mapping[str, Any] | None) -> tuple[bool, dict[str, Any]]:
        positions = {}
        if dry_run:
            flat = active is None
            return flat, {"mode": "dry_run_state", "flat": flat}
        for sleeve in self._trade_sleeves():
            client = self.sleeve_clients[sleeve.sleeve_id]
            symbol_positions = {}
            for symbol in self.config.strategy.symbols:
                qty, side, avg_price = _position(client.positions(category=self.config.strategy.category, symbol=symbol))
                symbol_positions[symbol] = {"qty": str(qty), "side": side, "avg_price": str(avg_price)}
            positions[sleeve.sleeve_id] = symbol_positions
        flat = all(Decimal(row["qty"]) == 0 for sleeve_rows in positions.values() for row in sleeve_rows.values())
        return flat, {"mode": "exchange", "flat": flat, "positions": positions}

    def _rebalance_if_needed(self, *, bundle: MarketBundle, dry_run: bool, flat: bool) -> dict[str, Any]:
        snapshots = self._sleeve_equity_snapshot(dry_run=dry_run)
        self.state.save_sleeve_snapshot({"as_of": bundle.decision_as_of.isoformat(), "dry_run": dry_run, "sleeves": snapshots})
        total = sum(Decimal(row["equity_usdt"]) for row in snapshots)
        if total <= 0:
            return {"status": "skipped", "reason": "zero_total_equity", "sleeves": snapshots}
        for row in snapshots:
            target = Decimal(str(row["target_weight"]))
            actual_weight = Decimal(row["equity_usdt"]) / total
            row["actual_weight"] = str(actual_weight)
            row["drift_pp"] = float((actual_weight - target) * Decimal("100"))
        max_drift = max(abs(float(row["drift_pp"])) for row in snapshots) if snapshots else 0.0
        if max_drift <= float(self.config.split.rebalance_threshold_pp):
            return {"status": "not_needed", "max_drift_pp": max_drift, "sleeves": snapshots}
        if not flat:
            self.state.set_reconciliation("blocked", "rebalance_needed_but_positions_not_flat", {"max_drift_pp": max_drift, "sleeves": snapshots})
            return {"status": "blocked", "reason": "positions_not_flat", "max_drift_pp": max_drift, "sleeves": snapshots}
        transfers = self._plan_rebalance_transfers(snapshots, total)
        responses = []
        for transfer in transfers:
            response = self.master_client.universal_transfer(
                from_member_id=transfer["from_member_id"],
                to_member_id=transfer["to_member_id"],
                from_account_type=transfer["from_account_type"],
                to_account_type=transfer["to_account_type"],
                coin=self.config.split.coin,
                amount=Decimal(transfer["amount"]),
                environment=self.config.environment,
                dry_run=dry_run,
            )
            responses.append({"transfer": transfer, "response": response})
        return {"status": "submitted" if responses else "not_needed", "max_drift_pp": max_drift, "sleeves": snapshots, "transfers": responses}

    def _sleeve_equity_snapshot(self, *, dry_run: bool) -> list[dict[str, Any]]:
        rows = []
        for sleeve in self.config.split.sleeves:
            if dry_run:
                equity = Decimal(str(sleeve.dry_run_equity_usdt))
            elif sleeve.is_cash:
                equity = self._wallet_equity(self.master_client)
            else:
                equity = self._wallet_equity(self.sleeve_clients[sleeve.sleeve_id])
            rows.append(
                {
                    "sleeve_id": sleeve.sleeve_id,
                    "kind": sleeve.kind,
                    "member_id": sleeve.member_id or (self.config.master_account.member_id if sleeve.is_cash else ""),
                    "account_type": sleeve.account_type,
                    "target_weight": float(sleeve.target_weight),
                    "leverage": float(sleeve.leverage),
                    "equity_usdt": str(equity),
                }
            )
        return rows

    def _plan_rebalance_transfers(self, snapshots: list[dict[str, Any]], total: Decimal) -> list[dict[str, str]]:
        by_id = {row["sleeve_id"]: row for row in snapshots}
        surplus = []
        deficit = []
        for row in snapshots:
            actual = Decimal(row["equity_usdt"])
            target = total * Decimal(str(row["target_weight"]))
            delta = actual - target
            row["target_equity_usdt"] = str(target)
            if abs(delta) < Decimal("0.01"):
                continue
            item = {"sleeve_id": row["sleeve_id"], "amount": abs(delta)}
            if delta > 0:
                surplus.append(item)
            else:
                deficit.append(item)
        transfers: list[dict[str, str]] = []
        for src in surplus:
            for dst in deficit:
                if src["amount"] <= 0 or dst["amount"] <= 0:
                    continue
                amount = min(src["amount"], dst["amount"])
                src_row = by_id[src["sleeve_id"]]
                dst_row = by_id[dst["sleeve_id"]]
                from_member = src_row.get("member_id") or self.config.master_account.member_id
                to_member = dst_row.get("member_id") or self.config.master_account.member_id
                if not from_member or not to_member:
                    raise BybitDemoError("rebalance transfer requires member_id for every sleeve and master account")
                transfers.append(
                    {
                        "from_sleeve_id": str(src["sleeve_id"]),
                        "to_sleeve_id": str(dst["sleeve_id"]),
                        "from_member_id": str(from_member),
                        "to_member_id": str(to_member),
                        "from_account_type": str(src_row.get("account_type") or "UNIFIED"),
                        "to_account_type": str(dst_row.get("account_type") or "UNIFIED"),
                        "amount": str(quantize_down(amount, Decimal("0.01"))),
                    }
                )
                src["amount"] -= amount
                dst["amount"] -= amount
        return [transfer for transfer in transfers if Decimal(transfer["amount"]) > 0]

    def _close_all_sleeves(
        self,
        *,
        decision_id: str,
        bundle: MarketBundle,
        active: Mapping[str, Any],
        dry_run: bool,
        reason: str,
    ) -> list[dict[str, Any]]:
        orders = []
        symbol = str(active.get("symbol"))
        for sleeve in self._trade_sleeves():
            client = self.sleeve_clients[sleeve.sleeve_id]
            if dry_run:
                sleeve_payload = dict((active.get("sleeves") or {}).get(sleeve.sleeve_id) or {})
                qty = Decimal(str(sleeve_payload.get("qty", "0")))
                side = str(active.get("side"))
            else:
                qty, pos_side, _ = _position(client.positions(category=self.config.strategy.category, symbol=symbol))
                side = "long" if pos_side == "Buy" else "short"
            if qty <= 0:
                continue
            close_side = "Sell" if side == "long" else "Buy"
            response = client.create_market_order(
                category=self.config.strategy.category,
                symbol=symbol,
                side=close_side,
                qty=qty,
                reduce_only=True,
                environment=self.config.environment,
                dry_run=dry_run,
            )
            event = {"sleeve_id": sleeve.sleeve_id, "action": "close", "symbol": symbol, "side": close_side, "qty": str(qty), "reason": reason, "response": response}
            self.state.save_order_event(decision_id, sleeve.sleeve_id, "close", event)
            orders.append(event)
        return orders

    def _open_selected(
        self,
        *,
        decision_id: str,
        bundle: MarketBundle,
        selected: Mapping[str, Any],
        dry_run: bool,
    ) -> list[dict[str, Any]]:
        symbol = str(selected["secid"])
        final_side = str(selected["final_side"])
        bybit_side = "Buy" if final_side == "long" else "Sell"
        spec = bundle.specs[symbol]
        entry = Decimal(str(_entry_price(bundle, selected)))
        opened: list[tuple[SleeveConfig, Decimal]] = []
        events: list[dict[str, Any]] = []
        try:
            for sleeve in self._trade_sleeves():
                client = self.sleeve_clients[sleeve.sleeve_id]
                leverage = min(Decimal(str(sleeve.leverage)), spec.max_leverage)
                equity = Decimal(str(sleeve.dry_run_equity_usdt)) if dry_run else self._wallet_equity(client)
                qty = order_qty(equity=equity, leverage=float(leverage), price=entry, spec=spec)
                if qty <= 0:
                    event = {"sleeve_id": sleeve.sleeve_id, "action": "skip_open", "reason": "qty_below_minimum", "equity_usdt": str(equity)}
                    events.append(event)
                    self.state.save_order_event(decision_id, sleeve.sleeve_id, "skip_open", event)
                    continue
                client.set_leverage(category=self.config.strategy.category, symbol=symbol, leverage=float(leverage), environment=self.config.environment, dry_run=dry_run)
                order_response = client.create_market_order(
                    category=self.config.strategy.category,
                    symbol=symbol,
                    side=bybit_side,
                    qty=qty,
                    reduce_only=False,
                    environment=self.config.environment,
                    dry_run=dry_run,
                )
                opened.append((sleeve, qty))
                sl = stop_price(side=final_side, entry_price=entry, stop_loss_pct=float(self.config.strategy.stop_loss_pct), spec=spec)
                stop_response = client.set_trading_stop(
                    category=self.config.strategy.category,
                    symbol=symbol,
                    stop_loss=sl,
                    trigger_by=self.config.strategy.sl_trigger_by,
                    environment=self.config.environment,
                    dry_run=dry_run,
                )
                event = {
                    "sleeve_id": sleeve.sleeve_id,
                    "action": "open",
                    "symbol": symbol,
                    "side": bybit_side,
                    "final_side": final_side,
                    "qty": str(qty),
                    "entry_price": str(entry),
                    "stop_loss": str(sl),
                    "leverage": str(leverage),
                    "equity_usdt": str(equity),
                    "order_response": order_response,
                    "stop_response": stop_response,
                }
                self.state.save_order_event(decision_id, sleeve.sleeve_id, "open", event)
                events.append(event)
        except Exception as exc:
            self.state.set_reconciliation("blocked", "open_failed_rollback_required", {"error": str(exc), "opened": [(s.sleeve_id, str(q)) for s, q in opened]})
            for sleeve, qty in opened:
                try:
                    close_side = "Sell" if final_side == "long" else "Buy"
                    rollback = self.sleeve_clients[sleeve.sleeve_id].create_market_order(
                        category=self.config.strategy.category,
                        symbol=symbol,
                        side=close_side,
                        qty=qty,
                        reduce_only=True,
                        environment=self.config.environment,
                        dry_run=dry_run,
                    )
                    rollback_event = {"sleeve_id": sleeve.sleeve_id, "action": "rollback_close", "symbol": symbol, "qty": str(qty), "response": rollback}
                    self.state.save_order_event(decision_id, sleeve.sleeve_id, "rollback_close", rollback_event)
                    events.append(rollback_event)
                except Exception as rollback_exc:
                    self.state.set_reconciliation("blocked", "rollback_close_failed", {"error": str(rollback_exc), "original_error": str(exc)})
            raise
        return events

    def _trade_sleeves(self) -> list[SleeveConfig]:
        return [sleeve for sleeve in self.config.split.sleeves if not sleeve.is_cash]

    def _wallet_equity(self, client: BybitV5Client) -> Decimal:
        payload = client.wallet_balance(account_type="UNIFIED", coin=self.config.split.coin)
        result = payload.get("result") if isinstance(payload, Mapping) else {}
        accounts = result.get("list", []) if isinstance(result, Mapping) else []
        for account in accounts:
            coins = account.get("coin", []) if isinstance(account, Mapping) else []
            for coin in coins:
                if str(coin.get("coin")) == self.config.split.coin:
                    for key in ("equity", "walletBalance", "usdValue"):
                        value = coin.get(key)
                        if value not in (None, ""):
                            return Decimal(str(value))
        return Decimal("0")


def seconds_until_next_decision(*, delay_seconds: int) -> float:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    target_dt = current_hour + timedelta(seconds=int(delay_seconds))
    if now > target_dt:
        target_dt = current_hour + timedelta(hours=1, seconds=int(delay_seconds))
    target = target_dt.timestamp()
    return max(target - now.timestamp(), 0.0)


def _decision_id(as_of: str, selected: Mapping[str, Any] | None, *, rebalance_only: bool) -> str:
    raw = json.dumps({"as_of": as_of, "selected": selected, "rebalance_only": rebalance_only}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _entry_price(bundle: MarketBundle, selected: Mapping[str, Any]) -> float:
    symbol = str(selected["secid"])
    ticker = bundle.tickers[symbol]
    if str(selected.get("final_side")) == "long":
        return float(ticker.ask1_price if ticker.ask1_price > 0 else bundle.forming_open[symbol])
    return float(ticker.bid1_price if ticker.bid1_price > 0 else bundle.forming_open[symbol])


def _active_sleeves_from_orders(orders: list[dict[str, Any]]) -> dict[str, Any]:
    out = {}
    for order in orders:
        if order.get("action") == "open":
            out[str(order["sleeve_id"])] = {
                "qty": order.get("qty"),
                "entry_price": order.get("entry_price"),
                "stop_loss": order.get("stop_loss"),
                "leverage": order.get("leverage"),
            }
    return out


def _position(payload: Mapping[str, Any]) -> tuple[Decimal, str, Decimal]:
    result = payload.get("result") if isinstance(payload, Mapping) else {}
    rows = result.get("list", []) if isinstance(result, Mapping) else []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        qty = Decimal(str(row.get("size") or "0"))
        side = str(row.get("side") or "")
        avg_price = Decimal(str(row.get("avgPrice") or "0"))
        if qty != 0:
            return qty, side, avg_price
    return Decimal("0"), "", Decimal("0")


def _wallet_compact(payload: Mapping[str, Any], *, coin: str) -> list[dict[str, Any]]:
    result = payload.get("result") if isinstance(payload, Mapping) else {}
    accounts = result.get("list", []) if isinstance(result, Mapping) else []
    out = []
    for account in accounts:
        coins = account.get("coin", []) if isinstance(account, Mapping) else []
        for item in coins:
            if str(item.get("coin")) == coin:
                out.append({key: item.get(key) for key in ("coin", "equity", "walletBalance", "usdValue", "availableToWithdraw")})
    return out
