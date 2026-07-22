from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


class BotStateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def save_decision(self, decision_id: str, as_of: str, payload: Mapping[str, Any]) -> None:
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    "insert or replace into decisions(decision_id, as_of, payload_json, created_at) values (?, ?, ?, ?)",
                    (decision_id, as_of, _json(payload), _now()),
                )

    def load_active_episode(self) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("select payload_json from active_episode where id = 1").fetchone()
        return json.loads(row[0]) if row else None

    def set_active_episode(self, payload: Mapping[str, Any]) -> None:
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    "insert or replace into active_episode(id, symbol, side, as_of, target_timestamp, payload_json, updated_at) values (1, ?, ?, ?, ?, ?, ?)",
                    (
                        payload.get("symbol"),
                        payload.get("side"),
                        payload.get("as_of"),
                        payload.get("target_timestamp"),
                        _json(payload),
                        _now(),
                    ),
                )

    def clear_active_episode(self) -> None:
        with closing(self._connect()) as conn:
            with conn:
                conn.execute("delete from active_episode where id = 1")

    def save_order_event(self, decision_id: str, sleeve_id: str, action: str, payload: Mapping[str, Any]) -> None:
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    "insert into order_events(decision_id, sleeve_id, action, payload_json, created_at) values (?, ?, ?, ?, ?)",
                    (decision_id, sleeve_id, action, _json(payload), _now()),
                )

    def save_sleeve_snapshot(self, payload: Mapping[str, Any]) -> None:
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    "insert into sleeve_snapshots(as_of, payload_json, created_at) values (?, ?, ?)",
                    (payload.get("as_of"), _json(payload), _now()),
                )

    def set_reconciliation(self, status: str, reason: str, payload: Mapping[str, Any] | None = None) -> None:
        with closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    "insert or replace into reconciliation(id, status, reason, payload_json, updated_at) values (1, ?, ?, ?, ?)",
                    (status, reason, _json(payload or {}), _now()),
                )

    def reconciliation(self) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("select status, reason, payload_json, updated_at from reconciliation where id = 1").fetchone()
        if not row:
            return None
        return {"status": row[0], "reason": row[1], "payload": json.loads(row[2] or "{}"), "updated_at": row[3]}

    def status(self) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            active = conn.execute("select payload_json from active_episode where id = 1").fetchone()
            decision = conn.execute("select decision_id, as_of, payload_json from decisions order by created_at desc limit 1").fetchone()
            recon = conn.execute("select status, reason, payload_json, updated_at from reconciliation where id = 1").fetchone()
        return {
            "active_episode": json.loads(active[0]) if active else None,
            "latest_decision": {
                "decision_id": decision[0],
                "as_of": decision[1],
                "payload": json.loads(decision[2]),
            }
            if decision
            else None,
            "reconciliation": {
                "status": recon[0],
                "reason": recon[1],
                "payload": json.loads(recon[2] or "{}"),
                "updated_at": recon[3],
            }
            if recon
            else None,
        }

    def _init(self) -> None:
        with closing(self._connect()) as conn:
            with conn:
                conn.executescript(
                    """
                create table if not exists decisions(
                    decision_id text primary key,
                    as_of text not null,
                    payload_json text not null,
                    created_at text not null
                );
                create table if not exists active_episode(
                    id integer primary key check(id = 1),
                    symbol text not null,
                    side text not null,
                    as_of text not null,
                    target_timestamp text not null,
                    payload_json text not null,
                    updated_at text not null
                );
                create table if not exists order_events(
                    id integer primary key autoincrement,
                    decision_id text not null,
                    sleeve_id text not null,
                    action text not null,
                    payload_json text not null,
                    created_at text not null
                );
                create table if not exists sleeve_snapshots(
                    id integer primary key autoincrement,
                    as_of text,
                    payload_json text not null,
                    created_at text not null
                );
                create table if not exists reconciliation(
                    id integer primary key check(id = 1),
                    status text not null,
                    reason text not null,
                    payload_json text not null,
                    updated_at text not null
                );
                """
                )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)


def _json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
