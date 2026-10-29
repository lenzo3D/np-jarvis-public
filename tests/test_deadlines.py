from datetime import datetime, timedelta, timezone

from core.deadlines import Deadline, DeadlineStore, duplicates_confirmed, extracted_key

T0 = datetime(2026, 10, 27, 2, 0, tzinfo=timezone.utc)


def dl(key="bs:assignment:1:5", title="Tutorial 5", start="2026-10-30T15:29:00+00:00", end="2026-10-30T15:59:00+00:00",
       course="26S2-731IV_013686", kind="assignment", source="brightspace", conf="confirmed", all_day=False):
    return Deadline(key=key, title=title, course_code=course, kind=kind, start_utc=start, end_utc=end,
                    all_day=all_day, source=source, source_ref="", confidence=conf, quote="")


def test_upsert_reports_changes(db):
    s = DeadlineStore(db)
    assert s.upsert(dl(), T0) is True
    assert s.upsert(dl(), T0) is False
    assert s.upsert(dl(end="2026-11-02T15:59:00+00:00"), T0) is True
    assert [d.key for d in s.active()] == ["bs:assignment:1:5"]


def test_mark_gone_missing(db):
    s = DeadlineStore(db)
    s.upsert(dl(), T0)
    s.upsert(dl(key="bs:assignment:1:6"), T0)
    s.upsert(dl(key="x:abc", source="announcement", conf="tentative"), T0)
    s.mark_gone_missing("brightspace", {"bs:assignment:1:6"})
    assert sorted(d.key for d in s.active()) == ["bs:assignment:1:6", "x:abc"]
    assert s.get("bs:assignment:1:5").status == "gone"


def test_gone_row_revives_when_seen_again(db):
    s = DeadlineStore(db)
    s.upsert(dl(), T0)
    s.mark_gone_missing("brightspace", set())
    s.upsert(dl(), T0)
    assert s.get("bs:assignment:1:5").status == "active"


def test_user_deleted_never_revives(db):
    s = DeadlineStore(db)
    s.upsert(dl(key="x:abc", conf="tentative", source="teams"), T0)
    s.mark_user_deleted("x:abc")
    assert s.upsert(dl(key="x:abc", conf="tentative", source="teams"), T0) is False
    assert s.is_user_deleted("x:abc") and s.active() == []


def test_set_event_and_upcoming(db):
    s = DeadlineStore(db)
    s.upsert(dl(), T0)
    s.upsert(dl(key="bs:assignment:1:9", start="2026-12-30T15:29:00+00:00", end="2026-12-30T15:59:00+00:00"), T0)
    s.set_event("bs:assignment:1:5", "ev1", "h1")
    d = s.get("bs:assignment:1:5")
    assert (d.gcal_event_id, d.gcal_hash) == ("ev1", "h1")
    assert [x.key for x in s.upcoming(T0, 14)] == ["bs:assignment:1:5"]


def test_extracted_key_normalises():
    a = extracted_key("26S2-73TMBI", "quiz", "Quiz 2!", "2026-11-10")
    b = extracted_key("26S2-73TMBI", "quiz", "  quiz   2 ", "2026-11-10")
    assert a == b and a.startswith("x:") and a != extracted_key("26S2-73TMBI", "quiz", "Quiz 2", "2026-11-11")


def test_duplicates_confirmed_same_day_similar_title():
    confirmed = [dl(title="Topic 1 Homework", start="2026-11-01T15:29:00+00:00", end="2026-11-01T15:59:00+00:00")]
    x = dl(key="x:1", title="Homework for Topic 1", start=None, end=None, all_day=True, conf="tentative")
    x.start_utc = "2026-11-01"  # all-day date
    assert duplicates_confirmed(x, confirmed)
    y = dl(key="x:2", title="Quiz 2", conf="tentative", start="2026-11-01", end=None, all_day=True)
    assert not duplicates_confirmed(y, confirmed)
    z = dl(key="x:3", title="Topic 1 Homework", conf="tentative", start="2026-11-02", end=None, all_day=True)
    assert not duplicates_confirmed(z, confirmed)
