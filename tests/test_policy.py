from datetime import datetime, time, timezone

from ops.policy import decide, in_quiet_hours

NOON_SGT = datetime(2026, 10, 26, 4, 0, tzinfo=timezone.utc)
NIGHT_SGT = datetime(2026, 10, 26, 16, 0, tzinfo=timezone.utc)  # 00:00 SGT


def test_quiet_wraps_midnight():
    assert in_quiet_hours(time(23, 30), "23-7")
    assert in_quiet_hours(time(6, 59), "23-7")
    assert not in_quiet_hours(time(7, 0), "23-7")
    assert not in_quiet_hours(time(12, 0), "23-7")


def test_quiet_same_day_range():
    assert in_quiet_hours(time(13, 0), "12-14")
    assert not in_quiet_hours(time(15, 0), "12-14")


def test_no_quiet_spec():
    assert not in_quiet_hours(time(3, 0), None)


def test_red_sent_in_quiet_hours(db):
    db.set_setting("quiet", "23-7")
    assert decide("red", "INV2101", db, NIGHT_SGT) == "send"


def test_yellow_batched(db):
    assert decide("yellow", "INV2101", db, NOON_SGT) == "batch"


def test_white_briefing(db):
    assert decide("white", None, db, NOON_SGT) == "briefing"


def test_muted_course_drops_yellow_keeps_red(db):
    db.set_json_setting("muted", ["INV2101"])
    assert decide("yellow", "INV2101", db, NOON_SGT) == "drop"
    assert decide("red", "INV2101", db, NOON_SGT) == "send"
    assert decide("yellow", "ACC1001", db, NOON_SGT) == "batch"


def test_paused_drops_all(db):
    db.set_setting("paused", "1")
    assert decide("red", "INV2101", db, NOON_SGT) == "drop"
    assert decide("yellow", None, db, NOON_SGT) == "drop"
