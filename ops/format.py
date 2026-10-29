"""Plain-text rendering for Telegram messages."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from core.text import html_to_text  # noqa: F401  (re-exported)
from ops.diff import Change

LEVEL_LABEL = {"red": "Urgent", "yellow": "Update", "white": "Info"}
KIND_LABEL = {
    "assignment": "assignment", "announcement": "announcement", "content": "new material",
    "grade": "grade released", "teams_post": "Teams post", "teams_dm": "Teams message",
    "teams_mention": "Teams mention", "group_action": "class chat", "email": "email",
}


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt_dt(dt_utc: datetime, tz) -> str:
    return dt_utc.astimezone(tz).strftime("%a %d %b %H:%M")


def countdown(due: datetime, now: datetime) -> str:
    delta = due - now
    secs = delta.total_seconds()
    if secs < 0:
        return "(overdue)"
    if secs < 86400:
        return f"({int(secs // 3600)}h left)"
    days = int(secs // 86400)
    return f"({days} day{'s' if days != 1 else ''})"


def _course_label(code: str | None, names: dict[str, str] | None) -> str:
    from ops.gcal_events import short_course

    return short_course((names or {}).get(code or "", ""), code) if code else ""


BODY_LIMIT = 1500  # stored bodies are already plain text (capped at 1500-2000 on fetch); don't cut them harder here


def _clip(text: str, limit: int = BODY_LIMIT) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip() + "…"


def alert_text(change: Change, level: str, tz, now: datetime | None = None,
               course_names: dict[str, str] | None = None) -> str:
    item = change.item
    if item.kind == "email":
        p = item.payload
        who = p.get("sender") or p.get("address") or "unknown sender"
        lines = [f"[{LEVEL_LABEL[level]}] Email from {who}", item.title]
        if p.get("preview"):
            lines.append(_clip(p["preview"], 400))
        return "\n".join(lines)
    label = KIND_LABEL.get(item.kind, item.kind)
    verb = "updated " if change.kind == "changed" else "new " if item.kind in ("assignment", "announcement") else ""
    what = (verb + label).capitalize()
    course = _course_label(item.course_code, course_names)
    head = f"[{LEVEL_LABEL[level]}] {course} · {what}" if course else f"[{LEVEL_LABEL[level]}] {what}"
    lines = [head, item.title]
    if item.kind == "content":
        if item.payload.get("module"):
            lines.append(f"Location: {item.payload['module']}")
        if item.payload.get("type"):
            lines.append(f"Type: {item.payload['type']}")
    if item.kind == "assignment":
        due = parse_iso(item.payload.get("due_at")) or parse_iso(item.payload.get("end_at"))
        if "due_at" in change.changed_fields:
            old, new = (parse_iso(v) for v in change.changed_fields["due_at"])
            lines.append(f"Due date changed: {fmt_dt(old, tz) if old else 'none'} → {fmt_dt(new, tz) if new else 'none'}")
        elif due:
            lines.append(f"Due {fmt_dt(due, tz)} {countdown(due, now) if now else ''}".rstrip())
        else:
            lines.append("No due date set")
    body = item.payload.get("body_text")
    if body:
        lines.append(_clip(body))
    if item.kind == "grade" and item.payload.get("value") is not None:
        out_of = item.payload.get("out_of")
        lines.append(f"Grade: {item.payload['value']}" + (f" / {out_of}" if out_of else ""))
    return "\n".join(lines)
