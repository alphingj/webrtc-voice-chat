"""Admin panel HTTP application and JSON API (listener on the admin port only).

Every route below except the static login page and POST /api/login requires an
admin-scoped session cookie plus, for state changes, the session's CSRF token.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from urllib.parse import urlsplit

from aiohttp import web

from auth import (
    CSRF_HEADER,
    SESSION_COOKIE_ADMIN,
    password_error,
    room_name_error,
    token_hash,
    username_error,
)
from db import EDITABLE_SETTINGS, now
from signaling import (
    ApiError,
    CSP_APP,
    client_ip,
    clear_session_cookie,
    origin_allowed,
    public_user,
    read_json,
    security_middleware,
    set_session_cookie,
)

log = logging.getLogger("voice.admin")

AUDIT_ACTIONS = (
    "login",
    "login_failed",
    "login_locked",
    "logout",
    "register",
    "room_join",
    "room_leave",
    "room_create",
    "room_update",
    "room_delete",
    "room_close",
    "room_open",
    "room_access",
    "kick",
    "force_mute",
    "user_create",
    "user_update",
    "user_delete",
    "password_reset",
    "invite_create",
    "invite_revoke",
    "settings_update",
    "session_revoked",
)


def _session(request: web.Request, csrf: bool = True):
    token = request.cookies.get(SESSION_COOKIE_ADMIN)
    session = request.app["auth"].get_session(token, "admin") if token else None
    if not session or session["role"] != "admin":
        raise ApiError(401, "unauthorized", "Administrator sign-in required.")
    if csrf and request.method not in ("GET", "HEAD", "OPTIONS"):
        if not request.app["auth"].csrf_ok(session, request.headers.get(CSRF_HEADER)):
            raise ApiError(403, "csrf", "Invalid or missing CSRF token.")
    return session, token


def _get_user(db, user_id: int):
    user = db.row("SELECT * FROM users WHERE id = ?", (user_id,))
    if not user:
        raise ApiError(404, "not_found", "User not found.")
    return user


def _get_room(db, room_id: int):
    room = db.row("SELECT * FROM rooms WHERE id = ?", (room_id,))
    if not room:
        raise ApiError(404, "not_found", "Room not found.")
    return room


def _active_admins_excluding(db, user_id: int) -> int:
    return int(db.value("SELECT COUNT(*) FROM users WHERE role = 'admin' AND disabled = 0 AND id != ?", (user_id,)))


def _bool(value, field: str) -> bool:
    if not isinstance(value, bool):
        raise ApiError(400, "bad_field", "{} must be true or false.".format(field))
    return value


def _int_in(value, field: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(400, "bad_field", "{} must be a number.".format(field))
    if value < low or value > high:
        raise ApiError(400, "bad_field", "{} must be between {} and {}.".format(field, low, high))
    return value


def _str_field(value, field: str, low: int, high: int) -> str:
    if not isinstance(value, str):
        raise ApiError(400, "bad_field", "{} must be text.".format(field))
    value = value.strip()
    if len(value) < low or len(value) > high:
        raise ApiError(400, "bad_field", "{} must be {} to {} characters.".format(field, low, high))
    return value


def _validate_ice_servers(value) -> list:
    if not isinstance(value, list) or len(value) > 10:
        raise ApiError(400, "bad_ice", "ice_servers must be a list of at most 10 entries.")
    cleaned = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ApiError(400, "bad_ice", "Each ICE server must be an object.")
        urls = entry.get("urls")
        if isinstance(urls, str):
            urls = [urls]
        if not isinstance(urls, list) or not urls or len(urls) > 10:
            raise ApiError(400, "bad_ice", "Each ICE server needs a urls list.")
        fixed = []
        for url in urls:
            if not isinstance(url, str) or len(url) > 200:
                raise ApiError(400, "bad_ice", "ICE URLs must be strings under 200 characters.")
            if not url.startswith(("stun:", "stuns:", "turn:", "turns:")):
                raise ApiError(400, "bad_ice", "ICE URLs must start with stun:, stuns:, turn: or turns:.")
            fixed.append(url)
        item = {"urls": fixed}
        if entry.get("username") is not None:
            item["username"] = _str_field(entry["username"], "ice username", 0, 200)
        if entry.get("credential") is not None:
            item["credential"] = _str_field(entry["credential"], "ice credential", 0, 300)
        cleaned.append(item)
    return cleaned


def _validate_origins(value) -> list:
    if not isinstance(value, list) or not value or len(value) > 20:
        raise ApiError(400, "bad_origins", "allowed_origins must be a non-empty list of at most 20 origins.")
    cleaned = []
    for origin in value:
        if not isinstance(origin, str) or len(origin) > 200:
            raise ApiError(400, "bad_origins", "Each origin must be a string.")
        parts = urlsplit(origin)
        if parts.scheme not in ("http", "https") or not parts.netloc or parts.path not in ("", "/"):
            raise ApiError(400, "bad_origins", "Use origins like https://100.87.117.55:9123")
        if parts.query or parts.fragment:
            raise ApiError(400, "bad_origins", "Origins must not contain a query or fragment.")
        normalized = "{}://{}".format(parts.scheme, parts.netloc)
        if normalized not in cleaned:
            cleaned.append(normalized)
    return cleaned


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


async def admin_login(request: web.Request):
    if not origin_allowed(request):
        raise ApiError(403, "origin", "Origin not allowed.")
    data = await read_json(request)
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    ip = client_ip(request)
    auth = request.app["auth"]
    db = request.app["db"]

    keys = ["ip:" + ip, "user:" + username.lower()]
    locked = auth.lock_remaining(keys)
    if locked:
        db.audit("login_locked", target=username, detail="admin", ip=ip)
        raise ApiError(429, "locked", "Too many attempts. Try again later.", retry_after=locked)

    user = db.row("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,))
    ok = False
    if user and not user["disabled"] and user["role"] == "admin":
        ok = await auth.verify_async(password, user["password_hash"])
    if not ok:
        auth.record_attempt(keys[0], False)
        auth.record_attempt(keys[1], False)
        db.audit("login_failed", target=username or "(blank)", detail="admin", ip=ip)
        raise ApiError(401, "invalid_credentials", "Invalid username or password.")

    auth.clear_attempts(keys)
    auth.destroy_session(request.cookies.get(SESSION_COOKIE_ADMIN))
    session = auth.create_session(user["id"], "admin", ip, request.headers.get("User-Agent"))
    db.execute("UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?", (now(), now(), user["id"]))
    db.audit("login", actor_name=user["username"], actor_id=user["id"], detail="admin", ip=ip)

    response = web.json_response(
        {"ok": True, "user": public_user(user), "csrf": session["csrf"], "expires_at": session["expires_at"]}
    )
    set_session_cookie(response, "admin", session["token"], session["expires_at"])
    return response


async def admin_logout(request: web.Request):
    session, token = _session(request)
    request.app["auth"].destroy_session(token)
    request.app["db"].audit(
        "logout", actor_name=session["username"], actor_id=session["user_id"], detail="admin", ip=client_ip(request)
    )
    response = web.json_response({"ok": True})
    clear_session_cookie(response, "admin")
    return response


async def admin_me(request: web.Request):
    session, _token = _session(request)
    db = request.app["db"]
    settings = request.app["settings"]
    config = request.app["config"]
    return web.json_response(
        {
            "user": {
                "id": session["user_id"],
                "username": session["username"],
                "role": session["role"],
            },
            "csrf": session["csrf"],
            "settings": settings.as_dict(),
            "server": {
                "bind_ip": config.get("bind_ip"),
                "voice_port": config.get("voice_port"),
                "admin_port": config.get("admin_port"),
                "voice_url": config.get("voice_url"),
                "admin_url": config.get("admin_url"),
                "db_path": config.get("db_path"),
            },
            "stats": {
                "users": int(db.value("SELECT COUNT(*) FROM users") or 0),
                "disabled_users": int(db.value("SELECT COUNT(*) FROM users WHERE disabled = 1") or 0),
                "rooms": int(db.value("SELECT COUNT(*) FROM rooms") or 0),
                "online": len(request.app["manager"].peers),
            },
        }
    )


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


async def list_users(request: web.Request):
    _session(request, csrf=False)
    db = request.app["db"]
    query = request.query.get("q", "").strip()
    if query:
        like = "%" + query.replace("%", "\\%").replace("_", "\\_") + "%"
        users = db.rows(
            "SELECT u.id, u.username, u.role, u.disabled, u.created_at, u.updated_at, u.last_login_at,"
            " (SELECT COUNT(*) FROM sessions s WHERE s.user_id = u.id) AS session_count,"
            " (SELECT COUNT(*) FROM room_access ra WHERE ra.user_id = u.id) AS room_grants"
            " FROM users u WHERE u.username LIKE ? ESCAPE '\\' ORDER BY u.username COLLATE NOCASE",
            (like,),
        )
    else:
        users = db.rows(
            "SELECT u.id, u.username, u.role, u.disabled, u.created_at, u.updated_at, u.last_login_at,"
            " (SELECT COUNT(*) FROM sessions s WHERE s.user_id = u.id) AS session_count,"
            " (SELECT COUNT(*) FROM room_access ra WHERE ra.user_id = u.id) AS room_grants"
            " FROM users u ORDER BY u.username COLLATE NOCASE"
        )
    return web.json_response({"users": users})


async def create_user(request: web.Request):
    session, _token = _session(request)
    data = await read_json(request)
    db = request.app["db"]
    settings = request.app["settings"]
    username = (data.get("username") or "").strip()
    message = username_error(username)
    if message:
        raise ApiError(400, "bad_username", message)
    message = password_error(data.get("password") or "")
    if message:
        raise ApiError(400, "bad_password", message)
    role = data.get("role", "user")
    if role not in ("admin", "user"):
        raise ApiError(400, "bad_role", "Role must be admin or user.")
    if int(db.value("SELECT COUNT(*) FROM users") or 0) >= int(settings.get("max_users", 50)):
        raise ApiError(409, "user_limit", "User limit reached. Raise it in Settings first.")
    if db.row("SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (username,)):
        raise ApiError(409, "username_taken", "That username is already taken.")

    password_hash = await request.app["auth"].hash_async(data["password"])
    try:
        uid = db.execute(
            "INSERT INTO users (username, password_hash, role, disabled, created_at, updated_at)"
            " VALUES (?, ?, ?, 0, ?, ?)",
            (username, password_hash, role, now(), now()),
        )
    except Exception:
        raise ApiError(409, "username_taken", "That username is already taken.")
    db.audit(
        "user_create",
        actor_name=session["username"],
        actor_id=session["user_id"],
        target=username,
        detail="role={}".format(role),
        ip=client_ip(request),
    )
    return web.json_response({"user": public_user(_get_user(db, uid))}, status=201)


async def update_user(request: web.Request):
    session, _token = _session(request)
    db = request.app["db"]
    user = _get_user(db, int(request.match_info["user_id"]))
    data = await read_json(request)
    changes = {}

    if "username" in data:
        username = (data.get("username") or "").strip()
        message = username_error(username)
        if message:
            raise ApiError(400, "bad_username", message)
        if db.row("SELECT 1 FROM users WHERE username = ? COLLATE NOCASE AND id != ?", (username, user["id"])):
            raise ApiError(409, "username_taken", "That username is already taken.")
        changes["username"] = username
    if "role" in data:
        role = data["role"]
        if role not in ("admin", "user"):
            raise ApiError(400, "bad_role", "Role must be admin or user.")
        changes["role"] = role
    if "disabled" in data:
        changes["disabled"] = 1 if _bool(data["disabled"], "disabled") else 0

    demoting = user["role"] == "admin" and (
        changes.get("role", "admin") != "admin" or changes.get("disabled", 0) == 1
    )
    if demoting and _active_admins_excluding(db, user["id"]) == 0:
        raise ApiError(409, "last_admin", "That is the last active administrator.")

    if not changes:
        return web.json_response({"user": public_user(user)})

    sets = ", ".join("{} = ?".format(key) for key in changes)  # keys come from the fixed list above
    params = list(changes.values()) + [now(), user["id"]]
    try:
        db.execute("UPDATE users SET {}, updated_at = ? WHERE id = ?".format(sets), tuple(params))
    except Exception:
        raise ApiError(409, "username_taken", "That username is already taken.")

    if changes.get("disabled") == 1:
        request.app["auth"].destroy_user_sessions(user["id"])
        await request.app["manager"].kick_user(user["id"], reason="account_disabled")
    db.audit(
        "user_update",
        actor_name=session["username"],
        actor_id=session["user_id"],
        target=user["username"],
        detail=", ".join("{}={}".format(k, v) for k, v in changes.items()),
        ip=client_ip(request),
    )
    return web.json_response({"user": public_user(_get_user(db, user["id"]))})


async def delete_user(request: web.Request):
    session, _token = _session(request)
    db = request.app["db"]
    user = _get_user(db, int(request.match_info["user_id"]))
    if user["id"] == session["user_id"]:
        raise ApiError(409, "self_delete", "You cannot delete your own account.")
    if user["role"] == "admin" and user["disabled"] == 0 and _active_admins_excluding(db, user["id"]) == 0:
        raise ApiError(409, "last_admin", "That is the last active administrator.")

    request.app["auth"].destroy_user_sessions(user["id"])
    await request.app["manager"].kick_user(user["id"], reason="account_deleted")
    db.execute("DELETE FROM users WHERE id = ?", (user["id"],))
    db.audit(
        "user_delete",
        actor_name=session["username"],
        actor_id=session["user_id"],
        target=user["username"],
        ip=client_ip(request),
    )
    return web.json_response({"ok": True})


async def reset_user_password(request: web.Request):
    session, token = _session(request)
    db = request.app["db"]
    user = _get_user(db, int(request.match_info["user_id"]))
    data = await read_json(request)
    message = password_error(data.get("password") or "")
    if message:
        raise ApiError(400, "bad_password", message)

    password_hash = await request.app["auth"].hash_async(data["password"])
    db.execute("UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?", (password_hash, now(), user["id"]))
    keep = token if user["id"] == session["user_id"] else None
    request.app["auth"].destroy_user_sessions(user["id"], except_token=keep)
    await request.app["manager"].kick_user(user["id"], reason="password_changed")
    db.audit(
        "password_reset",
        actor_name=session["username"],
        actor_id=session["user_id"],
        target=user["username"],
        ip=client_ip(request),
    )
    return web.json_response({"ok": True})


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------


async def list_invites(request: web.Request):
    _session(request, csrf=False)
    db = request.app["db"]
    rows = db.rows(
        "SELECT i.id, i.token_hint, i.created_at, i.expires_at, i.used_at, u.username AS used_by_name,"
        " c.username AS created_by_name"
        " FROM invites i"
        " LEFT JOIN users u ON u.id = i.used_by"
        " LEFT JOIN users c ON c.id = i.created_by"
        " ORDER BY i.id DESC LIMIT 100"
    )
    current = now()
    for row in rows:
        row["expired"] = row["used_at"] is None and int(row["expires_at"]) <= current
        row["pending"] = row["used_at"] is None and int(row["expires_at"]) > current
    return web.json_response({"invites": rows})


async def create_invite(request: web.Request):
    session, _token = _session(request)
    db = request.app["db"]
    data = await read_json(request)
    hours = _int_in(data.get("expires_hours", 24), "expires_hours", 1, int(request.app["config"].get("invite_max_hours", 720)))
    token = secrets.token_urlsafe(32)
    expires = now() + hours * 3600
    invite_id = db.execute(
        "INSERT INTO invites (token_hash, token_hint, created_by, created_at, expires_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (token_hash(token), token[:6], session["user_id"], now(), expires),
    )
    url = "{}/?invite={}".format(request.app["config"]["voice_url"].rstrip("/"), token)
    db.audit(
        "invite_create",
        actor_name=session["username"],
        actor_id=session["user_id"],
        target=token[:6],
        detail="expires in {}h".format(hours),
        ip=client_ip(request),
    )
    return web.json_response(
        {
            "invite": {
                "id": invite_id,
                "hint": token[:6],
                "created_at": now(),
                "expires_at": expires,
                "used_at": None,
                "pending": True,
            },
            "token": token,
            "url": url,
        },
        status=201,
    )


async def revoke_invite(request: web.Request):
    session, _token = _session(request)
    db = request.app["db"]
    invite_id = int(request.match_info["invite_id"])
    invite = db.row("SELECT * FROM invites WHERE id = ?", (invite_id,))
    if not invite:
        raise ApiError(404, "not_found", "Invite not found.")
    db.execute("DELETE FROM invites WHERE id = ?", (invite_id,))
    db.audit(
        "invite_revoke",
        actor_name=session["username"],
        actor_id=session["user_id"],
        target=invite["token_hint"],
        ip=client_ip(request),
    )
    return web.json_response({"ok": True})


# ---------------------------------------------------------------------------
# Rooms
# ---------------------------------------------------------------------------


def _room_admin_payload(db, manager, room) -> dict:
    access = [
        row["user_id"]
        for row in db.rows("SELECT user_id FROM room_access WHERE room_id = ? ORDER BY user_id", (room["id"],))
    ]
    return {
        "id": room["id"],
        "name": room["name"],
        "max_participants": int(room["max_participants"]),
        "has_password": bool(room["password_hash"]),
        "access_mode": room["access_mode"],
        "closed": bool(room["closed"]),
        "created_at": room["created_at"],
        "occupants": manager.occupancy(room["id"]),
        "access_user_ids": access,
    }


async def list_rooms(request: web.Request):
    _session(request, csrf=False)
    db = request.app["db"]
    manager = request.app["manager"]
    rooms = db.rows("SELECT * FROM rooms ORDER BY name COLLATE NOCASE")
    return web.json_response({"rooms": [_room_admin_payload(db, manager, room) for room in rooms]})


async def create_room(request: web.Request):
    session, _token = _session(request)
    data = await read_json(request)
    db = request.app["db"]
    settings = request.app["settings"]
    name = (data.get("name") or "").strip()
    message = room_name_error(name)
    if message:
        raise ApiError(400, "bad_name", message)
    if db.value("SELECT COUNT(*) FROM rooms") >= int(settings.get("max_rooms", 20)):
        raise ApiError(409, "room_limit", "Room limit reached. Raise it in Settings first.")
    if db.row("SELECT 1 FROM rooms WHERE name = ? COLLATE NOCASE", (name,)):
        raise ApiError(409, "room_exists", "A room with that name already exists.")
    hard_cap = int(settings.get("max_participants_per_room", 6))
    max_participants = _int_in(data.get("max_participants", hard_cap), "max_participants", 2, hard_cap)
    access_mode = data.get("access_mode", "all")
    if access_mode not in ("all", "selected"):
        raise ApiError(400, "bad_access", "access_mode must be all or selected.")

    password_hash = None
    password = data.get("password")
    if password:
        if not isinstance(password, str) or len(password) < 4 or len(password) > 200:
            raise ApiError(400, "bad_password", "Room password must be 4-200 characters.")
        password_hash = await request.app["auth"].hash_async(password)

    room_id = db.execute(
        "INSERT INTO rooms (name, max_participants, password_hash, access_mode, closed, created_at)"
        " VALUES (?, ?, ?, ?, 0, ?)",
        (name, max_participants, password_hash, access_mode, now()),
    )
    if access_mode == "selected":
        _replace_access(db, room_id, data.get("user_ids") or [])
    room = _get_room(db, room_id)
    db.audit(
        "room_create",
        actor_name=session["username"],
        actor_id=session["user_id"],
        target=name,
        detail="max={} access={}".format(max_participants, access_mode),
        ip=client_ip(request),
    )
    return web.json_response({"room": _room_admin_payload(db, request.app["manager"], room)}, status=201)


def _replace_access(db, room_id: int, user_ids) -> None:
    if not isinstance(user_ids, list) or len(user_ids) > 500:
        raise ApiError(400, "bad_users", "user_ids must be a list.")
    clean = []
    for value in user_ids:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ApiError(400, "bad_users", "user_ids must contain numbers.")
        if db.row("SELECT 1 FROM users WHERE id = ?", (value,)):
            clean.append(value)
    with db.transaction() as conn:
        conn.execute("DELETE FROM room_access WHERE room_id = ?", (room_id,))
        for uid in clean:
            conn.execute("INSERT OR IGNORE INTO room_access (room_id, user_id) VALUES (?, ?)", (room_id, uid))


async def update_room(request: web.Request):
    session, _token = _session(request)
    db = request.app["db"]
    settings = request.app["settings"]
    room = _get_room(db, int(request.match_info["room_id"]))
    data = await read_json(request)
    changes = {}

    if "name" in data:
        name = (data.get("name") or "").strip()
        message = room_name_error(name)
        if message:
            raise ApiError(400, "bad_name", message)
        if db.row("SELECT 1 FROM rooms WHERE name = ? COLLATE NOCASE AND id != ?", (name, room["id"])):
            raise ApiError(409, "room_exists", "A room with that name already exists.")
        changes["name"] = name
    if "max_participants" in data:
        hard_cap = int(settings.get("max_participants_per_room", 6))
        value = _int_in(data["max_participants"], "max_participants", 2, hard_cap)
        if value < request.app["manager"].occupancy(room["id"]):
            raise ApiError(409, "room_occupied", "There are more people in the room than that.")
        changes["max_participants"] = value
    if "password" in data:
        password = data.get("password")
        if not isinstance(password, str) or len(password) < 4 or len(password) > 200:
            raise ApiError(400, "bad_password", "Room password must be 4-200 characters.")
        changes["password_hash"] = await request.app["auth"].hash_async(password)
    if data.get("clear_password") is True:
        changes["password_hash"] = None
    if "closed" in data:
        changes["closed"] = 1 if _bool(data["closed"], "closed") else 0
    if "access_mode" in data:
        if data["access_mode"] not in ("all", "selected"):
            raise ApiError(400, "bad_access", "access_mode must be all or selected.")
        changes["access_mode"] = data["access_mode"]
        if data["access_mode"] == "all":
            db.execute("DELETE FROM room_access WHERE room_id = ?", (room["id"],))

    if changes:
        sets = ", ".join("{} = ?".format(key) for key in changes)
        db.execute(
            "UPDATE rooms SET {} WHERE id = ?".format(sets),
            tuple(changes.values()) + (room["id"],),
        )
    if changes.get("closed") == 1:
        await request.app["manager"].close_room(room["id"], reason="room_closed", mark_closed=True)
    db.audit(
        "room_update",
        actor_name=session["username"],
        actor_id=session["user_id"],
        target=room["name"],
        detail=", ".join(sorted(changes.keys())) or "no changes",
        ip=client_ip(request),
    )
    return web.json_response({"room": _room_admin_payload(db, request.app["manager"], _get_room(db, room["id"]))})


async def delete_room(request: web.Request):
    session, _token = _session(request)
    db = request.app["db"]
    room = _get_room(db, int(request.match_info["room_id"]))
    await request.app["manager"].close_room(room["id"], reason="room_deleted", mark_closed=False)
    db.execute("DELETE FROM rooms WHERE id = ?", (room["id"],))
    db.audit(
        "room_delete",
        actor_name=session["username"],
        actor_id=session["user_id"],
        target=room["name"],
        ip=client_ip(request),
    )
    return web.json_response({"ok": True})


async def set_room_access(request: web.Request):
    session, _token = _session(request)
    db = request.app["db"]
    room = _get_room(db, int(request.match_info["room_id"]))
    data = await read_json(request)
    mode = data.get("mode")
    if mode not in ("all", "selected"):
        raise ApiError(400, "bad_access", "mode must be all or selected.")
    if mode == "all":
        db.execute("DELETE FROM room_access WHERE room_id = ?", (room["id"],))
    else:
        _replace_access(db, room["id"], data.get("user_ids") or [])
    db.execute("UPDATE rooms SET access_mode = ? WHERE id = ?", (mode, room["id"]))
    db.audit(
        "room_access",
        actor_name=session["username"],
        actor_id=session["user_id"],
        target=room["name"],
        detail=mode,
        ip=client_ip(request),
    )
    return web.json_response({"room": _room_admin_payload(db, request.app["manager"], _get_room(db, room["id"]))})


async def close_room(request: web.Request):
    session, _token = _session(request)
    db = request.app["db"]
    room = _get_room(db, int(request.match_info["room_id"]))
    removed = await request.app["manager"].close_room(room["id"], reason="room_closed", mark_closed=True)
    db.audit(
        "room_close",
        actor_name=session["username"],
        actor_id=session["user_id"],
        target=room["name"],
        detail="removed {} peer(s)".format(removed),
        ip=client_ip(request),
    )
    return web.json_response({"ok": True, "removed": removed})


async def open_room(request: web.Request):
    session, _token = _session(request)
    db = request.app["db"]
    room = _get_room(db, int(request.match_info["room_id"]))
    db.execute("UPDATE rooms SET closed = 0 WHERE id = ?", (room["id"],))
    db.audit(
        "room_open",
        actor_name=session["username"],
        actor_id=session["user_id"],
        target=room["name"],
        ip=client_ip(request),
    )
    return web.json_response({"ok": True})


# ---------------------------------------------------------------------------
# Live view
# ---------------------------------------------------------------------------


async def live_view(request: web.Request):
    _session(request, csrf=False)
    db = request.app["db"]
    manager = request.app["manager"]
    rooms = db.rows("SELECT id, name, max_participants, closed FROM rooms ORDER BY name COLLATE NOCASE")
    by_room = {}
    for peer in manager.live_snapshot():
        by_room.setdefault(peer["room_id"], []).append(peer)
    payload = []
    for room in rooms:
        members = by_room.get(room["id"], [])
        payload.append(
            {
                "id": room["id"],
                "name": room["name"],
                "max_participants": int(room["max_participants"]),
                "closed": bool(room["closed"]),
                "count": len(members),
                "participants": members,
            }
        )
    return web.json_response({"rooms": payload, "generated_at": now(), "online": len(manager.peers)})


async def kick_peer(request: web.Request):
    session, _token = _session(request)
    peer_id = request.match_info["peer_id"]
    peername = None
    for peer in request.app["manager"].peers.values():
        if peer.id == peer_id:
            peername = peer.username
            break
    kicked = await request.app["manager"].kick_peer(peer_id, reason="kicked_by_admin")
    if not kicked:
        raise ApiError(404, "not_found", "That peer is no longer connected.")
    request.app["db"].audit(
        "kick",
        actor_name=session["username"],
        actor_id=session["user_id"],
        target=peername,
        detail="peer {}".format(peer_id),
        ip=client_ip(request),
    )
    return web.json_response({"ok": True})


async def mute_peer(request: web.Request):
    session, _token = _session(request)
    peer_id = request.match_info["peer_id"]
    peername = None
    for peer in request.app["manager"].peers.values():
        if peer.id == peer_id:
            peername = peer.username
            break
    muted = await request.app["manager"].force_mute(peer_id)
    if not muted:
        raise ApiError(404, "not_found", "That peer is no longer connected.")
    request.app["db"].audit(
        "force_mute",
        actor_name=session["username"],
        actor_id=session["user_id"],
        target=peername,
        detail="peer {}".format(peer_id),
        ip=client_ip(request),
    )
    return web.json_response({"ok": True})


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


async def audit_log(request: web.Request):
    _session(request, csrf=False)
    db = request.app["db"]
    try:
        limit = max(1, min(200, int(request.query.get("limit", "50"))))
        offset = max(0, int(request.query.get("offset", "0")))
    except ValueError:
        raise ApiError(400, "bad_query", "limit and offset must be numbers.")
    action = request.query.get("action", "").strip()
    query = request.query.get("q", "").strip()

    where = []
    params = []
    if action:
        where.append("action = ?")
        params.append(action)
    if query:
        like = "%" + query.replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(actor_name LIKE ? ESCAPE '\\' OR target LIKE ? ESCAPE '\\' OR detail LIKE ? ESCAPE '\\' OR ip LIKE ? ESCAPE '\\')")
        params.extend([like, like, like, like])
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = int(db.value("SELECT COUNT(*) FROM audit_log" + clause, tuple(params)) or 0)
    entries = db.rows(
        "SELECT id, ts, actor_id, actor_name, action, target, detail, ip FROM audit_log"
        + clause
        + " ORDER BY id DESC LIMIT ? OFFSET ?",
        tuple(params) + (limit, offset),
    )
    return web.json_response({"entries": entries, "total": total, "limit": limit, "offset": offset, "actions": list(AUDIT_ACTIONS)})


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


async def get_settings(request: web.Request):
    _session(request, csrf=False)
    return web.json_response({"settings": request.app["settings"].as_dict(), "editable": list(EDITABLE_SETTINGS)})


async def update_settings(request: web.Request):
    session, _token = _session(request)
    data = await read_json(request)
    settings = request.app["settings"]
    changes = {}

    for key in data:
        if key not in EDITABLE_SETTINGS:
            raise ApiError(400, "bad_setting", "Unknown setting: {}".format(key[:40]))

    if "max_users" in data:
        changes["max_users"] = _int_in(data["max_users"], "max_users", 1, 1000)
    if "max_rooms" in data:
        changes["max_rooms"] = _int_in(data["max_rooms"], "max_rooms", 1, 100)
    if "max_participants_per_room" in data:
        changes["max_participants_per_room"] = _int_in(
            data["max_participants_per_room"], "max_participants_per_room", 2, 12
        )
    if "session_lifetime_hours" in data:
        changes["session_lifetime_hours"] = _int_in(data["session_lifetime_hours"], "session_lifetime_hours", 1, 720)
    if "admin_session_lifetime_hours" in data:
        changes["admin_session_lifetime_hours"] = _int_in(
            data["admin_session_lifetime_hours"], "admin_session_lifetime_hours", 1, 72
        )
    if "audit_retention_days" in data:
        changes["audit_retention_days"] = _int_in(data["audit_retention_days"], "audit_retention_days", 1, 3650)
    if "login_max_attempts" in data:
        changes["login_max_attempts"] = _int_in(data["login_max_attempts"], "login_max_attempts", 3, 20)
    if "login_lockout_seconds" in data:
        changes["login_lockout_seconds"] = _int_in(data["login_lockout_seconds"], "login_lockout_seconds", 60, 86400)
    if "chat_max_length" in data:
        changes["chat_max_length"] = _int_in(data["chat_max_length"], "chat_max_length", 50, 2000)
    if "ice_servers" in data:
        changes["ice_servers"] = _validate_ice_servers(data["ice_servers"])
    if "allowed_origins" in data:
        origins = _validate_origins(data["allowed_origins"])
        current_origin = request.headers.get("Origin")
        if current_origin and current_origin.rstrip("/") not in origins:
            raise ApiError(400, "bad_origins", "Keep this panel's own origin in the list, or you will lock yourself out.")
        changes["allowed_origins"] = origins

    if not changes:
        raise ApiError(400, "no_changes", "No recognised settings supplied.")
    settings.update(changes)
    request.app["db"].audit(
        "settings_update",
        actor_name=session["username"],
        actor_id=session["user_id"],
        target="settings",
        detail=", ".join(sorted(changes.keys())),
        ip=client_ip(request),
    )
    return web.json_response({"settings": settings.as_dict()})


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


async def admin_index(request: web.Request):
    path = os.path.join(request.app["static_dir"], "admin", "index.html")
    response = web.FileResponse(path)
    response.headers["Cache-Control"] = "no-store"
    return response


def create_admin_app(config: dict, db, settings, auth, manager) -> web.Application:
    app = web.Application(middlewares=[security_middleware], client_max_size=32768)
    app["config"] = config
    app["db"] = db
    app["settings"] = settings
    app["auth"] = auth
    app["manager"] = manager
    app["static_dir"] = config["static_dir"]
    app["csp"] = CSP_APP

    app.router.add_get("/", admin_index)
    app.router.add_static("/s/", os.path.join(config["static_dir"], "admin"))

    app.router.add_post("/api/login", admin_login)
    app.router.add_post("/api/logout", admin_logout)
    app.router.add_get("/api/me", admin_me)

    app.router.add_get("/api/users", list_users)
    app.router.add_post("/api/users", create_user)
    app.router.add_patch("/api/users/{user_id}", update_user)
    app.router.add_delete("/api/users/{user_id}", delete_user)
    app.router.add_post("/api/users/{user_id}/password", reset_user_password)

    app.router.add_get("/api/invites", list_invites)
    app.router.add_post("/api/invites", create_invite)
    app.router.add_delete("/api/invites/{invite_id}", revoke_invite)

    app.router.add_get("/api/rooms", list_rooms)
    app.router.add_post("/api/rooms", create_room)
    app.router.add_patch("/api/rooms/{room_id}", update_room)
    app.router.add_delete("/api/rooms/{room_id}", delete_room)
    app.router.add_put("/api/rooms/{room_id}/access", set_room_access)
    app.router.add_post("/api/rooms/{room_id}/close", close_room)
    app.router.add_post("/api/rooms/{room_id}/open", open_room)

    app.router.add_get("/api/live", live_view)
    app.router.add_post("/api/live/{peer_id}/kick", kick_peer)
    app.router.add_post("/api/live/{peer_id}/mute", mute_peer)

    app.router.add_get("/api/audit", audit_log)
    app.router.add_get("/api/settings", get_settings)
    app.router.add_put("/api/settings", update_settings)
    return app
