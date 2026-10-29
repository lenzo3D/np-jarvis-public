import json
from datetime import datetime, timedelta, timezone

from core.config import load_settings
from ops.diff import content_hash
from ops.reminders import pending_reminders, run_reminders
from ops.watcher import Services
from tests.fakes import Clock, FakeBS, FakeNotifier, assignment

DUE = datetime(2026, 10, 30, 15, 59, tzinfo=timezone.utc)


def row(sent=(), submitted=0, due=DUE.isoformat()):
    return {"item_key": "k", "due_at": due, "submitted": submitted, "reminders_sent_json": json.dumps(list(sent))}


def test_reminder_fires_at_3_days():
    assert pending_reminders([row()], DUE - timedelta(days=3)) == [("k", "3 days")]


def test_no_reminder_before_threshold():
    assert pending_reminders([row()], DUE - timedelta(days=3, minutes=1)) == []


def test_reminder_fires_once_per_threshold():
    assert pending_reminders([row(sent=["3 days"])], DUE - timedelta(days=2)) == []
    assert pending_reminders([row(sent=["3 days"])], DUE - timedelta(hours=23)) == [("k", "1 day")]


def test_skips_submitted():
    assert pending_reminders([row(submitted=1)], DUE - timedelta(hours=2)) == []


def test_skips_null_due():
    assert pending_reminders([row(due=None)], DUE) == []


def test_past_due_no_reminder():
    assert pending_reminders([row()], DUE + timedelta(minutes=1)) == []


def test_late_start_sends_only_nearest():
    assert pending_reminders([row()], DUE - timedelta(hours=20)) == [("k", "1 day")]


def _svc(db, env_file, now):
    return Services(settings=load_settings(env_file), db=db, sessions=None, bs=FakeBS(),
                    notifier=FakeNotifier(), clock=Clock(now))


def _seed(db, now):
    it = assignment(55, due=DUE.isoformat())
    db.upsert_item(it, content_hash(it), now)
    db.conn.execute("insert into due_dates(item_key, due_at) values (?,?)", (it.stable_id, DUE.isoformat()))
    return it


async def test_reminder_text_in_sgt(db, env_file):
    now = DUE - timedelta(hours=23)
    s = _svc(db, env_file, now)
    _seed(db, now)
    await run_reminders(s)
    text, buttons = s.notifier.messages[0]
    assert "Fri 30 Oct 23:59" in text and "Tutorial 55" in text and "INV2101" in text
    assert buttons == [[("Do it", "doit:bs:assignment:1001:55")]]
    await run_reminders(s)
    assert len(s.notifier.messages) == 1


async def test_run_rechecks_submission(db, env_file):
    now = DUE - timedelta(hours=2)
    s = _svc(db, env_file, now)
    _seed(db, now)
    s.bs.submitted_folders.add(55)
    await run_reminders(s)
    assert s.notifier.messages == []
    assert db.conn.execute("select submitted from due_dates").fetchone()[0] == 1


async def test_reminders_respect_pause(db, env_file):
    now = DUE - timedelta(hours=23)
    s = _svc(db, env_file, now)
    _seed(db, now)
    db.set_setting("paused", "1")
    await run_reminders(s)
    assert s.notifier.messages == []
    db.set_setting("paused", "0")
    await run_reminders(s)
    assert len(s.notifier.messages) == 1
