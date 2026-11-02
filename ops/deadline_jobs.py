"""Scheduled deadline work: Brightspace → deadlines → Google Calendar; library refresh + extraction."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from core import hostos, project_tasks, usagecap
from core.deadlines import DeadlineStore, display_moment, local_date
from core.gcal import CalendarAuthRequired, GCal
from ops import project_tasks as project_tasks_ops
from ops.calsync import sync
from ops.deadline_sources import _course_names, extract_chats, refresh_confirmed
from ops.gcal_events import short_course
from ops.scan import full_scan

log = logging.getLogger(__name__)
_sync_lock = asyncio.Lock()
_scan_lock = asyncio.Lock()


def get_gcal(ctx):
    if ctx.extra.get("gcal") is not None:
        return ctx.extra["gcal"]
    token = ctx.settings.state_dir / "google_token.json"
    if not token.exists():
        return None
    ctx.extra["gcal"] = GCal(token_path=token, client_path=ctx.settings.state_dir / "google_client.json")
    return ctx.extra["gcal"]


async def calendar_sync(ctx):
    async with _sync_lock:
        return await _calendar_sync(ctx)


async def _calendar_sync(ctx):
    try:  # your own project-group tasks and the groups' meetings become tentative deadlines first (local, no network)
        project_tasks.sync_deadlines(ctx.settings, ctx.db, ctx.clock())
    except Exception:  # noqa: BLE001 - never let this stop the calendar
        log.warning("project task calendar mirror failed", exc_info=True)
    gcal = get_gcal(ctx)
    if gcal is None:
        return None
    try:
        rep = await sync(DeadlineStore(ctx.db), gcal, await _course_names(ctx), ctx.settings.tz)
    except CalendarAuthRequired as e:
        if ctx.db.get_setting("gcal_warned") != "1":
            await ctx.notifier.send(f"Google Calendar needs reconnecting: {e}\n"
                                    f"Run: {hostos.python_hint('-m core.gcal login')}")
            ctx.db.set_setting("gcal_warned", "1")
        ctx.extra.pop("gcal", None)
        return None
    ctx.db.set_setting("gcal_warned", "0")
    if rep.inserted or rep.patched or rep.deleted:
        log.info("calendar sync: %s", rep)
    return rep


async def confirmed_job(ctx) -> None:
    """Every 15 min: Brightspace due dates → deadlines → calendar."""
    await refresh_confirmed(ctx)
    await calendar_sync(ctx)


async def extract_job(ctx) -> dict | None:
    """Hourly: re-read changed course material, extract tentative dates from it and from chats, sync.
    Returns None if a scan is already running (never two at once)."""
    if _scan_lock.locked():
        return None
    async with _scan_lock:
        stats = await full_scan(ctx, extract=True)
        stats["deadlines"] += await extract_chats(ctx)
        try:
            await project_tasks_ops.extract_all(ctx)
        except Exception:  # noqa: BLE001 - the project task list must never stop the date scan
            log.warning("project task extraction failed", exc_info=True)
        await refresh_confirmed(ctx)
    await calendar_sync(ctx)
    return stats


async def scan_command(ctx) -> str:
    stats = await extract_job(ctx)
    if stats is None:
        return "A scan is already running — I'll post the results when it's done."
    total = len(DeadlineStore(ctx.db).active())
    cal = "synced to Google Calendar" if get_gcal(ctx) else "Google Calendar not connected yet"
    errors = f", {stats['errors']} errors" if stats["errors"] else ""
    held = ("\nDate extraction is paused to save usage; it picks up where it left off when the period resets "
            "(or when you /usage resume).") if usagecap.paused(ctx) else ""
    return (f"Scan done: {stats['courses']} courses, {stats['documents']} documents, "
            f"{stats['announcements']} announcements, {stats['assignments']} assignment briefs read"
            f"{errors}.\n"
            f"{stats['deadlines']} new tentative date(s) found; {total} deadlines tracked ({cal}).\n"
            "Ask me anything, e.g. \"when are all my assignments due?\"" + held)


async def deadlines_text(ctx, days: int = 14) -> str:
    tz = ctx.settings.tz
    items = DeadlineStore(ctx.db).upcoming(ctx.clock(), days, tz)
    if not items:
        return f"No deadlines in the next {days} days."
    names = await _course_names(ctx)
    lines = [f"Next {days} days (tentative dates are marked)"]
    for d in items:
        tag = "" if d.confidence == "confirmed" else " [tentative]"
        row = ctx.db.conn.execute("select submitted, user_confirmed from due_dates where item_key=?", (d.key,)).fetchone()
        if row and row["submitted"]:
            tag += " [submitted]"
        elif row and row["user_confirmed"]:
            tag += " [you say submitted; POLITEMall has not confirmed it]"
        if d.all_day:
            when = local_date(d, tz).strftime("%a %d %b")
        else:
            when = datetime.fromisoformat(display_moment(d)).astimezone(tz).strftime("%a %d %b %H:%M")
        who = "" if d.source == "project" else f"{short_course(names.get(d.course_code or '', ''), d.course_code)}: "
        lines.append(f"{when} · {who}{d.title}{tag}")
    return "\n".join(lines)
