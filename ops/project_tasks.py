"""Project group assistant: who owes what, read from the chat of each group marked "Project".

Extraction is incremental. Every stored message is marked once it has been handled (per chat and message id, not by a
position: a Telegram backfill stores the newest messages first, so a single watermark cannot be right). A Claude text
job with tools off ("project_tasks", haiku by default) reads the oldest unmarked messages, in time order, and returns
strict JSON that is validated here before anything is written. While the usage cap has the job paused nothing runs and
nothing is marked, so the next run after the reset covers the gap. The bot never posts in a group because of this feature: the only
output is a private Telegram digest and the panel.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from core import project_tasks as pt
from core import tg_group, usagecap
from core.deadlines import SGT
from core.models import GroupMessage
from ops import claude_runner, persona
from ops.deadline_sources import _hhmm

log = logging.getLogger(__name__)
JOB = "project_tasks"
FIRST_LOOKBACK = timedelta(days=14)  # a group seen for the first time is read back this far
CHUNK_MESSAGES = 120
CHUNK_CHARS = 30_000
MAX_CHUNKS_PER_RUN = 4  # the rest waits for the next run
CONTEXT_MESSAGES = 6
DIGEST_HOUR, DIGEST_GRACE = 19, timedelta(hours=3)  # Sunday 19:00 Singapore time


@dataclass
class Parsed:
    tasks: list[dict] = field(default_factory=list)
    updates: list[dict] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)


def parse_extraction(raw: str, msgs: dict[int, GroupMessage], open_ids: set[int], now: datetime, tz=SGT) -> Parsed | None:
    """Validate the model's JSON. None = unusable reply (retry later, nothing written). Single bad items are skipped:
    an item with an impossible date or time is dropped rather than guessed at."""
    m = re.search(r"\{.*\}", raw or "", re.S)
    try:
        data = json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not all(isinstance(data.get(k, []), list) for k in ("tasks", "updates", "decisions")):
        return None
    if not any(k in data for k in ("tasks", "updates", "decisions")):
        return None
    today = now.astimezone(tz).date()
    out = Parsed()
    for it in data.get("tasks") or []:
        if not isinstance(it, dict):
            continue
        task = str(it.get("task") or "").strip()[:200]
        kind = "meeting" if it.get("kind") == "meeting" else "task"
        msg_id = _msg_id(it.get("msg"), msgs)
        due = hhmm = None
        try:
            if it.get("due") not in (None, "", "null"):
                due = date.fromisoformat(str(it["due"]))
                hhmm = _hhmm(it.get("time"))
        except (ValueError, TypeError):
            continue
        if not task or (kind == "meeting" and due is None):
            continue
        if due is not None and (due < today - timedelta(days=60) or due > today + timedelta(days=240)):
            continue
        due_utc, all_day = pt.due_fields(due, hhmm, tz) if due else (None, False)
        out.tasks.append({"task": task, "owner": str(it["owner"]).strip()[:60] if it.get("owner") else None,
                          "volunteered": it.get("volunteered") is True, "kind": kind, "due_utc": due_utc,
                          "all_day": all_day, "msg": msg_id, "quote": str(it.get("quote") or "")[:200]})
    for it in data.get("updates") or []:
        msg_id = _msg_id(it.get("msg"), msgs) if isinstance(it, dict) else None
        if (msg_id is not None and isinstance(it.get("id"), int) and it["id"] in open_ids
                and it.get("status") in ("done", "dropped")):  # an update must point at a message of this batch
            out.updates.append({"id": it["id"], "status": it["status"], "msg": msg_id})
    for it in data.get("decisions") or []:
        text = str(it.get("text") or "").strip() if isinstance(it, dict) else ""
        if text:
            out.decisions.append({"text": text[:240], "msg": _msg_id(it.get("msg"), msgs), "quote": str(it.get("quote") or "")[:200]})
    return out


def _msg_id(v, msgs: dict[int, GroupMessage]) -> int | None:
    return v if isinstance(v, int) and v in msgs else None


def may_close(db, t: pt.Task, src: GroupMessage | None, known: list[str], aliases: dict, mine: set[str]) -> str:
    """What a chat message that says a task is finished may do: "close" it, "suggest" it to the user, or "ignore" it.
    Only the task's own owner can close it, and only by sender id: the owner's name must map to exactly one sender id
    in the group (display names are free text, so a name used by two accounts is ambiguous and only suggests). With no
    id seen for the name, an exact name match is used. The user's own tasks and unowned tasks are never closed from the
    chat: the user decides in the panel (message ids are visible to the model, so a cited message proves nothing about
    who is speaking)."""
    if src is None:
        return "ignore"
    if t.owner_is_me or not t.owner_name:
        return "suggest"
    ids = {r[0] for r in db.conn.execute(
        "select distinct sender_id from group_messages where chat_id=? and lower(sender)=lower(?) and sender_id is not null",
        (t.chat_id, t.owner_name))}
    if len(ids) > 1:
        return "suggest"
    if ids:
        return "close" if src.sender_id in ids else "ignore"
    who, _ = pt.resolve_owner(src.sender, known, aliases, mine)
    same = (who or "").lower() == t.owner_name.lower() or src.sender.strip().lower() == t.owner_name.lower()
    return "close" if same else "ignore"


def apply_parsed(ctx, chat_id: int, parsed: Parsed, msgs: dict[int, GroupMessage]) -> dict:
    """Write validated results. Returns counts: new / merged / done / decisions / ignored."""
    store, now, s = pt.TaskStore(ctx.db), ctx.clock(), ctx.settings
    known = tg_group.sender_names(ctx.db, chat_id)
    aliases, mine, mid = pt.get_aliases(ctx.db), pt.my_names(s, ctx.db, chat_id), pt.me_id(s, ctx.db)
    counts = {"new": 0, "merged": 0, "done": 0, "suggested": 0, "decisions": 0, "ignored": 0}
    for t in parsed.tasks:
        src = msgs.get(t["msg"]) if t["msg"] else None
        if t["volunteered"] and src:
            # "I'll do it": the owner is whoever sent the message; it is YOU only if the sender id is yours
            name, _ = pt.resolve_owner(src.sender, known, aliases, set())
            is_me = mid is not None and src.sender_id == mid
            volunteered = True
        else:
            name, is_me = pt.resolve_owner(t["owner"], known, aliases, mine)
            volunteered = False
        if t["kind"] == "meeting":
            name, is_me, volunteered = None, False, False  # a meeting belongs to the group
        dup = store.find_similar(chat_id, t["task"], name, is_me, t["kind"], now, t["due_utc"], t["all_day"])
        if dup is not None:
            if dup.status != "open":
                continue
            if dup.owner_is_me and t["due_utc"] and store.is_move(dup, t["due_utc"], t["all_day"]) and not (
                    src is not None and mid is not None and src.sender_id == mid):
                # someone else moves the user's own date (and so their calendar event): the user decides, in the panel
                counts["suggested"] += store.add_suggestion(dup, "", src.sender if src else None, t["msg"],
                                                            t["quote"], now, "date", t["due_utc"], t["all_day"])
            elif store.merge_into(dup, t["due_utc"], t["all_day"], now, tz=ctx.settings.tz):
                counts["merged"] += 1
            continue
        asked_by = src.sender if (src and not volunteered and t["kind"] == "task") else None
        store.add(chat_id, t["task"], name, is_me, volunteered, t["kind"], t["due_utc"], t["all_day"],
                  t["msg"], t["quote"], now, asked_by)
        counts["new"] += 1
    for u in parsed.updates:
        cur = store.get(u["id"])
        if not (cur and cur.chat_id == chat_id and cur.status == "open"):
            continue
        src = msgs.get(u["msg"])
        verdict = may_close(ctx.db, cur, src, known, aliases, mine)
        if verdict == "close":
            counts["done"] += store.set_status(u["id"], u["status"], now)
        elif verdict == "suggest":
            # the chat says the user's (or nobody's, or an ambiguously named person's) task is finished: the user decides
            counts["suggested"] += store.add_suggestion(cur, u["status"], src.sender, u["msg"], src.text, now)
        else:
            counts["ignored"] += 1
            log.info("ignored a claim that task %s is %s from a message by someone else", cur.id, u["status"])
    for d in parsed.decisions:
        if store.add_decision(chat_id, d["text"], d["msg"], d["quote"], now):
            counts["decisions"] += 1
    return counts


def _line(m: GroupMessage, tz) -> str:
    return f"#{m.msg_id} [{m.ts.astimezone(tz):%a %d %b %H:%M}] {pt.clean_text(m.sender or '?')}: {pt.clean_text(' '.join(m.text.split()))}"


def build_input(ctx, chat_id: int, context: list[GroupMessage], new: list[GroupMessage]) -> str:
    tz = ctx.settings.tz
    open_lines = []
    for t in pt.TaskStore(ctx.db).open_tasks(chat_id):
        owner = t.owner_name or "nobody yet"
        open_lines.append(f"{t.id} | {owner} | {t.task} | {pt.due_text(t, tz) or 'no date'}")
    return "\n".join(["OPEN TASKS:", *(open_lines or ["(none)"]), "", "EARLIER MESSAGES (context only):",
                      *([_line(m, tz) for m in context] or ["(none)"]), "", "NEW MESSAGES:",
                      *[_line(m, tz) for m in new]])


def _chunks(rows: list[GroupMessage]) -> list[list[GroupMessage]]:
    out, cur, size = [], [], 0
    for m in rows:
        n = len(m.text) + 60
        if cur and (len(cur) >= CHUNK_MESSAGES or size + n > CHUNK_CHARS):
            out.append(cur)
            cur, size = [], 0
        cur.append(m)
        size += n
    return out + ([cur] if cur else [])


FAIL_LIMIT = 3  # a chunk the model keeps getting wrong is skipped after this many tries, so one bad chunk cannot stall a group


def _failed(ctx, chat_id: int, first: int) -> int:
    """Count a strike against the chunk that starts with message `first`: the model answered, but with unusable output.
    (Errors, timeouts and usage-cap stops are not strikes; they just retry on the next run.)"""
    key = f"project_tasks_fail:{chat_id}"
    cur = ctx.db.get_json_setting(key, {}) or {}
    n = (cur.get("n", 0) + 1) if cur.get("first") == first else 1
    ctx.db.set_json_setting(key, {"first": first, "n": n})
    return n


async def extract_group(ctx, g: dict) -> dict:
    """Read the messages of one project group that have not been handled yet, oldest first. Returns the counts written."""
    total = {"new": 0, "merged": 0, "done": 0, "suggested": 0, "decisions": 0, "ignored": 0}
    chat_id, now = g["id"], ctx.clock()
    run_text = ctx.extra.get("run_text") or claude_runner.run_text
    rows = tg_group.unprocessed(ctx.db, chat_id, now - FIRST_LOOKBACK, CHUNK_MESSAGES * MAX_CHUNKS_PER_RUN)
    if not rows:
        return total
    for new in _chunks(rows)[:MAX_CHUNKS_PER_RUN]:
        if usagecap.paused(ctx):
            break  # the cap was reached mid-run: stop here, what is not marked is read next time
        first = new[0].msg_id
        fails = ctx.db.get_json_setting(f"project_tasks_fail:{chat_id}", {}) or {}
        if fails.get("first") == first and fails.get("n", 0) >= FAIL_LIMIT:
            log.warning("project tasks: skipping %d messages of %s after %d failed tries", len(new), g["label"], FAIL_LIMIT)
            tg_group.mark_processed(ctx.db, chat_id, [m.msg_id for m in new])
            continue
        context = tg_group.context_before(ctx.db, chat_id, new[0].ts, CONTEXT_MESSAGES)
        prompt = claude_runner.load_prompt("project_tasks", today=now.astimezone(ctx.settings.tz).date().isoformat(),
                                           group=g["label"].replace("\n", " "),
                                           user=persona.user_name(ctx.settings) or "the student")
        try:
            with claude_runner.labelled(JOB):
                raw = await run_text(prompt, build_input(ctx, chat_id, context, new),
                                     model=usagecap.model_for(ctx.db, JOB), run_as=ctx.settings.claude_run_as)
        except claude_runner.ClaudeError as e:
            log.warning("project task extraction failed (will retry next run): %s", e)
            break
        by_id = {m.msg_id: m for m in new}
        parsed = parse_extraction(raw, by_id, {t.id for t in pt.TaskStore(ctx.db).open_tasks(chat_id)}, now, ctx.settings.tz)
        if parsed is None:
            log.warning("project task extraction returned unusable JSON (try %d, will retry)", _failed(ctx, chat_id, first))
            break
        for k, v in apply_parsed(ctx, chat_id, parsed, by_id).items():
            total[k] += v
        tg_group.mark_processed(ctx.db, chat_id, list(by_id))  # only after the results are stored
    return total


async def extract_all(ctx) -> dict:
    """Hourly: every project group, then mirror the user's own dated tasks and the meetings into the deadlines table."""
    total = {"new": 0, "merged": 0, "done": 0, "suggested": 0, "decisions": 0, "ignored": 0}
    tg_group.prune_processed(ctx.db, ctx.clock() - timedelta(days=30))
    for g in pt.project_groups(ctx.settings, ctx.db):
        if usagecap.paused(ctx):
            break  # nothing is read and no watermark moves; the calendar mirror below is local and still runs
        try:
            for k, v in (await extract_group(ctx, g)).items():
                total[k] += v
        except Exception:  # noqa: BLE001 - one group must not stop the others
            log.warning("project task extraction crashed for %s", g["label"], exc_info=True)
    pt.sync_deadlines(ctx.settings, ctx.db, ctx.clock())
    return total


