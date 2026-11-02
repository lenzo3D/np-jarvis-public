from datetime import datetime, timezone

from core.config import load_settings
from core.deadlines import DeadlineStore
from core.models import Course, Item
from ops.deadline_sources import confirmed_from_calendar, confirmed_from_db, refresh_confirmed
from ops.diff import content_hash
from ops.watcher import Services
from tests.fakes import Clock, FakeBS, FakeNotifier

NOW = datetime(2026, 10, 27, 2, 0, tzinfo=timezone.utc)
BASE = "https://nplms.polite.edu.sg"


def put(db, ou, folder, title, due=None, end=None, code="26S2-731IV_013686"):
    it = Item("brightspace", f"bs:assignment:{ou}:{folder}", code, "assignment", title,
              {"ou": ou, "folder_id": folder, "due_at": due, "end_at": end})
    db.upsert_item(it, content_hash(it), NOW)


def test_confirmed_from_db(db):
    put(db, 1044225, 55, "Tutorial 5", due="2026-10-30T15:59:00+00:00")
    put(db, 1044225, 56, "Reflection", end="2026-11-20T15:59:00+00:00")
    put(db, 1044225, 57, "Optional")
    out = {d.key: d for d in confirmed_from_db(db, BASE)}
    assert set(out) == {"bs:assignment:1044225:55", "bs:assignment:1044225:56"}
    t5 = out["bs:assignment:1044225:55"]
    assert (t5.start_utc, t5.end_utc) == ("2026-10-30T15:29:00+00:00", "2026-10-30T15:59:00+00:00")
    assert t5.confidence == "confirmed" and t5.kind == "assignment"
    assert t5.source_ref.startswith(BASE) and "1044225" in t5.source_ref


def ev(i, title, start, end, ou=1043604):
    return {"CalendarEventId": i, "OrgUnitId": ou, "Title": title, "StartDateTime": start, "EndDateTime": end,
            "IsAllDayEvent": False}


def test_confirmed_from_calendar_kinds_and_lessons():
    events = [ev(1, "Homework 1", "2026-11-01T15:59:00.000Z", "2026-11-01T15:59:00.000Z"),
              ev(2, "Quiz 2", "2026-11-10T02:00:00.000Z", "2026-11-10T03:00:00.000Z"),
              ev(3, "Lesson 7 - Bond pricing", "2026-10-28T01:00:00.000Z", "2026-10-28T03:00:00.000Z"),
              ev(4, "Clinic Session with UOB", "2026-10-29T06:00:00.000Z", "2026-10-29T07:00:00.000Z"),
              ev(5, "Read received feedback - Peer Evaluation", "2026-11-01T16:00:00.000Z", "2026-11-01T16:00:00.000Z")]
    out = {d.key: d for d in confirmed_from_calendar(events, {1043604: "26S2-73BKALY_012872"}, [])}
    assert set(out) == {"bs:cal:1", "bs:cal:2", "bs:cal:5"}
    assert out["bs:cal:1"].kind == "homework" and out["bs:cal:1"].end_utc == "2026-11-01T15:59:00+00:00"
    assert out["bs:cal:2"].kind == "quiz" and out["bs:cal:2"].start_utc == "2026-11-10T02:00:00+00:00"
    assert out["bs:cal:1"].course_code == "26S2-73BKALY_012872"


def test_calendar_event_duplicating_assignment_skipped(db):
    put(db, 1043604, 9, "Homework 1", due="2026-11-01T15:59:00+00:00", code="26S2-73BKALY_012872")
    assignments = confirmed_from_db(db, BASE)
    events = [ev(1, "Homework 1 - Due", "2026-11-01T15:59:00.000Z", "2026-11-01T15:59:00.000Z")]
    assert confirmed_from_calendar(events, {1043604: "26S2-73BKALY_012872"}, assignments) == []


