"""Panel view of the project group task lists (who owes what), plus the few edits the panel allows.

Everything here is local: marking, editing and the alias list only change JARVIS's own database. Nothing is posted to a
group and nothing is sent anywhere. Group text (task wording, quotes, names) is shown as plain text by the page.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from core import project_tasks as pt
from core import tg_group
from ops import project_tasks as ops_tasks


def overview(settings, db, now: datetime) -> dict:
    store, tz = pt.TaskStore(db), settings.tz
    out = []
    for g in pt.project_groups(settings, db):
        sug = {x["task_id"]: x for x in store.pending_suggestions(g["id"])}
        tasks = [ops_tasks.task_json(t, g["label"], tz, ops_tasks._sug_json(sug.get(t.id), tz)) for t in store.for_group(g["id"], now)]
        decisions = [{"id": d["id"], "text": d["text"], "quote": d["source_quote"] or ""}
                     for d in store.decisions(g["id"], now - timedelta(days=14))]
        out.append({"id": g["id"], "label": g["label"], "tasks": tasks, "decisions": decisions,
                    "people": sorted(tg_group.sender_names(db, g["id"]), key=str.lower)})
    aliases = [{"alias": a, "person": p} for a, p in sorted(pt.get_aliases(db).items())]
    return {"groups": out, "aliases": aliases, "has_project_groups": bool(out)}


def _task_in_project_group(settings, db, task_id) -> pt.Task | None:
    try:
        t = pt.TaskStore(db).get(int(task_id))
    except (TypeError, ValueError):
        return None
    return t if t and t.chat_id in {g["id"] for g in pt.project_groups(settings, db)} else None


def set_status(settings, db, task_id, status: str, now: datetime) -> dict:
    if status not in pt.STATUSES or _task_in_project_group(settings, db, task_id) is None:
        return {"ok": False, "message": "Unknown task or status."}
    pt.TaskStore(db).set_status(int(task_id), status, now)
    pt.sync_deadlines(settings, db, now)
    return {"ok": True}


def suggestion(settings, db, suggestion_id, action: str, now: datetime) -> dict:
    """Answer "somebody says this task is finished": close it, or keep it open."""
    try:
        x = pt.TaskStore(db).suggestion(int(suggestion_id))
    except (TypeError, ValueError):
        x = None
    if x is None or action not in ("close", "accept", "keep") or _task_in_project_group(settings, db, x["task_id"]) is None:
        return {"ok": False, "message": "Unknown suggestion."}
    if not pt.TaskStore(db).resolve_suggestion(x["id"], action in ("close", "accept"), now):
        return {"ok": False, "message": "That was already answered."}
    pt.sync_deadlines(settings, db, now)
    return {"ok": True}


def edit(settings, db, body: dict, now: datetime) -> dict:
    t = _task_in_project_group(settings, db, body.get("id"))
    if t is None:
        return {"ok": False, "message": "Unknown task."}
    changes: dict = {}
    if "owner" in body:
        raw = str(body.get("owner") or "").strip()
        if raw:
            name, is_me = pt.resolve_owner(raw, tg_group.sender_names(db, t.chat_id), pt.get_aliases(db),
                                           pt.my_names(settings, db, t.chat_id))
        else:
            name, is_me = None, False
        if isinstance(body.get("owner_is_me"), bool):
            is_me = body["owner_is_me"]
        if is_me and (name is None or name.lower() == "me"):
            name = "Me"
        changes.update(owner_name=name, owner_is_me=is_me)
    elif isinstance(body.get("owner_is_me"), bool):
        changes["owner_is_me"] = body["owner_is_me"]
    if "due_date" in body:
        day = str(body.get("due_date") or "").strip()
        if not day:
            changes.update(due_utc=None, all_day=False)
        else:
            try:
                d = date.fromisoformat(day)
                tm = str(body.get("due_time") or "").strip()
                m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", tm) if tm else None
                if tm and not m:
                    raise ValueError("time")
            except ValueError:
                return {"ok": False, "message": "Use a date like 2026-11-06 and a time like 15:00."}
            due, all_day = pt.due_fields(d, (int(m.group(1)), int(m.group(2))) if m else None, settings.tz)
            changes.update(due_utc=due, all_day=all_day)
    pt.TaskStore(db).edit(t.id, now, **changes)
    pt.sync_deadlines(settings, db, now)
    return {"ok": True}


def confirm(settings, db, task_id, now: datetime) -> dict:
    """The user okays putting a task someone else gave them on their calendar."""
    t = _task_in_project_group(settings, db, task_id)
    if t is None:
        return {"ok": False, "message": "Unknown task."}
    pt.TaskStore(db).confirm(t.id, now)
    pt.sync_deadlines(settings, db, now)
    return {"ok": True}


def save_aliases(settings, db, pairs, now: datetime) -> dict:
    saved = pt.set_aliases(db, pairs if isinstance(pairs, list) else [])
    moved = pt.apply_aliases(settings, db, now)
    pt.sync_deadlines(settings, db, now)
    return {"ok": True, "aliases": [{"alias": a, "person": p} for a, p in sorted(saved.items())], "moved": moved}
