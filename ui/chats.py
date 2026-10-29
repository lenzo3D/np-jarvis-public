"""Panel views of the Telegram groups JARVIS reads (read-only; there is no way to send from here)."""
from __future__ import annotations

from datetime import datetime, timezone

from core import groups, tg_group
from ops import telethon_main

HEARTBEAT_OK = 180  # seconds


def _age(iso: str | None) -> int | None:
    if not iso:
        return None
    return max(0, int((datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()))


def telegram_state(settings, db, ops_running: bool) -> dict:
    """Where the user is in the setup (setup -> pick -> ready) and whether messages are being collected.

    Two ways in: add the JARVIS bot to the group (the bot only reads), or log in with your own account (Telethon).
    `telethon` says how far the second one is: no_api_keys -> login -> ready."""
    keys = bool(settings.telethon_api_id and settings.telethon_api_hash)
    has_session = telethon_main.session_path(settings).exists()
    status = db.get_json_setting("telethon_status", {}) or {}
    chosen = groups.configured(settings, db)
    stage = "ready" if chosen else "pick" if groups.dialogs(db) else "setup"
    telethon = "no_api_keys" if not keys else "login" if not has_session else "ready"
    age = _age(status.get("at"))
    bot_ids = {g["id"] for g in groups.bot_groups(db)}
    bot_covers = any(g["id"] in bot_ids for g in chosen)
    if stage == "setup":
        health = "setup"
    elif not ops_running:
        health = "offline"
    elif bot_covers or (telethon == "ready" and status.get("state") == "listening" and age is not None and age <= HEARTBEAT_OK):
        health = "live"
    elif telethon == "ready" and status.get("state") == "login_needed":
        health = "login"
    elif telethon == "ready" and status.get("state") == "error":
        health = "failing"
    else:
        health = "stale"
    return {"stage": stage, "telethon": telethon, "health": health, "listener": status.get("state"),
            "heartbeat_age": age, "groups": len(chosen), "bot_groups": len(bot_ids)}


def overview(settings, db, ops_running: bool) -> dict:
    stats = tg_group.stats(db)
    out = {**telegram_state(settings, db, ops_running), "dialogs": groups.dialogs(db), "chosen": []}
    for g in groups.configured(settings, db):
        st = stats.get(g["id"], {})
        out["chosen"].append({**g, "messages": st.get("count", 0), "last": st.get("last")})
    return out


def messages(settings, db, chat_id: int, query: str = "", limit: int = 150) -> dict:
    if chat_id not in groups.ids(settings, db):
        return {"ok": False, "message": "That group is not one JARVIS reads."}
    tz = settings.tz
    limit = max(1, min(limit, 300))
    msgs = tg_group.search(db, query, limit, chat_id)[::-1] if query.strip() else tg_group.latest(db, chat_id, limit)
    return {"ok": True, "label": groups.label_for(settings, db, chat_id), "query": query,
            "messages": [{"id": m.msg_id, "sender": m.sender or "?", "text": m.text, "day": m.ts.astimezone(tz).strftime("%a %d %b"),
                          "time": m.ts.astimezone(tz).strftime("%H:%M")} for m in msgs]}
