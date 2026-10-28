"""Signaling server (WebSocket) and the voice-facing HTTP application.

The server never touches media. It authenticates a WebSocket from the session
cookie, tracks room membership, and relays offer/answer/candidate messages
between peers. The newcomer always offers to the peers already in the room,
which removes offer glare without a full perfect-negotiation implementation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

from auth import (
    CSRF_HEADER,
    SESSION_COOKIE_APP,
    SESSION_COOKIE_ADMIN,
    password_error,
    token_hash,
    username_error,
)
from db import now

log = logging.getLogger("voice.signaling")

CSP_APP = (
    "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
    "img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; "
    "media-src 'self' blob:; worker-src 'self'; manifest-src 'self'; form-action 'self'"
)

MAX_SDP_BYTES = 24000
MAX_CANDIDATE_BYTES = 2000


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, retry_after=None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


def json_error(status: int, code: str, message: str):
    return web.json_response({"error": code, "message": message}, status=status)


def client_ip(request: web.Request) -> str:
    # Tailscale is a private network; X-Forwarded-For is only honoured when the
    # operator explicitly enables trust_proxy in config.json.
    if request.app["config"].get("trust_proxy"):
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
    return (request.remote or "unknown")[:64]


def origin_of(value: str):
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    return "{}://{}".format(parts.scheme, parts.netloc)


def request_origins(request: web.Request):
    """Origins that this exact listener legitimately serves."""
    host = request.host
    scheme = "https" if request.secure else "http"
    return {"{}://{}".format(scheme, host), "https://{}".format(host)}


def origin_allowed(request: web.Request, require: bool = False) -> bool:
    origin = request.headers.get("Origin")
    if not origin:
        return not require
    normalized = origin_of(origin)
    if normalized is None:
        return False
    allowed = set(request.app["settings"].get("allowed_origins") or [])
    if normalized in allowed or normalized in request_origins(request):
        return True
    log.warning("origin rejected", extra={"event": "origin_reject", "origin": origin, "ip": client_ip(request)})
    return False


@web.middleware
async def security_middleware(request: web.Request, handler):
    try:
        response = await handler(request)
    except ApiError as exc:
        response = json_error(exc.status, exc.code, exc.message)
        if exc.retry_after:
            response.headers["Retry-After"] = str(int(exc.retry_after))
    except web.HTTPException as exc:
        if exc.status >= 400 and request.path.startswith("/api/"):
            response = json_error(exc.status, "http_error", exc.reason or "Request failed")
        else:
            response = exc
    except (ConnectionResetError, asyncio.CancelledError):
        raise
    except Exception:
        log.exception("unhandled request error", extra={"event": "request_error", "path": request.path})
        response = json_error(500, "internal", "Internal server error")

    headers = response.headers
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("Referrer-Policy", "no-referrer")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Permissions-Policy", "camera=(), geolocation=(), microphone=(self)")
    headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    headers.setdefault("Strict-Transport-Security", "max-age=15552000")
    headers.setdefault("Content-Security-Policy", request.app["csp"])
    headers.setdefault("Cache-Control", "no-store")
    return response


async def read_json(request: web.Request) -> dict:
    if request.content_type != "application/json":
        raise ApiError(415, "unsupported_media_type", "Send application/json.")
    if request.content_length and request.content_length > 32768:
        raise ApiError(413, "payload_too_large", "Request body is too large.")
    try:
        data = await request.json()
    except (ValueError, TypeError):
        raise ApiError(400, "invalid_json", "Malformed JSON body.")
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_json", "JSON body must be an object.")
    return data


def set_session_cookie(response, scope: str, token: str, expires_at: int) -> None:
    name = SESSION_COOKIE_ADMIN if scope == "admin" else SESSION_COOKIE_APP
    max_age = max(60, int(expires_at - time.time()))
    response.set_cookie(
        name,
        token,
        max_age=max_age,
        httponly=True,
        secure=True,
        samesite="Lax",
        path="/",
    )


def clear_session_cookie(response, scope: str) -> None:
    name = SESSION_COOKIE_ADMIN if scope == "admin" else SESSION_COOKIE_APP
    response.del_cookie(name, path="/")


def public_user(user) -> dict:
    return {"id": user["id"], "username": user["username"], "role": user["role"]}


def room_payload(room, occupancy: int, user_id: int) -> dict:
    return {
        "id": room["id"],
        "name": room["name"],
        "max_participants": int(room["max_participants"]),
        "occupants": occupancy,
        "has_password": bool(room["password_hash"]),
        "closed": bool(room["closed"]),
        "access_mode": room["access_mode"],
        "joined": occupancy,
        "can_join": (not room["closed"]) and occupancy < int(room["max_participants"]),
    }


class Peer:
    __slots__ = (
        "id",
        "user_id",
        "username",
        "role",
        "ws",
        "room_id",
        "joined_at",
        "last_seen",
        "muted",
        "ip",
        "session_expires",
        "send_lock",
        "window_start",
        "msg_count",
        "rate_warned",
    )

    def __init__(self, ws, user, ip: str):
        self.id = secrets.token_hex(8)
        self.user_id = int(user["user_id"])
        self.username = user["username"]
        self.role = user["role"]
        self.ws = ws
        self.room_id = None
        self.joined_at = int(time.time())
        self.last_seen = time.monotonic()
        self.muted = False
        self.ip = ip
        self.session_expires = int(user["expires_at"])
        self.send_lock = asyncio.Lock()
        self.window_start = time.monotonic()
        self.msg_count = 0
        self.rate_warned = False

    def brief(self) -> dict:
        return {
            "peer_id": self.id,
            "user_id": self.user_id,
            "username": self.username,
            "muted": bool(self.muted),
        }


class Signaling:
    def __init__(self, config, db, auth, settings) -> None:
        self.config = config
        self.db = db
        self.auth = auth
        self.settings = settings
        self.peers = {}
        self.rooms = {}

    # -- socket plumbing -------------------------------------------------------

    async def send(self, peer: Peer, payload: dict) -> None:
        if peer.ws.closed:
            return
        try:
            async with peer.send_lock:
                await peer.ws.send_json(payload, dumps=lambda value: json.dumps(value, separators=(",", ":")))
        except (ConnectionResetError, RuntimeError, ValueError):
            pass

    async def broadcast(self, room_id: int, payload: dict, exclude=None) -> None:
        targets = [p for p in self.rooms.get(room_id, {}).values() if p.id != exclude]
        if not targets:
            return
        await asyncio.gather(*(self.send(p, payload) for p in targets), return_exceptions=True)

    def occupancy(self, room_id: int) -> int:
        return len(self.rooms.get(room_id, {}))

    def _allow_message(self, peer: Peer) -> bool:
        limit = int(self.settings.get("ws_rate_limit_messages", 120))
        window = float(self.config.get("ws_rate_limit_window_seconds", 10))
        current = time.monotonic()
        if current - peer.window_start >= window:
            peer.window_start = current
            peer.msg_count = 0
            peer.rate_warned = False
        peer.msg_count += 1
        if peer.msg_count <= limit:
            return True
        if not peer.rate_warned:
            peer.rate_warned = True
            asyncio.create_task(
                self.send(peer, {"type": "error", "code": "rate_limited", "message": "Slow down."})
            )
        return False

    # -- WebSocket lifecycle ---------------------------------------------------

    async def handle_ws(self, request: web.Request):
        if not origin_allowed(request, require=True):
            return web.Response(status=403, text="Origin not allowed")
        token = request.cookies.get(SESSION_COOKIE_APP) or request.query.get("token")
        session = self.auth.get_session(token, "app")
        if not session:
            return web.Response(status=401, text="Unauthorized")

        ws = web.WebSocketResponse(
            heartbeat=float(self.config.get("ws_heartbeat_seconds", 25)),
            max_msg_size=int(self.config.get("ws_max_message_bytes", 65536)),
        )
        await ws.prepare(request)
        peer = Peer(ws, session, client_ip(request))
        self.peers[peer.id] = peer
        log.info(
            "websocket open",
            extra={"event": "ws_open", "peer": peer.id, "user": peer.username, "ip": peer.ip},
        )
        await self.send(
            peer,
            {
                "type": "hello",
                "peer_id": peer.id,
                "user": {"id": peer.user_id, "username": peer.username, "role": peer.role},
                "ice_servers": self.settings.get("ice_servers") or [],
                "limits": {
                    "max_participants": int(self.settings.get("max_participants_per_room", 6)),
                    "chat_max_length": int(self.settings.get("chat_max_length", 500)),
                },
            },
        )
        try:
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    peer.last_seen = time.monotonic()
                    if not self._allow_message(peer):
                        continue
                    try:
                        data = json.loads(message.data)
                    except ValueError:
                        await self.send(peer, {"type": "error", "code": "invalid_json", "message": "Malformed message."})
                        continue
                    if not isinstance(data, dict):
                        continue
                    try:
                        await self._dispatch(peer, data)
                    except ApiError as exc:
                        await self.send(peer, {"type": "error", "code": exc.code, "message": exc.message})
                elif message.type == WSMsgType.ERROR:
                    log.warning(
                        "websocket error",
                        extra={"event": "ws_error", "peer": peer.id, "detail": str(ws.exception())},
                    )
                    break
        finally:
            if peer.id in self.peers:
                await self.discard(peer, reason="disconnected")
        return ws

    async def _dispatch(self, peer: Peer, data: dict) -> None:
        mtype = data.get("type")
        if not isinstance(mtype, str):
            raise ApiError(400, "missing_type", "Message type is required.")
        if mtype == "ping":
            await self.send(peer, {"type": "pong", "ts": int(time.time())})
        elif mtype == "join":
            await self.join(peer, data)
        elif mtype == "leave":
            await self.leave_room(peer, reason="left")
        elif mtype in ("offer", "answer"):
            await self.relay_sdp(peer, mtype, data)
        elif mtype == "candidate":
            await self.relay_candidate(peer, data)
        elif mtype in ("mute", "mute-state"):
            await self.set_muted(peer, bool(data.get("muted")))
        elif mtype == "chat":
            await self.chat(peer, data)
        else:
            raise ApiError(400, "unknown_type", "Unknown message type: {}".format(mtype[:32]))

    # -- room membership -------------------------------------------------------

    def can_access(self, room, user_id: int, role: str) -> bool:
        if role == "admin":
            return True
        if room["access_mode"] == "all":
            return True
        found = self.db.row(
            "SELECT 1 AS ok FROM room_access WHERE room_id = ? AND user_id = ?",
            (room["id"], user_id),
        )
        return found is not None

    async def join(self, peer: Peer, data: dict) -> None:
        if peer.room_id is not None:
            await self.leave_room(peer, reason="switch")
        try:
            room_id = int(data.get("room_id"))
        except (TypeError, ValueError):
            raise ApiError(400, "bad_room", "room_id must be a number.")

        room = self.db.row("SELECT * FROM rooms WHERE id = ?", (room_id,))
        if not room:
            raise ApiError(404, "not_found", "That room does not exist.")
        if room["closed"]:
            raise ApiError(409, "room_closed", "That room is closed.")
        if not self.can_access(room, peer.user_id, peer.role):
            raise ApiError(403, "forbidden", "You do not have access to that room.")

        if room["password_hash"]:
            supplied = data.get("password")
            if not isinstance(supplied, str) or not supplied:
                raise ApiError(403, "bad_password", "This room needs a password.")
            if not await self.auth.verify_async(supplied, room["password_hash"]):
                raise ApiError(403, "bad_password", "Wrong room password.")

        hard_cap = int(self.settings.get("max_participants_per_room", 6))
        capacity = min(int(room["max_participants"]), hard_cap)
        if self.occupancy(room_id) >= capacity:
            raise ApiError(409, "room_full", "That room is full.")

        existing = list(self.rooms.get(room_id, {}).values())
        peer.muted = bool(data.get("muted", False))
        peer.joined_at = int(time.time())
        peer.room_id = room_id
        self.rooms.setdefault(room_id, {})[peer.id] = peer

        await self.send(
            peer,
            {
                "type": "joined",
                "you": peer.id,
                "room": {
                    "id": room["id"],
                    "name": room["name"],
                    "max_participants": capacity,
                },
                "peers": [p.brief() for p in existing],
                "ice_servers": self.settings.get("ice_servers") or [],
                "limits": {
                    "max_participants": hard_cap,
                    "chat_max_length": int(self.settings.get("chat_max_length", 500)),
                },
            },
        )
        await self.broadcast(room_id, {"type": "peer-joined", "peer": peer.brief()}, exclude=peer.id)
        self.db.audit(
            "room_join",
            actor_name=peer.username,
            actor_id=peer.user_id,
            target=room["name"],
            detail="peer {}".format(peer.id),
            ip=peer.ip,
        )

    async def leave_room(self, peer: Peer, reason: str = "left") -> None:
        room_id = peer.room_id
        if room_id is None:
            return
        members = self.rooms.get(room_id, {})
        members.pop(peer.id, None)
        if not members:
            self.rooms.pop(room_id, None)
        peer.room_id = None
        await self.broadcast(
            room_id,
            {"type": "peer-left", "peer_id": peer.id, "username": peer.username, "reason": reason},
        )

    async def discard(self, peer: Peer, reason: str = "disconnected") -> None:
        was_in_room = peer.room_id is not None
        room_id = peer.room_id
        await self.leave_room(peer, reason=reason)
        self.peers.pop(peer.id, None)
        if was_in_room:
            room_name = self.db.value("SELECT name FROM rooms WHERE id = ?", (room_id,)) if room_id else None
            self.db.audit(
                "room_leave",
                actor_name=peer.username,
                actor_id=peer.user_id,
                target=room_name,
                detail=reason,
                ip=peer.ip,
            )
        log.info(
            "websocket closed",
            extra={"event": "ws_close", "peer": peer.id, "user": peer.username, "detail": reason},
        )

    # -- relays ----------------------------------------------------------------

    async def _target(self, peer: Peer, data: dict) -> Peer:
        target_id = data.get("to")
        if not isinstance(target_id, str):
            raise ApiError(400, "bad_target", "Missing peer id.")
        target = self.peers.get(target_id)
        if target is None or target.room_id is None or target.room_id != peer.room_id:
            raise ApiError(404, "peer_gone", "That peer is no longer in the room.")
        return target

    async def relay_sdp(self, peer: Peer, mtype: str, data: dict) -> None:
        if peer.room_id is None:
            raise ApiError(409, "not_in_room", "Join a room first.")
        sdp = data.get("sdp")
        if not isinstance(sdp, dict):
            raise ApiError(400, "bad_sdp", "sdp must be an object.")
        if sdp.get("type") not in ("offer", "answer"):
            raise ApiError(400, "bad_sdp", "sdp.type must be offer or answer.")
        body = sdp.get("sdp")
        if not isinstance(body, str) or len(body) > MAX_SDP_BYTES or not body.startswith("v="):
            raise ApiError(400, "bad_sdp", "Malformed SDP.")
        target = await self._target(peer, data)
        await self.send(target, {"type": mtype, "from": peer.id, "sdp": {"type": sdp["type"], "sdp": body}})

    async def relay_candidate(self, peer: Peer, data: dict) -> None:
        if peer.room_id is None:
            raise ApiError(409, "not_in_room", "Join a room first.")
        candidate = data.get("candidate")
        if candidate is None:
            return
        if not isinstance(candidate, dict):
            raise ApiError(400, "bad_candidate", "candidate must be an object.")
        raw = candidate.get("candidate")
        if raw is None:
            return
        if not isinstance(raw, str) or len(raw) > MAX_CANDIDATE_BYTES:
            raise ApiError(400, "bad_candidate", "Malformed ICE candidate.")
        target = await self._target(peer, data)
        await self.send(target, {"type": "candidate", "from": peer.id, "candidate": candidate})

    async def set_muted(self, peer: Peer, muted: bool) -> None:
        peer.muted = muted
        if peer.room_id is not None:
            await self.broadcast(
                peer.room_id,
                {"type": "mute-state", "peer_id": peer.id, "muted": muted},
                exclude=peer.id,
            )

    async def chat(self, peer: Peer, data: dict) -> None:
        if peer.room_id is None:
            raise ApiError(409, "not_in_room", "Join a room first.")
        text = data.get("text")
        max_len = int(self.settings.get("chat_max_length", 500))
        if not isinstance(text, str):
            raise ApiError(400, "bad_chat", "text is required.")
        text = text.strip()
        if not text or len(text) > max_len:
            raise ApiError(400, "bad_chat", "Message must be 1-{} characters.".format(max_len))
        await self.broadcast(
            peer.room_id,
            {
                "type": "chat",
                "from": peer.id,
                "username": peer.username,
                "text": text,
                "ts": int(time.time()),
            },
        )

    # -- admin controls --------------------------------------------------------

    async def kick_peer(self, peer_id: str, reason: str = "kicked_by_admin") -> bool:
        peer = self.peers.get(peer_id)
        if peer is None:
            return False
        await self.send(
            peer,
            {
                "type": "kicked",
                "reason": reason,
                "message": "An administrator removed you from the room.",
            },
        )
        # Remove from the room before closing so the reason broadcast to peers is
        # correct; the handler's finally block then finds nothing left to discard.
        await self.discard(peer, reason=reason)
        try:
            await peer.ws.close(code=4001, message=b"kicked")
        except (ConnectionResetError, RuntimeError):
            pass
        return True

    async def kick_user(self, user_id: int, reason: str = "account_changed") -> int:
        targets = [p.id for p in list(self.peers.values()) if p.user_id == int(user_id)]
        count = 0
        for peer_id in targets:
            if await self.kick_peer(peer_id, reason=reason):
                count += 1
        return count

    async def force_mute(self, peer_id: str) -> bool:
        peer = self.peers.get(peer_id)
        if peer is None:
            return False
        peer.muted = True
        await self.send(peer, {"type": "force-mute", "by": "admin"})
        if peer.room_id is not None:
            await self.broadcast(
                peer.room_id,
                {"type": "mute-state", "peer_id": peer.id, "muted": True},
            )
        return True

    async def close_room(self, room_id: int, reason: str = "room_closed", mark_closed: bool = True) -> int:
        if mark_closed:
            self.db.execute("UPDATE rooms SET closed = 1 WHERE id = ?", (room_id,))
        members = list(self.rooms.pop(room_id, {}).values())
        for peer in members:
            await self.send(
                peer,
                {
                    "type": "room-closed",
                    "reason": reason,
                    "message": "An administrator closed this room.",
                },
            )
            try:
                await peer.ws.close(code=4002, message=b"room closed")
            except (ConnectionResetError, RuntimeError):
                pass
            peer.room_id = None
            self.peers.pop(peer.id, None)
        return len(members)

    def live_snapshot(self) -> list:
        current = int(time.time())
        snapshot = []
        for peer in self.peers.values():
            if peer.room_id is None:
                continue
            snapshot.append(
                {
                    "peer_id": peer.id,
                    "user_id": peer.user_id,
                    "username": peer.username,
                    "room_id": peer.room_id,
                    "joined_at": peer.joined_at,
                    "duration": max(0, current - peer.joined_at),
                    "muted": bool(peer.muted),
                    "ip": peer.ip,
                }
            )
        snapshot.sort(key=lambda item: (item["room_id"], item["joined_at"]))
        return snapshot

    async def shutdown(self) -> None:
        for peer in list(self.peers.values()):
            try:
                await peer.ws.close(code=1012, message=b"server restart")
            except (ConnectionResetError, RuntimeError):
                pass
        self.peers.clear()
        self.rooms.clear()

    async def cleanup_stale(self) -> None:
        threshold = float(self.config.get("ws_heartbeat_seconds", 25)) * 4
        current = time.monotonic()
        for peer in list(self.peers.values()):
            if current - peer.last_seen > threshold:
                log.info("closing stale peer", extra={"event": "ws_stale", "peer": peer.id})
                await self.discard(peer, reason="timeout")
                try:
                    await peer.ws.close(code=1001, message=b"timeout")
                except (ConnectionResetError, RuntimeError):
                    pass


# ---------------------------------------------------------------------------
# Voice app HTTP handlers
# ---------------------------------------------------------------------------


async def index(request: web.Request):
    path = os.path.join(request.app["static_dir"], "app", "index.html")
    response = web.FileResponse(path)
    response.headers["Cache-Control"] = "no-store"
    return response


async def service_worker(request: web.Request):
    path = os.path.join(request.app["static_dir"], "app", "sw.js")
    response = web.FileResponse(path, headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})
    return response


async def manifest(request: web.Request):
    path = os.path.join(request.app["static_dir"], "app", "manifest.json")
    response = web.FileResponse(path, headers={"Cache-Control": "no-cache"})
    return response


async def server_cert(request: web.Request):
    """The public certificate only; useful for installing it on Android."""
    response = web.FileResponse(
        request.app["config"]["cert_file"],
        headers={
            "Content-Type": "application/x-x509-ca-cert",
            "Content-Disposition": 'attachment; filename="voice-server.crt"',
            "Cache-Control": "no-cache",
        },
    )
    return response


async def health(request: web.Request):
    return web.json_response({"ok": True, "ts": int(time.time()), "service": "voice"})


def _require_session(request: web.Request):
    token = request.cookies.get(SESSION_COOKIE_APP)
    session = request.app["auth"].get_session(token, "app") if token else None
    if not session:
        raise ApiError(401, "unauthorized", "Sign in first.")
    return session, token


def _require_csrf(request: web.Request, session) -> None:
    if not request.app["auth"].csrf_ok(session, request.headers.get(CSRF_HEADER)):
        raise ApiError(403, "csrf", "Invalid or missing CSRF token.")


async def api_login(request: web.Request):
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
        db.audit("login_locked", target=username, ip=ip)
        raise ApiError(429, "locked", "Too many attempts. Try again later.", retry_after=locked)

    user = db.row("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,))
    ok = False
    if user and not user["disabled"]:
        ok = await auth.verify_async(password, user["password_hash"])
    if not ok:
        auth.record_attempt(keys[0], False)
        auth.record_attempt(keys[1], False)
        db.audit("login_failed", target=username or "(blank)", ip=ip)
        raise ApiError(401, "invalid_credentials", "Invalid username or password.")

    auth.clear_attempts(keys)
    # Rotate away any old app session presented by this browser.
    auth.destroy_session(request.cookies.get(SESSION_COOKIE_APP))
    session = auth.create_session(user["id"], "app", ip, request.headers.get("User-Agent"))
    db.execute("UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?", (now(), now(), user["id"]))
    db.audit("login", actor_name=user["username"], actor_id=user["id"], ip=ip)

    response = web.json_response(
        {
            "ok": True,
            "user": public_user(user),
            "csrf": session["csrf"],
            "expires_at": session["expires_at"],
        }
    )
    set_session_cookie(response, "app", session["token"], session["expires_at"])
    return response


async def api_register(request: web.Request):
    if not origin_allowed(request):
        raise ApiError(403, "origin", "Origin not allowed.")
    data = await read_json(request)
    token = data.get("token") or ""
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    ip = client_ip(request)
    auth = request.app["auth"]
    db = request.app["db"]

    locked = auth.lock_remaining(["ip:" + ip])
    if locked:
        raise ApiError(429, "locked", "Too many attempts. Try again later.", retry_after=locked)

    if not isinstance(token, str) or len(token) < 20 or len(token) > 128:
        raise ApiError(400, "bad_invite", "Invite token is missing or malformed.")
    message = username_error(username)
    if message:
        raise ApiError(400, "bad_username", message)
    message = password_error(password)
    if message:
        raise ApiError(400, "bad_password", message)

    invite = db.row("SELECT * FROM invites WHERE token_hash = ?", (token_hash(token),))
    if not invite or invite["used_at"] is not None:
        raise ApiError(400, "bad_invite", "This invite is invalid or already used.")
    if int(invite["expires_at"]) <= now():
        raise ApiError(400, "bad_invite", "This invite has expired.")

    if db.value("SELECT COUNT(*) FROM users") >= int(request.app["settings"].get("max_users", 50)):
        raise ApiError(409, "user_limit", "The user limit for this server has been reached.")
    if db.row("SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (username,)):
        raise ApiError(409, "username_taken", "That username is already taken.")

    password_hash = await auth.hash_async(password)
    uid = None
    try:
        with db.transaction() as conn:
            cur = conn.execute(
                "UPDATE invites SET used_at = ? WHERE id = ? AND used_at IS NULL",
                (now(), invite["id"]),
            )
            if cur.rowcount != 1:
                raise ApiError(409, "bad_invite", "This invite has already been used.")
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, role, disabled, created_at, updated_at)"
                " VALUES (?, ?, 'user', 0, ?, ?)",
                (username, password_hash, now(), now()),
            )
            uid = cur.lastrowid
            conn.execute("UPDATE invites SET used_by = ? WHERE id = ?", (uid, invite["id"]))
    except ApiError:
        raise
    except Exception:
        log.exception("invite registration failed", extra={"event": "register_error", "ip": ip})
        raise ApiError(500, "internal", "Registration failed. Ask the administrator for a new invite.")

    session = auth.create_session(uid, "app", ip, request.headers.get("User-Agent"))
    db.audit("register", actor_name=username, actor_id=uid, detail="via invite", ip=ip)
    user = db.row("SELECT * FROM users WHERE id = ?", (uid,))
    response = web.json_response(
        {"ok": True, "user": public_user(user), "csrf": session["csrf"], "expires_at": session["expires_at"]}
    )
    set_session_cookie(response, "app", session["token"], session["expires_at"])
    return response


async def api_logout(request: web.Request):
    session, token = _require_session(request)
    _require_csrf(request, session)
    request.app["auth"].destroy_session(token)
    request.app["db"].audit("logout", actor_name=session["username"], actor_id=session["user_id"], ip=client_ip(request))
    response = web.json_response({"ok": True})
    clear_session_cookie(response, "app")
    return response


async def api_me(request: web.Request):
    session, _token = _require_session(request)
    settings = request.app["settings"]
    return web.json_response(
        {
            "user": {
                "id": session["user_id"],
                "username": session["username"],
                "role": session["role"],
            },
            "csrf": session["csrf"],
            "ice_servers": settings.get("ice_servers") or [],
            "limits": {
                "max_participants": int(settings.get("max_participants_per_room", 6)),
                "chat_max_length": int(settings.get("chat_max_length", 500)),
            },
        }
    )


async def api_rooms(request: web.Request):
    session, _token = _require_session(request)
    db = request.app["db"]
    manager = request.app["manager"]
    rooms = db.rows("SELECT * FROM rooms ORDER BY name COLLATE NOCASE")
    result = []
    for room in rooms:
        if not manager.can_access(room, session["user_id"], session["role"]):
            continue
        payload = room_payload(room, manager.occupancy(room["id"]), session["user_id"])
        payload["max_participants"] = min(
            int(room["max_participants"]),
            int(request.app["settings"].get("max_participants_per_room", 6)),
        )
        payload["can_join"] = (not payload["closed"]) and payload["occupants"] < payload["max_participants"]
        result.append(payload)
    return web.json_response({"rooms": result})


def create_voice_app(config: dict, db, settings, auth, manager) -> web.Application:
    app = web.Application(middlewares=[security_middleware], client_max_size=32768)
    app["config"] = config
    app["db"] = db
    app["settings"] = settings
    app["auth"] = auth
    app["manager"] = manager
    app["static_dir"] = config["static_dir"]
    app["csp"] = CSP_APP

    app.router.add_get("/", index)
    app.router.add_get("/manifest.json", manifest)
    app.router.add_get("/sw.js", service_worker)
    app.router.add_get("/server.crt", server_cert)
    app.router.add_static("/s/", os.path.join(config["static_dir"], "app"))
    app.router.add_static("/icons/", os.path.join(config["static_dir"], "app", "icons"))

    app.router.add_post("/api/login", api_login)
    app.router.add_post("/api/register", api_register)
    app.router.add_post("/api/logout", api_logout)
    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/rooms", api_rooms)
    app.router.add_get("/api/health", health)
    app.router.add_get("/ws", manager.handle_ws)
    return app
