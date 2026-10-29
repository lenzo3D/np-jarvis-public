"""Deadline reminders at T-3d, T-1d, T-3h unless already submitted."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from core.auth import AuthRequired
from ops.format import fmt_dt, parse_iso

log = logging.getLogger(__name__)
due_thresholds = [(timedelta(days=3), "3 days"), (timedelta(days=1), "1 day"), (timedelta(hours=3), "3 hours")]


def _due_labels(due: datetime, now: datetime) -> list[str]:
    """Labels whose threshold has been reached, largest window first."""
    if now >= due:
        return []
    return [label for delta, label in due_thresholds if now >= due - delta]


def pending_reminders(rows, now_utc: datetime) -> list[tuple[str, str]]:
    out = []
    for r in rows:
        due = parse_iso(r["due_at"])
        if not due or r["submitted"]:
            continue
        reached = _due_labels(due, now_utc)
        if not reached:
            continue
        nearest = reached[-1]
        if nearest not in json.loads(r["reminders_sent_json"] or "[]"):
            out.append((r["item_key"], nearest))
    return out


async def run_reminders(ctx) -> None:
    db, now = ctx.db, ctx.clock()
    if db.get_setting("paused") == "1":
        return  # reminders are not marked sent, so they fire on resume if still relevant
    rows = db.conn.execute("select * from due_dates where due_at is not null and submitted=0").fetchall()
    for key, label in pending_reminders(rows, now):
        item = db.conn.execute("select * from items where stable_id=?", (key,)).fetchone()
        if not item:
            continue
        payload = json.loads(item["payload_json"])
        try:
            if await ctx.bs.submitted(payload["ou"], payload["folder_id"]):
                db.conn.execute("update due_dates set submitted=1 where item_key=?", (key,))
                continue
        except AuthRequired:
            pass  # still remind; the sweep handles the login prompt
        except Exception:  # noqa: BLE001
            log.exception("submission check failed for %s", key)
        due = parse_iso(next(r["due_at"] for r in rows if r["item_key"] == key))
        await ctx.notifier.send(
            f"Reminder: {item['course_code']}: \"{item['title']}\" due {fmt_dt(due, ctx.settings.tz)} ({label} left)",
            [[("Do it", f"doit:{key}")]],
        )
        # mark this and every larger window as sent so a late start never sends stale ones
        sent = _due_labels(due, now)
        db.conn.execute("update due_dates set reminders_sent_json=? where item_key=?", (json.dumps(sent), key))
