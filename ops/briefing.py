"""Group chat summaries and the daily briefing (via `claude -p`, no tools)."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta

from core import groups, tg_group
from core.models import Item
from ops import claude_runner, persona
from ops.diff import Change
from ops.format import fmt_dt, parse_iso
from ops.watcher import deliver

log = logging.getLogger(__name__)
SUMMARY_THRESHOLD = 50
DEFAULT_LOOKBACK = timedelta(hours=6)
MAX_INPUT_CHARS = 60000


def _run_text(ctx):
    return ctx.extra.get("run_text") or claude_runner.run_text


def _parse_json(text: str):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) and isinstance(data.get("summary", []), list) else None


def _groups_to_summarise(ctx) -> list[dict]:
    """The configured groups, or (nothing configured) one catch-all over every stored message."""
    cfg = groups.configured(ctx.settings, ctx.db)
    return cfg or [{"id": None, "label": groups.DEFAULT_LABEL, "kind": "class"}]


async def group_summary(ctx, since: datetime, chat_id: int | None = None, label: str | None = None,
                        kind: str = "class") -> None:
    tz = ctx.settings.tz
    label = label or groups.DEFAULT_LABEL
    msgs = tg_group.messages_since(ctx.db, chat_id, since)
    if not msgs:
        return
    lines = [f"[{m.ts.astimezone(tz):%H:%M}] {m.sender}: {m.text}" for m in msgs]
    input_text = "\n".join(lines)[-MAX_INPUT_CHARS:]
    key = "last_summary" if chat_id is None else f"last_summary:{chat_id}"
    try:
        with claude_runner.labelled("group_summary"):
            raw = await _run_text(ctx)(claude_runner.load_prompt("group_summary", group=label, kind=kind, **persona.fields(ctx.settings)), input_text,
                                       run_as=ctx.settings.claude_run_as)
    except claude_runner.ClaudeError as e:
        log.warning("group summary failed: %s", e)
        return
    ctx.db.set_setting(key, ctx.clock().isoformat())
    data = _parse_json(raw)
    if data is None:
        await ctx.notifier.send(f"{label} summary\n" + raw[:3000])
        _remember_highlights(ctx, label, chat_id, [raw[:500]])
        return
    bullets = [str(b) for b in data.get("summary", []) if str(b).strip()]
    if bullets:
        await ctx.notifier.send(f"{label} summary\n" + "\n".join(f"• {b}" for b in bullets))
    _remember_highlights(ctx, label, chat_id, bullets)
    changes = []
    for a in data.get("actions", []) or []:
        text = str(a.get("text", "")).strip()
        if not text:
            continue
        due = a.get("due")
        key2 = hashlib.sha256(f"{chat_id}|{text}|{due}".encode()).hexdigest()[:16]
        body = f"Deadline: {due}" if due else ""
        changes.append(Change("new", Item("group", f"group:action:{key2}", None, "group_action", text,
                                          {"body_text": body, "group": label})))
    await deliver(ctx, changes)


def _remember_highlights(ctx, label: str, chat_id: int | None, bullets: list[str]) -> None:
    """The morning briefing reads these: one entry per group, prefixed with the group name when there are several."""
    store = ctx.db.get_json_setting("group_highlights", {})
    store[str(chat_id)] = [f"{label}: {b}" if chat_id is not None else b for b in bullets]
    ctx.db.set_json_setting("group_highlights", store)
    ctx.db.set_json_setting("last_group_summary", [b for v in store.values() for b in v])


async def scheduled_group_summary(ctx) -> None:
    for g in _groups_to_summarise(ctx):
        key = "last_summary" if g["id"] is None else f"last_summary:{g['id']}"
        last = parse_iso(ctx.db.get_setting(key)) or ctx.clock() - DEFAULT_LOOKBACK
        await group_summary(ctx, last, g["id"], g["label"], g["kind"])


async def check_group_threshold(ctx) -> None:
    for g in _groups_to_summarise(ctx):
        key = "last_summary" if g["id"] is None else f"last_summary:{g['id']}"
        last = parse_iso(ctx.db.get_setting(key)) or ctx.clock() - DEFAULT_LOOKBACK
        if tg_group.count_since(ctx.db, last, g["id"]) >= SUMMARY_THRESHOLD:
            await group_summary(ctx, last, g["id"], g["label"], g["kind"])


async def build_briefing_input(ctx, now: datetime) -> str:
    tz, db = ctx.settings.tz, ctx.db
    out = [f"Now: {fmt_dt(now, tz)} (Singapore)"]
    local = now.astimezone(tz)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        events = await ctx.bs.calendar(start, start + timedelta(days=1)) if hasattr(ctx.bs, "calendar") else []
        ev_lines = [f"- {fmt_dt(parse_iso(e['StartDateTime']), tz)} {e.get('Title', '')} {e.get('Location') or ''}".rstrip()
                    for e in events if e.get("StartDateTime")]
    except Exception as e:  # noqa: BLE001
        ev_lines = [f"- (calendar unavailable: {type(e).__name__})"]
    out += ["", "Today's classes and events:"] + (ev_lines or ["- none listed"])
    due_lines, nodue = [], []
    for d in db.conn.execute("select * from due_dates"):
        r = db.conn.execute("select * from items where stable_id=?", (d["item_key"],)).fetchone()
        if not r:
            continue
        mark = "submitted" if d["submitted"] else "not submitted"
        when = parse_iso(d["due_at"])
        if when is None:
            nodue.append(f"- {r['course_code']}: {r['title']} ({mark})")
        elif now <= when <= now + timedelta(days=7):
            due_lines.append((when, f"- {r['course_code']}: {r['title']} — due {fmt_dt(when, tz)} ({mark})"))
    out += ["", "Due in the next 7 days:"] + ([t for _, t in sorted(due_lines)] or ["- nothing"])
    if nodue:
        out += ["", "No due date:"] + nodue
    queue = db.get_json_setting("briefing_queue", [])
    if queue:
        out += ["", "New since yesterday:"] + [f"- {q['text']}" for q in queue[-40:]]
    if not getattr(ctx.settings, "semester_start", None):
        out += ["", f"Note for {persona.address(ctx.settings)}: SEMESTER_START is still not set in secrets.env, so teaching-plan dates given only as "
                "'Week N' can't be placed in the calendar. He asked to be reminded."]
    highlights = db.get_json_setting("last_group_summary", [])
    if highlights:
        out += ["", "Class chat highlights:"] + [f"- {h}" for h in highlights]
    return "\n".join(out)


async def daily_briefing(ctx) -> None:
    now = ctx.clock()
    facts = await build_briefing_input(ctx, now)
    try:
        with claude_runner.labelled("briefing"):
            text = await _run_text(ctx)(claude_runner.load_prompt("briefing", **persona.fields(ctx.settings)), facts, run_as=ctx.settings.claude_run_as)
    except claude_runner.ClaudeError as e:
        log.warning("briefing formatting failed: %s", e)
        text = "Daily briefing\n\n" + facts
    await ctx.notifier.send(text)
    ctx.db.set_json_setting("briefing_queue", [])
    ctx.db.set_setting("last_briefing_date", now.astimezone(ctx.settings.tz).date().isoformat())


async def maybe_daily_briefing(ctx) -> None:
    """Runs every few minutes; sends the briefing once per day at or after the configured time."""
    if ctx.db.get_setting("paused") == "1":
        return
    local = ctx.clock().astimezone(ctx.settings.tz)
    hh, mm = (ctx.db.get_setting("briefing_time") or "07:00").split(":")
    due = local.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    if local < due or ctx.db.get_setting("last_briefing_date") == local.date().isoformat():
        return
    if local - due > timedelta(hours=3):
        return  # server was down at briefing time; skip rather than send a stale briefing at night
    await daily_briefing(ctx)
