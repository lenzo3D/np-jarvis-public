"""Which Telegram groups JARVIS reads and what they are called.

The user picks groups in the panel (stored in the database); GROUP_CHAT_IDS in secrets.env is the fallback.
Each group has a label ("Class", "Project team") and a kind: class, project or other.
"""
from __future__ import annotations

KINDS = ("class", "project", "other")
MAX_GROUPS = 10
DEFAULT_LABEL = "Class chat"  # used when messages exist but no group has been configured at all


def configured(settings, db) -> list[dict]:
    chosen = db.get_json_setting("group_chats", None)
    if isinstance(chosen, list):
        return [g for g in (_clean(x) for x in chosen) if g]
    out = []
    for n, chat_id in enumerate(settings.group_chat_ids):
        label = settings.group_labels.get(chat_id) or ("Class chat" if n == 0 else f"Group {n + 1}")
        out.append({"id": chat_id, "label": label, "kind": "class" if n == 0 else "other"})
    return out


def _clean(g) -> dict | None:
    try:
        chat_id = int(g["id"])
        label = str(g.get("label") or "").strip()[:40]
        kind = g.get("kind") if g.get("kind") in KINDS else "other"
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return {"id": chat_id, "label": label or f"Group {abs(chat_id) % 1000}", "kind": kind}


def save(db, groups: list[dict]) -> list[dict]:
    cleaned, seen = [], set()
    for g in groups[:MAX_GROUPS]:
        c = _clean(g)
        if c and c["id"] not in seen:
            seen.add(c["id"])
            cleaned.append(c)
    db.set_json_setting("group_chats", cleaned)
    return cleaned


def ids(settings, db) -> list[int]:
    return [g["id"] for g in configured(settings, db)]


def label_for(settings, db, chat_id: int) -> str:
    for g in configured(settings, db):
        if g["id"] == chat_id:
            return g["label"]
    return f"Group {abs(chat_id) % 1000}"


def find(settings, db, ref: str | int | None) -> dict | None:
    """Match a group by id, label or kind ('project')."""
    if ref in (None, ""):
        return None
    groups = configured(settings, db)
    text = str(ref).strip().lower()
    for g in groups:
        if str(g["id"]) == text or g["label"].lower() == text:
            return g
    for g in groups:
        if g["kind"] == text or text in g["label"].lower():
            return g
    return None


def bot_groups(db) -> list[dict]:
    """Groups the Ops bot has been added to by the owner (id + title), kept apart from the Telethon dialog list."""
    return db.get_json_setting("bot_groups", [])


def add_bot_group(db, chat_id: int, title: str) -> None:
    current = [g for g in bot_groups(db) if g["id"] != int(chat_id)]
    current.append({"id": int(chat_id), "name": (title or f"Group {abs(int(chat_id)) % 1000}")[:80]})
    db.set_json_setting("bot_groups", current[:100])


def remove_bot_group(db, chat_id: int) -> None:
    db.set_json_setting("bot_groups", [g for g in bot_groups(db) if g["id"] != int(chat_id)])


def save_dialogs(db, dialogs: list[dict]) -> None:
    db.set_json_setting("telegram_dialogs", [{"id": int(d["id"]), "name": str(d["name"])[:80]} for d in dialogs][:300])


def dialogs(db) -> list[dict]:
    """Every group you could choose: your own Telegram groups (if the account reader is set up) plus groups the bot is in."""
    seen, out = set(), []
    for d in db.get_json_setting("telegram_dialogs", []) + bot_groups(db):
        if d["id"] not in seen:
            seen.add(d["id"])
            out.append(d)
    return out
