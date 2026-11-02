"""Usage cap: period arithmetic, warnings, pause/resume, job skipping and resumption, model choice, prefilter."""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from core import tg_group, usagecap
from core.config import load_settings
from core.db import DB
from core.library import Library
from core.models import GroupMessage
from ops import briefing, claude_runner, deadline_jobs, group_reply, persona, usage_watch
from ops.bot import Commands
from ops.chat_agent import build_agent_command
from ops.deadline_sources import datey_lines, extract_chats, extract_from_library
from ops.watcher import Services
from tests.fakes import Clock, FakeBS, FakeNotifier

SGT = ZoneInfo("Asia/Singapore")
# Wed 28 Oct 2026, 10:00 SGT
NOW = datetime(2026, 10, 28, 2, 0, tzinfo=timezone.utc)


def log(db, when, cost=None, model="sonnet", in_t=None, out_t=None, job="chat_telegram"):
    db.record_usage(when, job, "agent" if cost is not None else "text", model, True, 1.0, in_t, out_t, cost_usd=cost)


def svc(db, env_file, now=NOW):
    s = Services(settings=load_settings(env_file), db=db, sessions=None, bs=FakeBS(), notifier=FakeNotifier(),
                 clock=Clock(now))
    calls = []

    async def run_text(prompt, input_text, model="sonnet", timeout=180, run_as=None):
        calls.append({"prompt": prompt, "input": input_text, "model": model})
        return json.dumps({"deadlines": [], "summary": ["x"], "actions": []})

    s.extra["run_text"], s.extra["calls"] = run_text, calls
    return s


# ---------- periods (Asia/Singapore) ----------
def test_day_and_week_boundaries_are_singapore_midnight_and_monday():
    # 23:30 UTC on Tue 27 Oct is 07:30 SGT Wed 28 Oct
    t = datetime(2026, 10, 27, 23, 30, tzinfo=timezone.utc)
    assert usagecap.period_start(t, SGT, "daily") == datetime(2026, 10, 28, 0, 0, tzinfo=SGT)
    assert usagecap.period_start(t, SGT, "weekly") == datetime(2026, 10, 26, 0, 0, tzinfo=SGT)  # Monday
    assert usagecap.period_end(t, SGT, "daily") == datetime(2026, 10, 29, 0, 0, tzinfo=SGT)
    assert usagecap.period_end(t, SGT, "weekly") == datetime(2026, 11, 2, 0, 0, tzinfo=SGT)


def test_sunday_night_belongs_to_the_week_that_started_on_monday():
    sun = datetime(2026, 11, 1, 15, 59, tzinfo=timezone.utc)  # 23:59 SGT Sunday
    assert usagecap.period_start(sun, SGT, "weekly") == datetime(2026, 10, 26, 0, 0, tzinfo=SGT)
    mon = sun + timedelta(minutes=1)  # 00:00 SGT Monday
    assert usagecap.period_start(mon, SGT, "weekly") == datetime(2026, 11, 2, 0, 0, tzinfo=SGT)


def test_spend_counts_only_this_period(db):
    cap = {"daily": 1.0, "weekly": 10.0}
    usagecap.set_config(db, **cap)
    log(db, datetime(2026, 10, 27, 15, 59, tzinfo=timezone.utc), cost=0.50)   # 23:59 SGT Tue: yesterday
    log(db, datetime(2026, 10, 27, 16, 0, tzinfo=timezone.utc), cost=0.25)    # 00:00 SGT Wed: today
    snap = usagecap.snapshot(db, SGT, NOW)
    assert snap["periods"]["daily"]["used"] == 0.25 and snap["periods"]["weekly"]["used"] == 0.75
    assert snap["periods"]["daily"]["percent"] == 25


def test_cost_is_reported_or_estimated_from_tokens(db):
    log(db, NOW, cost=0.1234)
    log(db, NOW, model="haiku", in_t=1_000_000, out_t=100_000)          # 1.00 + 0.50
    log(db, NOW, model=None, in_t=100_000, out_t=10_000)                 # sonnet: 0.30 + 0.15
    usagecap.set_config(db, daily=5)
    assert usagecap.snapshot(db, SGT, NOW)["periods"]["daily"]["used"] == pytest.approx(0.1234 + 1.5 + 0.45, abs=0.01)


