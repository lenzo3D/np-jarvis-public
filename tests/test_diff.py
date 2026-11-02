from datetime import datetime, timezone

from core.models import Item
from ops.diff import content_hash, detect

T0 = datetime(2026, 10, 26, 1, 0, tzinfo=timezone.utc)


def a(i, due="2026-10-30T15:59:00Z", title=None):
    return Item("brightspace", f"bs:a:{i}", "INV2101", "assignment", title or f"T{i}", {"due_at": due})


def test_first_run_is_silent(db):
    assert detect(db, "brightspace", [a(1), a(2), a(3)], T0) == []
    assert db.get_item("brightspace", "bs:a:2") is not None
    assert db.get_setting("baseline:brightspace") == "1"


def test_new_item_detected(db):
    detect(db, "brightspace", [a(1)], T0)
    changes = detect(db, "brightspace", [a(1), a(2)], T0)
    assert [(c.kind, c.item.stable_id) for c in changes] == [("new", "bs:a:2")]


def test_changed_due_date_reports_field(db):
    detect(db, "brightspace", [a(1)], T0)
    changes = detect(db, "brightspace", [a(1, due="2026-11-02T15:59:00Z")], T0)
    assert len(changes) == 1
    assert changes[0].kind == "changed"
    assert changes[0].changed_fields == {"due_at": ("2026-10-30T15:59:00Z", "2026-11-02T15:59:00Z")}


def test_unchanged_item_no_change(db):
    detect(db, "brightspace", [a(1)], T0)
    assert detect(db, "brightspace", [a(1)], T0) == []


def test_baseline_is_per_source(db):
    detect(db, "brightspace", [a(1)], T0)
    teams = Item("teams", "teams:c:1", None, "teams_post", "hi", {})
    assert detect(db, "teams", [teams], T0) == []


def b(i, code, ou):
    return Item("brightspace", f"bs:b:{ou}:{i}", code, "assignment", f"T{i}", {"ou": ou, "due_at": "2026-10-30T15:59:00Z"})


def test_a_new_course_is_baselined_silently_but_old_courses_still_alert(db):
    detect(db, "brightspace", [b(1, "INV2101", 1)], T0)
    fresh = {}
    changes = detect(db, "brightspace", [b(1, "INV2101", 1), b(2, "INV2101", 1), b(1, "NEW1001", 2), b(2, "NEW1001", 2),
                                         b(3, "NEW1001", 2)], T0, fresh)
    assert fresh == {"NEW1001": 3}
    assert [(c.kind, c.item.stable_id) for c in changes] == [("new", "bs:b:1:2")]
    assert db.get_item("brightspace", "bs:b:2:3") is not None
    fresh2 = {}
    again = detect(db, "brightspace", [b(1, "NEW1001", 2), b(4, "NEW1001", 2)], T0, fresh2)
    assert fresh2 == {} and [c.item.stable_id for c in again] == ["bs:b:2:4"]  # from now on its changes alert


def test_course_identity_is_the_org_unit_not_the_code(db):
    detect(db, "brightspace", [b(1, "INV2101", 1)], T0)
    fresh = {}
    ch = detect(db, "brightspace", [b(1, "INV2101-RENAMED", 1), b(2, "INV2101-RENAMED", 1)], T0, fresh)
    assert fresh == {} and [c.item.stable_id for c in ch if c.kind == "new"] == ["bs:b:1:2"]


def test_old_course_that_had_no_items_is_seen_through_the_complete_list(db):
    detect(db, "brightspace", [b(1, "INV2101", 1)], T0, complete={1, 7})   # course 7 was read completely, 0 items
    fresh = {}
    ch = detect(db, "brightspace", [b(1, "INV2101", 1), b(1, "OLD7", 7)], T0, fresh, {1, 7})
    assert fresh == {} and [c.item.stable_id for c in ch] == ["bs:b:7:1"]   # a real new item still alerts


def test_a_partially_read_new_course_is_skipped_until_it_reads_completely(db):
    detect(db, "brightspace", [b(1, "INV2101", 1)], T0, complete={1})
    fresh = {}
    ch = detect(db, "brightspace", [b(1, "INV2101", 1), b(1, "NEW1001", 2)], T0, fresh, complete={1})  # 2 incomplete
    # not complete: nothing of course 2 is stored, flagged or announced
    ch = detect(db, "brightspace", [b(1, "INV2101", 1), b(1, "NEW1001", 2)], T0, fresh, complete={1})
    assert fresh == {} and ch == [] and db.get_item("brightspace", "bs:b:2:1") is None
    assert db.get_setting("baseline:brightspace:ou:2") != "1"
    ch = detect(db, "brightspace", [b(1, "INV2101", 1), b(1, "NEW1001", 2), b(2, "NEW1001", 2)], T0, fresh, complete={1, 2})
    assert fresh == {"NEW1001": 2} and ch == []   # the whole course is baselined at once, no flood


def test_a_course_deferred_three_sweeps_is_baselined_anyway(db):
    detect(db, "brightspace", [b(1, "INV2101", 1)], T0, complete={1})
    for n in (1, 2):
        fresh = {}
        detect(db, "brightspace", [b(1, "INV2101", 1), b(1, "NEW1001", 2)], T0, fresh, complete={1})
        assert fresh == {} and db.get_item("brightspace", "bs:b:2:1") is None, n
    fresh = {}
    ch = detect(db, "brightspace", [b(1, "INV2101", 1), b(1, "NEW1001", 2)], T0, fresh, complete={1})
    assert fresh == {"NEW1001": 1} and ch == [] and db.get_item("brightspace", "bs:b:2:1") is not None
    ch = detect(db, "brightspace", [b(1, "NEW1001", 2), b(2, "NEW1001", 2)], T0, complete={1})
    assert [c.item.stable_id for c in ch] == ["bs:b:2:2"]  # afterwards real changes alert


def test_ou_newly_added_to_a_stored_payload_is_not_a_change(db):
    old = Item("brightspace", "bs:grade:1:7", "INV2101", "grade", "Quiz", {"value": 8})
    detect(db, "brightspace", [old], T0)
    new = Item("brightspace", "bs:grade:1:7", "INV2101", "grade", "Quiz", {"ou": 1, "value": 8})
    assert detect(db, "brightspace", [new], T0) == []
    assert db.get_item("brightspace", "bs:grade:1:7")["content_hash"] != content_hash(old)


def test_grades_of_a_new_or_deferred_course_follow_the_course(db):
    g = lambda ou, i: Item("brightspace", f"bs:grade:{ou}:{i}", "NEW1001", "grade", "G", {"ou": ou, "value": 1})  # noqa: E731
    detect(db, "brightspace", [b(1, "INV2101", 1)], T0, complete={1})
    fresh = {}
    ch = detect(db, "brightspace", [b(1, "INV2101", 1), g(2, 1)], T0, fresh, complete={1, 2})
    assert ch == [] and fresh == {"NEW1001": 1}
