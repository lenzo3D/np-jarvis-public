"""Deadlines store (spec: docs/superpowers/specs/2026-10-27-deadlines-calendar-design.md).

start_utc is an ISO UTC datetime for timed deadlines, or a plain SGT date 'YYYY-MM-DD' when all_day.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from core.db import DB

SGT = ZoneInfo("Asia/Singapore")
DEADLINE_KINDS = {"assignment", "homework", "other"}
STOPWORDS = {"the", "a", "an", "for", "of", "to", "and", "on", "in", "due", "submission", "submit", "by"}
CONTENT_FIELDS = ("title", "course_code", "kind", "start_utc", "end_utc", "all_day", "source", "source_ref",
                  "confidence", "quote")


@dataclass
class Deadline:
    key: str
    title: str
    course_code: str | None
    kind: str
    start_utc: str
    end_utc: str | None
    all_day: bool
    source: str
    source_ref: str
    confidence: str
    quote: str
    status: str = "active"
    gcal_event_id: str | None = None
    gcal_hash: str | None = None


def local_date(d: Deadline, tz=SGT) -> date:
    """The day a deadline belongs to: the due moment (end) for deadline kinds, else the start."""
    if d.all_day:
        return date.fromisoformat(d.start_utc[:10])
    moment = d.end_utc if d.end_utc and d.kind in DEADLINE_KINDS else d.start_utc
    return datetime.fromisoformat(moment).astimezone(tz).date()


def _norm_words(title: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", title.lower()) if w not in STOPWORDS]


def extracted_key(course: str | None, kind: str, title: str, day: str) -> str:
    blob = f"{course or ''}|{kind}|{' '.join(_norm_words(title))}|{day}"
    return "x:" + hashlib.sha1(blob.encode()).hexdigest()[:16]


def _numbers(words: set[str]) -> set[str]:
    return {w for w in words if w.isdigit()}


def _course_words(code: str | None) -> set[str]:
    return set(re.findall(r"[a-z]+", (code or "").lower()))


def similar(d: Deadline, others: list[Deadline]) -> bool:
    """Same day, compatible course (equal, or unknown on either side), same numbers ('Quiz 1' ≠ 'Quiz 2'),
    and word overlap ≥ 50% (Jaccard, ignoring course-code words like 'tmbi')."""
    for o in others:
        if o.key == d.key or local_date(o) != local_date(d):
            continue
        if d.course_code and o.course_code and d.course_code != o.course_code:
            continue
        noise = _course_words(d.course_code) | _course_words(o.course_code)
        a, b = set(_norm_words(d.title)) - noise, set(_norm_words(o.title)) - noise
        if not a or not b or _numbers(a) != _numbers(b):
            continue
        if len(a & b) / len(a | b) >= 0.5:
            return True
    return False


def duplicates_confirmed(d: Deadline, confirmed: list[Deadline]) -> bool:
    return similar(d, confirmed)


class DeadlineStore:
    def __init__(self, db: DB):
        self.db = db

    def _row(self, r) -> Deadline:
        names = {f.name for f in fields(Deadline)}
        d = Deadline(**{k: r[k] for k in r.keys() if k in names})
        d.all_day = bool(d.all_day)
        return d

    def get(self, key: str) -> Deadline | None:
        r = self.db.conn.execute("select * from deadlines where key=?", (key,)).fetchone()
        return self._row(r) if r else None

    def upsert(self, d: Deadline, now: datetime) -> bool:
        """Insert or update content. Returns True if anything changed. user_deleted rows are left alone."""
        cur = self.get(d.key)
        if cur and cur.status == "user_deleted":
            return False
        values = {k: getattr(d, k) for k in CONTENT_FIELDS}
        values["all_day"] = int(bool(values["all_day"]))
        if cur is None:
            self.db.conn.execute(
                f"insert into deadlines(key, {', '.join(CONTENT_FIELDS)}, status, updated_at) "
                f"values (?, {', '.join('?' for _ in CONTENT_FIELDS)}, 'active', ?)",
                (d.key, *values.values(), now.isoformat()))
            return True
        old = {k: getattr(cur, k) for k in CONTENT_FIELDS}
        old["all_day"] = int(bool(old["all_day"]))
        if old == values and cur.status == "active":
            return False
        self.db.conn.execute(
            f"update deadlines set {', '.join(f'{k}=?' for k in CONTENT_FIELDS)}, status='active', updated_at=? "
            "where key=?", (*values.values(), now.isoformat(), d.key))
        return True

    def active(self) -> list[Deadline]:
        return [self._row(r) for r in self.db.conn.execute(
            "select * from deadlines where status='active' order by start_utc")]

    def by_status(self, status: str) -> list[Deadline]:
        return [self._row(r) for r in self.db.conn.execute("select * from deadlines where status=?", (status,))]

    def mark_gone(self, key: str) -> None:
        self.db.conn.execute("update deadlines set status='gone' where key=? and status='active'", (key,))

    def needing_sync(self) -> list[Deadline]:
        return [self._row(r) for r in self.db.conn.execute(
            "select * from deadlines where status='active' or (status='gone' and gcal_event_id is not null)")]

    def mark_gone_missing(self, source: str, keys_seen: set[str]) -> int:
        rows = self.db.conn.execute("select key from deadlines where source=? and status='active'", (source,))
        gone = [r["key"] for r in rows if r["key"] not in keys_seen]
        for k in gone:
            self.db.conn.execute("update deadlines set status='gone' where key=?", (k,))
        return len(gone)

    def set_event(self, key: str, event_id: str | None, h: str | None) -> None:
        self.db.conn.execute("update deadlines set gcal_event_id=?, gcal_hash=? where key=?", (event_id, h, key))

    def mark_user_deleted(self, key: str) -> None:
        self.db.conn.execute(
            "update deadlines set status='user_deleted', gcal_event_id=null, gcal_hash=null where key=?", (key,))

    def is_user_deleted(self, key: str) -> bool:
        d = self.get(key)
        return bool(d and d.status == "user_deleted")

    def upcoming(self, now: datetime, days: int, tz=SGT) -> list[Deadline]:
        today = now.astimezone(tz).date()
        horizon = today + timedelta(days=days)
        out = [d for d in self.active() if today <= local_date(d, tz) <= horizon]
        return sorted(out, key=lambda d: (local_date(d, tz), d.start_utc))


def as_dict(d: Deadline) -> dict:
    return asdict(d)