# ---------- config and pause ----------
def test_off_by_default_and_never_pauses(db):
    log(db, NOW, cost=500)
    snap = usagecap.snapshot(db, SGT, NOW)
    assert not snap["enabled"] and not snap["paused"] and not usagecap.is_paused(db, SGT, NOW)
    assert usagecap.describe(snap) == "Claude usage cap: off"


def test_config_rejects_junk(db):
    assert usagecap.set_config(db, "abc", -3, 1) == {"daily": None, "weekly": None, "keep_group_replies": True}
    assert usagecap.set_config(db, "2.5", "")["daily"] == 2.5


def test_pauses_at_100_percent_and_resumes_until_the_period_ends(db):
    usagecap.set_config(db, daily=1.0)
    log(db, NOW, cost=0.99)
    assert not usagecap.is_paused(db, SGT, NOW)
    log(db, NOW, cost=0.01)
    assert usagecap.is_paused(db, SGT, NOW)
    usagecap.resume(db, SGT, NOW)
    assert not usagecap.is_paused(db, SGT, NOW)
    assert "resumed" in usagecap.describe(usagecap.snapshot(db, SGT, NOW))
    log(db, NOW + timedelta(hours=1), cost=5)                       # still resumed after more spending
    assert not usagecap.is_paused(db, SGT, NOW + timedelta(hours=1))
    tomorrow = NOW + timedelta(days=1)                              # new day, new allowance
    assert not usagecap.is_paused(db, SGT, tomorrow)
    log(db, tomorrow, cost=2)
    assert usagecap.is_paused(db, SGT, tomorrow)                    # and the old resume does not carry over


def test_weekly_cap_pauses_independently(db):
    usagecap.set_config(db, daily=100, weekly=2.0)
    log(db, NOW - timedelta(days=2), cost=2.5)
    assert usagecap.is_paused(db, SGT, NOW)
    assert not usagecap.is_paused(db, SGT, NOW + timedelta(days=5))  # next Monday


def test_raising_the_cap_unpauses(db):
    usagecap.set_config(db, daily=1)
    log(db, NOW, cost=1.5)
    assert usagecap.is_paused(db, SGT, NOW)
    usagecap.set_config(db, daily=3)
    assert not usagecap.is_paused(db, SGT, NOW)


# ---------- warnings ----------
async def test_warnings_sent_once_per_period_each(db, env_file):
    s = svc(db, env_file)
    usagecap.set_config(db, daily=1.0)
    assert await usage_watch.check(s) == []
    log(db, NOW, cost=0.85)
    assert await usage_watch.check(s) == ["daily:80"]
    assert await usage_watch.check(s) == []                                    # not again
    log(db, NOW, cost=0.20)
    assert await usage_watch.check(s) == ["daily:100"]
    assert await usage_watch.check(s) == []
    texts = [m for m, _ in s.notifier.messages]
    assert len(texts) == 2 and "80%" in texts[0] and "spent" in texts[1] and "Sir" in texts[0]
    assert s.notifier.messages[0][1] is None
    assert s.notifier.messages[1][1] == [[("Resume for the rest of today", "usage:resume")]]
    s.clock.now = NOW + timedelta(days=1)                                      # next day: fresh
    log(db, s.clock.now, cost=0.9)
    assert await usage_watch.check(s) == ["daily:80"]


async def test_jumping_straight_past_100_sends_one_message_and_no_late_80(db, env_file):
    s = svc(db, env_file)
    usagecap.set_config(db, daily=1.0)
    log(db, NOW, cost=1.4)
    assert await usage_watch.check(s) == ["daily:100"]
    assert await usage_watch.check(s) == [] and len(s.notifier.messages) == 1


async def test_changing_the_cap_rearms_warnings(db, env_file):
    s = svc(db, env_file)
    usagecap.set_config(db, daily=1.0)
    log(db, NOW, cost=0.9)
    await usage_watch.check(s)
    usagecap.set_config(db, daily=1.1)
    assert await usage_watch.check(s) == ["daily:80"]


