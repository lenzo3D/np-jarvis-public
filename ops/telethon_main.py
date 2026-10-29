"""Telethon listener for your Telegram groups (class, project...), logged in as you. Read-only by construction.

Normally it runs inside Ops (see ops/main.py) and the panel handles the one-time login. Standalone, for a server:

    .venv/bin/python -m ops.telethon_main login   # one-time, asks for phone + code
    .venv/bin/python -m ops.telethon_main         # run listener
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient, events

from core import groups, tg_group
from core.config import load_settings
from core.db import DB
from core.models import GroupMessage

log = logging.getLogger("telethon_main")
BACKFILL = 200
DIALOG_REFRESH = 600.0


def _sender_name(msg) -> str:
    s = getattr(msg, "sender", None)
    if s is None:
        return str(getattr(msg, "sender_id", "") or "?")
    name = " ".join(x for x in [getattr(s, "first_name", None), getattr(s, "last_name", None)] if x)
    return name or getattr(s, "title", None) or getattr(s, "username", None) or "?"


def to_group_message(chat_id: int, msg) -> GroupMessage | None:
    text = getattr(msg, "message", None) or ""
    if not text.strip():
        return None
    return GroupMessage(chat_id, msg.id, _sender_name(msg), text, msg.date)


def client_for(settings) -> TelegramClient:
    if not settings.telethon_api_id or not settings.telethon_api_hash:
        raise SystemExit("Set TELETHON_API_ID and TELETHON_API_HASH in secrets.env (my.telegram.org)")
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    return TelegramClient(str(settings.state_dir / "telethon"), settings.telethon_api_id, settings.telethon_api_hash)


LOGIN_LOCK_FRESH = 600.0


def session_path(settings) -> Path:
    return Path(settings.state_dir) / "telethon.session"


def login_lock_path(settings) -> Path:
    return Path(settings.state_dir) / "telethon.login.lock"


def session_ready(settings) -> bool:
    """A saved Telegram session exists and nobody is in the middle of logging in (both would touch the same file)."""
    lock = login_lock_path(settings)
    if lock.exists() and time.time() - lock.stat().st_mtime < LOGIN_LOCK_FRESH:
        return False
    return session_path(settings).exists()


def _status(db, state: str, **extra) -> None:
    db.set_json_setting("telethon_status", {"state": state, "at": datetime.now(timezone.utc).isoformat(), **extra})


async def _backfill(client, db, chat_id: int) -> None:
    async for msg in client.iter_messages(chat_id, limit=BACKFILL):
        gm = to_group_message(chat_id, msg)
        if gm:
            tg_group.store_message(db, gm)


async def _refresh_dialogs(client, db) -> None:
    found = []
    async for d in client.iter_dialogs(limit=200):
        if d.is_group or d.is_channel:
            found.append({"id": d.id, "name": d.name})
    groups.save_dialogs(db, found)


async def _watch(client, settings, db, poll: float) -> None:
    """One authorised connection: mirror the chosen groups into the database until the connection drops."""
    backfilled: set[int] = set()

    async def on_new(event):
        if event.chat_id not in groups.ids(settings, db):
            return
        msg = event.message
        await msg.get_sender()
        gm = to_group_message(event.chat_id, msg)
        if gm:
            tg_group.store_message(db, gm)

    client.add_event_handler(on_new, events.NewMessage())
    last_dialogs: float | None = None  # None: refresh the group list on the first pass, whatever the uptime
    while client.is_connected():
        wanted = groups.ids(settings, db)
        for chat_id in wanted:
            if chat_id not in backfilled:
                await _backfill(client, db, chat_id)
                backfilled.add(chat_id)
        if last_dialogs is None or time.monotonic() - last_dialogs > DIALOG_REFRESH:
            await _refresh_dialogs(client, db)
            last_dialogs = time.monotonic()
        _status(db, "listening", groups=len(wanted))
        await asyncio.sleep(poll)


async def listen(settings, db, client_factory=client_for, poll: float = 30.0, ready=session_ready) -> None:
    """Keep a Telegram connection alive for the lifetime of the process. Runs inside Ops (or standalone).
    Waits quietly for a login, picks up newly chosen groups within `poll` seconds, and reconnects after errors."""
    if not settings.telethon_api_id or not settings.telethon_api_hash:
        _status(db, "no_api_keys")
        return
    while True:
        if not ready(settings):  # not logged in yet (or a login is in progress): do not touch the session file
            _status(db, "login_needed")
            await asyncio.sleep(poll)
            continue
        client = client_factory(settings)
        try:
            await client.connect()
            if await client.is_user_authorized():
                await _watch(client, settings, db, poll)
            else:
                _status(db, "login_needed")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - network drops, flood waits: report and retry
            log.warning("telegram listener error: %s: %s", type(e).__name__, e)
            _status(db, "error", message=f"{type(e).__name__}")
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        await asyncio.sleep(poll)


async def run() -> None:
    settings = load_settings()
    db = DB(settings.db_path)
    db.init()
    await listen(settings, db)


async def login() -> None:
    client = client_for(load_settings())
    await client.start()  # interactive: phone number + login code typed by the user
    me = await client.get_me()
    print(f"Logged in as {me.first_name}. Session saved.")
    await client.disconnect()


async def list_chats() -> None:
    client = client_for(load_settings())
    await client.connect()
    async for d in client.iter_dialogs(limit=100):
        if d.is_group or d.is_channel:
            print(f"{d.id}\t{d.name}")
    await client.disconnect()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    asyncio.run({"login": login, "list": list_chats, "run": run}[cmd]())
