"""SQLite storage: print tokens, an event log the measurements come from, one heartbeat row per
minute for availability, and a key/value table for poll positions.

One connection is shared by the event loop and the worker threads, so every call takes a lock.
Each statement is tiny (one row), which keeps the time the loop spends here in microseconds.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens(
    tag TEXT PRIMARY KEY,
    created REAL NOT NULL,
    expires REAL NOT NULL,
    used REAL,
    minted_by TEXT
);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    channel TEXT NOT NULL,
    kind TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT,
    sender TEXT,
    filename TEXT,
    pages INTEGER,
    bytes INTEGER,
    sent_ts REAL,
    received_ts REAL,
    picked_ts REAL,
    spooled_ts REAL,
    done_ts REAL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS heartbeats(minute INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

EVENT_FIELDS = ("ts", "channel", "kind", "outcome", "reason", "sender", "filename", "pages", "bytes",
                "sent_ts", "received_ts", "picked_ts", "spooled_ts", "done_ts", "detail")


class Store:
    def __init__(self, path: Path | str):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def write(self, sql: str, args: tuple = ()) -> tuple[int, int]:
        """Run a statement and return (rowcount, lastrowid)."""
        with self._lock:
            cur = self._db.execute(sql, args)
            return cur.rowcount, cur.lastrowid

    def query(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def scalar(self, sql: str, args: tuple = ()):
        rows = self.query(sql, args)
        return rows[0][0] if rows else None

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        value = self.scalar("SELECT value FROM kv WHERE key = ?", (key,))
        return default if value is None else value

    def kv_set(self, key: str, value) -> None:
        self.write("INSERT INTO kv(key, value) VALUES(?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))

    def log_event(self, **fields) -> int:
        fields.setdefault("ts", time.time())
        unknown = set(fields) - set(EVENT_FIELDS)
        if unknown:
            raise ValueError(f"unknown event fields: {sorted(unknown)}")
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        _, rowid = self.write(f"INSERT INTO events({cols}) VALUES({marks})", tuple(fields.values()))
        return rowid

    def update_event(self, event_id: int, **fields) -> None:
        unknown = set(fields) - set(EVENT_FIELDS)
        if unknown:
            raise ValueError(f"unknown event fields: {sorted(unknown)}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.write(f"UPDATE events SET {sets} WHERE id = ?", (*fields.values(), event_id))

    def heartbeat(self, now: float | None = None) -> None:
        minute = int((now or time.time()) // 60)
        self.write("INSERT OR IGNORE INTO heartbeats(minute) VALUES(?)", (minute,))

    def close(self) -> None:
        with self._lock:
            self._db.close()