async def test_no_warning_when_off(db, env_file):
    s = svc(db, env_file)
    log(db, NOW, cost=99)
    assert await usage_watch.check(s) == [] and s.notifier.messages == []


async def test_usage_command_and_resume_button(db, env_file):
    s = svc(db, env_file)
    usagecap.set_config(db, daily=1.0)
    assert "nothing is paused" in (await usage_watch.command(s, "resume")).lower()
    log(db, NOW, cost=2)
    assert "PAUSED" in await usage_watch.command(s)
    assert "running again" in await usage_watch.on_resume_button(s)
    assert not usagecap.is_paused(db, SGT, NOW)


async def test_bot_commands_route_to_usage_hook(db):
    seen = []

    async def usage(*a):
        seen.append(a)
        return "ok"

    c = Commands(db, {"usage": usage})
    await c.run("usage", [])
    await c.run("usage", ["resume"])
    await c.run("resume", ["usage"])
    assert seen == [(), ("resume",), ("resume",)]


# ---------- job skipping and resumption ----------
def add_group_msgs(db, n=3):
    for i in range(n):
        tg_group.store_message(db, GroupMessage(-100, i, "Ben", f"quiz friday {i}", NOW - timedelta(hours=1)))


async def test_group_summary_skipped_while_paused_then_covers_the_gap(db, env_file):
    s = svc(db, env_file)
    usagecap.set_config(db, daily=1.0)
    log(db, NOW, cost=2)
    add_group_msgs(db)
    await briefing.group_summary(s, NOW - timedelta(hours=6))
    assert s.extra["calls"] == [] and s.notifier.messages == [] and db.get_setting("last_summary") is None
    usagecap.resume(db, SGT, NOW)                                  # the same messages are summarised afterwards
    await briefing.group_summary(s, NOW - timedelta(hours=6))
    assert len(s.extra["calls"]) == 1 and s.extra["calls"][0]["model"] == "haiku"


async def test_extraction_held_when_paused_and_picked_up_after(db, env_file):
    s = svc(db, env_file)
    usagecap.set_config(db, daily=1.0)
    log(db, NOW, cost=2)
    lib = Library(db)
    lib.upsert("bs:announcement:1:7", "C1", "announcement", "Quiz", "", "", "Quiz 2 on 10 Nov", NOW, "v1")
    tg_group.store_message(db, GroupMessage(-100, 1, "Ben", "meet thu 2pm", NOW - timedelta(minutes=5)))
    assert await extract_from_library(s, ["bs:announcement:1:7"]) == 0
    assert await extract_chats(s) == 0
    assert s.extra["calls"] == []
    assert lib.pending_extraction() == ["bs:announcement:1:7"]      # not marked as done
    assert db.get_setting("last_chat_extract") is None              # the chat window did not move
    usagecap.resume(db, SGT, NOW)
    await extract_from_library(s, [])                               # no keys passed: the pending list drives it
    await extract_chats(s)
    models = {c["model"] for c in s.extra["calls"]}
    assert len(s.extra["calls"]) == 2 and models == {"sonnet", "haiku"}
    assert lib.pending_extraction() == [] and db.get_setting("last_chat_extract")


async def test_manual_scan_reply_says_extraction_is_paused(db, env_file, monkeypatch):
    s = svc(db, env_file)
    usagecap.set_config(db, daily=1.0)
    log(db, NOW, cost=2)

    async def fake_extract(ctx):
        return {"courses": 1, "documents": 0, "announcements": 0, "assignments": 0, "errors": 0, "deadlines": 0}

    monkeypatch.setattr(deadline_jobs, "extract_job", fake_extract)
    assert "paused to save usage" in await deadline_jobs.scan_command(s)


async def test_briefing_still_goes_out_as_a_plain_list_when_paused(db, env_file):
    s = svc(db, env_file)
    usagecap.set_config(db, daily=1.0)
    log(db, NOW, cost=2)
    await briefing.daily_briefing(s)
    assert s.extra["calls"] == []
    text = s.notifier.messages[0][0]
    assert text.startswith("Good morning, Sir") and "paused to save usage" in text and "Due in the next 7 days" in text
    assert db.get_setting("last_briefing_date")