# ---------------------------------------------------------------- digest and /tasks
def _owner_label(t: pt.Task) -> str:
    return "You" if t.owner_is_me else (t.owner_name or "Nobody yet")


def _order(names: list[str]) -> list[str]:
    return sorted(names, key=lambda n: (n != "You", n == "Nobody yet", n.lower()))


def digest_data(ctx, g: dict) -> dict:
    tz, now = ctx.settings.tz, ctx.clock()
    store = pt.TaskStore(ctx.db)
    today = now.astimezone(tz).date()
    open_ = store.for_group(g["id"], now, "open")
    week: dict[str, list[pt.Task]] = {}
    overdue: list[pt.Task] = []
    undated = 0
    for t in open_:
        if not t.due_utc:
            undated += t.kind == "task"
            continue
        moment = pt.due_moment(t, tz)
        if t.kind == "meeting":
            if now <= moment or moment.date() == today:
                if moment.date() <= today + timedelta(days=7):
                    week.setdefault("Group", []).append(t)
        elif moment < now:
            overdue.append(t)
        elif moment.date() <= today + timedelta(days=7):
            week.setdefault(_owner_label(t), []).append(t)
    decided = store.decisions(g["id"], now - timedelta(days=7))
    for lst in week.values():
        lst.sort(key=lambda t: pt.due_moment(t, tz))
    overdue.sort(key=lambda t: pt.due_moment(t, tz))
    return {"week": week, "overdue": overdue, "decided": decided, "undated": undated,
            "suggested": store.pending_suggestions(g["id"])}


