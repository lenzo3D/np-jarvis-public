from datetime import datetime, timedelta, timezone

from core.deadlines import Deadline, DeadlineStore, duplicates_confirmed, extracted_key, local_date

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


def test_event_like_only_for_clearly_event_like_new_items():
    from datetime import datetime
    from core.deadlines import SGT, event_like
    at = lambda h, m=0, day=10: datetime(2026, 11, day, h, m, tzinfo=SGT)  # noqa: E731
    assert event_like("other", "Industry briefing", at(14), None)
    assert event_like("other", "Group rehearsal", at(14), at(16))
    assert event_like("other", "Team huddle", at(14), at(15))           # same-day range
    assert not event_like("other", "CA2 Reflection", at(23, 59), at(23, 59))   # a point in time = due
    assert not event_like("other", "MST week", at(0, 0, 9), at(23, 59, 20))     # several days = window, due at the end
    assert not event_like("other", "Briefing report due", at(14), None)         # worded as due
    assert not event_like("quiz", "Session quiz", at(14), None)


def test_old_rows_keep_behaving_as_due_moments_after_the_upgrade(tmp_path):
    import sqlite3
    from datetime import datetime, timezone
    from core.db import DB
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""create table deadlines (key text primary key, title text not null, course_code text, kind text not null,
      start_utc text not null, end_utc text, all_day integer not null default 0, source text not null, source_ref text,
      confidence text not null, quote text, status text not null default 'active', gcal_event_id text, gcal_hash text,
      updated_at text not null);
      insert into deadlines values ('p:1','CA2 Reflection',null,'other','2026-11-10T15:29:00+00:00','2026-11-10T15:59:00+00:00',
        0,'brightspace','','confirmed','','active',null,null,'x');""")
    old.commit(), old.close()
    db = DB(path)
    db.init()
    d = DeadlineStore(db).get("p:1")
    assert d.as_event is False and local_date(d).isoformat() == "2026-11-10"
    from core.deadlines import is_due
    assert is_due(d.kind, d.as_event)
    # a re-upsert (same content, or a fresh scan that would now call it an event) never flips the stored decision
    DeadlineStore(db).upsert(Deadline(**{**d.__dict__, "title": "Group meeting", "as_event": True}),
                             datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert DeadlineStore(db).get("p:1").as_event is False