async def test_briefing_uses_claude_when_not_paused(db, env_file):
    s = svc(db, env_file)
    usagecap.set_config(db, daily=100)
    await briefing.daily_briefing(s)
    assert s.extra["calls"][0]["model"] == "sonnet"


class Msg:
    def __init__(self, text="when is the quiz?", msg_id=1):
        self.text, self.msg_id = text, msg_id


class Client:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_to=None):
        self.sent.append(text)


async def reply_once(s, db):
    from core import groups

    groups.save(db, [{"id": -100, "label": "Team", "kind": "reply"}])
    seen = []

    async def runner(prompt, input_text, model="sonnet", timeout=180, run_as=None):
        seen.append(model)
        return "Friday."

    async def nosleep(_):
        pass

    r = group_reply.GroupReplier(s, run_text=runner, sleep=nosleep, typing_delay=False)
    c = Client()
    await r.on_message(c, -100, Msg())
    return seen, c, db.get_json_setting("group_reply_status", {})


async def test_group_replies_pause_by_default_but_can_be_kept(db, env_file):
    s = svc(db, env_file)
    usagecap.set_config(db, daily=1.0)
    log(db, NOW, cost=2)
    seen, client, status = await reply_once(s, db)
    assert seen == [] and client.sent == [] and status["state"] == "paused"
    usagecap.set_config(db, daily=1.0, keep_group_replies=True)
    seen, client, status = await reply_once(s, db)
    assert seen == ["haiku"] and client.sent == ["Friday."]


async def test_chat_and_do_it_are_never_gated(db, env_file):
    """Nothing in the chat agent or Do it consults the cap: they only ever read the model setting."""
    import inspect

    from ops import chat_agent, doit
    for mod in (chat_agent, doit):
        assert "is_paused" not in inspect.getsource(mod) and "usagecap.paused" not in inspect.getsource(mod)


# ---------- models ----------
def test_model_defaults_and_overrides(db):
    assert usagecap.model_for(db, "group_summary") == "haiku" and usagecap.model_for(db, "extract_chats") == "haiku"
    assert usagecap.model_for(db, "extract_docs") == "sonnet" and usagecap.model_for(db, "briefing") == "sonnet"
    assert usagecap.model_for(db, "chat") == ""
    usagecap.set_models(db, {"briefing": "haiku", "chat": "opus", "group_summary": "gpt-9", "bogus": "haiku"})
    assert usagecap.model_for(db, "briefing") == "haiku" and usagecap.model_for(db, "chat") == "opus"
    assert usagecap.model_for(db, "group_summary") == "haiku"      # invalid value ignored -> default
    usagecap.set_models(db, {"chat": ""})
    assert usagecap.model_for(db, "chat") == ""


def test_chat_command_carries_the_chat_model_only_when_set(env_file):
    s = load_settings(env_file)
    assert "--model" not in build_agent_command("hi", s, None)               # no DB yet: default
    d = DB(s.db_path)
    d.init()
    assert "--model" not in build_agent_command("hi", s, None)
    usagecap.set_models(d, {"chat": "haiku"})
    cmd = build_agent_command("hi", s, "sess")
    assert cmd[cmd.index("--model") + 1] == "haiku" and "--resume" in cmd


# ---------- prefilter ----------
@pytest.mark.parametrize("line", ["quiz on friday", "see you tmr", "meet at 3pm", "week 7 lab", "submit by 5 Nov",
                                  "due 12/11", "presentation next week", "1430hrs at the library", "EXAM is 3 Dec",
                                  "next mon?", "deadline extended"])
def test_datey_lines_matches(line):
    assert datey_lines([line], 0, min_chars=0) == [line]


@pytest.mark.parametrize("line", ["lol", "ok thanks!", "who has the notes", "haha sundae", "monkey"])
def test_datey_lines_ignores_chatter(line):
    assert datey_lines([line], 0) == []


def test_datey_lines_keep_one_line_of_context_each_side():
    lines = ["a", "b", "c", "the quiz?", "friday 3pm", "ok", "z1", "z2", "z3"]
    assert datey_lines(lines) == ["c", "the quiz?", "friday 3pm", "ok"]
    assert datey_lines(["x", "y"]) == []


