"""Turn a Deadline into a Google Calendar event body (spec: Calendar events section)."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta

from core.deadlines import Deadline

DEADLINE_KINDS = {"assignment", "homework", "other"}
TEST_KINDS = {"quiz", "test", "exam", "presentation"}
KIND_LABEL = {"assignment": "Assignment due", "homework": "Homework due", "quiz": "Quiz", "test": "Test",
              "exam": "Exam", "presentation": "Presentation", "meeting": "Meeting", "other": "Deadline"}
CONFIRMED_ICON, TENTATIVE_ICON = "\U0001F4CC", "\u2753"  # pushpin: certain; question mark: unverified
SOURCE_LABEL = {"brightspace": "POLITEMall", "announcement": "POLITEMall announcement",
                "document": "course document", "teams": "Teams", "group": "Telegram group", "email": "School email"}


def short_course(name: str, code: str | None) -> str:
    if name:
        base = re.sub(r"\s*\(.*?\)\s*", " ", name).strip()
        if base:
            return " ".join(w if w in {"&", "-"} else w[:1].upper() + w[1:].lower() for w in base.split())
    if code:
        c = re.sub(r"^\d{2}S\d-", "", code)
        return re.sub(r"_\d+(-ET)?$", "", c)
    return "School"


def _reminders(kind: str) -> dict:
    if kind == "meeting":
        mins = [30]
    elif kind in TEST_KINDS:
        mins = [1440]
    else:
        mins = [1440, 180]
    return {"useDefault": False, "overrides": [{"method": "popup", "minutes": m} for m in mins]}


def _dt(iso: str, tz) -> dict:
    return {"dateTime": datetime.fromisoformat(iso).astimezone(tz).isoformat(), "timeZone": tz.key}


def event_body(d: Deadline, course_name: str, tz) -> dict:
    prefix = CONFIRMED_ICON if d.confidence == "confirmed" else TENTATIVE_ICON
    summary = f"{prefix} {short_course(course_name, d.course_code)}: {d.title}"[:250]
    if d.all_day:
        day = date.fromisoformat(d.start_utc[:10])
        start, end = {"date": day.isoformat()}, {"date": (day + timedelta(days=1)).isoformat()}
    else:
        s = datetime.fromisoformat(d.start_utc)
        if d.end_utc:
            e = datetime.fromisoformat(d.end_utc)
        elif d.kind in DEADLINE_KINDS:
            s, e = s - timedelta(minutes=30), s
        else:
            e = s + timedelta(hours=1)
        start, end = _dt(s.isoformat(), tz), _dt(e.isoformat(), tz)
    lines = [f"{KIND_LABEL.get(d.kind, 'Deadline')} — {short_course(course_name, d.course_code)}",
             f"Source: {SOURCE_LABEL.get(d.source, d.source)}"]
    if d.confidence != "confirmed":
        lines.append(f"{TENTATIVE_ICON} Tentative: read from text by JARVIS — double-check the date.")
    if d.quote:
        lines.append(f'Quote: "{d.quote[:300]}"')
    if d.source_ref.startswith("http"):
        lines.append(d.source_ref)
    lines.append("Added by JARVIS — edits to this event may be overwritten; delete it to stop JARVIS re-adding it.")
    return {
        "summary": summary,
        "description": "\n".join(lines),
        "start": start,
        "end": end,
        "reminders": _reminders(d.kind),
        "extendedProperties": {"private": {"jarvis": "1", "jarvis_key": d.key}},
    }


def event_hash(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]
