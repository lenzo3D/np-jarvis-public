"""Who owes what in a project group: the task store, name mapping, de-duplication and the calendar mirror.

Only groups of kind "project" are used. The model never writes here directly: `ops/project_tasks.py` validates its
JSON and calls these functions. Nothing is ever deleted; "dropped" is a status. Done and dropped items stay visible
for 7 days, then are hidden (still in the database).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from core import groups
from core.deadlines import SGT, Deadline, DeadlineStore, _norm_words, _numbers, similar

STATUSES = ("open", "done", "dropped")
KINDS = ("task", "meeting")
SHOW_CLOSED_FOR = timedelta(days=7)
MAX_ALIASES = 60
PROJECT_KEY = "proj:"  # prefix of the deadlines-table keys this module owns


@dataclass
class Task:
    id: int
    chat_id: int
    task: str
    owner_name: str | None
    owner_is_me: bool
    volunteered: bool
    kind: str
    due_utc: str | None
    all_day: bool
    source_msg_id: int | None
    source_quote: str
    status: str
    created_at: str
    updated_at: str
    done_at: str | None
    assigned_by: str | None = None  # who asked, when someone else gave the task
    confirmed: bool = False  # the user okayed putting a task someone else gave them on their calendar
    date_note: str | None = None  # short history when the date moved ("was Fri 06 Nov")


def _utc(now: datetime) -> str:
    """All stored timestamps are UTC ISO strings, whatever zone the caller's clock uses."""
    return now.astimezone(timezone.utc).isoformat()


def _dt(iso: str | None) -> datetime | None:
    try:
        d = datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return None
    return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)


_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


def clean_text(text: str, urls: bool = False) -> str:
    """Control and bidi characters out; links too when the text is going to Google Calendar."""
    text = _CTRL.sub(" ", text or "")
    if urls:
        text = _URL.sub("", text)
    return re.sub(r" {2,}", " ", text).strip()


def _task(r) -> Task:
    return Task(r["id"], r["chat_id"], r["task"], r["owner_name"], bool(r["owner_is_me"]), bool(r["volunteered"]),
                r["kind"], r["due_utc"], bool(r["all_day"]), r["source_msg_id"], r["source_quote"] or "",
                r["status"], r["created_at"], r["updated_at"], r["done_at"], r["assigned_by"], bool(r["confirmed"]), r["date_note"])


# ---------------------------------------------------------------- who is who
def get_aliases(db) -> dict[str, str]:
    """alias (lower case) -> person. The person "me" means the user."""
    raw = db.get_json_setting("project_aliases", {})
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def set_aliases(db, pairs: list[dict]) -> dict[str, str]:
    """Replace the whole alias list. Blank or over-long entries are dropped; the user's own alias list is plain text."""
    cleaned: dict[str, str] = {}
    for p in pairs[:MAX_ALIASES] if isinstance(pairs, list) else []:
        if not isinstance(p, dict):
            continue
        alias, person = str(p.get("alias") or "").strip()[:40], str(p.get("person") or "").strip()[:40]
        if alias and person:
            cleaned[alias.lower()] = person
    db.set_json_setting("project_aliases", cleaned)
    return cleaned


def me_id(settings, db) -> int | None:
    """The user's own Telegram id: what the account reader reported, else the allowed id of the Ops bot."""
    try:
        return int(db.get_setting("telegram_me_id"))
    except (TypeError, ValueError):
        return getattr(settings, "allowed_telegram_id", None)


def my_names(settings, db, chat_id: int | None = None) -> set[str]:
    """Lower-case names that mean the user: their display names in the groups (by Telegram id) and USER_NAME."""
    from core import tg_group

    mid = me_id(settings, db)
    names = {n.lower() for n in tg_group.sender_names(db, chat_id, mid)} if (mid and chat_id is not None) else set()
    if getattr(settings, "user_name", ""):
        names.add(settings.user_name.lower())
    return names


