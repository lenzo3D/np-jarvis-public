import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from core import tg_group
from core.config import load_settings
from core.deadlines import DeadlineStore
from core.library import Library
from core.models import Course, GroupMessage
from ops.deadline_sources import extract_chats, extract_from_library, parse_extraction
from ops.watcher import Services
from tests.fakes import Clock, FakeBS, FakeNotifier

SGT = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 10, 27, 2, 0, tzinfo=timezone.utc)
SRC = {"bs:announcement:1:7": ("26S2-73TMBI_013687", "announcement")}


def raw(*items):
    return json.dumps({"deadlines": list(items)})


def x(**kw):
    base = {"source": "bs:announcement:1:7", "title": "Quiz 2", "kind": "quiz", "course": "26S2-73TMBI_013687",
            "date": "2026-11-10", "time": None, "end_time": None, "quote": "Quiz 2 is on 10 Nov"}
    base.update(kw)
    return base


def test_all_day_quiz():
    [d] = parse_extraction(raw(x()), SRC, NOW, SGT)
    assert d.all_day and d.start_utc == "2026-11-10" and d.end_utc is None
    assert d.confidence == "tentative" and d.source == "announcement" and d.kind == "quiz"
    assert d.key.startswith("x:") and d.quote == "Quiz 2 is on 10 Nov"


def test_timed_deadline_is_30_min_block_ending_at_due():
    [d] = parse_extraction(raw(x(kind="assignment", title="CA1 report", time="23:59")), SRC, NOW, SGT)
    assert (d.start_utc, d.end_utc) == ("2026-11-10T15:29:00+00:00", "2026-11-10T15:59:00+00:00")


def test_meeting_with_start_and_end():
    [d] = parse_extraction(raw(x(kind="meeting", title="Group meeting", time="14:00", end_time="15:30")), SRC, NOW, SGT)
    assert (d.start_utc, d.end_utc) == ("2026-11-10T06:00:00+00:00", "2026-11-10T07:30:00+00:00")


def test_rejects_bad_and_out_of_range():
    items = [x(date="2026-13-40"), x(date="2026-10-01"), x(date="2027-12-01"), x(time="25:00"), x(title=""),
             x(kind="party", title="Other thing")]
    out = parse_extraction(raw(*items), SRC, NOW, SGT)
    assert [d.title for d in out] == ["Other thing"] and out[0].kind == "other"


def test_unknown_course_falls_back_to_source_course():
    [d] = parse_extraction(raw(x(course="nonsense")), SRC, NOW, SGT)
    assert d.course_code == "26S2-73TMBI_013687"


def test_garbage_output():
    assert parse_extraction("I could not find anything", SRC, NOW, SGT) == []
    assert parse_extraction('{"deadlines": "none"}', SRC, NOW, SGT) == []
    assert parse_extraction('```json\n' + raw(x()) + '\n```', SRC, NOW, SGT)[0].title == "Quiz 2"


def svc(db, env_file, reply):
    s = Services(settings=load_settings(env_file), db=db, sessions=None, bs=FakeBS(), notifier=FakeNotifier(),
                 clock=Clock(NOW))
    s.bs.course_list = [Course(1, "26S2-73TMBI_013687", "TRANSAC MGT & BLOCKCHAIN INNOV(73TMBI_013687)")]
    calls = []

    async def run_text(prompt, input_text, model="sonnet", timeout=180, run_as=None):
        calls.append((prompt, input_text))
        return reply if isinstance(reply, str) else json.dumps(reply)

    s.extra["run_text"] = run_text
    s.extra["calls"] = calls
    return s


async def test_extract_from_library_adds_tentative_and_notes(db, env_file):
    s = svc(db, env_file, {"deadlines": [x()]})
    Library(db).upsert("bs:announcement:1:7", "26S2-73TMBI_013687", "announcement", "Quiz 2 date", "", "",
                       "Quiz 2 is on 10 Nov", NOW, "v1")
    n = await extract_from_library(s, ["bs:announcement:1:7"])
    store = DeadlineStore(db)
    assert n == 1 and store.active()[0].title == "Quiz 2"
    prompt, text = s.extra["calls"][0]
    assert "26S2-73TMBI_013687" in text and "Quiz 2 is on 10 Nov" in text and "2026-10-27" in prompt
    notes = db.get_json_setting("batch_queue", [])
    assert any("New date found (tentative)" in q["text"] and "Quiz 2" in q["text"] for q in notes)
    assert await extract_from_library(s, ["bs:announcement:1:7"]) == 0  # same deadline again: no new note


async def test_extracted_duplicate_of_confirmed_skipped(db, env_file):
    from core.deadlines import Deadline
    s = svc(db, env_file, {"deadlines": [x(title="Quiz 2 (TMBI)")]})
    DeadlineStore(db).upsert(Deadline("bs:cal:9", "Quiz 2", "26S2-73TMBI_013687", "quiz",
                                      "2026-11-10T02:00:00+00:00", "2026-11-10T03:00:00+00:00", False,
                                      "brightspace", "", "confirmed", ""), NOW)
    Library(db).upsert("bs:announcement:1:7", "26S2-73TMBI_013687", "announcement", "Q", "", "", "Quiz 2", NOW, "v")
    assert await extract_from_library(s, ["bs:announcement:1:7"]) == 0


async def test_only_relevant_documents_sent(db, env_file):
    s = svc(db, env_file, {"deadlines": []})
    lib = Library(db)
    lib.upsert("bs:content:1", "26S2-73TMBI_013687", "document", "Teaching Plan", "", "", "Week 9 quiz", NOW, "v")
    lib.upsert("bs:content:2", "26S2-73TMBI_013687", "document", "Lecture 3 notes", "", "", "Hashing is...", NOW, "v")
    await extract_from_library(s, ["bs:content:1", "bs:content:2"])
    text = s.extra["calls"][0][1]
    assert "Teaching Plan" in text and "Hashing is" not in text


async def test_extract_chats_uses_group_and_teams_since_last_run(db, env_file):
    s = svc(db, env_file, {"deadlines": [x(source="group", kind="meeting", title="Project meeting",
                                           time="14:00", course=None, quote="meet thu 2pm")]})
    tg_group.store_message(db, GroupMessage(-100, 1, "Ben", "meet thu 2pm at library", NOW - timedelta(minutes=5)))
    n = await extract_chats(s)
    assert n == 1 and DeadlineStore(db).active()[0].source == "group"
    assert "meet thu 2pm at library" in s.extra["calls"][0][1]
    await extract_chats(s)
    assert len(s.extra["calls"]) == 1  # nothing new since last run
