# Voice + Video + Chat (self-hosted TURN)

Duplicated from `../server.py` + `../static/index.html`, extended with video, chat, and TURN relay.

* Video mesh over WebRTC (camera + mic), camera on/off toggle, per-peer tiles
* In-room text chat (in-memory, last 100 per room, sent as history on join)
* Self-hosted TURN (coturn on the Pi): STUN + TURN ICE servers from `/config`

```
 phone A ---- media (UDP direct, or via local TURN) ---- phone B
    \------------------ WSS signaling --> Pi :9123 <-- WSS --/
```

## Files

```
voip-video/
  server.py                 signaling + chat relay + /config for TURN
  static/index.html         video grid + chat + TURN status
  turn_config.example.json  shape of self-hosted TURN credentials
  turn_config.json          YOUR keys (create it, never commit)
```

## 1. TURN: self-hosted only

The Pi runs its own TURN server (coturn). `/config` serves exactly those
entries — no third-party fallback, so all relay traffic stays on the Pi.
Every client is expected to reach the Pi directly (Tailscale or LAN).

Setup (one time, on the Pi):

```bash
sudo apt install -y coturn
cd ~/voice
cp turnserver.conf.example turnserver.conf
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
# paste that as the password in turnserver.conf's user= line,
# AND as username/credential in turn_config.json's static urls below
```

`turn_config.json` static section points at the Pi itself:

```json
{
  "urls": ["stun:100.87.117.55:3478", "turn:100.87.117.55:3478",
           "turn:192.168.1.42:3478", "turn:100.87.117.55:3478?transport=tcp"],
  "username": "voice",
  "credential": "SAME_PASSWORD_AS_turnserver.conf"
}
```

`server.py` starts `turnserver -c turnserver.conf` itself on boot and restarts
it if it dies (see `turnserver.log`). No extra service to manage.
Disable with `TURN_ENABLED=0`.

Notes:

* The Pi has no public IP, so self-hosted TURN only serves LAN + Tailscale
  peers. Public-URL visitors who can't reach the Pi directly will get
  direct-only media (no relay). (To relay for everyone from the Pi you'd
  need a router port-forward of UDP+TCP 3478 plus a DDNS name; ask if wanted.)
* Relay is password-protected and limited to Tailscale + home-LAN peers
  (`allowed-peer-ip`), with a ~20 Mbps total cap (`max-bps`).
* Check it: `curl -k https://100.87.117.55:9123/config` — the first entries
  should be your Pi's `turn:` URLs with `"turnConfigured": true`.

## 2. Run

```bash
cd voip-video
python3 server.py
# voice+video: https://100.87.117.55:9123  turn=on
```

The server binds `0.0.0.0:9123`, so it answers on localhost, the LAN
(`192.168.1.42`), and the Tailscale IP (`100.87.117.55`) alike. The
self-signed cert covers all three (`CERT_SANS` override if the LAN IP changes).
`BIND` / `PORT` can still be overridden: `BIND=127.0.0.1 PORT=9123 python3 server.py`.

Note: anyone on your LAN can now open the page and join any room (no auth,
same as before — previously only Tailscale members could reach it).

Open `https://100.87.117.55:9123` on two phones (same room name). Accept the cert warning. Allow camera + mic.

## Protocol (WebSocket `/ws`)

Old types still work: `join`, `joined`, `peer-joined`, `peer-left`, `offer`, `answer`, `candidate`, `mute`/`mute-state`, `leave`.

`join` accepts `mode: "full" | "chat"` (default `full`). Chat-only peers need no
mic/camera, appear in the roster with `mode: "chat"`, and are skipped in the
WebRTC mesh. Roster entries and `peer-joined` both carry `mode`.

New:

* `camera` → `camera-state` (`{id, cameraOff}`) — like mute, for the video flag
* `chat` (`{text}`) → broadcast `chat` (`{from, name, text, ts}`) to the whole room
* `joined` now also carries `history` (last 50 chat messages)
* `GET /config` → `{iceServers, turnConfigured}`
* `typing` → broadcast `typing` (`{from, name, typing}`) — indicator only, no storage
* `screen` → broadcast `screen-state` (`{id, sharing}`) so peers add/remove the screen tile
* `auth` (`{pin}`) → `admin-ok` / `admin-denied` (admin only beyond this point)
* `lock` (`{room, locked}`) → locked rooms refuse joins with `join-denied`
* `kick` (`{target}`) → target gets `kicked` and is disconnected
* `force-mute` (`{target, muted}`) → target's mic is turned off
* `GET /stats` → `{service, uptime_s, bind, port, turn, admin, locked, peers, rooms}`

## Features

* Screen sharing (`Share screen`, re-presents on stop), shown as its own tile
* `Fix connection` button + automatic ICE restart when a peer connection fails
* Per-peer quality dots (RTT/loss from `getStats`); sustained bad link auto-pauses your video
* Active-speaker spotlight (loudest peer tile doubles in size)
* Invite links (`?room=name`), remembered name + recent rooms (no public room list by design)
* Join as Video or Chat only (chat-only needs no mic/camera)
* Chat panel hidden by default, opens from the header bubble (dot on new messages)
* Icon call controls with labels, draggable + hideable self preview, solo peer fills the screen
* Typing indicator, iMessage-style bubbles
* Camera quality picker (720p/480p/360p/audio-only) on login
* PWA manifest + offline-cached shell + wake lock during calls
* Admin panel at the unlinked `/appleseed` path (no landing-page entry): per-room mute/kick/lock, live roster from `/stats`

## Admin PIN

The panel needs a PIN. Set it on the Pi (never commit it):

```bash
python3 - <<'EOF'
import json
p = "/home/gj/voice/turn_config.json"
cfg = json.load(open(p))
cfg.setdefault("adminPin", "4821")  # change me
json.dump(cfg, open(p, "w"), indent=2)
EOF
```

(`ADMIN_PIN` env var works too.) Without a PIN the admin endpoints stay disabled.

## Limits

* Chat is in-memory only. Restart clears rooms + history.
* Relay load is capped by coturn (`max-bps`) — direct Tailscale media stays free/unlimited.
