import json
from datetime import datetime, timedelta, timezone

from core.config import load_settings
from ops.diff import content_hash
from core.auth import AuthRequired
from core.deadlines import Deadline, DeadlineStore
from ops import reminders, scheduler
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
    assert buttons == [[("Do it", "doit:bs:assignment:1001:55")],
                       [("I submitted elsewhere", "rem:done:bs:assignment:1001:55")]]
    await run_reminders(s)
    assert len(s.notifier.messages) == 1


async def test_run_rechecks_submission(db, env_file):
    now = DUE - timedelta(hours=2)
    s = _svc(db, env_file, now)
    _seed(db, now)
    s.bs.submitted_folders.add(55)
    await run_reminders(s)
    assert [m[0].split(":")[0] for m in s.notifier.messages] == ["Submission confirmed"]
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


# ---------------------------------------------------------------- escalating ladder

LADDER = [(timedelta(days=3), "3 days"), (timedelta(days=1), "1 day"), (timedelta(hours=6), "6 hours"),
          (timedelta(hours=3), "3 hours"), (timedelta(hours=1), "1 hour"), (timedelta(minutes=30), "30 minutes")]


def test_ladder_windows_in_order():
    for delta, label in LADDER:
        assert pending_reminders([row()], DUE - delta) == [("k", label)]
        assert pending_reminders([row()], DUE - delta - timedelta(seconds=1))[:1] != [("k", label)]


def test_no_stale_burst_after_downtime():
    # down for two days: only the nearest reached window, never one per skipped window
    assert pending_reminders([row()], DUE - timedelta(minutes=45)) == [("k", "1 hour")]
    assert pending_reminders([row(sent=["3 days", "1 day", "6 hours", "3 hours", "1 hour"])],
                             DUE - timedelta(minutes=45)) == []


async def test_late_start_sends_one_then_nothing(db, env_file):
    now = DUE - timedelta(minutes=50)
    s = _svc(db, env_file, now)
    _seed(db, now)
    await run_reminders(s)
    await run_reminders(s)
    assert len(s.notifier.messages) == 1
    s.clock.now = DUE - timedelta(minutes=29)
    await run_reminders(s)
    assert len(s.notifier.messages) == 2 and "29 min left" in s.notifier.messages[1][0]


async def test_confirmed_submission_silences_and_notes_once(db, env_file):
    now = DUE - timedelta(hours=5)
    s = _svc(db, env_file, now)
    _seed(db, now)
    s.bs.submitted_folders.add(55)
    await run_reminders(s)
    assert len(s.notifier.messages) == 1 and s.notifier.messages[0][0].startswith("Submission confirmed")
    s.clock.now = DUE - timedelta(hours=2)
    await run_reminders(s)
    assert len(s.notifier.messages) == 1


async def test_no_note_when_confirmed_long_before(db, env_file):
    now = DUE - timedelta(days=2)
    s = _svc(db, env_file, now)
    _seed(db, now)
    db.conn.execute("update due_dates set submitted=1, confirm_note_sent=1")
    s.clock.now = DUE - timedelta(hours=2)
    await run_reminders(s)
    assert s.notifier.messages == []


async def test_uncertain_check_still_reminds(db, env_file):
    now = DUE - timedelta(hours=2)
    s = _svc(db, env_file, now)
    _seed(db, now)

    async def boom(ou, folder):
        raise AuthRequired("login needed")

    s.bs.submitted = boom
    await run_reminders(s)
    text = s.notifier.messages[0][0]
    assert "Could not confirm submission." in text and "[Urgent]" in text and "2h left" in text


async def test_urgent_ignores_quiet_hours_and_mute_but_early_respects_them(db, env_file):
    db.set_setting("quiet", "0-23")
    db.set_json_setting("muted", ["INV2101"])
    now = DUE - timedelta(hours=23)
    s = _svc(db, env_file, now)
    _seed(db, now)
    await run_reminders(s)
    assert s.notifier.messages == []  # held, not marked sent
    s.clock.now = DUE - timedelta(hours=5)
    await run_reminders(s)
    assert len(s.notifier.messages) == 1 and "[Urgent]" in s.notifier.messages[0][0]
    db.set_setting("paused", "1")
    s.clock.now = DUE - timedelta(minutes=45)
    await run_reminders(s)
    assert len(s.notifier.messages) == 1  # /pause still stops everything


async def test_resume_resends_urgent_once(db, env_file):
    from ops.bot import Commands

    now = DUE - timedelta(hours=2)
    s = _svc(db, env_file, now)
    _seed(db, now)
    await run_reminders(s)
    assert len(s.notifier.messages) == 1
    cmds = Commands(db, {})
    await cmds.run("pause", [])
    await cmds.run("resume", [])
    await run_reminders(s)
    await run_reminders(s)
    assert len(s.notifier.messages) == 2


