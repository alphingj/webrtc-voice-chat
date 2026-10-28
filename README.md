# Voice — super-minimal self-hosted WebRTC voice chat

A tiny, single-file voice chat server for phones on a Tailscale network.

* One Python file (`server.py`), one HTML file (`static/index.html`)
* One port: `9123` (HTTPS + WebSocket signaling)
* Self-signed certificate generated on first start
* Signaling only — audio goes phone-to-phone over WebRTC, never through the server
* No database, no user accounts, no admin panel, no build step

```
 phone A  ────────────── audio (UDP, direct over Tailscale) ────────────── phone B
    │                                                                        │
    │  WSS signaling (SDP / ICE only)                     WSS signaling      │
    └──────────────────────────►  Pi :9123  ◄───────────────────────────────┘
```

## Files

```
voice/
  server.py        the whole server (signaling + TLS + static file serving)
  static/index.html  the whole frontend (login, room, WebRTC mesh, mute, volume)
```

## Requirements

* Python 3 with `aiohttp` (from apt: `python3-aiohttp`)
* `openssl` (for the self-signed cert)
* Tailscale running on the Pi and on every phone, all in the same tailnet
* The Pi's Tailscale IP (this build defaults to `100.87.117.55`)

The Pi listens on **only** `100.87.117.55:9123`. Nothing on `0.0.0.0`. WebRTC media uses random UDP ports directly between phones.

## Run locally (testing)

```bash
python3 server.py
```

Open `https://127.0.0.1:9123` and accept the certificate warning.

## Deploy on the Raspberry Pi

```bash
# on the Pi
cd /home/gj/voice
python3 server.py
```

The server prints `voice: https://100.87.117.55:9123` and generates `certs/server.crt` + `certs/server.key` on first run (10-year self-signed cert with `subjectAltName = IP:100.87.117.55`).

To run it as a background service, use systemd, tmux, screen, or `nohup`:

```bash
nohup python3 server.py > voice.log 2>&1 &
```

## Open on a phone

1. Connect the phone to Tailscale and confirm it can reach the Pi: `ping 100.87.117.55`
2. Open **`https://100.87.117.55:9123`** in Chrome
3. Chrome shows a certificate warning — tap **Advanced → Proceed to 100.87.117.55 (unsafe)**
4. Enter a username and room name, tap **Join room**
5. Open the same URL on a second phone, use the same room name, and you're in a voice call

Audio travels directly between the phones. The server only relays the connection setup (SDP/ICE).

## How it works

1. Both phones connect to `wss://100.87.117.55:9123/ws` and `join` the same room.
2. The newcomer sends an `offer` to every peer already in the room; each peer answers.
3. ICE candidates are forwarded between peers so WebRTC can establish a direct media path.
4. The server never sees or touches audio. It only forwards JSON signaling messages.
5. Mute toggles the local mic track and broadcasts a `mute-state` so others can show it.
6. Each peer gets its own `<audio>` element with a per-peer volume slider.
7. If the WebSocket drops, the client reconnects and re-joins automatically.

## Limitations (intentional)

* **No accounts.** Anyone who opens the URL can join with any username.
* **No admin panel.** No kick, mute-by-admin, rooms list, or user management.
* **No database.** Rooms and peers live in memory only; restarting the server clears them.
* **No persistence.** Refreshing the page re-joins as a fresh peer.
* **One port.** Only `9123` is served.

If you need accounts, invites, per-user rooms, admin kick/mute, audit log, etc., those were in the previous full version (still available in git history). This minimal build deliberately drops all of it.

## Ports and firewall

* Pi listens on `100.87.117.55:9123` only (TCP/TLS).
* No UDP ports need to be opened on the Pi — media is phone-to-phone.
* Tailscale ACLs (if you use them): allow phones → Pi TCP 9123, and phone ↔ phone all UDP for media.