def resolve_owner(raw: str | None, known: list[str], aliases: dict[str, str], mine: set[str]) -> tuple[str | None, bool]:
    """Map what the chat called someone to (name, is_me). Text from the chat can never make a task yours with a word
    like "you" or "me": is_me is true only for an exact match with one of your own display names, or an alias YOU
    mapped to "me" in the panel. Otherwise: alias list, exact sender name, a first name matching one known sender."""
    name = clean_text(raw or "")[:60]
    if not name:
        return None, False
    person = aliases.get(name.lower())
    if person is not None:
        if person.strip().lower() == "me":
            return "Me", True
        name = person
    low = name.lower()
    if low in mine:
        return name, True
    for k in known:
        if k.lower() == low:
            return k, k.lower() in mine
    first = [k for k in known if k.split() and k.split()[0].lower() == low]
    if len(first) == 1:
        return first[0], first[0].lower() in mine
    return name, False


# ---------------------------------------------------------------- dates
def due_fields(day: date, hhmm: tuple[int, int] | None, tz=SGT) -> tuple[str, bool]:
    """(due_utc, all_day): an SGT date for date-only tasks (as the deadlines table does), else an ISO UTC moment."""
    if hhmm is None:
        return day.isoformat(), True
    local = datetime(day.year, day.month, day.day, hhmm[0], hhmm[1], tzinfo=tz)
    return local.astimezone(timezone.utc).isoformat(), False


def due_moment(t: Task, tz=SGT) -> datetime:
    """When a task is due as a timezone-aware moment (all-day tasks: the end of that day)."""
    if t.all_day:
        d = date.fromisoformat(t.due_utc[:10])
        return datetime(d.year, d.month, d.day, 23, 59, tzinfo=tz)
    return datetime.fromisoformat(t.due_utc).astimezone(tz)


def due_text(t: Task, tz=SGT) -> str:
    return fmt_due(t.due_utc, t.all_day, tz)


def fmt_due(due_utc: str | None, all_day: bool, tz=SGT) -> str:
    if not due_utc:
        return ""
    if all_day:
        m = datetime.combine(date.fromisoformat(due_utc[:10]), datetime.min.time(), tzinfo=tz)
    else:
        m = datetime.fromisoformat(due_utc).astimezone(tz)
    return m.strftime("%a %d %b") if all_day else m.strftime("%a %d %b %H:%M")


# ---------------------------------------------------------------- the store
def _same_owner(a_name, a_me, b_name, b_me) -> bool:
    if a_me or b_me:
        return bool(a_me) == bool(b_me)
    return (a_name or "").lower() == (b_name or "").lower()


GENERIC = {"do", "does", "doing", "finish", "complete", "make", "prepare", "write", "send", "handle", "work", "take",
           "get", "update", "start", "up", "it", "my", "our", "part", "task", "will", "can", "i", "ll"}


def words_similar(a: str, b: str, threshold: float = 0.5, strip_generic: bool = False) -> bool:
    """Same wording test as deadlines.similar: matching numbers and enough words in common. For tasks, generic verbs
    ("finish", "do") are ignored and the bar is higher, so "slides for the report" is not "slides for the presentation"."""
    wa, wb = set(_norm_words(a)), set(_norm_words(b))
    if strip_generic and wa - GENERIC and wb - GENERIC:
        wa, wb = wa - GENERIC, wb - GENERIC
    if not wa or not wb or _numbers(wa) != _numbers(wb):
        return False
    return len(wa & wb) / len(wa | wb) >= threshold


def _due_day(due_utc: str | None, all_day: bool) -> date | None:
    if not due_utc:
        return None
    return date.fromisoformat(due_utc[:10]) if all_day else datetime.fromisoformat(due_utc).astimezone(SGT).date()


