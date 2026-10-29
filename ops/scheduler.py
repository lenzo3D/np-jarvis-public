"""APScheduler job wiring (Asia/Singapore). Later features add their jobs here."""
from __future__ import annotations

from apscheduler.triggers.cron import CronTrigger

from ops import briefing, deadline_jobs, mail_jobs, reminders, watcher

TZ = "Asia/Singapore"


def register(sched, services) -> None:
    opts = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 300, "replace_existing": True}
    sched.add_job(watcher.sweep_brightspace, CronTrigger(minute="*/15", hour="7-23", timezone=TZ),
                  args=[services], id="bs_day", **opts)
    sched.add_job(watcher.sweep_brightspace, CronTrigger(minute=0, hour="0-6", timezone=TZ),
                  args=[services], id="bs_night", **opts)
    sched.add_job(watcher.flush_batch, CronTrigger(minute=5, timezone=TZ),
                  args=[services], id="flush_batch", **opts)
    sched.add_job(watcher.flush_retry, CronTrigger(minute="*/5", timezone=TZ),
                  args=[services], id="flush_retry", **opts)
    sched.add_job(reminders.run_reminders, CronTrigger(minute="*/15", timezone=TZ),
                  args=[services], id="reminders", **opts)
    sched.add_job(mail_jobs.sweep_outlook, CronTrigger(minute="*/10", hour="7-23", timezone=TZ),
                  args=[services], id="mail_day", **opts)
    sched.add_job(mail_jobs.sweep_outlook, CronTrigger(minute=20, hour="0-6", timezone=TZ),
                  args=[services], id="mail_night", **opts)
    sched.add_job(watcher.sweep_teams, CronTrigger(minute="*/10", timezone=TZ),
                  args=[services], id="teams", **opts)
    sched.add_job(briefing.scheduled_group_summary, CronTrigger(hour="13,18,22", minute=0, timezone=TZ),
                  args=[services], id="group_summary", **opts)
    sched.add_job(briefing.check_group_threshold, CronTrigger(minute="*/15", timezone=TZ),
                  args=[services], id="group_threshold", **opts)
    sched.add_job(briefing.maybe_daily_briefing, CronTrigger(minute="*/5", timezone=TZ),
                  args=[services], id="briefing", **opts)
    sched.add_job(deadline_jobs.confirmed_job, CronTrigger(minute="7,22,37,52", timezone=TZ),
                  args=[services], id="deadlines_confirmed", **opts)
    sched.add_job(deadline_jobs.extract_job, CronTrigger(minute=40, hour="7-23", timezone=TZ),
                  args=[services], id="deadlines_extract", **opts)
    sched.add_job(save_sessions, CronTrigger(minute="*/30", timezone=TZ), args=[services], id="save_sessions", **opts)
    sched.add_job(expire_actions, CronTrigger(minute=30, timezone=TZ), args=[services], id="expire_actions", **opts)


def register_boot(sched, services, delay_s: float = 20.0) -> None:
    """One-off checks shortly after JARVIS starts, so connection indicators and alerts catch up within a minute
    instead of waiting for the next quarter-hour (e.g. right after a login or an update)."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from apscheduler.triggers.date import DateTrigger

    start = datetime.now(ZoneInfo(TZ)) + timedelta(seconds=delay_s)
    jobs = [(watcher.sweep_brightspace, "boot_bs"), (watcher.sweep_teams, "boot_teams"), (mail_jobs.sweep_outlook, "boot_mail")]
    for i, (fn, job_id) in enumerate(jobs):
        sched.add_job(fn, DateTrigger(run_date=start + timedelta(seconds=15 * i), timezone=TZ), args=[services], id=job_id,
                      replace_existing=True, misfire_grace_time=300, max_instances=1)


async def expire_actions(services) -> None:
    from core import actions

    actions.expire(services.db, services.clock())


async def save_sessions(services) -> None:
    """Keep the on-disk copy of browser session cookies fresh, so a crash or Ctrl+C never loses the login."""
    for site in list(getattr(services.sessions, "_contexts", {})):
        await services.sessions.save_cookies(site)
