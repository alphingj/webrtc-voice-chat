#!/usr/bin/env python3
"""Voice chat entrypoint: voice app on one port, admin panel on another.

Both listeners bind to config["bind_ip"] only (never 0.0.0.0 unless the
operator explicitly configures it) and share one process, one SQLite database
and one signaling manager so admin kick/mute reaches live sockets instantly.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
import os
import signal
import ssl
import struct
import sys
import time
import zlib
from datetime import datetime, timezone

from aiohttp import web

from admin_api import create_admin_app
from auth import Auth
from db import Database, Settings
from signaling import Signaling, create_voice_app

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VERSION = "1.0.0"
log = logging.getLogger("voice")

DEFAULTS = {
    "bind_ip": "100.87.117.55",
    "voice_port": 9123,
    "admin_port": 9124,
    "cert_file": "certs/server.crt",
    "key_file": "certs/server.key",
    "db_path": "voice.db",
    "static_dir": "static",
    "max_users": 50,
    "max_rooms": 20,
    "max_participants_per_room": 6,
    "session_lifetime_hours": 72,
    "admin_session_lifetime_hours": 12,
    "ice_servers": [],
    "allowed_origins": ["https://100.87.117.55:9123", "https://100.87.117.55:9124"],
    "audit_retention_days": 90,
    "login_max_attempts": 5,
    "login_lockout_seconds": 900,
    "ws_max_message_bytes": 65536,
    "ws_rate_limit_messages": 120,
    "ws_rate_limit_window_seconds": 10,
    "ws_heartbeat_seconds": 25,
    "bind_retry_seconds": 5,
    "invite_max_hours": 720,
    "chat_max_length": 500,
    "log_level": "INFO",
    "trust_proxy": False,
}

PATH_KEYS = ("cert_file", "key_file", "db_path", "static_dir")


# ---------------------------------------------------------------------------
# Configuration and logging
# ---------------------------------------------------------------------------


def load_config(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        raise SystemExit("Config not found: {}".format(path))
    except ValueError as exc:
        raise SystemExit("Config is not valid JSON: {}".format(exc))
    if not isinstance(raw, dict):
        raise SystemExit("Config must be a JSON object.")

    config = dict(DEFAULTS)
    config.update(raw)

    for key in PATH_KEYS:
        value = config.get(key)
        if not isinstance(value, str) or not value:
            raise SystemExit("Config key {} must be a non-empty string.".format(key))
        if not os.path.isabs(value):
            config[key] = os.path.normpath(os.path.join(BASE_DIR, value))

    try:
        bind_ip = str(ipaddress.ip_address(str(config["bind_ip"])))
    except ValueError:
        raise SystemExit("bind_ip is not a valid IP address: {}".format(config["bind_ip"]))
    config["bind_ip"] = bind_ip

    for key in ("voice_port", "admin_port"):
        value = config.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not (1 <= value <= 65535):
            raise SystemExit("{} must be a port number.".format(key))
    if config["voice_port"] == config["admin_port"]:
        raise SystemExit("voice_port and admin_port must differ.")

    if not isinstance(config.get("ice_servers"), list):
        raise SystemExit("ice_servers must be a list.")
    if not isinstance(config.get("allowed_origins"), list) or not config["allowed_origins"]:
        raise SystemExit("allowed_origins must be a non-empty list.")

    host = "[{}]".format(bind_ip) if ":" in bind_ip else bind_ip
    config["voice_url"] = "https://{}:{}".format(host, config["voice_port"])
    config["admin_url"] = "https://{}:{}".format(host, config["admin_port"])
    config["config_path"] = os.path.abspath(path)
    return config


class JsonFormatter(logging.Formatter):
    """One JSON object per line; journald keeps the fields searchable."""

    EXTRA_KEYS = ("event", "peer", "user", "room", "ip", "detail", "path", "origin", "port", "attempt")

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in self.EXTRA_KEYS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str) -> None:
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# PWA icons (generated once, no PIL, no binary files in the repo)
# ---------------------------------------------------------------------------


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def _png(width: int, rgb: bytearray) -> bytes:
    stride = width * 3
    raw = bytearray()
    for y in range(width):
        raw.append(0)
        raw.extend(rgb[y * stride:(y + 1) * stride])
    header = struct.pack(">IIBBBBB", width, width, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _png_chunk(b"IEND", b"")
    )


def _icon_rgb(size: int) -> bytearray:
    """A dark tile with three teal voice bars, drawn at 2x and downsampled."""
    scale = 2
    width = size * scale
    bars = ((0.285, 0.375, 0.34), (0.455, 0.545, 0.64), (0.625, 0.715, 0.44))
    high = bytearray(width * width * 3)
    for py in range(width):
        ny = (py + 0.5) / width - 0.5
        for px in range(width):
            nx = (px + 0.5) / width - 0.5
            light = max(0.0, 1.0 - (nx * nx + ny * ny) * 2.4)
            red = int(12 + 16 * light)
            green = int(17 + 24 * light)
            blue = int(22 + 30 * light)
            for x0, x1, height in bars:
                radius = (x1 - x0) / 2.0
                cx = (x0 + x1) / 2.0 - 0.5
                top = -height / 2.0 + radius
                bottom = height / 2.0 - radius
                clamped = top if ny < top else bottom if ny > bottom else ny
                dx = nx - cx
                dy = ny - clamped
                if dx * dx + dy * dy <= radius * radius:
                    red, green, blue = 64, 224, 208
                    break
            index = (py * width + px) * 3
            high[index] = red
            high[index + 1] = green
            high[index + 2] = blue

    out = bytearray(size * size * 3)
    samples = scale * scale
    for y in range(size):
        for x in range(size):
            red = green = blue = 0
            for sy in range(scale):
                row = (y * scale + sy) * width
                for sx in range(scale):
                    index = (row + x * scale + sx) * 3
                    red += high[index]
                    green += high[index + 1]
                    blue += high[index + 2]
            target = (y * size + x) * 3
            out[target] = red // samples
            out[target + 1] = green // samples
            out[target + 2] = blue // samples
    return out


def ensure_icons(static_dir: str) -> None:
    icons = os.path.join(static_dir, "app", "icons")
    os.makedirs(icons, exist_ok=True)
    for size in (192, 512):
        path = os.path.join(icons, "icon-{}.png".format(size))
        if os.path.exists(path) and os.path.getsize(path) > 0:
            continue
        with open(path, "wb") as handle:
            handle.write(_png(size, _icon_rgb(size)))
        log.info("generated icon", extra={"detail": path})


# ---------------------------------------------------------------------------
# TLS and listeners
# ---------------------------------------------------------------------------


def build_ssl_context(config: dict) -> ssl.SSLContext:
    cert = config["cert_file"]
    key = config["key_file"]
    if not os.path.exists(cert) or not os.path.exists(key):
        raise SystemExit(
            "TLS certificate missing.\n"
            "  expected: {}\n  and:      {}\n"
            "Run ./setup.sh, or generate it with the openssl command in README.md.".format(cert, key)
        )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    try:
        context.options |= ssl.OP_NO_COMPRESSION
    except AttributeError:
        pass
    return context


async def start_site(runner, config: dict, port: int, context: ssl.SSLContext, stop: asyncio.Event, label: str):
    """Bind one listener, retrying forever while the Tailscale IP is absent."""
    attempt = 0
    while not stop.is_set():
        attempt += 1
        site = web.TCPSite(
            runner,
            host=config["bind_ip"],
            port=port,
            ssl_context=context,
            shutdown_timeout=3.0,
        )
        try:
            await site.start()
        except OSError as exc:
            log.warning(
                "bind failed, retrying",
                extra={
                    "event": "bind_retry",
                    "port": port,
                    "attempt": attempt,
                    "detail": "{}: {}".format(type(exc).__name__, exc),
                },
            )
            await asyncio.sleep(float(config.get("bind_retry_seconds", 5)))
            continue
        log.info(
            "listening",
            extra={"event": "listen", "port": port, "detail": "{} {}".format(label, config["bind_ip"])},
        )
        return site
    return None


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


async def maintenance_loop(stop: asyncio.Event, db: Database, auth: Auth, manager: Signaling, settings: Settings):
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=300)
            return
        except asyncio.TimeoutError:
            pass
        try:
            await manager.cleanup_stale()
            await asyncio.to_thread(auth.cleanup)
            days = int(settings.get("audit_retention_days", 90))
            await asyncio.to_thread(db.audit_prune, days)
        except Exception:
            log.exception("maintenance pass failed", extra={"event": "maintenance_error"})


async def run(config: dict) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    db = Database(config["db_path"])
    db.init()
    settings = Settings(db, config)
    auth = Auth(db, config, settings)
    manager = Signaling(config, db, auth, settings)

    ensure_icons(config["static_dir"])
    for sub in ("app", "admin"):
        directory = os.path.join(config["static_dir"], sub)
        if not os.path.isdir(directory):
            raise SystemExit("Static directory missing: {}".format(directory))

    context = build_ssl_context(config)
    voice_app = create_voice_app(config, db, settings, auth, manager)
    admin_app = create_admin_app(config, db, settings, auth, manager)

    voice_runner = web.AppRunner(voice_app, access_log=None)
    admin_runner = web.AppRunner(admin_app, access_log=None)
    await voice_runner.setup()
    await admin_runner.setup()

    if config["bind_ip"] in ("0.0.0.0", "::"):
        log.warning("bind_ip is wildcard; this exposes the app beyond Tailscale", extra={"event": "wildcard_bind"})

    voice_site = await start_site(voice_runner, config, config["voice_port"], context, stop, "voice")
    admin_site = await start_site(admin_runner, config, config["admin_port"], context, stop, "admin")
    if voice_site is None or admin_site is None:
        await voice_runner.cleanup()
        await admin_runner.cleanup()
        db.close()
        return 1

    log.info(
        "voice server ready",
        extra={
            "event": "ready",
            "detail": "voice {} admin {}".format(config["voice_url"], config["admin_url"]),
        },
    )

    await asyncio.to_thread(db.audit_prune, int(settings.get("audit_retention_days", 90)))
    maintenance = asyncio.create_task(maintenance_loop(stop, db, auth, manager, settings))

    await stop.wait()
    log.info("shutting down", extra={"event": "shutdown"})
    maintenance.cancel()
    try:
        await maintenance
    except (asyncio.CancelledError, Exception):
        pass
    await manager.shutdown()
    await voice_runner.cleanup()
    await admin_runner.cleanup()
    db.close()
    log.info("stopped", extra={"event": "stopped"})
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Self-hosted WebRTC voice chat server")
    parser.add_argument(
        "--config",
        default=os.path.join(BASE_DIR, "config.json"),
        help="path to config.json (default: next to server.py)",
    )
    parser.add_argument("--version", action="version", version="voice " + VERSION)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    config = load_config(args.config)
    setup_logging(config.get("log_level", "INFO"))
    log.info("starting", extra={"event": "startup", "detail": "config " + config["config_path"]})
    try:
        return asyncio.run(run(config))
    except SystemExit as exc:
        if exc.code not in (0, None):
            log.error("startup failed", extra={"event": "startup_error", "detail": str(exc.code)})
        return int(exc.code or 1) if isinstance(exc.code, int) else 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