class TaskStore:
    def __init__(self, db):
        self.db = db

    def get(self, task_id: int) -> Task | None:
        r = self.db.conn.execute("select * from group_tasks where id=?", (int(task_id),)).fetchone()
        return _task(r) if r else None

    def add(self, chat_id: int, task: str, owner_name: str | None, owner_is_me: bool, volunteered: bool, kind: str,
            due_utc: str | None, all_day: bool, source_msg_id: int | None, source_quote: str, now: datetime,
            assigned_by: str | None = None) -> int:
        cur = self.db.conn.execute(
            "insert into group_tasks(chat_id, task, owner_name, owner_is_me, volunteered, kind, due_utc, all_day, "
            "source_msg_id, source_quote, status, created_at, updated_at, assigned_by) values (?,?,?,?,?,?,?,?,?,?, 'open', ?, ?, ?)",
            (chat_id, clean_text(task)[:200], owner_name, int(owner_is_me), int(volunteered), kind if kind in KINDS else "task",
             due_utc, int(all_day), source_msg_id, clean_text(source_quote)[:200], _utc(now), _utc(now),
             clean_text(assigned_by)[:60] if assigned_by else None))
        return cur.lastrowid

    def find_similar(self, chat_id: int, task: str, owner_name: str | None, owner_is_me: bool, kind: str,
                     now: datetime, due_utc: str | None = None, all_day: bool = False,
                     closed_within: timedelta = timedelta(days=14)) -> Task | None:
        """The same task already on the list: an open one (or one closed recently) of the same owner that says the same
        thing. Meetings must also be on the same day. An open task is the same task whatever its date (a moved date is
        an update, see merge_into); a recently closed one only if the dates are within a day, else it is a new task."""
        floor = now.astimezone(timezone.utc) - closed_within
        new_day = _due_day(due_utc, all_day)
        for r in self.db.conn.execute("select * from group_tasks where chat_id=? and kind=? order by status='open' desc, id",
                                      (chat_id, kind)):
            t = _task(r)
            if t.status != "open" and (_dt(t.done_at or t.updated_at) or floor) < floor:
                continue
            if not _same_owner(owner_name, owner_is_me, t.owner_name, t.owner_is_me):
                continue
            old_day = _due_day(t.due_utc, t.all_day)
            if kind == "meeting":
                if old_day != new_day or not words_similar(task, t.task):
                    continue
            else:
                if not words_similar(task, t.task, 0.6, strip_generic=True):
                    continue
                if t.status != "open" and old_day and new_day and abs((old_day - new_day).days) > 1:
                    continue
            return t
        return None

    def merge_into(self, existing: Task, due_utc: str | None, all_day: bool, now: datetime, tz=SGT) -> bool:
        """The same task mentioned again: keep one row. A date is filled in when the row had none. A different date
        moves the task (the calendar mirror follows on its next sync) and the old date is kept as a short note."""
        if not due_utc:
            return False
        if not existing.due_utc:
            self.db.conn.execute("update group_tasks set due_utc=?, all_day=?, updated_at=? where id=?",
                                 (due_utc, int(all_day), _utc(now), existing.id))
            return True
        if not self.is_move(existing, due_utc, all_day):
            return False
        note = f"was {due_text(existing, tz)}"
        history = (existing.date_note + "; " if existing.date_note else "") + note
        self.db.conn.execute("update group_tasks set due_utc=?, all_day=?, date_note=?, updated_at=? where id=?",
                             (due_utc, int(all_day), history[-120:], _utc(now), existing.id))
        return True

    @staticmethod
    def is_move(existing: Task, due_utc: str, all_day: bool) -> bool:
        """A dated task and a different date: another day, or another time on the same day. (A bare "Friday" does not
        undo an earlier "Friday 15:00".)"""
        if not existing.due_utc:
            return False
        if _due_day(existing.due_utc, existing.all_day) == _due_day(due_utc, all_day):
            return not (existing.all_day or all_day or existing.due_utc == due_utc)
        return True

    # suggestions: "somebody says this is finished / moved", for the user to accept or keep
    def add_suggestion(self, t: Task, new_status: str, claimed_by: str | None, msg_id: int | None, quote: str,
                       now: datetime, kind: str = "close", new_due: str | None = None, new_all_day: bool = False) -> int:
        if kind == "date":
            same = ("select 1 from group_task_suggestions where task_id=? and state='pending' and kind='date' and new_due=?",
                    (t.id, new_due))
            if not new_due:
                return 0
        else:
            same = ("select 1 from group_task_suggestions where task_id=? and state='pending' and kind='close' and new_status=?",
                    (t.id, new_status))
            if new_status not in ("done", "dropped"):
                return 0
        if self.db.conn.execute(*same).fetchone():
            return 0
        self.db.conn.execute(
            "insert into group_task_suggestions(task_id, chat_id, new_status, claimed_by, msg_id, quote, created_at, kind, "
            "new_due, new_all_day) values (?,?,?,?,?,?,?,?,?,?)",
            (t.id, t.chat_id, new_status, clean_text(claimed_by or "")[:60] or None, msg_id, clean_text(quote)[:200],
             _utc(now), kind, new_due, int(new_all_day)))
        return 1

    def pending_suggestions(self, chat_id: int | None = None) -> list[dict]:
        sql, args = ("select s.*, t.task from group_task_suggestions s join group_tasks t on t.id=s.task_id "
                     "where s.state='pending' and t.status='open'"), []
        if chat_id is not None:
            sql += " and s.chat_id=?"
            args.append(chat_id)
        return [dict(r) for r in self.db.conn.execute(sql + " order by s.id", args)]

    def suggestion(self, suggestion_id) -> dict | None:
        r = self.db.conn.execute("select * from group_task_suggestions where id=?", (int(suggestion_id),)).fetchone()
        return dict(r) if r else None

    def resolve_suggestion(self, suggestion_id: int, accept: bool, now: datetime) -> bool:
        """Close (accept) or keep the task. Only a pending suggestion can be answered, once."""
        x = self.suggestion(suggestion_id)
        if x is None or x["state"] != "pending":
            return False
        self.db.conn.execute("update group_task_suggestions set state=? where id=?",
                             ("accepted" if accept else "kept", x["id"]))
        if accept and x["kind"] == "date":
            t = self.get(x["task_id"])
            if t is not None and t.status == "open":
                self.merge_into(t, x["new_due"], bool(x["new_all_day"]), now)
        elif accept:
            self.set_status(x["task_id"], x["new_status"], now)
        else:  # keep: every pending claim of this kind on this task is answered
            self.db.conn.execute("update group_task_suggestions set state='kept' where task_id=? and state='pending' and kind=?",
                                 (x["task_id"], x["kind"]))
        return True

    def set_status(self, task_id: int, status: str, now: datetime) -> bool:
        if status not in STATUSES or self.get(task_id) is None:
            return False
        done_at = _utc(now) if status in ("done", "dropped") else None
        self.db.conn.execute("update group_tasks set status=?, updated_at=?, done_at=? where id=?",
                             (status, _utc(now), done_at, int(task_id)))
        return True

    def confirm(self, task_id: int, now: datetime) -> bool:
        """The user okays a task someone else gave them: from now on it may go on their calendar."""
        if self.get(task_id) is None:
            return False
        self.db.conn.execute("update group_tasks set confirmed=1, updated_at=? where id=?", (_utc(now), int(task_id)))
        return True

    def edit(self, task_id: int, now: datetime, owner_name=..., owner_is_me=..., due_utc=..., all_day=...) -> bool:
        """Panel edits. Arguments left out keep their value; due_utc=None clears the date."""
        t = self.get(task_id)
        if t is None:
            return False
        vals = {"owner_name": t.owner_name if owner_name is ... else owner_name,
                "owner_is_me": int(t.owner_is_me if owner_is_me is ... else owner_is_me),
                "due_utc": t.due_utc if due_utc is ... else due_utc,
                "all_day": int(t.all_day if all_day is ... else all_day)}
        if vals["due_utc"] is None:
            vals["all_day"] = 0
        # the user assigning a task to themselves in the panel is their own say-so, so no further confirmation is needed
        confirmed = 1 if (owner_is_me is not ... and owner_is_me) else int(t.confirmed)
        self.db.conn.execute("update group_tasks set owner_name=?, owner_is_me=?, due_utc=?, all_day=?, confirmed=?, updated_at=? where id=?",
                             (vals["owner_name"], vals["owner_is_me"], vals["due_utc"], vals["all_day"], confirmed, _utc(now), int(task_id)))
        return True

    def for_group(self, chat_id: int | None, now: datetime, status: str | None = None, include_hidden: bool = False) -> list[Task]:
        sql, args = "select * from group_tasks where 1=1", []
        if chat_id is not None:
            sql += " and chat_id=?"
            args.append(chat_id)
        if status:
            sql += " and status=?"
            args.append(status)
        out = [_task(r) for r in self.db.conn.execute(sql + " order by id", args)]
        if include_hidden:
            return out
        floor = now.astimezone(timezone.utc) - SHOW_CLOSED_FOR
        return [t for t in out if t.status == "open" or (_dt(t.done_at or t.updated_at) or floor) >= floor]

    def open_tasks(self, chat_id: int) -> list[Task]:
        return [_task(r) for r in self.db.conn.execute(
            "select * from group_tasks where chat_id=? and status='open' order by id", (chat_id,))]

    # decisions
    def add_decision(self, chat_id: int, text: str, source_msg_id: int | None, quote: str, now: datetime) -> bool:
        text = clean_text(text)[:240]
        floor = now.astimezone(timezone.utc) - timedelta(days=30)
        for r in self.db.conn.execute("select text, created_at from group_decisions where chat_id=?", (chat_id,)):
            if (_dt(r["created_at"]) or floor) >= floor and (r["text"].lower() == text.lower() or words_similar(text, r["text"])):
                return False
        self.db.conn.execute("insert into group_decisions(chat_id, text, source_msg_id, source_quote, created_at) values (?,?,?,?,?)",
                             (chat_id, text, source_msg_id, clean_text(quote)[:200], _utc(now)))
        return True

    def decisions(self, chat_id: int | None, since: datetime) -> list[dict]:
        sql, args = "select * from group_decisions where 1=1", []
        if chat_id is not None:
            sql += " and chat_id=?"
            args.append(chat_id)
        floor = since.astimezone(timezone.utc)
        return [dict(r) for r in self.db.conn.execute(sql + " order by id", args)
                if (_dt(r["created_at"]) or floor) >= floor]


