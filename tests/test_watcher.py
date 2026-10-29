from core.auth import AuthRequired
from core.models import Item
from ops.watcher import Services, flush_batch, sweep_brightspace
from tests.fakes import Clock, FakeBS, FakeNotifier, assignment


def services(db, env_file):
    from core.config import load_settings
    return Services(settings=load_settings(env_file), db=db, sessions=None, bs=FakeBS(),
                    notifier=FakeNotifier(), clock=Clock())


async def test_baseline_sends_nothing(db, env_file):
    s = services(db, env_file)
    s.bs.assign[1001] = [assignment(1), assignment(2)]
    await sweep_brightspace(s)
    assert s.notifier.messages == []


async def test_new_assignment_sends_red_with_buttons(db, env_file):
    s = services(db, env_file)
    await sweep_brightspace(s)
    s.bs.assign[1001] = [assignment(5)]
    await sweep_brightspace(s)
    text, buttons = s.notifier.messages[0]
    assert text.startswith("[Urgent]") and "Tutorial 5" in text
    datas = [d for row in buttons for _, d in row]
    assert "doit:bs:assignment:1001:5" in datas and "mute:INV2101" in datas
    row = db.conn.execute("select due_at from due_dates where item_key='bs:assignment:1001:5'").fetchone()
    assert row["due_at"] == "2026-10-30T15:59:00+00:00"


async def test_content_has_download_button(db, env_file):
    s = services(db, env_file)
    await sweep_brightspace(s)
    s.bs.content[1001] = [Item("brightspace", "bs:content:702", "INV2101", "content", "Tutorial 7.docx",
                               {"ou": 1001, "topic_id": 702, "type": "File", "module": "Week 7"})]
    await sweep_brightspace(s)
    assert s.notifier.messages == []  # yellow → batched
    queue = db.get_json_setting("batch_queue", [])
    assert len(queue) == 1 and queue[0]["buttons"] == [[["Download", "dl:1001:702"]]]


async def test_yellow_goes_to_batch_then_flush_sends_one_message(db, env_file):
    s = services(db, env_file)
    await sweep_brightspace(s)
    s.bs.news[1001] = [Item("brightspace", f"bs:announcement:1001:{i}", "INV2101", "announcement",
                            f"Week {i} notes", {"body_text": "hello"}) for i in range(3)]
    await sweep_brightspace(s)
    assert s.notifier.messages == []
    await flush_batch(s)
    assert len(s.notifier.messages) == 1
    assert s.notifier.messages[0][0].count("Week") == 3
    await flush_batch(s)
    assert len(s.notifier.messages) == 1  # queue cleared


async def test_flush_waits_during_quiet_hours(db, env_file):
    s = services(db, env_file)
    db.set_setting("quiet", "0-23")
    db.set_json_setting("batch_queue", [{"text": "x", "buttons": None}])
    await flush_batch(s)
    assert s.notifier.messages == []


async def test_three_failures_one_warning(db, env_file):
    s = services(db, env_file)
    s.bs.fail = RuntimeError("boom")
    for _ in range(4):
        await sweep_brightspace(s)
    warnings = [m for m, _ in s.notifier.messages if "checks failing" in m]
    assert len(warnings) == 1
    s.bs.fail = None
    await sweep_brightspace(s)
    await sweep_brightspace(s)
    assert len([m for m, _ in s.notifier.messages if "back to normal" in m]) == 1


async def test_auth_required_prompts_retry_not_failure(db, env_file):
    s = services(db, env_file)
    s.bs.fail = AuthRequired("brightspace")
    await sweep_brightspace(s)
    await sweep_brightspace(s)
    prompts = [(m, b) for m, b in s.notifier.messages if "session expired" in m]
    assert len(prompts) == 1  # not repeated every sweep
    assert prompts[0][1] == [[("Log in now", "login:retry:brightspace")]]
    row = db.conn.execute("select consecutive_failures from source_health where source='brightspace'").fetchone()
    assert row is None or row[0] == 0


class FakeTeams:
    def __init__(self):
        self.items = []
        self.since = None

    async def recent(self, since):
        self.since = since
        return list(self.items)


async def test_teams_mention_alert(db, env_file):
    from ops.watcher import sweep_teams
    s = services(db, env_file)
    s.teams = FakeTeams()
    await sweep_teams(s)
    s.teams.items = [Item("teams", "teams:t1/ch1:p2", None, "teams_mention", "Mr Tan in INV2101 › General",
                          {"chat_name": "INV2101 › General", "sender": "Mr Tan", "body_text": "Jake please present"})]
    await sweep_teams(s)
    text, _ = s.notifier.messages[0]
    assert text.startswith("[Urgent]") and "Jake please present" in text
    assert s.teams.since == s.clock() - __import__("datetime").timedelta(hours=6)


