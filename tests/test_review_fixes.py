"""Regression tests for the deadlines/calendar review findings (I4-I8, M1, M2, M12)."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from core.config import load_settings
from core.deadlines import Deadline, DeadlineStore, local_date, similar
from core.library import Library
from core.models import Course, Item
from ops import deadline_jobs
from ops.deadline_sources import confirmed_from_db, extract_chats, extract_from_library, refresh_confirmed
from ops.diff import content_hash
from ops.watcher import Services
from tests.fakes import Clock, FakeBS, FakeNotifier

NOW = datetime(2026, 10, 27, 2, 0, tzinfo=timezone.utc)
C = "26S2-73TMBI_013687"


def d(key, title, start, conf="tentative", course=C, kind="quiz", all_day=True, end=None, source="announcement"):
    return Deadline(key, title, course, kind, start, end, all_day, source, "", conf, "")


def svc(db, env_file, reply=None, fail=False):
    s = Services(settings=load_settings(env_file), db=db, sessions=None, bs=FakeBS(), notifier=FakeNotifier(),
                 clock=Clock(NOW))
    s.bs.course_list = [Course(1, C, "TMBI")]
    calls = []

    async def run_text(prompt, text, model="sonnet", timeout=180, run_as=None):
        calls.append(text)
        if fail:
            from ops.claude_runner import ClaudeError
            raise ClaudeError("timeout")
        return reply if isinstance(reply, str) else json.dumps(reply or {"deadlines": []})

    s.extra.update(run_text=run_text, calls=calls)
    return s


def x(title="Quiz 2", date="2026-11-10", source="bs:announcement:1:7", course=C):
    return {"source": source, "title": title, "kind": "quiz", "course": course, "date": date, "time": None,
            "end_time": None, "quote": "q"}


# ---- M2: similarity
def test_similar_requires_matching_numbers_and_jaccard():
    a = d("a", "Quiz 1", "2026-11-10")
    assert not similar(d("b", "Quiz 2", "2026-11-10"), [a])
    assert similar(d("c", "TMBI Quiz 1", "2026-11-10"), [a])
    assert not similar(d("e", "Quiz 1", "2026-11-11"), [a])
    assert similar(d("f", "Quiz 1", "2026-11-10", course=None), [a])  # chat extraction without a course


# ---- M1: midnight due date
def test_midnight_deadline_belongs_to_due_day():
    due = d("a", "Project Report", "2026-11-10T15:30:00+00:00", kind="assignment", all_day=False,
            end="2026-11-10T16:00:00+00:00", conf="confirmed")  # due 11 Nov 00:00 SGT
    assert local_date(due).isoformat() == "2026-11-11"


# ---- I4: notes appended without clobbering concurrent queue writes
async def test_extraction_notes_do_not_clobber_batch_queue(db, env_file):
    s = svc(db, env_file)
    Library(db).upsert("bs:announcement:1:7", C, "announcement", "Quiz", "", "", "Quiz 2 on 10 Nov", NOW, "v1")

    async def run_text(prompt, text, model="sonnet", timeout=180, run_as=None):
        db.set_json_setting("batch_queue", [{"text": "🟡 sweep alert", "buttons": None}])  # arrives meanwhile
        return json.dumps({"deadlines": [x()]})

    s.extra["run_text"] = run_text
    await extract_from_library(s, ["bs:announcement:1:7"])
    texts = [q["text"] for q in db.get_json_setting("batch_queue", [])]
    assert "🟡 sweep alert" in texts and any("Quiz 2" in t for t in texts)


# ---- M12: wording when calendar not connected
async def test_note_wording_without_calendar(db, env_file):
    s = svc(db, env_file, reply={"deadlines": [x()]})
    Library(db).upsert("bs:announcement:1:7", C, "announcement", "Quiz", "", "", "Quiz 2 on 10 Nov", NOW, "v1")
    await extract_from_library(s, ["bs:announcement:1:7"])
    [note] = [q["text"] for q in db.get_json_setting("batch_queue", [])]
    assert note.startswith("New date found") and "Google Calendar" not in note


# ---- I5: failed extraction retried
async def test_failed_extraction_is_retried_later(db, env_file):
    s = svc(db, env_file, fail=True)
    Library(db).upsert("bs:announcement:1:7", C, "announcement", "Quiz", "", "", "Quiz 2 on 10 Nov", NOW, "v1")
    await extract_from_library(s, ["bs:announcement:1:7"])
    assert Library(db).pending_extraction() == ["bs:announcement:1:7"]
    s2 = svc(db, env_file, reply={"deadlines": [x()]})
    assert await extract_from_library(s2, []) == 1  # picks up the pending item by itself
    assert Library(db).pending_extraction() == []


async def test_garbage_output_also_retried(db, env_file):
    s = svc(db, env_file, reply="sorry, I can't")
    Library(db).upsert("bs:announcement:1:7", C, "announcement", "Quiz", "", "", "Quiz 2", NOW, "v1")
    await extract_from_library(s, ["bs:announcement:1:7"])
    assert Library(db).pending_extraction() == ["bs:announcement:1:7"]


async def test_chat_cursor_only_advances_on_success(db, env_file):
    from core import tg_group
    from core.models import GroupMessage
    tg_group.store_message(db, GroupMessage(-100, 1, "Ben", "quiz moved to fri", NOW - timedelta(minutes=5)))
    await extract_chats(svc(db, env_file, fail=True))
    assert db.get_setting("last_chat_extract") is None
    s = svc(db, env_file)
    await extract_chats(s)
    assert "quiz moved to fri" in s.extra["calls"][0] and db.get_setting("last_chat_extract")


# ---- I6a: reworded / duplicate tentatives
async def test_reworded_deleted_tentative_not_readded(db, env_file):
    store = DeadlineStore(db)
    store.upsert(d("x:old", "CA2 Presentation", "2026-11-10", kind="presentation"), NOW)
    store.mark_user_deleted("x:old")
    s = svc(db, env_file, reply={"deadlines": [dict(x(title="Group Presentation CA2"), kind="presentation")]})
    Library(db).upsert("bs:announcement:1:7", C, "announcement", "P", "", "", "CA2 presentation", NOW, "v1")
    assert await extract_from_library(s, ["bs:announcement:1:7"]) == 0
    assert store.active() == []


async def test_similar_active_tentative_not_duplicated(db, env_file):
    store = DeadlineStore(db)
    store.upsert(d("x:old", "Quiz 2", "2026-11-10"), NOW)
    s = svc(db, env_file, reply={"deadlines": [x(title="TMBI Quiz 2")]})
    Library(db).upsert("bs:announcement:1:7", C, "announcement", "Q", "", "", "Quiz 2", NOW, "v1")
    assert await extract_from_library(s, ["bs:announcement:1:7"]) == 0
    assert len(store.active()) == 1


# ---- I6c + I7 + I6b: confirmed refresh
def put(db, folder, title, due, seen=NOW):
    it = Item("brightspace", f"bs:assignment:1:{folder}", C, "assignment", title,
              {"ou": 1, "folder_id": folder, "due_at": due, "end_at": None})
    db.upsert_item(it, content_hash(it), seen)


async def test_confirmed_retires_matching_tentative(db, env_file):
    store = DeadlineStore(db)
    store.upsert(d("x:t", "CA1 Report", "2026-11-10", kind="assignment"), NOW)
    put(db, 9, "CA1 Report submission", "2026-11-10T15:59:00+00:00")
    await refresh_confirmed(svc(db, env_file))
    assert store.get("x:t").status == "gone"
    assert store.get("bs:assignment:1:9").status == "active"


def test_stale_assignment_items_excluded(db):
    put(db, 9, "Still there", "2026-11-10T15:59:00+00:00", seen=NOW)
    put(db, 8, "Deleted folder", "2026-11-12T15:59:00+00:00", seen=NOW - timedelta(hours=5))
    assert [x.key for x in confirmed_from_db(db, "https://b")] == ["bs:assignment:1:9"]


async def test_empty_course_list_changes_nothing(db, env_file):
    store = DeadlineStore(db)
    put(db, 9, "T", "2026-11-10T15:59:00+00:00")
    s = svc(db, env_file)
    await refresh_confirmed(s)
    s.bs.course_list = []
    await refresh_confirmed(s)
    assert store.get("bs:assignment:1:9").status == "active"


# ---- I8: scans don't overlap
async def test_extract_job_skips_when_already_running(db, env_file, monkeypatch):
    s = svc(db, env_file)
    gate, runs = asyncio.Event(), []

    async def slow_scan(ctx, extract=True):
        runs.append(1)
        await gate.wait()
        return {"courses": 0, "documents": 0, "announcements": 0, "assignments": 0, "errors": 0, "deadlines": 0}

    monkeypatch.setattr(deadline_jobs, "full_scan", slow_scan)
    t = asyncio.create_task(deadline_jobs.extract_job(s))
    await asyncio.sleep(0)
    assert await deadline_jobs.extract_job(s) is None
    gate.set()
    await t
    assert runs == [1]
