"""Events the user asked JARVIS (in chat) to put on their calendar: meetings, appointments, reminders to self.

They live in the deadlines table (source "mine", confirmed) so the normal Google Calendar sync adds, updates and
removes them, and the Calendar tab and reminders see them. Only rows with the "mine:" key can be removed this way.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta

from core.deadlines import Deadline, DeadlineStore
from core.project_tasks import clean_text

MINE_KEY = "mine:"
SOURCE = "mine"
KINDS = {"meeting": "meeting", "event": "other"}  # what the tool accepts -> stored kind
MAX_AHEAD = timedelta(days=400)


class EventError(ValueError):
    pass


def _parse(value: str, tz) -> datetime:
    try:
        dt = datetime.fromisoformat(str(value).strip())
    except ValueError:
        raise EventError(f"not a date and time: {value!r} (use e.g. 2026-11-10T16:00)") from None
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt  # no offset given: Singapore time


def key_for(title: str, start: str) -> str:
    return MINE_KEY + hashlib.sha1(f"{title.lower()}|{start}".encode()).hexdigest()[:16]


def build(title: str, start: str, now: datetime, tz, end: str | None = None, all_day: bool = False,
          kind: str = "event", note: str = "") -> Deadline:
    title = clean_text(title, urls=True)[:120]
    if not title:
        raise EventError("a title is required")
    if kind not in KINDS:
        raise EventError(f"kind must be one of {sorted(KINDS)}")
    if all_day:
        try:
            day = date.fromisoformat(str(start).strip()[:10])
        except ValueError:
            raise EventError(f"not a date: {start!r} (use e.g. 2026-11-10)") from None
        if day < now.astimezone(tz).date() or day > (now + MAX_AHEAD).date():
            raise EventError("the date must be today or later, and within about a year")
        start_utc, end_utc = day.isoformat(), None
    else:
        s = _parse(start, tz)
        e = _parse(end, tz) if end else None
        if s < now - timedelta(hours=1) or s > now + MAX_AHEAD:
            raise EventError("the start must be in the future, and within about a year")
        if e is not None and not (s < e <= s + timedelta(days=7)):
            raise EventError("the end must be after the start (and within a week of it)")
        start_utc = s.astimezone(tz).isoformat()
        end_utc = e.astimezone(tz).isoformat() if e else None
    return Deadline(key=key_for(title, start_utc), title=title, course_code=None, kind=KINDS[kind],
                    start_utc=start_utc, end_utc=end_utc, all_day=all_day, source=SOURCE, source_ref="chat",
                    confidence="confirmed", quote=clean_text(note, urls=True)[:300], as_event=True)


def add(db, d: Deadline, now: datetime) -> None:
    store = DeadlineStore(db)
    if store.is_user_deleted(d.key):  # deleted in Google before, but asked for again: a fresh start
        db.conn.execute("delete from deadlines where key=?", (d.key,))
    store.upsert(d, now)


def remove(db, key: str) -> bool:
    if not key.startswith(MINE_KEY):
        return False
    d = DeadlineStore(db).get(key)
    if d is None or d.status != "active":
        return False
    DeadlineStore(db).mark_gone(key)
    return True


def listing(db) -> list[Deadline]:
    return [d for d in DeadlineStore(db).active() if d.key.startswith(MINE_KEY)]
