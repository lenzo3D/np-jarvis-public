from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ops.scheduler import register

SGT = ZoneInfo("Asia/Singapore")


def test_register_brightspace_jobs():
    sched = AsyncIOScheduler(timezone=SGT)
    register(sched, services=object())
    ids = {j.id for j in sched.get_jobs()}
    assert {"bs_day", "bs_night", "flush_batch", "reminders", "teams", "expire_actions", "group_summary", "group_threshold", "briefing", "deadlines_confirmed", "deadlines_extract", "save_sessions"} <= ids


def test_brightspace_day_schedule_every_15_min():
    sched = AsyncIOScheduler(timezone=SGT)
    register(sched, services=object())
    trig = sched.get_job("bs_day").trigger
    t = trig.get_next_fire_time(None, datetime(2026, 10, 26, 9, 1, tzinfo=SGT))
    assert (t.hour, t.minute) == (9, 15)
    night = trig.get_next_fire_time(None, datetime(2026, 10, 26, 23, 50, tzinfo=SGT))
    assert (night.day, night.hour) == (27, 7)


async def test_save_sessions_job_saves_open_contexts():
    from ops.scheduler import save_sessions

    saved = []

    class SM:
        _contexts = {"brightspace": object(), "teams": object()}

        async def save_cookies(self, site, ctx=None):
            saved.append(site)

    await save_sessions(type("S", (), {"sessions": SM()})())
    assert saved == ["brightspace", "teams"]
