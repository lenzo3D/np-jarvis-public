"""Submission safety net: an escalating reminder ladder for Brightspace assignments, a missed-deadline note, and
two reminders for other confirmed deadlines (quizzes, tests).

Assignments: 3 days, 1 day, 6 hours, 3 hours, 1 hour, 30 minutes before the due time, until a submission is
confirmed. Only the nearest window reached is sent, and every larger window is marked sent with it, so a restart or a
late start never sends a burst of stale reminders. Inside the last 6 hours reminders are urgent: they ignore quiet
hours and course mutes (only /pause stops them). The submission is re-checked before every reminder; if the check
cannot run the reminder still goes out and says so.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from core.auth import AuthRequired
from core.deadlines import DeadlineStore, is_due
from ops import policy
from ops.deadline_sources import fresh_cutoff
from ops.format import fmt_dt, parse_iso

log = logging.getLogger(__name__)
due_thresholds = [(timedelta(days=3), "3 days"), (timedelta(days=1), "1 day"), (timedelta(hours=6), "6 hours"),
                  (timedelta(hours=3), "3 hours"), (timedelta(hours=1), "1 hour"), (timedelta(minutes=30), "30 minutes")]
deadline_thresholds = [(timedelta(days=1), "1 day"), (timedelta(hours=3), "3 hours")]  # confirmed non-dropbox deadlines
URGENT = timedelta(hours=6)  # inside this, reminders ignore quiet hours and mutes
NOTE_WINDOW = timedelta(hours=24)  # "Submission confirmed" is only worth a message this close to the deadline
LAST_STRETCH = timedelta(minutes=30)  # snooze never reaches into this
SNOOZE = timedelta(hours=1)
MISSED_WINDOW = timedelta(hours=12)  # older misses (e.g. after long downtime) are recorded silently


def _col(r, name, default=None):
    try:
        v = r[name]
    except (KeyError, IndexError):
        return default
    return default if v is None else v


def _due_labels(due: datetime, now: datetime, thresholds=None) -> list[str]:
    """Labels whose threshold has been reached, largest window first."""
    if now >= due:
        return []
    return [label for delta, label in (thresholds or due_thresholds) if now >= due - delta]


def pending_reminders(rows, now_utc: datetime) -> list[tuple[str, str]]:
    out = []
    for r in rows:
        due = parse_iso(r["due_at"])
        if not due or r["submitted"] or _col(r, "user_confirmed", 0):
            continue
        reached = _due_labels(due, now_utc)
        if not reached:
            continue
        nearest = reached[-1]
        if nearest not in json.loads(r["reminders_sent_json"] or "[]"):
            out.append((r["item_key"], nearest))
    return out


def time_left(delta: timedelta) -> str:
    mins = max(int(delta.total_seconds() // 60), 0)
    if mins >= 48 * 60:
        return f"{mins // 1440} days"
    if mins >= 60:
        h, m = divmod(mins, 60)
        return f"{h}h {m}m" if m else f"{h}h"
    return f"{mins} min"


async def _check(ctx, payload: dict, key: str) -> bool | None:
    """True = Brightspace confirms a submission, False = confirmed none, None = could not check."""
    try:
        return bool(await ctx.bs.submitted(payload["ou"], payload["folder_id"]))
    except AuthRequired:
        return None  # still remind; the sweep handles the login prompt
    except Exception:  # noqa: BLE001
        log.exception("submission check failed for %s", key)
        return None


def _blocked_by_policy(db, course: str | None, now: datetime, tz) -> bool:
    """Non-urgent reminders wait out quiet hours and muted courses (they are sent once that ends)."""
    return policy.quiet_now(db, now, tz) or policy.is_muted(db, course)


def _buttons(key: str, left: timedelta) -> list[list[tuple[str, str]]]:
    rows = [[("Do it", f"doit:{key}")]]
    if left <= URGENT and left > LAST_STRETCH:  # snooze is pointless once it cannot reach past the last stretch
        rows.append([("Snooze 1h", f"rem:snooze:{key}"), ("I submitted elsewhere", f"rem:done:{key}")])
    else:
        rows.append([("I submitted elsewhere", f"rem:done:{key}")])
    return rows


async def _send_confirmed_note(ctx, item, due) -> None:
    await ctx.notifier.send(f"Submission confirmed: {item['course_code']}: \"{item['title']}\" "
                            f"(due {fmt_dt(due, ctx.settings.tz)}). No more reminders for it.")


def _missed_text(ctx, item, payload: dict, due: datetime, checked: bool | None) -> str:
    tz = ctx.settings.tz
    text = f"Missed: {item['course_code']}: \"{item['title']}\" was due {fmt_dt(due, tz)}. "
    text += "No submission found." if checked is False else "Could not confirm submission."
    end = parse_iso(payload.get("end_at"))
    if payload.get("due_at") and end and end > due:
        text += f" The folder stays open until {fmt_dt(end, tz)}, so you can still submit late."
    return text


def _set(db, key: str, **cols) -> None:
    db.conn.execute(f"update due_dates set {', '.join(f'{c}=?' for c in cols)} where item_key=?", (*cols.values(), key))


async def run_reminders(ctx) -> None:
    db, now = ctx.db, ctx.clock()
    if db.get_setting("paused") == "1":
        return  # nothing is marked sent, so reminders fire on resume if still relevant
    force = db.get_setting("resume_remind") == "1"  # set by /resume: re-send anything due in the next 6 h once
    if force:
        db.set_setting("resume_remind", "0")
    for r in db.conn.execute("select * from due_dates where due_at is not null").fetchall():
        try:
            await _one(ctx, r, now, force)
        except Exception:  # noqa: BLE001 - one bad row must not stop the rest
            log.exception("reminder failed for %s", r["item_key"])
    await run_deadline_reminders(ctx, force)


async def _one(ctx, r, now: datetime, force: bool) -> None:
    db, tz, key = ctx.db, ctx.settings.tz, r["item_key"]
    due = parse_iso(r["due_at"])
    item = db.conn.execute("select * from items where stable_id=?", (key,)).fetchone()
    if not due or not item:
        return
    if item["last_seen"] < fresh_cutoff(db):
        return  # folder deleted or replaced: no ladder, no Missed message
    payload = json.loads(item["payload_json"])
    left = due - now
    confirmed, by_user = bool(r["submitted"]), bool(_col(r, "user_confirmed", 0))

    if left <= timedelta(0):  # past due
        if _col(r, "missed_sent", 0) or confirmed or by_user:
            return
        if -left > MISSED_WINDOW:
            _set(db, key, missed_sent=1)
            return
        checked = await _check(ctx, payload, key)
        if checked:
            _set(db, key, submitted=1, confirm_note_sent=1)
            return
        await ctx.notifier.send(_missed_text(ctx, item, payload, due, checked), [[("Do it", f"doit:{key}")]])
        _set(db, key, missed_sent=1)
        return

    urgent = left <= URGENT
    if confirmed:  # silent, except one short note the first time it is seen inside the final 24 h
        if (not _col(r, "confirm_note_sent", 0) and left <= NOTE_WINDOW
                and (urgent or not _blocked_by_policy(db, item["course_code"], now, tz))):
            await _send_confirmed_note(ctx, item, due)
            _set(db, key, confirm_note_sent=1)
        return
    if by_user:
        return

    reached = _due_labels(due, now)
    if not reached:
        return
    sent = json.loads(r["reminders_sent_json"] or "[]")
    forced = force and urgent
    if reached[-1] in sent and not forced:
        return
    snooze_until = parse_iso(_col(r, "snooze_until"))
    if snooze_until and now < snooze_until and left > LAST_STRETCH and not forced:
        return
    if not urgent and _blocked_by_policy(db, item["course_code"], now, tz):
        return

    checked = await _check(ctx, payload, key)  # re-checked before every reminder
    if checked:
        _set(db, key, submitted=1, confirm_note_sent=1)
        if left <= NOTE_WINDOW:
            await _send_confirmed_note(ctx, item, due)
        return
    status = "Could not confirm submission." if checked is None else "Not submitted yet."
    head = "[Urgent] " if urgent else "Reminder: "
    await ctx.notifier.send(
        f"{head}{item['course_code']}: \"{item['title']}\" due {fmt_dt(due, tz)} ({time_left(left)} left). {status}",
        _buttons(key, left))
    # mark this and every larger window as sent so a late start never sends stale ones
    _set(db, key, reminders_sent_json=json.dumps(reached))


# ---------------------------------------------------------------- buttons

def snooze(ctx, key: str) -> str:
    """Quiet the reminders for an hour, but never into the last 30 minutes before the deadline."""
    now = ctx.clock()
    r = ctx.db.conn.execute("select * from due_dates where item_key=?", (key,)).fetchone()
    due = parse_iso(r["due_at"]) if r else None
    if not due:
        return "That deadline was not found."
    if due - now <= LAST_STRETCH:
        return "No snooze in the last 30 minutes before the deadline."
    until = min(now + SNOOZE, due - LAST_STRETCH)
    _set(ctx.db, key, snooze_until=until.isoformat())
    return f"Snoozed until {fmt_dt(until, ctx.settings.tz)}. I will remind you again in the last 30 minutes at the latest."


def submitted_elsewhere(ctx, key: str) -> str:
    """The user says it is handed in (by email, on paper). Stops reminders; POLITEMall stays unconfirmed."""
    if not ctx.db.conn.execute("select 1 from due_dates where item_key=?", (key,)).fetchone():
        return "That deadline was not found."
    _set(ctx.db, key, user_confirmed=1)
    return ("Noted: marked as submitted elsewhere, so no more reminders. /deadlines will still show that POLITEMall "
            "has not confirmed it.")


# ---------------------------------------------------------------- other confirmed deadlines (quizzes, tests)

async def run_deadline_reminders(ctx, force: bool = False) -> None:
    """1 day and 3 hours before confirmed deadlines that are not Brightspace dropboxes (those have the full ladder
    above). All-day entries are skipped: they have no time to count down to. No buttons, no nagging."""
    db, now, tz = ctx.db, ctx.clock(), ctx.settings.tz
    for d in DeadlineStore(db).active():
        try:
            await _deadline_one(ctx, d, force)
        except Exception:  # noqa: BLE001 - one bad row must not block the others (it is retried next run)
            log.exception("deadline reminder failed for %s", d.key)


async def _deadline_one(ctx, d, force: bool) -> None:
    """Marked sent only after the send succeeds: a failed send is retried (at-least-once; a crash between send and
    mark could repeat one message, which is better than losing a reminder)."""
    db, now, tz = ctx.db, ctx.clock(), ctx.settings.tz
    if d.confidence != "confirmed" or d.all_day or d.key.startswith("bs:assignment:"):
        return
    moment = parse_iso(d.end_utc if d.end_utc and is_due(d.kind, d.as_event) else d.start_utc)
    reached = _due_labels(moment, now, deadline_thresholds) if moment else []
    if not reached:
        return
    row = db.conn.execute("select * from deadline_reminders where key=?", (d.key,)).fetchone()
    sent = json.loads(row["sent_json"]) if row and row["moment"] == moment.isoformat() else []
    left = moment - now
    urgent = left <= URGENT
    if reached[-1] in sent and not (force and urgent):
        return
    if not urgent and _blocked_by_policy(db, d.course_code, now, tz):
        return
    verb = "due" if is_due(d.kind, d.as_event) else "on"
    head = "[Urgent] " if urgent else "Reminder: "
    await ctx.notifier.send(f"{head}{d.course_code or 'Course'}: \"{d.title}\" {verb} {fmt_dt(moment, tz)} "
                            f"({time_left(left)} left).")
    db.conn.execute("insert into deadline_reminders values (?,?,?) on conflict(key) do update set "
                    "moment=excluded.moment, sent_json=excluded.sent_json",
                    (d.key, moment.isoformat(), json.dumps(reached)))
