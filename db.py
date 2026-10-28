"""SQLite storage for the voice server.

One deliberate simplification: database calls are synchronous. SQLite on a
local file with WAL and a 5 s busy timeout is sub-millisecond for the handful
of rows this app moves, so running it on the event loop is cheaper than a
thread hop. Password hashing is the one expensive operation and that is
offloaded to a worker thread in auth.py.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT    NOT NULL,
    role          TEXT    NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user')),
    disabled      INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    last_login_at INTEGER
);

CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT    NOT NULL UNIQUE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    scope      TEXT    NOT NULL CHECK (scope IN ('app', 'admin')),
    csrf       TEXT    NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    ip         TEXT,
    user_agent TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS invites (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT    NOT NULL UNIQUE,
    token_hint TEXT    NOT NULL,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    used_at    INTEGER,
    used_by    INTEGER REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_invites_expiry ON invites(expires_at);

CREATE TABLE IF NOT EXISTS rooms (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    max_participants INTEGER NOT NULL DEFAULT 6,
    password_hash    TEXT,
    access_mode      TEXT    NOT NULL DEFAULT 'all' CHECK (access_mode IN ('all', 'selected')),
    closed           INTEGER NOT NULL DEFAULT 0,
    created_at       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS room_access (
    room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (room_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_room_access_user ON room_access(user_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL,
    actor_id   INTEGER,
    actor_name TEXT,
    action     TEXT    NOT NULL,
    target     TEXT,
    detail     TEXT,
    ip         TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);

CREATE TABLE IF NOT EXISTS login_attempts (
    id  INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT    NOT NULL,
    ts  INTEGER NOT NULL,
    ok  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_key ON login_attempts(key, ts);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now() -> int:
    return int(time.time())


class Database:
    """Thread-local connections against one WAL-mode SQLite file."""

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(path)
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._local = threading.local()

    # -- connection management -------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=15)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return conn

    def init(self) -> None:
        conn = self.connect()
        conn.executescript(SCHEMA)
        conn.commit()

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @contextlib.contextmanager
    def transaction(self):
        """BEGIN IMMEDIATE ... COMMIT. Use conn.execute() inside, not self.execute."""
        conn = self.connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    # -- small helpers ---------------------------------------------------------

    def rows(self, sql: str, params=()) -> list:
        return [dict(r) for r in self.connect().execute(sql, params).fetchall()]

    def row(self, sql: str, params=()):
        found = self.connect().execute(sql, params).fetchone()
        return dict(found) if found is not None else None

    def value(self, sql: str, params=()):
        found = self.connect().execute(sql, params).fetchone()
        return found[0] if found is not None else None

    def execute(self, sql: str, params=()) -> int:
        conn = self.connect()
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid if cur.lastrowid is not None else cur.rowcount

    def execute_rowcount(self, sql: str, params=()) -> int:
        conn = self.connect()
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount

    # -- audit -----------------------------------------------------------------

    def audit(self, action: str, actor_name=None, actor_id=None, target=None, detail=None, ip=None) -> None:
        try:
            self.execute(
                "INSERT INTO audit_log (ts, actor_id, actor_name, action, target, detail, ip)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (now(), actor_id, actor_name, action, _clip(target, 120), _clip(detail, 400), _clip(ip, 64)),
            )
        except Exception:  # auditing must never break the request it describes
            pass

    def audit_prune(self, days: int) -> int:
        cutoff = now() - max(1, int(days)) * 86400
        return self.execute_rowcount("DELETE FROM audit_log WHERE ts < ?", (cutoff,))


def _clip(value, limit: int):
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


EDITABLE_SETTINGS = (
    "max_users",
    "max_rooms",
    "max_participants_per_room",
    "session_lifetime_hours",
    "admin_session_lifetime_hours",
    "audit_retention_days",
    "login_max_attempts",
    "login_lockout_seconds",
    "chat_max_length",
    "ice_servers",
    "allowed_origins",
)


class Settings:
    """Config file values, optionally overridden from the DB settings table."""

    def __init__(self, db: Database, defaults: dict) -> None:
        self.db = db
        self._values = dict(defaults)
        try:
            rows = db.rows("SELECT key, value FROM settings")
        except Exception:
            rows = []
        for row in rows:
            key = row["key"]
            if key not in self._values:
                continue
            try:
                self._values[key] = json.loads(row["value"])
            except (TypeError, ValueError):
                continue

    def get(self, key: str, default=None):
        return self._values.get(key, default)

    def as_dict(self, keys=EDITABLE_SETTINGS) -> dict:
        return {k: self._values.get(k) for k in keys if k in self._values}

    def update(self, changes: dict) -> None:
        for key, value in changes.items():
            if key not in EDITABLE_SETTINGS:
                continue
            self._values[key] = value
            self.db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )

    def reset_cache(self) -> None:
        # Kept for symmetry; a restart is the supported way to drop DB overrides.
        pass
