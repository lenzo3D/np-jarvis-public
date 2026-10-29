"""Watcher jobs: fetch → diff → classify → deliver (spec section 4.8)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from core.auth import AuthRequired
from core.config import Settings
from core.db import DB
from core.models import Item
from ops.classify import classify
from ops.diff import Change, detect
from ops.format import alert_text
from ops.policy import decide, quiet_now

log = logging.getLogger(__name__)
SOURCE_NAMES = {"brightspace": "POLITEMall", "teams": "Teams", "group": "Class group", "outlook": "Outlook email"}
MAX_BATCH_BUTTON_ROWS = 8


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Services:
    settings: Settings
    db: DB
    sessions: Any
    bs: Any
    notifier: Any
    teams: Any = None
    group: Any = None
    clock: Callable[[], datetime] = field(default=_utcnow)
    extra: dict = field(default_factory=dict)


def buttons_for(change: Change) -> list[list[tuple[str, str]]]:
    item = change.item
    row: list[tuple[str, str]] = []
    if item.kind == "content" and item.payload.get("type") == "File":
        row.append(("Download", f"dl:{item.payload['ou']}:{item.payload['topic_id']}"))
    if item.kind == "assignment":
        row.append(("Do it", f"doit:{item.stable_id}"))
    rows = [row] if row else []
    if item.course_code and item.kind not in ("content",):
        rows.append([("Mute course", f"mute:{item.course_code}")])
    return rows


async def deliver(ctx: Services, changes: list[Change]) -> None:
    now = ctx.clock()
    tz = ctx.settings.tz
    names = ctx.db.get_json_setting("course_names", {})
    batch = ctx.db.get_json_setting("batch_queue", [])
    briefing = ctx.db.get_json_setting("briefing_queue", [])
    retry = ctx.db.get_json_setting("retry_queue", [])
    try:
        for ch in changes:
            level = classify(ch)
            decision = decide(level, ch.item.course_code, ctx.db, now)
            text = alert_text(ch, level, tz, now=now, course_names=names)
            entry = {"text": text, "buttons": buttons_for(ch) or None}
            if decision == "send":
                try:
                    await ctx.notifier.send(text, entry["buttons"])
                except Exception as e:  # noqa: BLE001 - never lose an urgent alert to a Telegram hiccup
                    log.warning("alert send failed, will retry: %s", e)
                    retry.append(entry)
            elif decision == "batch":
                batch.append(entry)
            elif decision == "briefing":
                briefing.append({"text": text, "at": now.isoformat()})
    finally:
        ctx.db.set_json_setting("batch_queue", batch)
        ctx.db.set_json_setting("briefing_queue", briefing)
        ctx.db.set_json_setting("retry_queue", retry)


async def flush_retry(ctx: Services) -> None:
    queue = ctx.db.get_json_setting("retry_queue", [])
    if not queue or ctx.db.get_setting("paused") == "1":
        return
    left = []
    for entry in queue:
        try:
            await ctx.notifier.send(entry["text"], entry.get("buttons"))
        except Exception as e:  # noqa: BLE001
            log.warning("retry send failed: %s", e)
            left.append(entry)
    ctx.db.set_json_setting("retry_queue", left)


async def flush_batch(ctx: Services) -> None:
    await flush_retry(ctx)
    if ctx.db.get_setting("paused") == "1" or quiet_now(ctx.db, ctx.clock(), ctx.settings.tz):
        return
    queue = ctx.db.get_json_setting("batch_queue", [])
    if not queue:
        return
    text = "Updates\n\n" + "\n\n".join(q["text"] for q in queue)
    rows = [tuple(map(tuple, row)) for q in queue for row in (q.get("buttons") or [])]
    rows = [list(r) for r in dict.fromkeys(rows)][:MAX_BATCH_BUTTON_ROWS]
    try:
        await ctx.notifier.send(text, rows or None)
    except Exception as e:  # noqa: BLE001 - keep the queue for the next hour
        log.warning("batch send failed: %s", e)
        return
    # re-read so items queued while we were sending are kept
    current = ctx.db.get_json_setting("batch_queue", [])
    ctx.db.set_json_setting("batch_queue", current[len(queue):])


async def run_source(ctx: Services, source: str, collect: Callable[[], Awaitable[list[Item]]],
                     on_items: Callable[[list[Item]], None] | None = None) -> list[Change]:
    """Run one sweep with auth handling and failure accounting."""
    db, now = ctx.db, ctx.clock()
    name = SOURCE_NAMES.get(source, source)
    prev = db.conn.execute("select consecutive_failures from source_health where source=?", (source,)).fetchone()
    prev_failures = prev[0] if prev else 0
    try:
        items = await collect()
    except AuthRequired:
        if db.get_setting(f"auth_prompted:{source}") != "1":
            await ctx.notifier.send(f"{name} session expired. Alerts for it are paused until you log in.",
                                    [[("Log in now", f"login:retry:{source}")]])
            db.set_setting(f"auth_prompted:{source}", "1")
        return []
    except Exception as e:  # noqa: BLE001
        log.exception("%s sweep failed", source)
        n = db.record_health(source, False, now)
        if n == 3:
            await ctx.notifier.send(f"{name} checks failing (3 in a row): {type(e).__name__}: {e}"[:500])
        return []
    db.record_health(source, True, now)
    db.set_setting(f"auth_prompted:{source}", "0")
    if prev_failures >= 3:
        await ctx.notifier.send(f"{name} is back to normal.")
    if on_items:
        on_items(items)
    changes = detect(db, source, items, now)
    await deliver(ctx, changes)
    return changes


def _upsert_due_dates(db: DB, items: list[Item]) -> None:
    for it in items:
        if it.kind != "assignment":
            continue
        due = it.payload.get("due_at") or it.payload.get("end_at")
        # A moved deadline gets a fresh set of reminders and is re-checked for submission.
        db.conn.execute(
            """insert into due_dates(item_key, due_at) values (?,?)
               on conflict(item_key) do update set
                 reminders_sent_json = case when due_dates.due_at is excluded.due_at
                                            then due_dates.reminders_sent_json else '[]' end,
                 submitted = case when due_dates.due_at is excluded.due_at then due_dates.submitted else 0 end,
                 due_at = excluded.due_at""",
            (it.stable_id, due),
        )


SUBMITTED_CHECK_WINDOW_DAYS = 7


async def _refresh_submitted(ctx: Services, items: list[Item]) -> None:
    from datetime import timedelta

    from ops.format import parse_iso

    now = ctx.clock()
    horizon = now + timedelta(days=SUBMITTED_CHECK_WINDOW_DAYS)
    for it in items:
        if it.kind != "assignment":
            continue
        due = parse_iso(it.payload.get("due_at") or it.payload.get("end_at"))
        if not due or not (now - timedelta(days=1) <= due <= horizon):
            continue
        row = ctx.db.conn.execute("select submitted from due_dates where item_key=?", (it.stable_id,)).fetchone()
        if row and row[0]:
            continue
        try:
            if await ctx.bs.submitted(it.payload["ou"], it.payload["folder_id"]):
                ctx.db.conn.execute("update due_dates set submitted=1 where item_key=?", (it.stable_id,))
        except AuthRequired:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("submission check failed for %s: %s", it.stable_id, e)


async def sweep_brightspace(ctx: Services) -> list[Change]:
    async def collect() -> list[Item]:
        items: list[Item] = []
        calls = failures = 0
        last_error: Exception | None = None
        for c in await ctx.bs.courses():
            for fetch in (ctx.bs.assignment_items, ctx.bs.announcements, ctx.bs.content_items, ctx.bs.grades):
                calls += 1
                try:
                    items += await fetch(c.ou, c.code)
                except AuthRequired:
                    raise
                except Exception as e:  # noqa: BLE001 - one disabled tool must not stop the sweep
                    failures += 1
                    last_error = e
                    log.warning("%s %s failed: %s", c.code, getattr(fetch, "__name__", fetch), e)
        if calls and failures == calls and last_error:
            raise last_error
        _upsert_due_dates(ctx.db, items)
        await _refresh_submitted(ctx, items)
        return items

    return await run_source(ctx, "brightspace", collect)


TEAMS_LOOKBACK_HOURS = 6


async def sweep_teams(ctx: Services) -> list[Change]:
    from datetime import timedelta

    if not ctx.teams:
        return []
    since = ctx.clock() - timedelta(hours=TEAMS_LOOKBACK_HOURS)
    return await run_source(ctx, "teams", lambda: ctx.teams.recent(since))