async def test_snooze_limits(db, env_file):
    now = DUE - timedelta(hours=2)
    s = _svc(db, env_file, now)
    _seed(db, now)
    key = "bs:assignment:1001:55"
    reminders.snooze(s, key)
    until = db.conn.execute("select snooze_until from due_dates").fetchone()[0]
    assert until.startswith((now + timedelta(hours=1)).isoformat()[:16])
    s.clock.now = DUE - timedelta(minutes=70)
    reminders.snooze(s, key)  # capped at 30 minutes before due
    assert db.conn.execute("select snooze_until from due_dates").fetchone()[0].startswith(
        (DUE - timedelta(minutes=30)).isoformat()[:16])
    await run_reminders(s)
    assert s.notifier.messages == []  # snoozed
    s.clock.now = DUE - timedelta(minutes=29)
    await run_reminders(s)
    assert len(s.notifier.messages) == 1  # last 30 minutes always come through
    assert "No snooze" in reminders.snooze(s, key)


async def test_submitted_elsewhere_stops_nags(db, env_file):
    now = DUE - timedelta(hours=2)
    s = _svc(db, env_file, now)
    _seed(db, now)
    reminders.submitted_elsewhere(s, "bs:assignment:1001:55")
    await run_reminders(s)
    s.clock.now = DUE + timedelta(minutes=5)
    await run_reminders(s)
    assert s.notifier.messages == []
    assert db.conn.execute("select submitted, user_confirmed from due_dates").fetchone()[:] == (0, 1)


async def test_missed_message_once_with_late_hint(db, env_file):
    now = DUE - timedelta(hours=1)
    s = _svc(db, env_file, now)
    it = assignment(55, due=DUE.isoformat())
    it.payload["end_at"] = (DUE + timedelta(days=1)).isoformat()
    db.upsert_item(it, content_hash(it), now)
    db.conn.execute("insert into due_dates(item_key, due_at, reminders_sent_json) values (?,?,?)",
                    (it.stable_id, DUE.isoformat(), '["3 days","1 day","6 hours","3 hours","1 hour","30 minutes"]'))
    s.clock.now = DUE + timedelta(minutes=2)
    await run_reminders(s)
    await run_reminders(s)
    assert len(s.notifier.messages) == 1
    text = s.notifier.messages[0][0]
    assert text.startswith("Missed:") and "No submission found." in text and "submit late" in text


async def test_old_misses_are_silent(db, env_file):
    now = DUE + timedelta(days=2)
    s = _svc(db, env_file, now)
    _seed(db, now)
    await run_reminders(s)
    assert s.notifier.messages == []


async def test_other_confirmed_deadlines_get_two_reminders(db, env_file):
    quiz = DUE + timedelta(days=5)
    now = quiz - timedelta(hours=20)
    s = _svc(db, env_file, now)
    DeadlineStore(db).upsert(Deadline(key="bs:cal:9", title="Quiz 1", course_code="INV2101", kind="quiz",
                                      start_utc=quiz.isoformat(), end_utc=None, all_day=False, source="brightspace",
                                      source_ref="", confidence="confirmed", quote=""), now)
    await run_reminders(s)
    await run_reminders(s)
    assert len(s.notifier.messages) == 1 and "Quiz 1" in s.notifier.messages[0][0]
    s.clock.now = quiz - timedelta(hours=2)
    await run_reminders(s)
    assert len(s.notifier.messages) == 2 and s.notifier.messages[1][1] is None


def test_reminders_job_runs_every_five_minutes():
    class Sched:
        jobs = {}

        def add_job(self, fn, trigger, **kw):
            self.jobs[kw["id"]] = trigger

    sc = Sched()
    scheduler.register(sc, None)
    fields = {f.name: str(f) for f in sc.jobs["reminders"].fields}
    assert fields["minute"] == "*/5"


# ---------------------------------------------------------------- review fixes

async def test_rem_callbacks_go_through_router_and_owner_check(db, env_file):
    from types import SimpleNamespace

    from ops.bot import CallbackRouter, build_app

    now = DUE - timedelta(hours=2)
    s = _svc(db, env_file, now)
    _seed(db, now)
    router = CallbackRouter()
    router.register("rem:snooze", lambda k: _async(reminders.snooze(s, k)))
    router.register("rem:done", lambda k: _async(reminders.submitted_elsewhere(s, k)))
    app = build_app(s.settings, db, hooks={}, router=router, token="123:abc")
    cb = next(h for hs in app.handlers.values() for h in hs if type(h).__name__ == "CallbackQueryHandler")
    replies = []

    def q(user_id, data):
        async def reply_text(t):
            replies.append(t)

        async def answer():
            pass

        return SimpleNamespace(data=data, from_user=SimpleNamespace(id=user_id), answer=answer,
                               message=SimpleNamespace(chat=SimpleNamespace(type="private"), reply_text=reply_text))

    key = "bs:assignment:1001:55"
    await cb.callback(SimpleNamespace(callback_query=q(999, f"rem:done:{key}")), SimpleNamespace())
    assert db.conn.execute("select user_confirmed from due_dates").fetchone()[0] == 0  # stranger ignored
    await cb.callback(SimpleNamespace(callback_query=q(s.settings.allowed_telegram_id, f"rem:snooze:{key}")),
                      SimpleNamespace())
    assert db.conn.execute("select snooze_until from due_dates").fetchone()[0] and "Snoozed" in replies[0]
    await cb.callback(SimpleNamespace(callback_query=q(s.settings.allowed_telegram_id, f"rem:done:{key}")),
                      SimpleNamespace())
    assert db.conn.execute("select user_confirmed from due_dates").fetchone()[0] == 1