async def test_chat_extraction_only_sends_datey_lines_and_skips_pure_chatter(db, env_file):
    s = svc(db, env_file)
    pad = "blah " * 120  # a batch over the small-batch threshold, so the prefilter applies
    for i, t in enumerate(["hello", "hi", "lol", "ok", "meet thu 2pm", "k", "cool", "nice", "haha"]):
        tg_group.store_message(db, GroupMessage(-100, i, "Ben", t + (" " + pad if t not in ("meet thu 2pm",) else ""), NOW - timedelta(minutes=30 - i)))
    await extract_chats(s)
    sent = s.extra["calls"][0]["input"]
    assert "meet thu 2pm" in sent and "hello" not in sent and "haha" not in sent and "] Ben: ok blah" in sent
    # only chatter: nothing is sent, and the window still moves on
    db.set_setting("last_chat_extract", NOW.isoformat())
    s.clock.now = NOW + timedelta(hours=1)
    tg_group.store_message(db, GroupMessage(-100, 99, "Ben", "lol " + pad * 8, NOW + timedelta(minutes=10)))
    before = len(s.extra["calls"])
    await extract_chats(s)
    assert len(s.extra["calls"]) == before and db.get_setting("last_chat_extract") == s.clock.now.isoformat()


# ---------- scheduler and wiring ----------
def test_teams_is_hourly_overnight():
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from ops import scheduler

    sched = AsyncIOScheduler(timezone="Asia/Singapore")
    scheduler.register(sched, object())
    jobs = {j.id: j for j in sched.get_jobs()}
    assert "hour='7-23'" in str(jobs["teams"].trigger) and "minute='*/10'" in str(jobs["teams"].trigger)
    assert "hour='0-6'" in str(jobs["teams_night"].trigger) and "minute='0'" in str(jobs["teams_night"].trigger)
    assert "usage_cap" in jobs


def test_after_record_hook_runs_and_cannot_break_recording():
    rows, hits = [], []
    claude_runner.set_usage_sink(lambda **kw: rows.append(kw))
    claude_runner.set_after_record(lambda: hits.append(1))
    try:
        claude_runner.record_usage(kind="text", model="haiku", ok=True, seconds=1.0)
        claude_runner.set_after_record(lambda: 1 / 0)
        claude_runner.record_usage(kind="text", model="haiku", ok=True, seconds=1.0)
    finally:
        claude_runner.set_usage_sink(None)
        claude_runner.set_after_record(None)
    assert len(rows) == 2 and hits == [1]


def test_persona_voice():
    s = type("S", (), {"address_as": "Madam"})()
    assert "Madam" in persona.usage_warning(s, "weekly", 80, 4, 5) and "this week" in persona.usage_warning(s, "weekly", 100, 5, 5)


async def test_teams_lines_are_prefiltered_too(db, env_file):
    from core.models import Item
    from ops.diff import content_hash

    s = svc(db, env_file)
    for i, t in enumerate(["thanks", "see you", "assignment due 5 Nov", "ok", "lol"]):
        t = t if "due" in t else t + " " + "blah " * 200
        it = Item("teams", f"t{i}", None, "chat", "m", {"chat_name": "Proj", "sender": "Ann", "body_text": t,
                                                        "ts": (NOW - timedelta(minutes=10 - i)).isoformat()})
        db.upsert_item(it, content_hash(it), NOW - timedelta(minutes=5))
    await extract_chats(s)
    sent = s.extra["calls"][0]["input"]
    assert "due 5 Nov" in sent and "thanks" not in sent and "lol" not in sent


# ---------- review fixes ----------
@pytest.mark.parametrize("line", ["MST moved to after recess", "hand in the report by noon", "lab report 2359",
                                  "CA2 brief out, 2 weeks to do", "in two weeks", "明天交报告", "esok hantar",
                                  "minggu depan", "submit by midnight", "report due 0900", "a few days left"])
def test_datey_lines_catches_real_deadline_wording(line):
    assert datey_lines([line], 0) == [line]


