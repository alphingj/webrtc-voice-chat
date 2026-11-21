#!/usr/bin/env python3
"""Voice + video + chat server.
Same single-file shape as ../server.py, plus:
- video: still mesh, client sends video tracks (server unchanged)
- chat: relay + short in-memory history per room
- TURN: /config endpoint serving Metered.ca ICE servers from env or turn_config.json
Signaling only — media goes peer-to-peer (or via TURN relay), never through this server.
"""
import asyncio
import json
import os
import ssl
import subprocess
import time
import urllib.request
import uuid

from aiohttp import web

PORT = int(os.environ.get("PORT", "9123"))
BIND = os.environ.get("BIND", "0.0.0.0")
# Addresses the self-signed cert must cover (comma-separated SAN list).
CERT_SANS = os.environ.get(
    "CERT_SANS",
    "IP:100.87.117.55,IP:127.0.0.1,IP:192.168.1.42,DNS:localhost",
)
BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")
CERT = os.path.join(BASE, "certs", "server.crt")
KEY = os.path.join(BASE, "certs", "server.key")
TURN_JSON = os.path.join(BASE, "turn_config.json")
# Cloudflare Origin certificate (lets the tunnel verify the origin with
# Full (strict)). Served ONLY when the TLS handshake names a carematch
# host; everything else gets the self-signed cert above.
ORIGIN_CRT = os.path.join(BASE, "certs", "origin.crt")
ORIGIN_KEY = os.path.join(BASE, "certs", "origin.key")
ORIGIN_EXACT = ("carematch.co.in",)
ORIGIN_SUFFIX = (".carematch.co.in",)
origin_ctx = None

# rooms: { room_id: set(ws) }
rooms = {}
# peer info: ws -> {id, name, room, open}
peer_info = {}
# chat history: { room_id: [ {from, name, text, ts} ] } (last 100, memory only)
chat_history = {}
# locked rooms: new joins are refused with join-denied
locked_rooms = set()
STARTED = time.time()
VERSION = "voice+video/8"


def read_admin_pin():
    """Admin PIN: turn_config.json (adminPin) > ADMIN_PIN env. Empty = disabled."""
    if os.path.exists(TURN_JSON):
        try:
            pin = json.load(open(TURN_JSON)).get("adminPin", "")
            if pin:
                return str(pin)
        except Exception:
            pass
    return os.environ.get("ADMIN_PIN", "")


TURN_CACHE_SECS = 3600
_turn_cache = {"at": 0.0, "ice": None}


def read_static_turn():
    """Static TURN credentials: turn_config.json > env vars. Returns (ice, ok)."""
    urls, username, credential = [], "", ""
    if os.path.exists(TURN_JSON):
        try:
            with open(TURN_JSON) as f:
                cfg = json.load(f)
            urls = cfg.get("urls", [])
            if isinstance(urls, str):
                urls = [u.strip() for u in urls.split(",") if u.strip()]
            username = cfg.get("username", "")
            credential = cfg.get("credential", "")
        except Exception as e:
            print(f"turn_config.json unreadable: {e}")
    if not urls:
        raw = os.environ.get("METERED_TURN_URLS", "")
        urls = [u.strip() for u in raw.split(",") if u.strip()]
        username = os.environ.get("METERED_TURN_USERNAME", "")
        credential = os.environ.get("METERED_TURN_CREDENTIAL", "")
    if isinstance(urls, str):
        urls = [u.strip() for u in urls.split(",") if u.strip()]
    stun_urls = [u for u in urls if u.startswith("stun:")]
    turn_urls = [u for u in urls if not u.startswith("stun:")]
    if not stun_urls:
        stun_urls = [os.environ.get(
            "METERED_STUN_URL", "stun:stun.metered.ca:80")]
    ice = [{"urls": stun_urls}]
    if turn_urls and username and credential:
        ice.append({"urls": turn_urls, "username": username,
                    "credential": credential})
    return ice, bool(turn_urls and username and credential)


def read_api_key():
    """Metered REST API key: turn_config.json (apiKey) > METERED_API_KEY env."""
    if os.path.exists(TURN_JSON):
        try:
            with open(TURN_JSON) as f:
                return json.load(f).get("apiKey", "")
        except Exception:
            pass
    return os.environ.get("METERED_API_KEY", "")


def fetch_metered_ice(api_key):
    """Fetch fresh iceServers from Metered REST API (vexcall app)."""
    url = ("https://vexcall.metered.live/api/v1/turn/credentials"
           f"?apiKey={api_key}")
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        ice = json.loads(r.read().decode())
    if not isinstance(ice, list) or not ice:
        raise ValueError("bad iceServers from metered API")
    return ice


