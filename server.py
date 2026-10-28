#!/usr/bin/env python3
"""Super-minimal WebRTC voice chat server.
One file. One port (9123). Self-signed cert. In-memory rooms.
Signaling only — audio goes phone-to-phone over WebRTC.
"""
import asyncio
import json
import os
import ssl
import subprocess
import uuid

from aiohttp import web

PORT = 9123
BIND = "100.87.117.55"
CERT = "certs/server.crt"
KEY = "certs/server.key"
BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")

# rooms: { room_id: set(ws) }
rooms = {}
# peer info: ws -> {id, name, room}
peer_info = {}


def ensure_cert():
    os.makedirs(os.path.join(BASE, "certs"), exist_ok=True)
    if not (os.path.exists(CERT) and os.path.exists(KEY)):
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days",
             "3650", "-nodes", "-keyout", KEY, "-out", CERT,
             "-subj", f"/CN={BIND}",
             "-addext", f"subjectAltName=IP:{BIND}",
             "-addext", "basicConstraints=critical,CA:TRUE"],
            check=True, capture_output=True,
        )
        os.chmod(KEY, 0o600)
        print(f"generated {CERT}")


async def index(request):
    return web.FileResponse(os.path.join(STATIC, "index.html"))


async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    peer_id = str(uuid.uuid4())[:8]
    peer_info[ws] = {"id": peer_id, "name": None, "room": None, "open": True}
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
                room = data.get("room", "main")
                name = data.get("name", peer_id)
                peer_info[ws]["name"] = name
                peer_info[ws]["room"] = room
                rooms.setdefault(room, set()).add(ws)
                roster = [
                    {"id": peer_info[o]["id"], "name": peer_info[o]["name"]}
                    for o in rooms[room]
                    if o is not ws and peer_info[o]["name"]
                ]
                await ws.send_json({"type": "joined", "id": peer_id, "room": room, "peers": roster})
                for o in rooms[room]:
                    if o is not ws:
                        try:
                            await o.send_json({"type": "peer-joined", "id": peer_id, "name": name})
                        except Exception:
                            pass
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
                muted = bool(data.get("muted"))
                for o in rooms.get(room, set()):
                    if o is not ws:
                        try:
                            await o.send_json({"type": "mute-state", "id": peer_id, "muted": muted})
                        except Exception:
                            pass

    finally:
        info = peer_info.pop(ws, None)
        if info:
            room = info["room"]
            name = info["name"]
            if room and room in rooms:
                rooms[room].discard(ws)
                if not rooms[room]:
                    del rooms[room]
                for o in rooms.get(room, set()):
                    try:
                        await o.send_json({"type": "peer-left", "id": peer_id, "name": name})
                    except Exception:
                        pass
            print(f"ws close {peer_id} ({name}) from {room}")
        try:
            await ws.close()
        except Exception:
            pass

    return ws


async def main():
    ensure_cert()
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static", STATIC, name="static", follow_symlinks=True)
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(CERT, KEY)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, BIND, PORT, ssl_context=ssl_ctx)
    await site.start()
    print(f"voice: https://{BIND}:{PORT}")
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped")