def digest_text(ctx, g: dict, always: bool = False) -> str | None:
    """The per-group digest, built from the stored data (no model call). None when there is nothing to say."""
    d, tz, a = digest_data(ctx, g), ctx.settings.tz, persona.address(ctx.settings)
    if not (d["week"] or d["overdue"] or d["decided"] or d["suggested"]) and not always:
        return None
    lines = [f"{g['label']}: where the project stands, {a}."]
    if d["suggested"]:
        lines += ["", "Waiting for your word (Close or Keep in the panel's Tasks tab)"]
        lines += [_suggestion_line(x, tz) for x in d["suggested"]]
    if d["week"]:
        lines += ["", "This week"]
        for who in _order(list(d["week"])):
            lines.append(f"{who}:" if who != "Group" else "Group:")
            for t in d["week"][who]:
                note = ""
                if t.owner_is_me and not (t.volunteered or t.confirmed):
                    note = f" (asked by {t.assigned_by or 'someone'}; not on your calendar until you confirm it in the panel)"
                lines.append(f"- {t.task}, {pt.due_text(t, tz)}{note}")
    if d["overdue"]:
        lines += ["", "Overdue"]
        lines += [f"- {_owner_label(t)}: {t.task}, was due {pt.due_text(t, tz)}" for t in d["overdue"]]
    if d["decided"]:
        lines += ["", "Decided"]
        lines += [f"- {x['text']}" for x in d["decided"]]
    if not (d["week"] or d["overdue"] or d["decided"] or d["suggested"]):
        lines += ["", f"Nothing dated or overdue, {a}."]
    if d["undated"]:
        lines += ["", f"{d['undated']} open item(s) have no date yet."]
    mine_overdue = sum(1 for t in d["overdue"] if t.owner_is_me)
    if mine_overdue:
        lines += ["", f"{mine_overdue} of the overdue item(s) are yours, {a}. I mention it only because I must."]
    return "\n".join(lines)