def load_turn_config():
    """Synchronous ICE servers for startup logging. Prefers cache/static."""
    if _turn_cache["ice"]:
        return _turn_cache["ice"], True
    return read_static_turn()


async def get_ice_servers():
    """ICE servers: self-hosted static TURN first, Metered API appended as fallback.

    Static (your Pi) costs nothing, so it goes first — browsers try every
    entry and use what works. Metered stays as fallback for visitors who
    cannot reach the Pi directly (e.g. public URL over symmetric NAT).
    """
    static_ice, static_ok = read_static_turn()
    api_key = read_api_key()
    api_ice, api_ok = [], False
    if api_key:
        age = time.time() - _turn_cache["at"]
        if _turn_cache["ice"] and age < TURN_CACHE_SECS:
            api_ice, api_ok = _turn_cache["ice"], True
        else:
            try:
                api_ice = await asyncio.to_thread(fetch_metered_ice, api_key)
                _turn_cache.update(at=time.time(), ice=api_ice)
                api_ok = True
            except Exception as e:
                print(f"metered API failed ({e}), using static only")
                if _turn_cache["ice"]:
                    api_ice, api_ok = _turn_cache["ice"], True
    seen = set()
    ice = []
    for entry in static_ice + api_ice:
        key = json.dumps(entry, sort_keys=True)
        if key not in seen:
            seen.add(key)
            ice.append(entry)
    return ice, bool(static_ok or api_ok)


TURN_BIN = os.environ.get("TURN_BIN", "turnserver")
TURN_CONF = os.environ.get("TURN_CONF", os.path.join(BASE, "turnserver.conf"))
TURN_START_TRIES = 0


def turn_port_busy():
    """True if something already answers TURN's port (e.g. system coturn)."""
    import socket
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect(("127.0.0.1", 3478))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def start_turnserver():
    """Run coturn as a child process so it autostarts with this service."""
    global TURN_START_TRIES
    if os.environ.get("TURN_ENABLED", "1") != "1":
        print("turnserver disabled (TURN_ENABLED=0)")
        return None
    if turn_port_busy():
        print("port 3478 already served (system coturn?) — leaving it alone")
        return "external"
    if not os.path.exists(TURN_CONF):
        print(f"no {TURN_CONF} — self-hosted TURN off, using Metered fallback")
        return None
    if TURN_START_TRIES >= 3:
        return None
    TURN_START_TRIES += 1
    try:
        proc = subprocess.Popen(
            [TURN_BIN, "-c", TURN_CONF],
            stdout=open(os.path.join(BASE, "turnserver.log"), "ab"),
            stderr=subprocess.STDOUT,
        )
        print(f"turnserver started (pid {proc.pid})")
        TURN_START_TRIES = 0
        return proc
    except FileNotFoundError:
        print(f"{TURN_BIN} not found — install coturn (sudo apt install coturn)")
        return None
    except Exception as e:
        print(f"turnserver failed to start: {e}")
        return None


def ensure_cert():
    os.makedirs(os.path.join(BASE, "certs"), exist_ok=True)
    san_file = os.path.join(BASE, "certs", "san.txt")
    current = ""
    if os.path.exists(san_file):
        with open(san_file) as f:
            current = f.read().strip()
    if (not (os.path.exists(CERT) and os.path.exists(KEY))
            or current != CERT_SANS):
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days",
             "3650", "-nodes", "-keyout", KEY, "-out", CERT,
             "-subj", "/CN=voice-video",
             "-addext", f"subjectAltName={CERT_SANS}",
             "-addext", "basicConstraints=critical,CA:TRUE"],
            check=True, capture_output=True,
        )
        os.chmod(KEY, 0o600)
        with open(san_file, "w") as f:
            f.write(CERT_SANS + "\n")
        print(f"generated {CERT} ({CERT_SANS})")


def sni_callback(sslobj, server_name, ctx):
    """Serve the Cloudflare Origin cert when the handshake names our domain."""
    if server_name and (server_name in ORIGIN_EXACT
                        or server_name.endswith(ORIGIN_SUFFIX)):
        if origin_ctx is not None:
            sslobj.context = origin_ctx


