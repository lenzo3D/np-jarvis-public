"""Delivery policy: pause, mutes, quiet hours, batching."""
from __future__ import annotations

from datetime import datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

from core.db import DB

SGT = ZoneInfo("Asia/Singapore")
Decision = Literal["send", "batch", "drop", "briefing"]


def in_quiet_hours(now_local: time, spec: str | None) -> bool:
    if not spec:
        return False
    start_s, end_s = spec.split("-")
    start, end = int(start_s), int(end_s)
    h = now_local.hour
    if start == end:
        return False
    if start < end:
        return start <= h < end
    return h >= start or h < end


def decide(level: str, course_code: str | None, db: DB, now_utc: datetime, tz=SGT) -> Decision:
    if db.get_setting("paused") == "1":
        return "drop"
    if level == "red":
        return "send"
    if course_code and course_code in db.get_json_setting("muted", []):
        return "drop"
    if level == "yellow":
        return "batch"
    return "briefing"


def quiet_now(db: DB, now_utc: datetime, tz=SGT) -> bool:
    return in_quiet_hours(now_utc.astimezone(tz).time(), db.get_setting("quiet"))
