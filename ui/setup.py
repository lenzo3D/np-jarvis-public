"""First-run setup for the panel: check the Telegram bot token, find the owner's Telegram id, save secrets.env."""
from __future__ import annotations

import re
from pathlib import Path

import httpx

from core.config import Settings, update_env_file

TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")
DEFAULT_BASE = "https://politemall.polite.edu.sg"


def needs_setup(settings: Settings) -> bool:
    return not (settings.ops_bot_token and settings.allowed_telegram_id and settings.politemall_user
                and settings.politemall_pass)


def status(settings: Settings) -> dict:
    return {"needed": needs_setup(settings),
            "have": {"bot": bool(settings.ops_bot_token), "owner": bool(settings.allowed_telegram_id),
                     "school": bool(settings.politemall_user and settings.politemall_pass)},
            "default_base": settings.politemall_base or DEFAULT_BASE}


async def _tg(token: str, method: str, **params):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"https://api.telegram.org/bot{token}/{method}", params=params)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {}


async def check_token(token: str) -> dict:
    token = (token or "").strip()
    if not TOKEN_RE.match(token):
        return {"ok": False, "error": "That does not look like a bot token. It looks like 123456789:AAH... and comes from @BotFather."}
    try:
        code, body = await _tg(token, "getMe")
    except httpx.HTTPError:
        return {"ok": False, "error": "Could not reach Telegram. Check your internet connection and try again."}
    if code != 200 or not body.get("ok"):
        return {"ok": False, "error": "Telegram did not accept that token. Copy it again from @BotFather."}
    me = body["result"]
    return {"ok": True, "username": me.get("username", ""), "name": me.get("first_name", "")}


async def find_owner(token: str) -> dict:
    """The owner is whoever sends the bot a private message. Returns the most recent sender."""
    try:
        code, body = await _tg((token or "").strip(), "getUpdates", timeout=0, allowed_updates='["message"]')
    except httpx.HTTPError:
        return {"ok": False, "error": "Could not reach Telegram. Check your internet connection and try again."}
    if code == 409:
        return {"ok": False, "error": "JARVIS is already running with this bot. Stop it from the top bar, then try again."}
    if code != 200 or not body.get("ok"):
        return {"ok": False, "error": "Telegram did not accept that token."}
    for upd in reversed(body.get("result", [])):
        msg = upd.get("message") or {}
        chat, sender = msg.get("chat") or {}, msg.get("from") or {}
        if chat.get("type") == "private" and not sender.get("is_bot") and sender.get("id"):
            return {"ok": True, "id": sender["id"], "name": sender.get("first_name", "")}
    return {"ok": False, "waiting": True, "error": "No message yet. Open your bot in Telegram, press Start, send it any message, then check again."}


def save(env_file: Path, values: dict) -> dict:
    token = str(values.get("token", "")).strip()
    user, pw = str(values.get("school_user", "")).strip(), str(values.get("school_pass", ""))
    base = str(values.get("school_base") or DEFAULT_BASE).strip().rstrip("/")
    name, address = str(values.get("name", "")).strip()[:40], str(values.get("address", "")).strip()[:20] or "Sir"
    try:
        owner = int(values.get("owner_id"))
    except (TypeError, ValueError):
        owner = 0
    if not TOKEN_RE.match(token):
        return {"ok": False, "error": "The bot token is missing or invalid."}
    if owner <= 0:
        return {"ok": False, "error": "Your Telegram account has not been found yet."}
    if not user or not pw:
        return {"ok": False, "error": "Enter your POLITEMall username and password."}
    if not base.startswith("https://"):
        return {"ok": False, "error": "The POLITEMall address must start with https://"}
    updates = {"OPS_BOT_TOKEN": token, "ALLOWED_TELEGRAM_ID": str(owner), "POLITEMALL_BASE": base,
               "POLITEMALL_USER": user, "POLITEMALL_PASS": pw, "USER_NAME": name, "ADDRESS_AS": address}
    try:
        update_env_file(env_file, updates)
    except ValueError:
        return {"ok": False, "error": "Values cannot contain line breaks."}
    return {"ok": True}
