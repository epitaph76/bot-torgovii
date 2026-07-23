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
        active_before = active

        flat, flat_payload = self._all_flat(dry_run=dry_run, active=active)
        if active and flat and not dry_run:
            self.state.clear_active_episode()
            active = None
            flat_payload["active_episode_cleared"] = "exchange_flat"
        rebalance = self._rebalance_if_needed(bundle=bundle, dry_run=dry_run, flat=flat)

        orders: list[dict[str, Any]] = []
        action = "rebalance_only" if rebalance_only else "none"
        reason = decision.reason
        if rebalance_only:
            reason = "rebalance_only"
        else:
            can_hold, hold_reason = active_can_hold(active=active, decision=decision, config=self.config)
            if active and can_hold:
                stop_orders = self._refresh_stops_for_active(
                    decision_id=decision_id,
                    bundle=bundle,
                    active=active,
                    selected=decision.selected or {},
                    dry_run=dry_run,
                )
                orders.extend(stop_orders)
                action = "hold"
                reason = hold_reason
                updated = dict(active)
                updated["as_of"] = decision.as_of
                updated["target_timestamp"] = decision.target_timestamp
                updated["last_decision"] = decision.selected
                updated["last_stop_refresh"] = stop_orders
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

        spread_snapshots = _spread_snapshot_rows(bundle, decision, self.config)
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
            "market_snapshot": _market_snapshot(bundle, self.config),
            "active_before": active_before,
            "flat": flat_payload,
            "rebalance": rebalance,
            "spread_snapshots": spread_snapshots,
            "orders": orders,
        }
        self.state.save_decision(decision_id, decision.as_of, payload)
        self.state.save_spread_snapshots(spread_snapshots)
        if not _payload_blocks_reconciliation(payload):
            self.state.clear_reconciliation()
        return payload

    def run_forever(self, *, dry_run: bool = False) -> None:
        while True:
            target_dt = next_decision_datetime(delay_seconds=int(self.config.decision_delay_seconds))
            self._sleep_until_decision(target_dt=target_dt, dry_run=dry_run)
            try:
                result = self.run_once(dry_run=dry_run)
                print(json.dumps(_decision_log_payload(result), ensure_ascii=False, default=str), flush=True)
            except Exception as exc:
                self.state.set_reconciliation("blocked", f"runtime_error:{type(exc).__name__}", {"error": str(exc)})
                print(json.dumps({"event": "runtime_error", "status": "blocked", "error": str(exc), "now_utc": _utcnow_iso()}, ensure_ascii=False), flush=True)
                time.sleep(max(int(self.config.loop_sleep_seconds), 1))

    def _sleep_until_decision(self, *, target_dt: datetime, dry_run: bool) -> None:
        heartbeat = max(int(self.config.heartbeat_seconds), 1)
        while True:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            remaining = (target_dt - now).total_seconds()
            if remaining <= 0:
                return
            active = self.state.load_active_episode()
            print(
                json.dumps(
                    {
                        "event": "heartbeat",
                        "environment": self.config.environment,
                        "dry_run": bool(dry_run),
                        "now_utc": now.isoformat(timespec="seconds"),
                        "next_decision_utc": target_dt.isoformat(timespec="seconds"),
                        "seconds_until_next_decision": int(max(remaining, 0)),
                        "active_symbol": active.get("symbol") if active else None,
                        "active_side": active.get("side") if active else None,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            time.sleep(min(remaining, heartbeat))

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
                    "max_notional_usdt": float(sleeve.max_notional_usdt),
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

    def _refresh_stops_for_active(
        self,
        *,
        decision_id: str,
        bundle: MarketBundle,
        active: Mapping[str, Any],
        selected: Mapping[str, Any],
        dry_run: bool,
    ) -> list[dict[str, Any]]:
        symbol = str(active.get("symbol"))
        final_side = str(selected.get("final_side") or active.get("side"))
        events: list[dict[str, Any]] = []
        for sleeve in self._trade_sleeves():
            client = self.sleeve_clients[sleeve.sleeve_id]
            if dry_run:
                sleeve_payload = dict((active.get("sleeves") or {}).get(sleeve.sleeve_id) or {})
                qty = Decimal(str(sleeve_payload.get("qty", "0")))
                avg_price = Decimal(str(sleeve_payload.get("entry_price") or active.get("entry_price") or _entry_price(bundle, selected)))
                pos_side = "Buy" if final_side == "long" else "Sell"
            else:
                qty, pos_side, avg_price = _position(client.positions(category=self.config.strategy.category, symbol=symbol))
            if qty <= 0:
                self.state.set_reconciliation("blocked", "hold_position_missing", {"sleeve_id": sleeve.sleeve_id, "symbol": symbol})
                raise BybitDemoError(f"hold position missing for {sleeve.sleeve_id}:{symbol}")
            expected_pos_side = "Buy" if final_side == "long" else "Sell"
            if pos_side and pos_side != expected_pos_side:
                self.state.set_reconciliation(
                    "blocked",
                    "hold_position_side_mismatch",
                    {"sleeve_id": sleeve.sleeve_id, "symbol": symbol, "expected": expected_pos_side, "actual": pos_side},
                )
                raise BybitDemoError(f"hold position side mismatch for {sleeve.sleeve_id}:{symbol}: expected {expected_pos_side}, got {pos_side}")
            entry = avg_price if avg_price > 0 else Decimal(str(active.get("entry_price") or _entry_price(bundle, selected)))
            sl = stop_price(side=final_side, entry_price=entry, stop_loss_pct=float(self.config.strategy.stop_loss_pct), spec=bundle.specs[symbol])
            response = client.set_trading_stop(
                category=self.config.strategy.category,
                symbol=symbol,
                stop_loss=sl,
                trigger_by=self.config.strategy.sl_trigger_by,
                environment=self.config.environment,
                dry_run=dry_run,
            )
            event = {
                "sleeve_id": sleeve.sleeve_id,
                "action": "refresh_stop",
                "symbol": symbol,
                "final_side": final_side,
                "qty": str(qty),
                "avg_price": str(avg_price),
                "stop_loss": str(sl),
                "response": response,
            }
            self.state.save_order_event(decision_id, sleeve.sleeve_id, "refresh_stop", event)
            events.append(event)
        return events

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
                max_notional = Decimal(str(sleeve.max_notional_usdt)) if float(sleeve.max_notional_usdt) > 0 else None
                qty = order_qty(equity=equity, leverage=float(leverage), price=entry, spec=spec, max_notional_usdt=max_notional)
                if qty <= 0:
                    event = {
                        "sleeve_id": sleeve.sleeve_id,
                        "action": "skip_open",
                        "reason": "qty_below_minimum",
                        "equity_usdt": str(equity),
                        "max_notional_usdt": str(max_notional) if max_notional is not None else None,
                    }
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
                    "max_notional_usdt": str(max_notional) if max_notional is not None else None,
                    "order_notional_usdt": str(quantize_down(qty * entry, Decimal("0.01"))),
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
    target_dt = next_decision_datetime(delay_seconds=delay_seconds)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return max(target_dt.timestamp() - now.timestamp(), 0.0)


def next_decision_datetime(*, delay_seconds: int) -> datetime:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    target_dt = current_hour + timedelta(seconds=int(delay_seconds))
    if now > target_dt:
        target_dt = current_hour + timedelta(hours=1, seconds=int(delay_seconds))
    return target_dt


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


def _market_snapshot(bundle: MarketBundle, config: BybitDemoBotConfig) -> dict[str, Any]:
    symbols: dict[str, Any] = {}
    for symbol in config.strategy.symbols:
        ticker = bundle.tickers[symbol]
        spread_pct = _ticker_spread_pct(ticker)
        spec = bundle.specs[symbol]
        symbols[symbol] = {
            "last_completed_candle": _last_completed_candle(bundle, symbol),
            "forming_open": str(bundle.forming_open[symbol]),
            "ticker": {
                "last_price": str(ticker.last_price),
                "bid1_price": str(ticker.bid1_price),
                "ask1_price": str(ticker.ask1_price),
                "bid_ask_spread_pct": spread_pct,
            },
            "instrument": {
                "tick_size": str(spec.tick_size),
                "qty_step": str(spec.qty_step),
                "min_order_qty": str(spec.min_order_qty),
                "min_notional": str(spec.min_notional),
                "max_market_qty": str(spec.max_market_qty),
                "max_leverage": str(spec.max_leverage),
            },
        }
    return {
        "decision_as_of": bundle.decision_as_of.isoformat(),
        "target_timestamp": bundle.target_timestamp.isoformat(),
        "symbols": symbols,
    }


def _last_completed_candle(bundle: MarketBundle, symbol: str) -> dict[str, Any] | None:
    frame = bundle.candles.get(symbol)
    if frame is None or frame.empty:
        return None
    row = frame.iloc[-1]
    out: dict[str, Any] = {}
    for key in ("timestamps", "end", "open", "high", "low", "close", "volume", "amount"):
        value = row.get(key)
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        elif isinstance(value, float) and not math.isfinite(value):
            value = None
        out[key] = value
    return out


def _ticker_spread_pct(ticker: Any) -> str | None:
    if ticker.bid1_price > 0 and ticker.ask1_price > 0:
        mid = (ticker.bid1_price + ticker.ask1_price) / Decimal("2")
        if mid > 0:
            return str((ticker.ask1_price - ticker.bid1_price) / mid)
    return None


def _spread_snapshot_rows(bundle: MarketBundle, decision: Any, config: BybitDemoBotConfig) -> list[dict[str, Any]]:
    ranked_by_symbol = {}
    for row in getattr(decision, "ranked_rows", []) or []:
        if isinstance(row, Mapping):
            ranked_by_symbol[str(row.get("secid"))] = row
    sampled_at = _utcnow_iso()
    rows = []
    for symbol in config.strategy.symbols:
        ticker = bundle.tickers[symbol]
        ranked = ranked_by_symbol.get(symbol, {})
        rows.append(
            {
                "decision_as_of": bundle.decision_as_of.isoformat(),
                "target_timestamp": bundle.target_timestamp.isoformat(),
                "sampled_at_utc": sampled_at,
                "symbol": symbol,
                "last_price": str(ticker.last_price),
                "bid1_price": str(ticker.bid1_price),
                "ask1_price": str(ticker.ask1_price),
                "bid_ask_spread_pct": _ticker_spread_pct(ticker),
                "estimated_spread_pct": ranked.get("estimated_spread_pct"),
                "execution_cost_bps": ranked.get("execution_cost_bps"),
                "liquidity_penalty": ranked.get("liquidity_penalty"),
            }
        )
    return rows


def _decision_log_payload(result: Mapping[str, Any]) -> dict[str, Any]:
    decision = result.get("decision", {})
    ranked_rows = decision.get("ranked_rows", []) if isinstance(decision, Mapping) else []
    compact_rank = []
    if isinstance(ranked_rows, list):
        for row in ranked_rows:
            if not isinstance(row, Mapping):
                continue
            compact_rank.append(
                {
                    "rank": row.get("rank"),
                    "symbol": row.get("secid"),
                    "side": row.get("final_side"),
                    "entry_selected": row.get("entry_selected"),
                    "net_pred_move_bps": row.get("net_pred_move_bps"),
                    "vol_ratio_168": row.get("vol_ratio_168"),
                    "execution_cost_bps": row.get("execution_cost_bps"),
                    "estimated_spread_pct": row.get("estimated_spread_pct"),
                    "slippage_one_way_pct": row.get("slippage_one_way_pct"),
                    "hl_relevant_wick_ratio_168": row.get("hl_relevant_wick_ratio_168"),
                    "hl_range_ratio_168": row.get("hl_range_ratio_168"),
                    "flip_applied": row.get("flip_applied"),
                    "actual_open": row.get("actual_open"),
                    "pred_close": row.get("pred_close"),
                    "pred_high": row.get("pred_high"),
                    "pred_low": row.get("pred_low"),
                }
            )
    return {
        "event": "decision",
        "now_utc": _utcnow_iso(),
        "decision_id": result.get("decision_id"),
        "dry_run": result.get("dry_run"),
        "action": result.get("action"),
        "reason": result.get("reason"),
        "as_of": result.get("as_of"),
        "target_timestamp": result.get("target_timestamp"),
        "ranked": compact_rank,
        "selected": decision.get("selected") if isinstance(decision, Mapping) else None,
        "market_snapshot": result.get("market_snapshot"),
        "flat": result.get("flat"),
        "rebalance": result.get("rebalance"),
        "spread_snapshots": result.get("spread_snapshots"),
        "orders": result.get("orders"),
    }


def _payload_blocks_reconciliation(payload: Mapping[str, Any]) -> bool:
    rebalance = payload.get("rebalance")
    return isinstance(rebalance, Mapping) and rebalance.get("status") == "blocked"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


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
