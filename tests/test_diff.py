from datetime import datetime, timezone

from core.models import Item
from ops.diff import detect

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