async def _async(v):
    return v


async def test_stale_item_gets_no_ladder_or_missed(db, env_file):
    now = DUE - timedelta(hours=2)
    s = _svc(db, env_file, now)
    _seed(db, now)  # the live folder, seen now
    gone = assignment(77, due=DUE.isoformat())
    db.upsert_item(gone, content_hash(gone), now - timedelta(days=3))
    db.conn.execute("insert into due_dates(item_key, due_at) values (?,?)", (gone.stable_id, DUE.isoformat()))
    await run_reminders(s)
    assert len(s.notifier.messages) == 1 and "Tutorial 55" in s.notifier.messages[0][0]
    s.clock.now = DUE + timedelta(minutes=2)
    await run_reminders(s)
    assert all("Tutorial 77" not in m[0] for m in s.notifier.messages)


def test_migration_marks_existing_submissions_as_noted(tmp_path):
    import sqlite3

    from core.db import DB

    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.execute("create table due_dates (item_key text primary key, due_at text, submitted integer not null default 0,"
              " reminders_sent_json text not null default '[]')")
    c.execute("insert into due_dates(item_key, due_at, submitted) values ('a','x',1),('b','x',0)")
    c.commit()
    c.close()
    d = DB(path)
    d.init()
    d.init()  # idempotent
    got = {r["item_key"]: r["confirm_note_sent"] for r in d.conn.execute("select * from due_dates")}
    assert got == {"a": 1, "b": 0}


def test_watcher_clears_new_columns_when_due_moves(db):
    from ops.watcher import _upsert_due_dates

    it = assignment(55, due=DUE.isoformat())
    _upsert_due_dates(db, [it])
    db.conn.execute("update due_dates set user_confirmed=1, snooze_until='x', confirm_note_sent=1, missed_sent=1, "
                    "submitted=1")
    _upsert_due_dates(db, [it])  # unchanged: kept
    assert tuple(db.conn.execute("select user_confirmed, confirm_note_sent, missed_sent from due_dates").fetchone()) == (1, 1, 1)
    _upsert_due_dates(db, [assignment(55, due=(DUE + timedelta(days=1)).isoformat())])
    assert tuple(db.conn.execute("select user_confirmed, snooze_until, confirm_note_sent, missed_sent, submitted "
                                 "from due_dates").fetchone()) == (0, None, 0, 0, 0)


async def test_failed_send_is_retried_and_does_not_block_others(db, env_file):
    now = DUE - timedelta(hours=2)
    s = _svc(db, env_file, now)
    _seed(db, now)
    other = assignment(56, due=DUE.isoformat())
    db.upsert_item(other, content_hash(other), now)
    db.conn.execute("insert into due_dates(item_key, due_at) values (?,?)", (other.stable_id, DUE.isoformat()))
    real, calls = s.notifier.send, []

    async def flaky(text, buttons=None):
        calls.append(text)
        if len(calls) == 1:
            raise RuntimeError("telegram down")
        return await real(text, buttons)

    s.notifier.send = flaky
    await run_reminders(s)  # first send fails, the other row still goes out
    assert len(s.notifier.messages) == 1
    await run_reminders(s)  # the failed one is retried, the sent one is not repeated
    assert len(s.notifier.messages) == 2
    assert len({m[0] for m in s.notifier.messages}) == 2


async def test_failed_send_for_one_quiz_does_not_block_another(db, env_file):
    quiz = DUE + timedelta(days=5)
    now = quiz - timedelta(hours=2)
    s = _svc(db, env_file, now)
    for n in (1, 2):
        DeadlineStore(db).upsert(Deadline(key=f"bs:cal:{n}", title=f"Quiz {n}", course_code="INV2101", kind="quiz",
                                          start_utc=quiz.isoformat(), end_utc=None, all_day=False,
                                          source="brightspace", source_ref="", confidence="confirmed", quote=""), now)
    real, n_calls = s.notifier.send, []

    async def flaky(text, buttons=None):
        n_calls.append(1)
        if len(n_calls) == 1:
            raise RuntimeError("down")
        return await real(text, buttons)

    s.notifier.send = flaky
    await run_reminders(s)
    assert len(s.notifier.messages) == 1


async def test_mute_is_case_insensitive_and_no_snooze_in_last_stretch(db, env_file):
    db.set_json_setting("muted", ["INV2101"])
    from ops import policy

    assert policy.is_muted(db, "inv2101") and not policy.is_muted(db, "ABC1")
    now = DUE - timedelta(minutes=20)
    s = _svc(db, env_file, now)
    _seed(db, now)
    await run_reminders(s)
    labels = [b[0] for row in s.notifier.messages[0][1] for b in row]
    assert "Snooze 1h" not in labels and "I submitted elsewhere" in labels
