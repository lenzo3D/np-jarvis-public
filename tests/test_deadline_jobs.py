import json
from datetime import datetime, timezone

from core.config import load_settings
from core.deadlines import Deadline, DeadlineStore
from core.gcal import CalendarAuthRequired
from core.models import Course
from ops import deadline_jobs
from ops.watcher import Services
from tests.fakes import Clock, FakeBS, FakeNotifier

NOW = datetime(2026, 10, 27, 2, 0, tzinfo=timezone.utc)


def svc(db, env_file):
    s = Services(settings=load_settings(env_file), db=db, sessions=None, bs=FakeBS(), notifier=FakeNotifier(),
                 clock=Clock(NOW))
    s.bs.course_list = [Course(1, "26S2-731IV_013686", "INVESTMENTS(731IV_013686)")]
    return s


def add(db, key, title, start, conf="confirmed", all_day=False):
    DeadlineStore(db).upsert(Deadline(key, title, "26S2-731IV_013686", "assignment", start, None, all_day,
                                      "brightspace", "", conf, ""), NOW)


async def test_deadlines_text(db, env_file):
    s = svc(db, env_file)
    add(db, "a", "Tutorial 5", "2026-10-30T15:29:00+00:00")
    add(db, "b", "Quiz 2", "2026-11-03", conf="tentative", all_day=True)
    add(db, "c", "Far away", "2027-01-30T15:29:00+00:00")
    text = await deadline_jobs.deadlines_text(s, days=14)
    assert "Fri 30 Oct 23:29 · Investments: Tutorial 5\n" in text + "\n"
    assert "Tue 03 Nov · Investments: Quiz 2 [tentative]" in text and "Far away" not in text


async def test_sync_warns_once_on_calendar_auth(db, env_file):
    s = svc(db, env_file)

    class BadCal:
        async def insert(self, body):
            raise CalendarAuthRequired("run login")

    s.extra["gcal"] = BadCal()
    add(db, "a", "Tutorial 5", "2026-10-30T15:29:00+00:00")
    await deadline_jobs.calendar_sync(s)
    await deadline_jobs.calendar_sync(s)
    warns = [m for m, _ in s.notifier.messages if "Google Calendar" in m]
    assert len(warns) == 1


async def test_sync_skipped_when_not_connected(db, env_file):
    s = svc(db, env_file)
    add(db, "a", "Tutorial 5", "2026-10-30T15:29:00+00:00")
    rep = await deadline_jobs.calendar_sync(s)
    assert rep is None and s.notifier.messages == []


def test_read_only_mcp_server_has_no_write_tools():
    import subprocess
    import sys
    code = ("import asyncio; from school_mcp.server import server; "
            "print(sorted(t.name for t in asyncio.run(server.list_tools())))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=".",
                         env={"SCHOOL_MCP_READONLY": "1", "PATH": ""}).stdout
    assert "get_deadlines" in out
    for w in ("submit_assignment", "post_discussion", "send_teams_message", "send_file_to_me", "download_file"):
        assert w not in out



async def test_course_names_cached_when_brightspace_fails(db, env_file):
    from ops.deadline_sources import _course_names
    s = svc(db, env_file)
    assert (await _course_names(s))["26S2-731IV_013686"] == "INVESTMENTS(731IV_013686)"
    s.bs.fail = RuntimeError("down")
    assert (await _course_names(s))["26S2-731IV_013686"] == "INVESTMENTS(731IV_013686)"


async def test_calendar_sync_is_serialised(db, env_file):
    import asyncio
    s = svc(db, env_file)
    active = {"n": 0, "max": 0}

    class SlowCal:
        async def insert(self, body):
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
            await asyncio.sleep(0.01)
            active["n"] -= 1
            return body["id"]

    s.extra["gcal"] = SlowCal()
    add(db, "a", "Tutorial 5", "2026-10-30T15:29:00+00:00")
    add(db, "b", "Tutorial 6", "2026-10-31T15:29:00+00:00")
    await asyncio.gather(deadline_jobs.calendar_sync(s), deadline_jobs.calendar_sync(s))
    assert active["max"] == 1
