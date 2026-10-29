from datetime import datetime, timedelta, timezone

from core.config import load_settings
from core.db import DB
from core.models import Item

T0 = datetime(2026, 10, 26, 1, 0, tzinfo=timezone.utc)


def test_init_creates_tables(tmp_path):
    d = DB(tmp_path / "x.db")
    d.init()
    names = {r[0] for r in d.conn.execute("select name from sqlite_master")}
    for t in ["items", "due_dates", "group_messages", "pending_actions", "settings", "source_health", "group_fts"]:
        assert t in names


def test_upsert_and_get_item(db):
    it = Item("brightspace", "bs:a:1", "INV2101", "assignment", "T5", {"due_at": None})
    db.upsert_item(it, "h1", T0)
    assert db.get_item("brightspace", "bs:a:1")["content_hash"] == "h1"
    db.upsert_item(it, "h2", T0 + timedelta(hours=1))
    row = db.get_item("brightspace", "bs:a:1")
    assert row["content_hash"] == "h2"
    assert row["first_seen"] == T0.isoformat()
    assert row["last_seen"] == (T0 + timedelta(hours=1)).isoformat()


def test_record_health_counts(db):
    assert db.record_health("bs", False, T0) == 1
    assert db.record_health("bs", False, T0) == 2
    assert db.record_health("bs", True, T0) == 0


def test_settings_roundtrip(db):
    assert db.get_setting("quiet", "none") == "none"
    db.set_setting("quiet", "23-7")
    assert db.get_setting("quiet") == "23-7"


def test_load_settings_defaults(env_file):
    s = load_settings(env_file)
    assert s.actions_enabled is False
    assert s.tz.key == "Asia/Singapore"
    assert s.allowed_telegram_id == 42
    assert s.group_chat_ids == []