def find_group(ctx, ref: str) -> dict | None:
    groups_ = pt.project_groups(ctx.settings, ctx.db)
    ref = (ref or "").strip().lower()
    if not ref:
        return None
    for g in groups_:
        if ref == str(g["id"]) or ref == g["label"].lower():
            return g
    for g in groups_:
        if ref in g["label"].lower():
            return g
    return None


async def tasks_command(ctx, ref: str = "") -> str:
    """/tasks [group]: the same digest, on demand (also while alerts are paused: you asked)."""
    a = persona.address(ctx.settings)
    groups_ = pt.project_groups(ctx.settings, ctx.db)
    if not groups_:
        return (f"No project group is set up, {a}. In the panel's Chats tab, set the group's type to Project "
                "and I shall keep its list.")
    if ref.strip():
        g = find_group(ctx, ref)
        if g is None:
            return f"I have no project group matching \"{ref.strip()[:40]}\", {a}. Known: " + ", ".join(x["label"] for x in groups_) + "."
        groups_ = [g]
    return "\n\n".join(digest_text(ctx, g, always=True) for g in groups_)


async def maybe_weekly_digest(ctx) -> int:
    """Runs every few minutes; one message per project group on Sunday from 19:00 Singapore time (within 3 hours).
    /pause stops it. Returns how many digests were sent."""
    if ctx.db.get_setting("paused") == "1":
        return 0
    local = ctx.clock().astimezone(ctx.settings.tz)
    due = local.replace(hour=DIGEST_HOUR, minute=0, second=0, microsecond=0)
    if local.weekday() != 6 or local < due or local - due > DIGEST_GRACE:
        return 0
    sent = 0
    for g in pt.project_groups(ctx.settings, ctx.db):
        key = f"last_project_digest:{g['id']}"
        if ctx.db.get_setting(key) == local.date().isoformat():
            continue
        text = digest_text(ctx, g)
        if text:
            try:
                await ctx.notifier.send(text)
            except Exception:  # noqa: BLE001 - not marked sent, so the next poll inside the grace window retries
                log.warning("project digest for %s could not be sent; will retry", g["label"], exc_info=True)
                continue
            sent += 1
        ctx.db.set_setting(key, local.date().isoformat())
    return sent