async def test_refresh_confirmed_marks_removed_future_items_gone(db, env_file):
    s = Services(settings=load_settings(env_file), db=db, sessions=None, bs=FakeBS(), notifier=FakeNotifier(),
                 clock=Clock(NOW))
    s.bs.course_list = [Course(1044225, "26S2-731IV_013686", "INVESTMENTS(731IV_013686)")]
    put(db, 1044225, 55, "Tutorial 5", due="2026-10-30T15:59:00+00:00")
    put(db, 1044225, 40, "Old tutorial", due="2026-10-01T15:59:00+00:00")
    await refresh_confirmed(s)
    store = DeadlineStore(db)
    assert {d.key for d in store.active()} == {"bs:assignment:1044225:55", "bs:assignment:1044225:40"}
    db.conn.execute("delete from items where stable_id like '%:55'")
    db.conn.execute("delete from items where stable_id like '%:40'")
    await refresh_confirmed(s)
    assert store.get("bs:assignment:1044225:55").status == "gone"
    assert store.get("bs:assignment:1044225:40").status == "active"  # past: kept as history


def test_kind_from_title_matches_whole_words_only():
    from ops.deadline_sources import kind_from_title
    for t in ("Photo contest entry", "Latest announcements", "Attest form", "Protest report briefing", "Examine the data",
              "Detest"):
        assert kind_from_title(t) == "other", t
    assert kind_from_title("Test 1") == "test" and kind_from_title("Mid-term TEST") == "test"
    assert kind_from_title("Quizzes week 3") == "quiz" and kind_from_title("Final Exam") == "exam"
    assert kind_from_title("Exam-style practice") == "exam"


def test_calendar_other_items_point_and_multi_day_stay_due_blocks_events_become_events():
    events = [ev(1, "CA2 Reflection", "2026-11-10T15:59:00.000Z", "2026-11-10T15:59:00.000Z"),
              ev(2, "MST week", "2026-11-08T16:00:00.000Z", "2026-11-20T15:59:00.000Z"),
              ev(3, "Alumni talk", "2026-11-12T06:00:00.000Z", None),
              ev(4, "Team huddle", "2026-11-12T06:00:00.000Z", "2026-11-12T07:00:00.000Z")]
    out = {d.key: d for d in confirmed_from_calendar(events, {}, [])}
    p, w, b, h = out["bs:cal:1"], out["bs:cal:2"], out["bs:cal:3"], out["bs:cal:4"]
    assert not p.as_event and p.end_utc == "2026-11-10T15:59:00+00:00" and p.start_utc == "2026-11-10T15:29:00+00:00"
    assert not w.as_event and w.end_utc == "2026-11-20T15:59:00+00:00" and w.start_utc == "2026-11-20T15:29:00+00:00"
    assert b.as_event and b.start_utc == "2026-11-12T06:00:00+00:00" and b.end_utc is None
    assert h.as_event and h.end_utc == "2026-11-12T07:00:00+00:00"


async def test_existing_due_block_row_keeps_its_shape_when_brightspace_shows_a_same_day_range(db, env_file):
    from core.deadlines import Deadline
    s = Services(settings=load_settings(env_file), db=db, sessions=None, bs=FakeBS(), notifier=FakeNotifier(),
                 clock=Clock(NOW))
    s.bs.course_list = [Course(1043604, "26S2-73BKALY_012872", "X")]
    s.bs.cal_events = [ev(1, "CA2 Reflection", "2026-11-10T01:00:00.000Z", "2026-11-10T15:59:00.000Z")]
    store = DeadlineStore(db)
    # stored before as_event existed: the old behaviour made every "other" item a due block (end-30min .. end)
    old = Deadline(key="bs:cal:1", title="CA2 Reflection", course_code="26S2-73BKALY_012872", kind="other",
                   start_utc="2026-11-10T15:29:00+00:00", end_utc="2026-11-10T15:59:00+00:00", all_day=False,
                   source="brightspace", source_ref="", confidence="confirmed", quote="", as_event=False)
    store.upsert(old, NOW)
    before = db.conn.execute("select * from deadlines where key='bs:cal:1'").fetchone()
    assert await refresh_confirmed(s) == 0
    after = db.conn.execute("select * from deadlines where key='bs:cal:1'").fetchone()
    assert tuple(before) == tuple(after) and not store.get("bs:cal:1").as_event
    # a brand-new row of the same shape is an event
    s.bs.cal_events = [ev(2, "CA3 Reflection", "2026-11-11T01:00:00.000Z", "2026-11-11T15:59:00.000Z")]
    await refresh_confirmed(s)
    assert store.get("bs:cal:2").as_event
