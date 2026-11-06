"""Turn a Deadline into a Google Calendar event body (spec: Calendar events section)."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta

from core.deadlines import Deadline, is_due
from core.project_tasks import clean_text

TEST_KINDS = {"quiz", "test", "exam", "presentation"}
KIND_LABEL = {"assignment": "Assignment due", "homework": "Homework due", "quiz": "Quiz", "test": "Test",
              "exam": "Exam", "presentation": "Presentation", "meeting": "Meeting", "other": "Deadline"}
CONFIRMED_ICON, TENTATIVE_ICON = "\U0001F4CC", "\u2753"  # pushpin: certain; question mark: unverified
SOURCE_LABEL = {"brightspace": "POLITEMall", "announcement": "POLITEMall announcement",
                "document": "course document", "teams": "Teams", "group": "Telegram group", "email": "School email",
                "project": "Project group chat", "mine": "You asked JARVIS to add it"}


def short_course(name: str, code: str | None) -> str:
    if name:
        base = re.sub(r"\s*\(.*?\)\s*", " ", name).strip()
        if base:
            return " ".join(w if w in {"&", "-"} else w[:1].upper() + w[1:].lower() for w in base.split())
    if code:
        c = re.sub(r"^\d{2}S\d-", "", code)
        return re.sub(r"_\d+(-ET)?$", "", c)
    return "School"


def _reminders(kind: str, as_event: bool = False) -> dict:
    if kind == "meeting":
        mins = [30]
    elif kind in TEST_KINDS:
        mins = [1440]
    elif kind == "other" and not is_due(kind, as_event):
        mins = [1440, 60]  # a timed event with no kind: a day before and an hour before
    else:
        mins = [1440, 180]
    return {"useDefault": False, "overrides": [{"method": "popup", "minutes": m} for m in mins]}


def _dt(iso: str, tz) -> dict:
    return {"dateTime": datetime.fromisoformat(iso).astimezone(tz).isoformat(), "timeZone": tz.key}


def event_body(d: Deadline, course_name: str, tz) -> dict:
    prefix = CONFIRMED_ICON if d.confidence == "confirmed" else TENTATIVE_ICON
    tentative = d.confidence != "confirmed"
    # text read by a model from other people's messages: no links (a calendar invite can be a phishing vehicle)
    title = clean_text(d.title, urls=True) if tentative else d.title
    quote = clean_text(d.quote, urls=True) if tentative else d.quote
    own = d.source in ("project", "mine")
    label = {"project": "Project group", "mine": "Personal"}.get(d.source) or short_course(course_name, d.course_code)
    summary = (f"{prefix} {title}" if own else f"{prefix} {label}: {title}")[:250]
    if d.all_day:
        day = date.fromisoformat(d.start_utc[:10])
        start, end = {"date": day.isoformat()}, {"date": (day + timedelta(days=1)).isoformat()}
    else:
        s = datetime.fromisoformat(d.start_utc)
        if d.end_utc:
            e = datetime.fromisoformat(d.end_utc)
        elif is_due(d.kind, d.as_event):
            s, e = s - timedelta(minutes=30), s
        else:
            e = s + timedelta(hours=1)  # default length of a timed event that has no end
        start, end = _dt(s.isoformat(), tz), _dt(e.isoformat(), tz)
    kind_label = "Event" if d.kind == "other" and not is_due(d.kind, d.as_event) else KIND_LABEL.get(d.kind, "Deadline")
    lines = [f"{kind_label} — {label}",
             f"Source: {SOURCE_LABEL.get(d.source, d.source)}"]
    if d.confidence != "confirmed":
        lines.append(f"{TENTATIVE_ICON} Tentative: read from text by JARVIS — double-check the date.")
    if quote:
        lines.append(f"Note: {quote[:300]}" if d.source == "mine" else f'Quote: "{quote[:300]}"')
    if d.source_ref.startswith("http"):
        lines.append(d.source_ref)
    lines.append("Added by JARVIS — edits to this event may be overwritten; delete it to stop JARVIS re-adding it.")
    return {
        "summary": summary,
        "description": "\n".join(lines),
        "start": start,
        "end": end,
        "reminders": _reminders(d.kind, d.as_event),
        "extendedProperties": {"private": {"jarvis": "1", "jarvis_key": d.key}},
    }


def event_hash(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]
