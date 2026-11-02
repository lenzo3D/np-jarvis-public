"""Where deadlines come from (spec: Sources).

Confirmed: Brightspace assignment folders and calendar events (no AI).
Tentative: Claude extraction from announcements, documents, Teams and the class group (see extract_*).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from core import usagecap
from core.deadlines import SGT, Deadline, DeadlineStore, duplicates_confirmed, event_like, is_due, local_date, similar
from ops.format import parse_iso

log = logging.getLogger(__name__)
LESSON_RE = re.compile(r"(?i)\b(lesson|lecture|class|clinic|consultation|session|lab|seminar|briefing)\b")
DUE_RE = re.compile(r"(?i)\b(due|quiz|test|exam|homework|assignment|submit|submission|deadline|presentation|"
                    r"feedback|evaluation|assessment|ca\d?)\b")


_KIND_RES = [(k, re.compile(rf"(?i)\b{pat}\b")) for k, pat in
             (("exam", "exams?"), ("quiz", "quiz(?:zes)?"), ("test", "tests?"), ("presentation", "presentations?"),
              ("homework", "homework"), ("assignment", "assignments?"))]


def kind_from_title(title: str) -> str:
    """Whole words only: "contest", "latest", "attest", "protest" are not tests and "examine" is not an exam."""
    for k, rx in _KIND_RES:
        if rx.search(title or ""):
            return k
    return "other"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


STALE_AFTER = timedelta(hours=2)


def fresh_cutoff(db) -> str:
    """last_seen cutoff for assignments: older than the latest sweep by STALE_AFTER means gone (deleted/hidden)."""
    latest = db.conn.execute("select max(last_seen) from items where kind='assignment' and source='brightspace'"
                             ).fetchone()[0]
    return (datetime.fromisoformat(latest) - STALE_AFTER).isoformat() if latest else ""


def confirmed_from_db(db, base: str) -> list[Deadline]:
    """Assignments seen in the latest Brightspace sweeps. Items not seen for 2h+ (folder deleted/hidden) are skipped."""
    cutoff = fresh_cutoff(db)
    out = []
    for r in db.conn.execute("select * from items where kind='assignment' and last_seen>=?", (cutoff,)):
        p = json.loads(r["payload_json"])
        due = parse_iso(p.get("due_at")) or parse_iso(p.get("end_at"))
        if not due:
            continue
        ref = f"{base}/d2l/lms/dropbox/user/folder_submit_files.d2l?ou={p.get('ou')}&db={p.get('folder_id')}"
        out.append(Deadline(key=r["stable_id"], title=r["title"], course_code=r["course_code"], kind="assignment",
                            start_utc=_iso(due - timedelta(minutes=30)), end_utc=_iso(due), all_day=False,
                            source="brightspace", source_ref=ref, confidence="confirmed", quote=""))
    return out


def confirmed_from_calendar(events: list[dict], course_by_ou: dict[int, str],
                            assignments: list[Deadline]) -> list[Deadline]:
    out = []
    for e in events:
        title = (e.get("Title") or "").strip()
        if not title or (LESSON_RE.search(title) and not DUE_RE.search(title)):
            continue
        start, end = parse_iso(e.get("StartDateTime")), parse_iso(e.get("EndDateTime"))
        if not start:
            continue
        kind = kind_from_title(title)
        # a point in time, or a range over several days (an assessment window), is a due moment at its end; a named
        # event or a same-day range is an event with its own start
        as_event = event_like(kind, title, start, end)
        if is_due(kind, as_event):
            due = end or start
            s_, e_ = due - timedelta(minutes=30), due
        else:
            s_, e_ = start, end if end and end > start else None
        d = Deadline(key=f"bs:cal:{e['CalendarEventId']}", title=title, course_code=course_by_ou.get(e.get("OrgUnitId")),
                     kind=kind, start_utc=_iso(s_), end_utc=_iso(e_) if e_ else None,
                     all_day=bool(e.get("IsAllDayEvent")), source="brightspace", source_ref="",
                     confidence="confirmed", quote="", as_event=kind == "other" and as_event)
        if d.all_day:
            d.start_utc, d.end_utc = start.astimezone(SGT).date().isoformat(), None
        if duplicates_confirmed(d, assignments):
            continue
        out.append(d)
    return out


def _keep_stored_shape(store: DeadlineStore, d: Deadline) -> None:
    """A row stored as a due block (as_event=0) keeps that shape, so the upgrade that introduced as_event does not
    rewrite an existing same-day range into a long event block (no hash change, no calendar PATCH)."""
    if not d.as_event or d.all_day:
        return
    cur = store.get(d.key)
    if cur is None or cur.as_event:
        return
    due = parse_iso(d.end_utc) or parse_iso(d.start_utc)
    d.start_utc, d.end_utc, d.as_event = _iso(due - timedelta(minutes=30)), _iso(due), False


async def refresh_confirmed(ctx) -> int:
    """Upsert Brightspace deadlines; future ones that disappeared are marked gone. Returns #changed."""
    store, now = DeadlineStore(ctx.db), ctx.clock()
    courses = await ctx.bs.courses()
    if not courses:
        return 0  # treat as a failed fetch: never mass-delete on an empty list
    assignments = confirmed_from_db(ctx.db, ctx.settings.politemall_base)
    try:
        events = await ctx.bs.calendar(now - timedelta(days=1), now + timedelta(days=180), [c.ou for c in courses])
    except Exception as e:  # noqa: BLE001 - calendar is a bonus source
        log.warning("calendar fetch failed: %s", e)
        events = None
    found = assignments + (confirmed_from_calendar(events, {c.ou: c.code for c in courses}, assignments)
                           if events is not None else [])
    for d in found:
        _keep_stored_shape(store, d)
    changed = sum(store.upsert(d, now) for d in found)
    seen = {d.key for d in found}
    today = now.astimezone(ctx.settings.tz).date()
    for d in store.active():
        if d.source == "brightspace" and (local_date(d, ctx.settings.tz) < today
                                          or (events is None and d.key.startswith("bs:cal:"))):
            seen.add(d.key)  # past deadlines stay as history; keep calendar ones if the fetch failed
    store.mark_gone_missing("brightspace", seen)
    confirmed = [d for d in store.active() if d.confidence == "confirmed"]
    for t in [d for d in store.active() if d.confidence == "tentative"]:
        if similar(t, confirmed):
            store.mark_gone(t.key)  # the real POLITEMall deadline replaces the tentative one
    return changed