def rename_owner(db, old: str, new: str, new_is_me: bool, now: datetime) -> int:
    """After the alias list changes: open tasks that were filed under `old` move to `new`."""
    cur = db.conn.execute("update group_tasks set owner_name=?, owner_is_me=?, updated_at=? "
                          "where status='open' and lower(owner_name)=lower(?) and owner_is_me=0",
                          (new, int(new_is_me), _utc(now), old))
    return cur.rowcount


def apply_aliases(settings, db, now: datetime) -> int:
    """Re-file open tasks whose owner is a known alias (called when the alias list is saved in the panel)."""
    changed = 0
    for alias, person in get_aliases(db).items():
        name, is_me = ("Me", True) if person.strip().lower() == "me" else resolve_owner(person, [], {}, my_names(settings, db))
        changed += rename_owner(db, alias, name or person, is_me, now)
    return changed


# ---------------------------------------------------------------- project groups
def project_groups(settings, db) -> list[dict]:
    return [g for g in groups.configured(settings, db) if g["kind"] == "project"]


# ---------------------------------------------------------------- calendar mirror (tentative events)
def sync_deadlines(settings, db, now: datetime) -> int:
    """Mirror the user's OWN dated open tasks and the groups' meetings into the deadlines table (source "project",
    tentative). Other people's tasks never go to the calendar. Done, dropped, re-owned or undated items are marked
    gone, so calendar sync removes their events. Rows the user deleted in Google stay deleted (the store's tombstone)."""
    store, tasks, tz = DeadlineStore(db), TaskStore(db), SGT
    wanted: dict[str, Deadline] = {}
    others = [d for d in store.active() if not d.key.startswith(PROJECT_KEY)]
    deleted = store.by_status("user_deleted")
    for g in project_groups(settings, db):
        for t in tasks.for_group(g["id"], now, "open"):
            # meetings, and tasks the user took on themselves, go automatically; a task someone else gave the user
            # waits until the user confirms it (anyone in the chat could otherwise write into their calendar)
            if not t.due_utc or not (t.kind == "meeting" or (t.owner_is_me and (t.volunteered or t.confirmed))):
                continue
            d = _deadline(t, g["label"])
            if similar(Deadline(**{**d.__dict__, "title": t.task}), others + deleted):
                continue  # a confirmed or already-tracked date for the same thing exists; do not double it up
            wanted[d.key] = d
    changed = 0
    for d in wanted.values():
        if store.get(d.key) is not None and store.is_user_deleted(d.key):
            continue
        changed += store.upsert(d, now)
    for d in store.active():
        if d.key.startswith(PROJECT_KEY) and d.key not in wanted:
            store.mark_gone(d.key)
            changed += 1
    return changed


def _deadline(t: Task, label: str) -> Deadline:
    title = clean_text(f"{label}: {t.task}", urls=True)[:120]
    if t.all_day:
        start, end = t.due_utc[:10], None
    elif t.kind == "meeting":
        start, end = t.due_utc, None
    else:
        due = datetime.fromisoformat(t.due_utc)
        start, end = (due - timedelta(minutes=30)).isoformat(), due.isoformat()
    return Deadline(key=f"{PROJECT_KEY}{t.id}", title=title, course_code=None,
                    kind="meeting" if t.kind == "meeting" else "other", start_utc=start, end_utc=end, all_day=t.all_day,
                    source="project", source_ref=str(t.chat_id), confidence="tentative", quote=clean_text(t.source_quote, urls=True)[:200])
