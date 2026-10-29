"""Panel view of the stored school email. Read-only: the panel can read what JARVIS has fetched, nothing more."""
from __future__ import annotations

from datetime import datetime, timezone

from core.mail import MailStore


def _cookie_saved(settings) -> bool:
    return (settings.state_dir / "sessions" / "outlook.cookies.json").exists()


def state(settings, db) -> dict:
    status = db.get_json_setting("outlook_status", {}) or {}
    stats = MailStore(db).stats()
    if not _cookie_saved(settings):
        stage = "not_set_up"
    elif db.get_setting("auth_prompted:outlook") == "1":
        stage = "login"
    elif status.get("state") == "no_access":
        stage = "no_access"
    elif status.get("state") != "ok" and stats["count"] == 0:
        stage = "waiting"
    else:
        stage = "ready"
    return {"stage": stage, "message": status.get("message", ""), "strategy": status.get("strategy"), **stats}


def _present(m: dict, tz) -> dict:
    when = datetime.fromisoformat(m["received"]).astimezone(tz)
    return {**m, "when": when.strftime("%a %d %b %H:%M"), "day": when.strftime("%Y-%m-%d"),
            "received": when.isoformat()}


def messages(settings, db, kind: str = "all", query: str = "", limit: int = 60) -> list[dict]:
    store = MailStore(db)
    if query.strip():
        rows = store.search(query, limit)
    else:
        rows = store.recent(limit, unread=kind == "unread", category="important" if kind == "important" else None)
    return [_present(m, settings.tz) for m in rows]


def message(settings, db, mail_id: str) -> dict | None:
    m = MailStore(db).get(mail_id)
    return _present(m, settings.tz) if m else None