async def test_one_endpoint_failure_does_not_stop_sweep(db, env_file):
    import httpx
    s = services(db, env_file)
    await sweep_brightspace(s)

    async def grades_403(ou, code):
        raise httpx.HTTPStatusError("403", request=httpx.Request("GET", "http://x"),
                                    response=httpx.Response(403))

    s.bs.grades = grades_403
    s.bs.assign[1001] = [assignment(9)]
    await sweep_brightspace(s)
    assert any("Tutorial 9" in m for m, _ in s.notifier.messages)
    assert not any("session expired" in m or "checks failing" in m for m, _ in s.notifier.messages)
    row = db.conn.execute("select consecutive_failures from source_health where source='brightspace'").fetchone()
    assert row[0] == 0


async def test_every_endpoint_failing_counts_as_failure(db, env_file):
    s = services(db, env_file)

    async def boom(ou, code):
        raise RuntimeError("down")

    for name in ("assignment_items", "announcements", "content_items", "grades"):
        setattr(s.bs, name, boom)
    await sweep_brightspace(s)
    row = db.conn.execute("select consecutive_failures from source_health where source='brightspace'").fetchone()
    assert row[0] == 1


async def test_due_date_change_resets_reminders(db, env_file):
    import json
    s = services(db, env_file)
    s.bs.assign[1001] = [assignment(5, due="2026-10-27T15:59:00+00:00")]
    await sweep_brightspace(s)
    db.conn.execute("update due_dates set reminders_sent_json=? , submitted=1",
                    (json.dumps(["3 days", "1 day"]),))
    s.bs.assign[1001] = [assignment(5, due="2026-11-03T15:59:00+00:00")]
    await sweep_brightspace(s)
    row = db.conn.execute("select * from due_dates").fetchone()
    assert row["due_at"] == "2026-11-03T15:59:00+00:00"
    assert json.loads(row["reminders_sent_json"]) == [] and row["submitted"] == 0


async def test_unchanged_due_keeps_reminders(db, env_file):
    import json
    s = services(db, env_file)
    s.bs.assign[1001] = [assignment(5)]
    await sweep_brightspace(s)
    db.conn.execute("update due_dates set reminders_sent_json=?", (json.dumps(["3 days"]),))
    await sweep_brightspace(s)
    assert json.loads(db.conn.execute("select reminders_sent_json from due_dates").fetchone()[0]) == ["3 days"]


async def test_sweep_refreshes_submitted_for_upcoming(db, env_file):
    s = services(db, env_file)  # clock 26 Oct; due 30 Oct is within 7 days
    s.bs.assign[1001] = [assignment(5), assignment(6, due="2026-12-30T15:59:00+00:00")]
    s.bs.submitted_folders = {5, 6}
    await sweep_brightspace(s)
    rows = dict(db.conn.execute("select item_key, submitted from due_dates").fetchall())
    assert rows["bs:assignment:1001:5"] == 1
    assert rows["bs:assignment:1001:6"] == 0  # far away: not checked every sweep


class FlakyNotifier(FakeNotifier):
    def __init__(self, fail_times):
        super().__init__()
        self.fail_times = fail_times

    async def send(self, text, buttons=None):
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("telegram timed out")
        return await super().send(text, buttons)


async def test_failed_red_alert_is_retried(db, env_file):
    s = services(db, env_file)
    await sweep_brightspace(s)
    s.notifier = FlakyNotifier(fail_times=1)
    s.bs.assign[1001] = [assignment(5), assignment(6)]
    s.bs.news[1001] = [Item("brightspace", "bs:announcement:1001:1", "INV2101", "announcement", "Week 3 notes",
                            {"body_text": "hi"})]
    await sweep_brightspace(s)  # first send fails, second succeeds, yellow still queued
    assert len(s.notifier.messages) == 1
    assert len(db.get_json_setting("batch_queue", [])) == 1
    await flush_batch(s)  # retries the failed red, then the batch
    texts = [m for m, _ in s.notifier.messages]
    assert sum("Tutorial" in t for t in texts if t.startswith("[Urgent]")) == 2
    assert db.get_json_setting("retry_queue", []) == []