# ---------------------------------------------------------------- tentative (Claude extraction)
from datetime import date as _date  # noqa: E402

from core.deadlines import extracted_key  # noqa: E402
from core.library import Library  # noqa: E402

KINDS = {"assignment", "homework", "quiz", "test", "exam", "presentation", "meeting", "other"}
SOURCE_OF_KIND = {"announcement": "announcement", "document": "document", "assignment": "document",
                  "teams": "teams", "group": "group", "email": "email"}
TEACHING_RE = re.compile(r"(?i)teaching plan|lesson plan|schedule|course outline|assessment|weekly plan|syllabus|"
                         r"module (guide|info)|course info")
DATEISH_RE = re.compile(r"(?i)\bweek\s*\d+\b|\b\d{1,2}\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)|"
                        r"\b\d{1,2}/\d{1,2}\b|\b(mon|tues|wednes|thurs|fri|satur|sun)day\b")
ASSESS_RE = re.compile(r"(?i)\b(quiz|test|exam|due|deadline|submit|submission|presentation|ca\d|assessment)\b")
MAX_CHARS_PER_CALL = 40_000
MAX_CHARS_PER_SOURCE = 15_000


def _hhmm(v) -> tuple[int, int] | None:
    if not v:
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(v).strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError("bad time")
    return int(m.group(1)), int(m.group(2))


