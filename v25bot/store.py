"""SQLite persistence (WAL). Single connection guarded by a lock."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id      TEXT PRIMARY KEY,
    received_at   TEXT NOT NULL,
    kind          TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    status        TEXT NOT NULL,
    result_json   TEXT NOT NULL,
    source_ip     TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id            TEXT PRIMARY KEY,
    event_id      TEXT,
    trading_day   TEXT,
    symbol        TEXT NOT NULL,
    role          TEXT NOT NULL,
    data_json     TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_day ON orders(trading_day, role);
CREATE TABLE IF NOT EXISTS fills (
    id            TEXT PRIMARY KEY,
    order_id      TEXT NOT NULL,
    trading_day   TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    role          TEXT NOT NULL,
    data_json     TEXT NOT NULL,
    ts            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fills_day ON fills(trading_day);
CREATE TABLE IF NOT EXISTS audit (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    level         TEXT NOT NULL,
    kind          TEXT NOT NULL,
    message       TEXT NOT NULL,
    data_json     TEXT
);
CREATE TABLE IF NOT EXISTS diagnostics (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at   TEXT NOT NULL,
    bot_id        TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    bar_time      INTEGER NOT NULL,
    seq           INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    dir           INTEGER,
    tf            INTEGER,
    lo            REAL,
    hi            REAL,
    t             INTEGER,
    msg           TEXT NOT NULL DEFAULT '',
    UNIQUE (bot_id, bar_time, seq)
);
CREATE INDEX IF NOT EXISTS diagnostics_bar ON diagnostics(bar_time);
CREATE TABLE IF NOT EXISTS kv (
    key           TEXT PRIMARY KEY,
    value_json    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ events
    def get_event(self, event_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()

    def claim_event(self, event_id: str, kind: str, sha: str, payload: dict, source_ip: str | None) -> bool:
        """Atomically reserve an event id. False if it already exists (replay/duplicate)."""
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
                    (event_id, _now(), kind, sha, json.dumps(payload), "processing", "{}", source_ip),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def finish_event(self, event_id: str, status: str, result: dict) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE events SET status=?, result_json=? WHERE event_id=?",
                (status, json.dumps(result, default=str), event_id),
            )

    def recent_events(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_id, received_at, kind, status, result_json FROM events "
                "ORDER BY received_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) | {"result": json.loads(r["result_json"])} for r in rows]

    # ------------------------------------------------------------------ orders
    def upsert_order(self, order: dict, trading_day: str, event_id: str | None = None) -> None:
        with self._lock:
            existing = self._conn.execute("SELECT event_id, trading_day FROM orders WHERE id=?", (order["id"],)).fetchone()
            if existing:
                self._conn.execute(
                    "UPDATE orders SET data_json=?, updated_at=? WHERE id=?",
                    (json.dumps(order), _now(), order["id"]),
                )
            else:
                self._conn.execute(
                    "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)",
                    (order["id"], event_id, trading_day, order["symbol"], order["role"],
                     json.dumps(order), _now(), _now()),
                )

    def orders(self, trading_day: str | None = None, role: str | None = None) -> list[dict]:
        sql, args = "SELECT event_id, trading_day, data_json FROM orders WHERE 1=1", []
        if trading_day:
            sql += " AND trading_day=?"
            args.append(trading_day)
        if role:
            sql += " AND role=?"
            args.append(role)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY created_at", args).fetchall()
        return [json.loads(r["data_json"]) | {"event_id": r["event_id"], "trading_day": r["trading_day"]} for r in rows]

    # ------------------------------------------------------------------ fills
    def add_fill(self, fill: dict, trading_day: str) -> bool:
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO fills VALUES (?,?,?,?,?,?,?)",
                    (fill["id"], fill["order_id"], trading_day, fill["symbol"], fill["role"],
                     json.dumps(fill), fill["ts"]),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def fills(self, trading_day: str | None = None) -> list[dict]:
        with self._lock:
            if trading_day:
                rows = self._conn.execute(
                    "SELECT data_json FROM fills WHERE trading_day=? ORDER BY ts, id", (trading_day,)
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT data_json FROM fills ORDER BY ts, id").fetchall()
        return [json.loads(r["data_json"]) for r in rows]

    # ------------------------------------------------------------------ audit
    def audit(self, level: str, kind: str, message: str, data: dict | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit (ts, level, kind, message, data_json) VALUES (?,?,?,?,?)",
                (_now(), level, kind, message, json.dumps(data, default=str) if data else None),
            )

    def recent_audit(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, level, kind, message, data_json FROM audit ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ diagnostics
    DIAG_FIELDS = ("kind", "dir", "tf", "lo", "hi", "t", "msg")

    def add_diagnostics(self, bot_id: str, strategy_version: str, bar_time: int,
                        rows: list[tuple[int, dict]]) -> int:
        """Insert (seq, row) pairs; a (bot_id, bar_time, seq) already stored is skipped. Returns rows added."""
        added = 0
        with self._lock:
            for seq, row in rows:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO diagnostics (received_at, bot_id, strategy_version, bar_time, seq, "
                    "kind, dir, tf, lo, hi, t, msg) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (_now(), bot_id, strategy_version, bar_time, seq,
                     *(row.get(f) for f in self.DIAG_FIELDS[:-1]), row.get("msg") or ""),
                )
                added += cur.rowcount
        return added

    def recent_diagnostics(self, limit: int = 200, kind: str | None = None,
                           since_ms: int | None = None) -> list[dict]:
        sql, args = "SELECT * FROM diagnostics WHERE 1=1", []
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if since_ms:
            sql += " AND bar_time>=?"
            args.append(since_ms)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY bar_time DESC, seq DESC LIMIT ?", (*args, limit)).fetchall()
        return [dict(r) for r in rows]

    def diagnostic_counts(self, since_ms: int | None = None) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, COUNT(*) AS n FROM diagnostics WHERE bar_time>=? GROUP BY kind", (since_ms or 0,)
            ).fetchall()
        return {r["kind"]: r["n"] for r in rows}

    def prune_diagnostics(self, before_ms: int) -> int:
        with self._lock:
            return self._conn.execute("DELETE FROM diagnostics WHERE bar_time<?", (before_ms,)).rowcount

    # ------------------------------------------------------------------ kv
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value_json FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value_json"]) if row else default

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
                "updated_at=excluded.updated_at",
                (key, json.dumps(value, default=str), _now()),
            )
