"""Password hashing, server-side sessions, CSRF tokens, login throttling.

Only stdlib is used so manage.py runs before aiohttp is installed.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import random
import re
import secrets
import time

# scrypt with 8 MiB of memory takes roughly 0.3-0.6 s on a Pi Zero W: slow
# enough to matter for an attacker, fast enough that login feels normal.
SCRYPT_LOG2_N = 13
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32

SESSION_COOKIE_APP = "voice_sid"
SESSION_COOKIE_ADMIN = "voice_admin_sid"
CSRF_HEADER = "X-CSRF-Token"

USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$")
ROOM_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,48}$")

MAX_PASSWORD_LEN = 200


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=1 << SCRYPT_LOG2_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=64 * 1024 * 1024,
    )
    return "scrypt${}${}${}${}${}".format(
        SCRYPT_LOG2_N,
        SCRYPT_R,
        SCRYPT_P,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(derived).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, log2_n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=1 << int(log2_n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
            maxmem=64 * 1024 * 1024,
        )
        return hmac.compare_digest(derived, expected)
    except (ValueError, TypeError):
        return False


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def username_error(username: str):
    if not isinstance(username, str) or not USERNAME_RE.match(username):
        return "Username must be 3-32 characters: letters, digits, dot, dash or underscore, starting with a letter or digit."
    return None


def password_error(password: str):
    if not isinstance(password, str):
        return "Password is required."
    if len(password) < 8:
        return "Password must be at least 8 characters."
    if len(password) > MAX_PASSWORD_LEN:
        return "Password must be at most 200 characters."
    if password.strip() == "":
        return "Password cannot be only whitespace."
    return None


def room_name_error(name: str):
    if not isinstance(name, str) or not ROOM_NAME_RE.match(name.strip()):
        return "Room name must be 1-48 printable characters."
    return None


class Auth:
    def __init__(self, db, config: dict, settings) -> None:
        self.db = db
        self.config = config
        self.settings = settings
        self._last_prune = 0.0

    # -- password helpers ------------------------------------------------------

    async def hash_async(self, password: str) -> str:
        return await asyncio.to_thread(hash_password, password)

    async def verify_async(self, password: str, stored: str) -> bool:
        return await asyncio.to_thread(verify_password, password, stored)

    # -- login throttling ------------------------------------------------------

    def lock_remaining(self, keys) -> int:
        """Seconds until the longest active lockout expires, 0 if not locked."""
        now = int(time.time())
        max_attempts = int(self.settings.get("login_max_attempts", 5))
        window = int(self.settings.get("login_lockout_seconds", 900))
        lockout = int(self.settings.get("login_lockout_seconds", 900))
        oldest_relevant = now - max(window, lockout)
        remaining = 0
        for key in keys:
            failures = self.db.rows(
                "SELECT ts FROM login_attempts WHERE key = ? AND ok = 0 AND ts >= ? ORDER BY ts DESC",
                (key, oldest_relevant),
            )
            if len(failures) >= max_attempts:
                until = int(failures[0]["ts"]) + lockout
                remaining = max(remaining, until - now)
        return max(0, remaining)

    def record_attempt(self, key: str, ok: bool) -> None:
        self.db.execute(
            "INSERT INTO login_attempts (key, ts, ok) VALUES (?, ?, ?)",
            (key, int(time.time()), 1 if ok else 0),
        )
        self._maybe_prune_attempts()

    def clear_attempts(self, keys) -> None:
        for key in keys:
            self.db.execute("DELETE FROM login_attempts WHERE key = ?", (key,))

    def _maybe_prune_attempts(self) -> None:
        now = time.monotonic()
        if now - self._last_prune < 600 and random.random() > 0.02:
            return
        self._last_prune = now
        cutoff = int(time.time()) - 7 * 86400
        self.db.execute_rowcount("DELETE FROM login_attempts WHERE ts < ?", (cutoff,))

    # -- sessions --------------------------------------------------------------

    def _lifetime_hours(self, scope: str) -> float:
        if scope == "admin":
            return float(self.settings.get("admin_session_lifetime_hours", 12))
        return float(self.settings.get("session_lifetime_hours", 72))

    def create_session(self, user_id: int, scope: str, ip=None, user_agent=None):
        token = new_token(32)
        csrf = new_token(24)
        created = int(time.time())
        expires = created + int(self._lifetime_hours(scope) * 3600)
        self.db.execute(
            "INSERT INTO sessions (token_hash, user_id, scope, csrf, created_at, expires_at, ip, user_agent)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (token_hash(token), user_id, scope, csrf, created, expires, ip, (user_agent or "")[:200]),
        )
        return {"token": token, "csrf": csrf, "expires_at": expires}

    def get_session(self, token, scope: str):
        if not token:
            return None
        found = self.db.row(
            "SELECT s.id AS session_id, s.csrf, s.created_at, s.expires_at, s.user_id, s.scope,"
            "       u.username, u.role, u.disabled"
            "  FROM sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.token_hash = ? AND s.scope = ?",
            (token_hash(token), scope),
        )
        if not found:
            return None
        if int(found["expires_at"]) <= int(time.time()):
            self.db.execute("DELETE FROM sessions WHERE id = ?", (found["session_id"],))
            return None
        if int(found["disabled"] or 0):
            self.db.execute("DELETE FROM sessions WHERE user_id = ?", (found["user_id"],))
            return None
        return found

    def destroy_session(self, token) -> None:
        if token:
            self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))

    def destroy_user_sessions(self, user_id: int, scope=None, except_token=None) -> None:
        if scope:
            if except_token:
                self.db.execute(
                    "DELETE FROM sessions WHERE user_id = ? AND scope = ? AND token_hash != ?",
                    (user_id, scope, token_hash(except_token)),
                )
            else:
                self.db.execute("DELETE FROM sessions WHERE user_id = ? AND scope = ?", (user_id, scope))
        else:
            if except_token:
                self.db.execute(
                    "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
                    (user_id, token_hash(except_token)),
                )
            else:
                self.db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def csrf_ok(self, session, provided) -> bool:
        if not session or not provided:
            return False
        return hmac.compare_digest(str(session.get("csrf", "")), str(provided))

    def cleanup(self) -> None:
        now_ts = int(time.time())
        self.db.execute_rowcount("DELETE FROM sessions WHERE expires_at <= ?", (now_ts,))
        self.db.execute_rowcount("DELETE FROM login_attempts WHERE ts < ?", (now_ts - 7 * 86400,))
        self.db.execute_rowcount(
            "DELETE FROM invites WHERE used_at IS NOT NULL AND used_at < ?",
            (now_ts - 30 * 86400,),
        )