@pytest.mark.parametrize("line", ["who is bringing snacks", "haha true", "thanks bro", "can someone share the notes pls",
                                  "I am so tired"])
def test_datey_lines_negatives(line):
    assert datey_lines([line], 0) == []


def test_small_batches_skip_the_prefilter():
    chat = ["morning", "anyone up", "lol"]
    assert datey_lines(chat, min_chars=4000) == chat
    assert datey_lines(chat, min_chars=5) == []


async def test_small_chat_batch_goes_out_whole(db, env_file):
    s = svc(db, env_file)
    tg_group.store_message(db, GroupMessage(-100, 1, "Ben", "ok so about that thing", NOW - timedelta(minutes=5)))
    await extract_chats(s)
    assert "about that thing" in s.extra["calls"][0]["input"]


async def test_warning_levels_use_the_raw_fraction(db, env_file):
    s = svc(db, env_file)
    usagecap.set_config(db, daily=100)
    log(db, NOW, cost=99.5)
    assert await usage_watch.check(s) == ["daily:80"] and not usagecap.is_paused(db, SGT, NOW)
    assert usagecap.snapshot(db, SGT, NOW)["periods"]["daily"]["percent"] == 99
    d2 = DB(db.path.parent / "b.db")
    d2.init()
    s2 = svc(d2, env_file)
    usagecap.set_config(d2, daily=100)
    log(d2, NOW, cost=79.5)
    assert await usage_watch.check(s2) == []


def test_config_validation_and_merge(db):
    for bad in (float("inf"), float("nan"), -1, "x", 0):
        assert usagecap.set_config(db, daily=bad)["daily"] is None
    assert usagecap.set_config(db, daily=1e9)["daily"] == usagecap.MAX_LIMIT
    usagecap.set_config(db, daily=2, weekly=5, keep_group_replies=True)
    assert usagecap.set_config(db, daily=3) == {"daily": 3.0, "weekly": 5.0, "keep_group_replies": True}
    usagecap.set_models(db, {"briefing": "haiku"})
    usagecap.set_models(db, {"chat": "opus"})
    assert usagecap.model_for(db, "briefing") == "haiku" and usagecap.model_for(db, "chat") == "opus"


async def test_email_arriving_during_pause_is_extracted_after_resume(db, env_file, tmp_path):
    from ops import mail_jobs
    from tests.test_mail import FakeReader, mail

    reply = {"deadlines": [{"title": "Quiz 2", "date": "2026-11-03", "kind": "quiz", "source": "email:n1",
                            "quote": "Quiz 2 on 3 Nov"}]}
    batch = [mail("n1", "Quiz 2 on Tuesday 3 Nov", addr="tan@np.edu.sg", body="Quiz 2 will be on Tuesday 3 Nov.")]
    s = mail_svc(db, env_file, tmp_path, FakeReader([[], batch, []]), reply)
    s.clock.now = NOW
    usagecap.set_config(db, daily=1.0)
    log(db, NOW, cost=2)
    await mail_jobs.sweep_outlook(s)
    await mail_jobs.sweep_outlook(s)
    assert s.extra["runs"] == [] and db.get_json_setting(mail_jobs.PENDING_KEY, []) == ["n1"]
    usagecap.resume(db, SGT, NOW)
    await mail_jobs.sweep_outlook(s)                       # a later sweep with no new mail drains the pending one
    from core.deadlines import DeadlineStore
    assert [d.title for d in DeadlineStore(db).active()] == ["Quiz 2"]
    assert db.get_json_setting(mail_jobs.PENDING_KEY, []) == [] and s.extra["models"] == ["haiku"]


def mail_svc(db, env_file, tmp_path, reader, reply):
    from tests.test_mail import svc as base

    s = base(db, env_file, tmp_path, reader, reply=reply)
    s.settings.tz  # noqa: B018
    models = []
    inner = s.extra["run_text"]

    async def run_text(prompt, input_text, model="sonnet", timeout=180, run_as=None):
        models.append(model)
        return await inner(prompt, input_text, model=model)

    s.extra["run_text"], s.extra["models"] = run_text, models
    return s
