import json
from datetime import datetime, timedelta, timezone

from core import tg_group
from core.config import load_settings
from core.models import GroupMessage, Item
from ops import briefing
from ops.claude_runner import ClaudeError
from ops.diff import content_hash
from ops.watcher import Services
from tests.fakes import Clock, FakeBS, FakeNotifier, assignment

NOW = datetime(2026, 10, 26, 23, 0, tzinfo=timezone.utc)  # Tue 27 Oct 07:00 SGT


def svc(db, env_file, reply=None, fail=False):
    s = Services(settings=load_settings(env_file), db=db, sessions=None, bs=FakeBS(), notifier=FakeNotifier(),
                 clock=Clock(NOW))
    calls = []

    async def fake_run_text(prompt, input_text, model="sonnet", timeout=180, run_as=None):
        calls.append((prompt, input_text))
        if fail:
            raise ClaudeError("usage limit")
        return reply if isinstance(reply, str) else json.dumps(reply)

    s.extra["run_text"] = fake_run_text
    s.extra["calls"] = calls
    return s


def add_msgs(db, n, start=NOW - timedelta(hours=2)):
    for i in range(n):
        tg_group.store_message(db, GroupMessage(-100, i, "Ben", f"msg {i}", start + timedelta(minutes=i)))


async def test_summary_actions_become_red_alerts(db, env_file):
    s = svc(db, env_file, reply={"summary": ["Project groups formed"],
                                 "actions": [{"text": "Submit peer review form", "due": "tonight 23:59"}]})
    add_msgs(db, 3)
    await briefing.group_summary(s, NOW - timedelta(hours=6))
    texts = [m for m, _ in s.notifier.messages]
    assert any("Project groups formed" in t for t in texts)
    assert any(t.startswith("[Urgent]") and "Submit peer review form" in t and "tonight 23:59" in t for t in texts)
    assert "msg 0" in s.extra["calls"][0][1]


async def test_summary_skips_when_no_messages(db, env_file):
    s = svc(db, env_file, reply={"summary": [], "actions": []})
    await briefing.group_summary(s, NOW - timedelta(hours=6))
    assert s.notifier.messages == [] and s.extra["calls"] == []


async def test_summary_malformed_json_fallback(db, env_file):
    s = svc(db, env_file, reply="Here is a summary: people talked about lunch.")
    add_msgs(db, 2)
    await briefing.group_summary(s, NOW - timedelta(hours=6))
    assert "people talked about lunch" in s.notifier.messages[0][0]


async def test_summary_json_in_code_fence(db, env_file):
    s = svc(db, env_file, reply='```json\n{"summary": ["A"], "actions": []}\n```')
    add_msgs(db, 1)
    await briefing.group_summary(s, NOW - timedelta(hours=6))
    assert s.notifier.messages[0][0] == "Class chat summary\n• A"


async def test_threshold_trigger_at_50(db, env_file):
    s = svc(db, env_file, reply={"summary": ["x"], "actions": []})
    db.set_setting("last_summary", (NOW - timedelta(hours=3)).isoformat())
    add_msgs(db, 49)
    await briefing.check_group_threshold(s)
    assert s.extra["calls"] == []
    tg_group.store_message(db, GroupMessage(-100, 999, "Ben", "one more", NOW - timedelta(minutes=1)))
    await briefing.check_group_threshold(s)
    assert len(s.extra["calls"]) == 1


async def test_briefing_input_lists_no_due_date_section(db, env_file):
    s = svc(db, env_file)
    it = assignment(55, due=(NOW + timedelta(days=3)).isoformat())
    db.upsert_item(it, content_hash(it), NOW)
    db.conn.execute("insert into due_dates(item_key, due_at, submitted) values (?,?,1)", (it.stable_id, it.payload["due_at"]))
    nd = Item("brightspace", "bs:assignment:1001:56", "INV2101", "assignment", "Reflection",
              {"ou": 1001, "folder_id": 56, "due_at": None, "end_at": None})
    db.upsert_item(nd, content_hash(nd), NOW)
    db.conn.execute("insert into due_dates(item_key, due_at) values (?,?)", (nd.stable_id, None))
    db.set_json_setting("briefing_queue", [{"text": "⚪ new calendar thing", "at": NOW.isoformat()}])
    text = await briefing.build_briefing_input(s, NOW)
    assert "Due in the next 7 days" in text and "INV2101: Tutorial 55 — due" in text and "(submitted)" in text and "Fri 30 Oct" in text
    assert "No due date" in text and "Reflection" in text
    assert "new calendar thing" in text


async def test_briefing_claude_failure_sends_raw(db, env_file):
    s = svc(db, env_file, fail=True)
    await briefing.daily_briefing(s)
    assert s.notifier.messages[0][0].startswith("Daily briefing")
    assert db.get_json_setting("briefing_queue", None) == []


async def test_briefing_due_check_runs_once_per_day(db, env_file):
    s = svc(db, env_file, reply="☀️ Good morning")
    await briefing.maybe_daily_briefing(s)
    await briefing.maybe_daily_briefing(s)
    assert len(s.notifier.messages) == 1
    s.clock.now = NOW - timedelta(hours=1)  # 06:00 SGT next check on a fresh db would not fire
    db.set_setting("last_briefing_date", "")
    await briefing.maybe_daily_briefing(s)
    assert len(s.notifier.messages) == 1