# ---------------------------------------------------------------- views for the API, MCP tools and the panel
def task_json(t: pt.Task, label: str, tz, suggestion: dict | None = None) -> dict:
    return {"id": t.id, "group_id": t.chat_id, "group": label, "task": t.task, "kind": t.kind,
            "owner": "You" if t.owner_is_me else t.owner_name, "owner_is_me": t.owner_is_me,
            "volunteered": t.volunteered, "asked_by": t.assigned_by if (not t.volunteered and t.kind == "task") else None,
            "needs_confirm": bool(t.owner_is_me and not (t.volunteered or t.confirmed) and t.kind == "task"
                                  and t.status == "open"), "due": pt.due_text(t, tz) or None,
            "due_date": t.due_utc[:10] if t.due_utc and t.all_day else (pt.due_moment(t, tz).date().isoformat() if t.due_utc else None),
            "due_time": None if (t.all_day or not t.due_utc) else pt.due_moment(t, tz).strftime("%H:%M"),
            "status": t.status, "source_msg_id": t.source_msg_id, "source_quote": t.source_quote,
            "done_at": t.done_at, "date_note": t.date_note, "suggestion": suggestion}


def group_matches(g: dict, ref: str) -> bool:
    ref = str(ref).strip().lower()
    return ref in ("project", str(g["id"])) or ref in g["label"].lower()


def listing(settings, db, now: datetime, group_ref: str | None = None, mine_only: bool = False,
            include_closed: bool = True) -> list[dict]:
    """Visible tasks (open, plus done/dropped for 7 days) of the project groups, optionally one group / only yours."""
    store, tz = pt.TaskStore(db), settings.tz
    out = []
    for g in pt.project_groups(settings, db):
        if group_ref and not group_matches(g, group_ref):
            continue
        sug = {x["task_id"]: x for x in store.pending_suggestions(g["id"])}
        for t in store.for_group(g["id"], now):
            if (mine_only and not t.owner_is_me) or (not include_closed and t.status != "open"):
                continue
            out.append(task_json(t, g["label"], tz, _sug_json(sug.get(t.id), tz)))
    return out


def _suggestion_line(x: dict, tz) -> str:
    who = x["claimed_by"] or "Someone"
    if x["kind"] == "date":
        return f"- {who} says \"{x['task']}\" moves to {pt.fmt_due(x['new_due'], bool(x['new_all_day']), tz)}"
    return f"- {who} says \"{x['task']}\" is {'done' if x['new_status'] == 'done' else 'no longer needed'}"


def _sug_json(x: dict | None, tz=None) -> dict | None:
    if not x:
        return None
    return {"id": x["id"], "by": x["claimed_by"], "kind": x["kind"], "status": x["new_status"], "quote": x["quote"] or "",
            "new_due": pt.fmt_due(x["new_due"], bool(x["new_all_day"]), tz) if x["kind"] == "date" else None}