def parse_extraction(raw: str, sources: dict[str, tuple[str | None, str]], now: datetime, tz,
                     known_courses: set[str] | None = None) -> list[Deadline]:
    m = re.search(r"\{.*\}", raw or "", re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        return []
    items = data.get("deadlines") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    known = known_courses if known_courses is not None else {c for c, _ in sources.values() if c}
    today = now.astimezone(tz).date()
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()[:120]
        src_course, src_kind = sources.get(str(it.get("source")), (None, "document"))
        try:
            day = _date.fromisoformat(str(it.get("date")))
            t, t_end = _hhmm(it.get("time")), _hhmm(it.get("end_time"))
        except (ValueError, TypeError):
            continue
        if not title or day < today - timedelta(days=1) or day > today + timedelta(days=240):
            continue
        kind = it.get("kind") if it.get("kind") in KINDS else "other"
        course = it.get("course") if it.get("course") in known else src_course
        as_event = t is not None and event_like(
            kind, title, datetime(day.year, day.month, day.day, *t, tzinfo=tz),
            datetime(day.year, day.month, day.day, *t_end, tzinfo=tz) if t_end else None, tz)
        if t is None:
            start, end, all_day = day.isoformat(), None, True
        else:
            s_local = datetime(day.year, day.month, day.day, *t, tzinfo=tz)
            if t_end:
                e_local = datetime(day.year, day.month, day.day, *t_end, tzinfo=tz)
                start, end = _iso(s_local), _iso(e_local) if e_local > s_local else None
            elif is_due(kind, as_event):
                start, end = _iso(s_local - timedelta(minutes=30)), _iso(s_local)
            else:
                start, end = _iso(s_local), None
            all_day = False
        out.append(Deadline(key=extracted_key(course, kind, title, day.isoformat()), title=title,
                            course_code=course, kind=kind, start_utc=start, end_utc=end, all_day=all_day,
                            source=SOURCE_OF_KIND.get(src_kind, src_kind), source_ref=str(it.get("source") or ""),
                            confidence="tentative", quote=str(it.get("quote") or "")[:200], as_event=as_event))
    return out


_MONTHS = r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
_DAYS = r"mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?"
# a chat line that could carry a date or a deadline; everything else ("lol", "ok thanks") never needs Claude
DATEY_RE = re.compile(
    rf"(?i)\b\d{{1,2}}[/.-]\d{{1,2}}(?:[/.-]\d{{2,4}})?\b|\b\d{{1,2}}(?:st|nd|rd|th)?\s*(?:{_MONTHS})\b|\b(?:{_MONTHS})\s*\d{{1,2}}\b|"
    rf"\b(?:{_DAYS})\b|\b(?:tmr|tmrw|tomorrow|tonight|next\s+(?:week|mon|tue|wed|thu|fri|sat|sun)|this\s+(?:week|fri|sat|sun)|"
    r"end of (?:the )?week|eod)\b|"
    r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\b\d{1,2}:\d{2}\b|\b\d{3,4}\s*(?:hrs?|h)\b|"
    r"\b(?:due|deadline|submit\w*|quiz|test|exam|presentation|assignment|week\s*\d+|wk\s*\d+|mst|ca\d|recess|noon|midnight|"
    r"hand(?:ing)?[\s-]*in|report|brief|esok|lusa|minggu|hari\s+ini|today)\b|"
    r"\b[0-2]\d[0-5]\d\b|"  # 24h times such as 2359 or 0900
    rf"\b(?:\d+|an?|one|two|three|four|five|six|seven|eight|nine|ten|few|couple)\s*(?:weeks?|wks?|days?)\b|"
    r"[^\x00-\u024f]")  # any non-Latin script (Chinese etc.): not judged, always sent
SMALL_BATCH_CHARS = 4000  # a batch this small is cheap, so it is sent whole rather than risk dropping a deadline


def datey_lines(lines: list, context: int = 1, key=lambda x: x, min_chars: int = 0) -> list:
    """Keep only the lines that look date-like, plus `context` neighbours each side (the line before often names the
    subject: "the quiz?" / "friday 3pm"). Order is preserved. `key` picks the text to test, so a timestamp prefix
    (which always looks like a date) is not what gets matched."""
    if sum(len(key(x)) for x in lines) < min_chars:
        return list(lines)
    keep: set[int] = set()
    for i, line in enumerate(lines):
        if DATEY_RE.search(key(line)):
            keep.update(range(max(0, i - context), min(len(lines), i + context + 1)))
    return [lines[i] for i in sorted(keep)]


def _worth_extracting(m: dict) -> bool:
    if m["kind"] in ("announcement", "assignment"):
        return True
    head = m["text"][:4000]
    return bool(TEACHING_RE.search(m["title"]) or (ASSESS_RE.search(head) and DATEISH_RE.search(head)))


async def _course_names(ctx) -> dict[str, str]:
    """Course code → name. Last good list is kept in the DB so a Brightspace hiccup doesn't rename every event."""
    cache = ctx.extra.get("courses")
    try:
        courses = cache[1] if cache else await ctx.bs.courses()
    except Exception as e:  # noqa: BLE001
        log.warning("course names unavailable, using last known: %s", e)
        return ctx.db.get_json_setting("course_names", {})
    names = {c.code: c.name for c in courses}
    if names:
        ctx.db.set_json_setting("course_names", names)
    return names or ctx.db.get_json_setting("course_names", {})


def week_table(start: str, recess_week: int | None = None, weeks: int = 18) -> list[tuple[str, _date]]:
    """[(label, Monday)] for teaching weeks 1..weeks, with the recess week (if any) as its own row after week
    `recess_week`; every later teaching week is one week further on."""
    first = _date.fromisoformat(start)
    rows, offset = [], 0
    for n in range(1, weeks + 1):
        rows.append((f"Week {n}", first + timedelta(days=7 * (n - 1 + offset))))
        if recess_week and n == recess_week:
            offset = 1
            rows.append(("Recess week", first + timedelta(days=7 * n)))
    return rows


def semester_text(start: str | None, recess_week: int | None = None) -> str:
    """The semester facts given to the extraction prompt: a ready table of Monday dates (recess already applied), so
    the model looks the date up instead of doing the arithmetic."""
    try:
        rows = week_table(start, recess_week) if start else None
    except ValueError:
        rows = None
    if not rows:
        return "No semester start date is known: skip dates given only as a week number."
    table = "; ".join(f"{label}: Monday {d.isoformat()}" for label, d in rows)
    note = " The recess week is a real week with its own dates." if recess_week else ""
    return (f"Semester week table (the Monday of each week, use these exact dates for 'Week N' or 'recess week'; "
            f"weeks not in the table: skip).{note} {table}.")


def _prompt(ctx, names: dict[str, str]) -> str:
    from ops import claude_runner

    tz = ctx.settings.tz
    sem = semester_text(getattr(ctx.settings, "semester_start", None), getattr(ctx.settings, "recess_week", None))
    return claude_runner.load_prompt("extract_deadlines", today=ctx.clock().astimezone(tz).date().isoformat(),
                                     semester=sem, courses="\n".join(f"- {c}: {n}" for c, n in names.items()))


def _json_ok(raw: str) -> bool:
    m = re.search(r"\{.*\}", raw or "", re.S)
    try:
        return bool(m) and isinstance(json.loads(m.group(0)).get("deadlines"), list)
    except (json.JSONDecodeError, AttributeError):
        return False


async def _run_blocks(ctx, blocks: list[tuple[str, str, str | None, str]],
                      names: dict[str, str], job: str = "extract_docs") -> tuple[int, set[str]]:
    """blocks: (source_id, kind, course, text). Returns (#new tentative deadlines, source ids handled OK).
    `job` picks the model setting. While the usage cap has the jobs paused nothing runs and nothing is reported as
    handled, so library items stay pending and the chat window does not advance: it all resumes after the reset."""
    from ops import claude_runner
    from ops.deadline_jobs import get_gcal
    from ops.gcal_events import short_course

    if not blocks or usagecap.paused(ctx):
        return 0, set()
    model = usagecap.model_for(ctx.db, job)
    run_text = ctx.extra.get("run_text") or claude_runner.run_text
    sources = {sid: (course, kind) for sid, kind, course, _ in blocks}
    chunks: list[tuple[str, list[str]]] = []
    cur, ids = "", []
    for sid, kind, course, text in blocks:
        part = f"### SOURCE {sid} | {course or 'unknown'} | {kind}\n{text[:MAX_CHARS_PER_SOURCE]}\n\n"
        if cur and len(cur) + len(part) > MAX_CHARS_PER_CALL:
            chunks.append((cur, ids))
            cur, ids = "", []
        cur += part
        ids.append(sid)
    if cur:
        chunks.append((cur, ids))
    store, now, tz = DeadlineStore(ctx.db), ctx.clock(), ctx.settings.tz
    prompt, new, ok, notes = _prompt(ctx, names), 0, set(), []
    in_calendar = " — added to Google Calendar" if get_gcal(ctx) else ""
    for chunk, chunk_ids in chunks:
        try:
            with claude_runner.labelled("extract_dates"):
                raw = await run_text(prompt, chunk, model=model, run_as=ctx.settings.claude_run_as)
        except claude_runner.ClaudeError as e:
            log.warning("deadline extraction failed (will retry): %s", e)
            continue
        if not _json_ok(raw):
            log.warning("deadline extraction returned no JSON (will retry)")
            continue
        ok.update(chunk_ids)
        active = store.active()
        confirmed = [d for d in active if d.confidence == "confirmed"]
        tentative = [d for d in active if d.confidence == "tentative"]
        deleted = store.by_status("user_deleted")
        for d in parse_extraction(raw, sources, now, tz, set(names)):
            if store.is_user_deleted(d.key) or similar(d, confirmed) or similar(d, deleted) or similar(d, tentative):
                continue
            existed = store.get(d.key) is not None
            if store.upsert(d, now) and not existed:
                new += 1
                tentative.append(d)
                when = local_date(d, tz).strftime("%a %d %b")
                course = short_course(names.get(d.course_code or "", ""), d.course_code) if d.course_code else "?"
                notes.append({"text": f"New date found (tentative): {course}: {d.title} — {when} (from {d.source}){in_calendar}",
                              "buttons": None})
    if notes:  # re-read and append with no await in between, so concurrent alerts aren't lost or repeated
        ctx.db.set_json_setting("batch_queue", ctx.db.get_json_setting("batch_queue", []) + notes)
    return new, ok


async def extract_from_library(ctx, keys: list[str]) -> int:
    """Extract from the given library items plus any whose last extraction failed."""
    lib, names = Library(ctx.db), await _course_names(ctx)
    blocks = []
    for k in dict.fromkeys(list(keys) + lib.pending_extraction()):
        m = lib.get(k)
        if not m:
            continue
        if _worth_extracting(m):
            blocks.append((k, m["kind"], m["course_code"], f"{m['title']}\n{m['text']}"))
        else:
            lib.mark_extracted(k)  # nothing to look for in it
    new, ok = await _run_blocks(ctx, blocks, names)
    for k in ok:
        lib.mark_extracted(k)
    return new


async def extract_chats(ctx) -> int:
    """Teams messages and class-group messages since the last successful run."""
    from core import groups, tg_group

    now, tz = ctx.clock(), ctx.settings.tz
    since = parse_iso(ctx.db.get_setting("last_chat_extract")) or now - timedelta(hours=24)
    blocks = []
    cfg = groups.configured(ctx.settings, ctx.db) or [{"id": None, "label": "class group", "kind": "class"}]
    for g in cfg:
        msgs = tg_group.messages_since(ctx.db, g["id"], since)
        pairs = datey_lines([(f"[{m.ts.astimezone(tz):%a %d %b %H:%M}] {m.sender}: {m.text}", m.text) for m in msgs],
                            key=lambda p: p[1], min_chars=SMALL_BATCH_CHARS)
        all_lines = [line for line, _ in pairs]
        for i in range(0, len(all_lines), 200):  # split instead of truncating, so recent messages are never dropped
            lines = all_lines[i:i + 200]
            sid = f"group{i // 200 or ''}" if g["id"] is None else f"group{g['id']}:{i // 200}"
            blocks.append((sid, "group", None, f"Telegram {g['kind']} group: {g['label']}\n" + "\n".join(lines)))
    by_conv: dict[str, list[tuple[str, str]]] = {}
    for r in ctx.db.conn.execute("select payload_json from items where source='teams' and first_seen>=?",
                                 (since.isoformat(),)):
        p = json.loads(r["payload_json"])
        ts = parse_iso(p.get("ts"))
        stamp = ts.astimezone(tz).strftime("%a %d %b %H:%M") if ts else ""
        by_conv.setdefault(p.get("chat_name") or "Teams", []).append(
            (f"[{stamp}] {p.get('sender')}: {p.get('body_text')}", str(p.get("body_text") or "")))
    for name, lines in by_conv.items():
        lines = [line for line, _ in datey_lines(lines, key=lambda p: p[1], min_chars=SMALL_BATCH_CHARS)]
        if not lines:
            continue
        blocks.append((f"teams:{name}"[:80], "teams", None, f"Teams: {name}\n" + "\n".join(lines)))
    if not blocks:
        ctx.db.set_setting("last_chat_extract", now.isoformat())
        return 0
    new, ok = await _run_blocks(ctx, blocks, await _course_names(ctx), job="extract_chats")
    if ok >= {sid for sid, *_ in blocks}:
        ctx.db.set_setting("last_chat_extract", now.isoformat())
    return new
