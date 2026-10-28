#!/usr/bin/env python3
"""Command-line administration. Uses only the Python standard library."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sqlite3
import sys

from auth import hash_password, password_error, room_name_error, username_error
from db import Database, now

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def resolve_db_path(config_path: str) -> str:
    try:
        with open(config_path, "r", encoding="utf-8") as handle:
            config = json.load(handle)
    except FileNotFoundError:
        sys.exit("Config not found: {}".format(config_path))
    except ValueError as exc:
        sys.exit("Config is not valid JSON: {}".format(exc))
    path = config.get("db_path", "voice.db")
    if not os.path.isabs(path):
        path = os.path.join(BASE_DIR, path)
    return path


def open_db(config_path: str) -> Database:
    database = Database(resolve_db_path(config_path))
    database.init()
    return database


def prompt_password(prompt: str = "Password: ") -> str:
    first = getpass.getpass(prompt)
    message = password_error(first)
    if message:
        sys.exit(message)
    second = getpass.getpass("Repeat password: ")
    if first != second:
        sys.exit("Passwords do not match.")
    return first


def cmd_create_admin(database: Database, args) -> int:
    username = args.username.strip()
    message = username_error(username)
    if message:
        sys.exit(message)
    if database.row("SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (username,)):
        sys.exit("User already exists: {}".format(username))
    password = prompt_password()
    database.execute(
        "INSERT INTO users (username, password_hash, role, disabled, created_at, updated_at)"
        " VALUES (?, ?, 'admin', 0, ?, ?)",
        (username, hash_password(password), now(), now()),
    )
    database.audit("user_create", actor_name="manage.py", target=username, detail="role=admin")
    print("Created administrator '{}'.".format(username))
    print("Sign in at the admin panel, then create rooms and users.")
    return 0


def cmd_reset_password(database: Database, args) -> int:
    username = args.username.strip()
    user = database.row("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,))
    if not user:
        sys.exit("No such user: {}".format(username))
    password = prompt_password("New password: ")
    database.execute(
        "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
        (hash_password(password), now(), user["id"]),
    )
    database.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
    database.audit("password_reset", actor_name="manage.py", target=user["username"], detail="CLI")
    print("Password reset for '{}'. Existing sessions were revoked.".format(user["username"]))
    return 0


def cmd_list_users(database: Database, args) -> int:
    users = database.rows(
        "SELECT id, username, role, disabled, last_login_at FROM users ORDER BY username COLLATE NOCASE"
    )
    if not users:
        print("No users yet. Run: python3 manage.py create-admin <username>")
        return 0
    print("{:>4}  {:<32} {:<6} {:<9} {}".format("id", "username", "role", "status", "last login"))
    for user in users:
        last = "-"
        if user["last_login_at"]:
            import datetime

            last = datetime.datetime.fromtimestamp(int(user["last_login_at"])).strftime("%Y-%m-%d %H:%M")
        status = "disabled" if user["disabled"] else "active"
        print("{:>4}  {:<32} {:<6} {:<9} {}".format(user["id"], user["username"], user["role"], status, last))
    return 0


def cmd_create_room(database: Database, args) -> int:
    name = args.name.strip()
    message = room_name_error(name)
    if message:
        sys.exit(message)
    if database.row("SELECT 1 FROM rooms WHERE name = ? COLLATE NOCASE", (name,)):
        sys.exit("Room already exists: {}".format(name))
    if args.max_participants < 2 or args.max_participants > 12:
        sys.exit("max-participants must be 2-12.")
    database.execute(
        "INSERT INTO rooms (name, max_participants, password_hash, access_mode, closed, created_at)"
        " VALUES (?, ?, NULL, 'all', 0, ?)",
        (name, args.max_participants, now()),
    )
    database.audit("room_create", actor_name="manage.py", target=name, detail="CLI")
    print("Created room '{}' (max {}).".format(name, args.max_participants))
    return 0


def cmd_list_rooms(database: Database, args) -> int:
    rooms = database.rows("SELECT id, name, max_participants, access_mode, closed FROM rooms ORDER BY name COLLATE NOCASE")
    if not rooms:
        print("No rooms yet. Create one in the admin panel.")
        return 0
    for room in rooms:
        state = "closed" if room["closed"] else "open"
        print(
            "{:>4}  {:<32} max={:<3} access={:<8} {}".format(
                room["id"], room["name"], room["max_participants"], room["access_mode"], state
            )
        )
    return 0


def cmd_prune_audit(database: Database, args) -> int:
    removed = database.audit_prune(args.days)
    print("Removed {} audit entries older than {} days.".format(removed, args.days))
    return 0


def cmd_init_db(database: Database, args) -> int:
    print("Database ready: {}".format(database.path))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Voice server administration")
    parser.add_argument(
        "--config",
        default=os.path.join(BASE_DIR, "config.json"),
        help="path to config.json",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create_admin = sub.add_parser("create-admin", help="create the first administrator")
    create_admin.add_argument("username")
    create_admin.set_defaults(func=cmd_create_admin)

    reset = sub.add_parser("reset-password", help="set a new password for a user")
    reset.add_argument("username")
    reset.set_defaults(func=cmd_reset_password)

    listing = sub.add_parser("list-users", help="list users")
    listing.set_defaults(func=cmd_list_users)

    room = sub.add_parser("create-room", help="create a room")
    room.add_argument("name")
    room.add_argument("--max-participants", type=int, default=6)
    room.set_defaults(func=cmd_create_room)

    rooms = sub.add_parser("list-rooms", help="list rooms")
    rooms.set_defaults(func=cmd_list_rooms)

    prune = sub.add_parser("prune-audit", help="delete old audit entries")
    prune.add_argument("--days", type=int, default=90)
    prune.set_defaults(func=cmd_prune_audit)

    init = sub.add_parser("init-db", help="create or migrate the database")
    init.set_defaults(func=cmd_init_db)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    database = open_db(args.config)
    try:
        return args.func(database, args)
    except sqlite3.Error as exc:
        sys.exit("Database error: {}".format(exc))


if __name__ == "__main__":
    sys.exit(main())