def build_ssl_context():
    """Self-signed by default; Origin cert via SNI when present."""
    global origin_ctx
    ensure_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT, KEY)
    if os.path.exists(ORIGIN_CRT) and os.path.exists(ORIGIN_KEY):
        try:
            origin_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            origin_ctx.load_cert_chain(ORIGIN_CRT, ORIGIN_KEY)
            ctx.set_servername_callback(sni_callback)
            print("origin cert loaded (SNI: *.carematch.co.in)")
        except Exception as e:
            print(f"origin cert unusable: {e}")
            origin_ctx = None
    return ctx


async def index(request):
    return web.FileResponse(os.path.join(STATIC, "index.html"))


# APK publishing: copy/symlink the built APK to this path (or set APK_FILE).
# Never commit the APK or the keystore; they are deployed alongside the server.
APK_FILE = os.environ.get("APK_FILE", os.path.join(STATIC, "voice.apk"))


async def download_handler(request):
    if not os.path.exists(APK_FILE):
        return web.json_response({"error": "no APK published yet"}, status=404)
    return web.FileResponse(
        APK_FILE,
        headers={"Content-Disposition": 'attachment; filename="voice.apk"'},
    )


async def download_hash_handler(request):
    import hashlib
    if not os.path.exists(APK_FILE):
        return web.json_response({"error": "no APK published yet"}, status=404)
    h = hashlib.sha256()
    with open(APK_FILE, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return web.json_response({
        "file": os.path.basename(APK_FILE),
        "sha256": h.hexdigest(),
        "size": os.path.getsize(APK_FILE),
    })


async def config_handler(request):
    ice, ok = await get_ice_servers()
    return web.json_response({"iceServers": ice, "turnConfigured": ok})


async def stats_handler(request):
    """Live stats without touching the network: rooms, peers, uptime, TURN mode."""
    if read_api_key() or _turn_cache["ice"]:
        mode = "metered-api"
    else:
        _, static_ok = read_static_turn()
        mode = "static" if static_ok else "off"
    return web.json_response({
        "service": VERSION,
        "uptime_s": int(time.time() - STARTED),
        "bind": BIND, "port": PORT,
        "turn": {"mode": mode, "configured": mode != "off"},
        "admin": bool(read_admin_pin()),
        "locked": sorted(locked_rooms),
        "peers": len(peer_info),
        "rooms": {
            room: [{"id": peer_info[o]["id"], "name": peer_info[o]["name"],
                    "mode": peer_info[o].get("mode", "full")}
                   for o in ws_set if peer_info.get(o, {}).get("name")]
            for room, ws_set in rooms.items()
        },
    })


async def broadcast(room, payload, skip=None):
    for o in list(rooms.get(room, set())):
        if o is skip:
            continue
        try:
            await o.send_json(payload)
        except Exception:
            pass


async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    peer_id = str(uuid.uuid4())[:8]
    peer_info[ws] = {"id": peer_id, "name": None, "room": None,
                       "open": True, "admin": False, "mode": "full"}
    print(f"ws open {peer_id} from {request.remote}")

    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except Exception:
                continue
            t = data.get("type")

            if t == "join":
                room = str(data.get("room", "main"))[:32] or "main"
                name = str(data.get("name", peer_id))[:24] or peer_id
                if room in locked_rooms and not peer_info[ws].get("admin"):
                    await ws.send_json({"type": "join-denied", "room": room,
                                        "reason": "Room is locked by admin"})
                    print(f"{peer_id} ({name}) denied locked room {room}")
                    continue
                mode = str(data.get("mode", "full"))[:8]
                if mode not in ("full", "chat"):
                    mode = "full"
                peer_info[ws]["name"] = name
                peer_info[ws]["room"] = room
                peer_info[ws]["mode"] = mode
                rooms.setdefault(room, set()).add(ws)
                roster = [
                    {"id": peer_info[o]["id"], "name": peer_info[o]["name"],
                     "mode": peer_info[o].get("mode", "full")}
                    for o in rooms[room]
                    if o is not ws and peer_info[o]["name"]
                ]
                await ws.send_json({"type": "joined", "id": peer_id,
                                    "room": room, "peers": roster,
                                    "history": chat_history.get(room, [])[-50:]})
                await broadcast(room, {"type": "peer-joined", "id": peer_id,
                                       "name": name, "mode": mode}, skip=ws)
                print(f"{peer_id} ({name}) joined {room}")

            elif t in ("offer", "answer", "candidate"):
                to_id = data.get("to")
                for o, info in peer_info.items():
                    if info["id"] == to_id and o is not ws and info["open"]:
                        try:
                            fwd = {"type": t, "from": peer_id}
                            if t == "candidate":
                                fwd["candidate"] = data.get("candidate")
                            else:
                                fwd["sdp"] = data.get("sdp")
                            await o.send_json(fwd)
                        except Exception:
                            pass

            elif t == "leave":
                break

            elif t == "mute":
                room = peer_info[ws]["room"]
                if not room:
                    continue
                await broadcast(room, {"type": "mute-state", "id": peer_id,
                                       "muted": bool(data.get("muted"))}, skip=ws)

            elif t == "camera":
                room = peer_info[ws]["room"]
                if not room:
                    continue
                await broadcast(room, {"type": "camera-state", "id": peer_id,
                                       "cameraOff": bool(data.get("cameraOff"))}, skip=ws)

            elif t == "auth":
                pin = str(data.get("pin", ""))
                real = read_admin_pin()
                ok = bool(real) and pin == real
                peer_info[ws]["admin"] = ok
                await ws.send_json({"type": "admin-ok" if ok else "admin-denied"})

            elif t == "lock":
                if not peer_info[ws].get("admin"):
                    continue
                target_room = str(data.get("room", ""))[:32]
                if not target_room:
                    continue
                if data.get("locked", True):
                    locked_rooms.add(target_room)
                else:
                    locked_rooms.discard(target_room)
                await broadcast(target_room, {"type": "lock-state",
                                              "room": target_room,
                                              "locked": target_room in locked_rooms})

            elif t == "kick":
                if not peer_info[ws].get("admin"):
                    continue
                target = data.get("target")
                for o, info in list(peer_info.items()):
                    if info["id"] == target and o is not ws:
                        try:
                            await o.send_json({"type": "kicked",
                                               "reason": "Removed by admin"})
                            await o.close()
                        except Exception:
                            pass
                        break

            elif t == "force-mute":
                if not peer_info[ws].get("admin"):
                    continue
                target = data.get("target")
                mute_on = bool(data.get("muted", True))
                for o, info in peer_info.items():
                    if info["id"] == target and o is not ws and info["open"]:
                        try:
                            await o.send_json({"type": "force-mute",
                                               "muted": mute_on})
                        except Exception:
                            pass
                        break

            elif t == "typing":
                room = peer_info[ws]["room"]
                name = peer_info[ws]["name"]
                if not room or not name:
                    continue
                await broadcast(room, {"type": "typing", "from": peer_id,
                                       "name": name,
                                       "typing": bool(data.get("typing"))},
                                skip=ws)

            elif t == "screen":
                room = peer_info[ws]["room"]
                if not room:
                    continue
                await broadcast(room, {"type": "screen-state", "id": peer_id,
                                       "sharing": bool(data.get("sharing"))},
                                skip=ws)

            elif t == "chat":
                room = peer_info[ws]["room"]
                name = peer_info[ws]["name"]
                if not room or not name:
                    continue
                text = str(data.get("text", ""))[:2000].strip()
                if not text:
                    continue
                item = {"from": peer_id, "name": name,
                        "text": text, "ts": int(time.time() * 1000)}
                chat_history.setdefault(room, []).append(item)
                chat_history[room] = chat_history[room][-100:]
                await broadcast(room, {"type": "chat", **item}, skip=None)

    finally:
        info = peer_info.pop(ws, None)
        if info:
            room = info["room"]
            name = info["name"]
            if room and room in rooms:
                rooms[room].discard(ws)
                if not rooms[room]:
                    del rooms[room]
                    chat_history.pop(room, None)
                await broadcast(room, {"type": "peer-left", "id": peer_id,
                                       "name": name})
            print(f"ws close {peer_id} ({name}) from {room}")
        try:
            await ws.close()
        except Exception:
            pass

    return ws


async def main():
    ssl_ctx = build_ssl_context()
    ice, ok = load_turn_config()
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/appleseed", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/config", config_handler)
    app.router.add_get("/stats", stats_handler)
    app.router.add_get("/download", download_handler)
    app.router.add_get("/download/sha256", download_hash_handler)
    app.router.add_static("/static", STATIC, name="static", follow_symlinks=True)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, BIND, PORT, ssl_context=ssl_ctx)
    await site.start()
    print(f"voice+video: https://{BIND}:{PORT} turn={'on' if ok else 'off (set turn_config.json)'}")
    turn_proc = start_turnserver()
    while True:
        await asyncio.sleep(60)
        if turn_proc == "external":
            if not turn_port_busy():
                turn_proc = start_turnserver()
        elif turn_proc is not None and turn_proc.poll() is not None:
            print(f"turnserver died (rc={turn_proc.returncode}), restarting")
            turn_proc = start_turnserver()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped")
