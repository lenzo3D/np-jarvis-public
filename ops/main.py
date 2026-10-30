"""Entry point for the ops service: Ops bot + scheduler (+ local API when present).

    .venv/bin/python -m ops.main
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from core.auth import AuthRequired, SessionManager
from core.brightspace import Brightspace
from core.outlook import Outlook
from core.teams import Teams
from core.config import load_settings
from core.db import DB
from ops import claude_runner, deadline_jobs, persona, scheduler, watcher
from ops.chat_agent import ChatAgent
from ops.approvals import Approvals
from ops.doit import DoIt
from ops.bot import CallbackRouter, Notifier, build_app
from ops.format import fmt_dt

log = logging.getLogger("ops")


def build_services():
    settings = load_settings()
    settings.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    db = DB(settings.db_path)
    db.init()
    claude_runner.attach_db(db)
    hooks: dict = {}
    router = CallbackRouter()
    app = build_app(settings, db, hooks, router)
    notifier = Notifier(app.bot, settings.allowed_telegram_id, persona.address(settings))
    sessions = SessionManager(settings, notifier.mfa_prompt)
    bs = Brightspace(sessions, settings.politemall_base, settings.course_prefix)
    services = watcher.Services(settings=settings, db=db, sessions=sessions, bs=bs, notifier=notifier,
                                teams=Teams(sessions))
    services.extra.update(app=app, hooks=hooks, router=router, outlook=Outlook(sessions, strategy=db.get_setting("outlook_strategy")))
    _wire(services)
    return services


def _wire(s: watcher.Services) -> None:
    hooks, router, db = s.extra["hooks"], s.extra["router"], s.db
    sweeps = {"brightspace": watcher.sweep_brightspace, "teams": watcher.sweep_teams}
    s.extra["sweeps"] = sweeps

    async def status() -> str:
        lines = ["JARVIS status"]
        for row in db.conn.execute("select * from source_health order by source"):
            last = fmt_dt(datetime.fromisoformat(row["last_ok"]), s.settings.tz) if row["last_ok"] else "never"
            lines.append(f"• {row['source']}: last ok {last}, failures in a row {row['consecutive_failures']}")
        for site in ("brightspace", "teams", "outlook"):
            if db.get_setting(f"auth_prompted:{site}") == "1":
                lines.append(f"• {site}: needs login (/login {site})")
        pending = db.conn.execute("select count(*) from pending_actions where status='pending'").fetchone()[0]
        lines.append(f"• pending approvals: {pending}")
        lines.append(f"• paused: {'yes' if db.get_setting('paused') == '1' else 'no'}; "
                     f"quiet: {db.get_setting('quiet') or 'off'}; muted: {', '.join(db.get_json_setting('muted', [])) or 'none'}")
        return "\n".join(lines)

    async def sweep() -> str:
        total = 0
        for fn in sweeps.values():
            total += len(await fn(s) or [])
        return f"Sweep done: {total} change(s)."

    async def login(site: str) -> str:
        async def run():
            try:
                await s.sessions.login(site)
                db.set_setting(f"auth_prompted:{site}", "0")
                await s.notifier.send(f"Logged in to {watcher.SOURCE_NAMES.get(site, site)}.")
            except AuthRequired as e:
                await s.notifier.send(f"Login failed: {e}", [[("Try again", f"login:retry:{site}")]])
            except Exception as e:  # noqa: BLE001
                log.exception("login crashed")
                await s.notifier.send(f"Login error: {type(e).__name__}: {e}"[:500])

        asyncio.get_running_loop().create_task(run())
        return f"Starting {watcher.SOURCE_NAMES.get(site, site)} login… watch for the Authenticator number."

    hooks.update(status=status, sweep=sweep, login=login)
    router.register("login:retry", login)

    async def mute(course: str) -> str:
        return await s.extra["app"].bot_data["commands"].run("mute", [course])

    router.register("mute", mute)

    ap = Approvals(s)
    s.extra.update(approvals=ap, propose=ap.propose, action_status=ap.status)
    router.register("act:approve", ap.on_approve)
    router.register("act:cancel", ap.on_cancel)
    router.register("act:preview", ap.on_preview)
    hooks["pending"] = ap.pending_text

    async def deadlines(days: int = 14) -> str:
        return await deadline_jobs.deadlines_text(s, days)

    async def scan() -> str:
        async def run():
            try:
                await s.notifier.send(await deadline_jobs.scan_command(s))
            except AuthRequired:
                await s.notifier.send("POLITEMall login needed before scanning.",
                                      [[("Log in now", "login:retry:brightspace")]])
            except Exception as e:  # noqa: BLE001
                log.exception("scan failed")
                await s.notifier.send(f"Scan failed: {type(e).__name__}: {e}"[:500])

        asyncio.get_running_loop().create_task(run())
        return "Reading all your current course material… this can take a few minutes for the first scan."

    chat = ChatAgent(s)
    s.extra["chat"] = chat

    async def new_conversation() -> str:
        return chat.reset()

    hooks.update(deadlines=deadlines, scan=scan, ask=chat.handle, new=new_conversation)

    doit = DoIt(s)
    s.extra["doit"] = doit
    router.register("doit", doit.on_doit)
    router.register("dl", doit.on_dl)


async def amain() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    s = build_services()
    app = s.extra["app"]
    sched = AsyncIOScheduler(timezone="Asia/Singapore")
    scheduler.register(sched, s)
    starters = s.extra.setdefault("starters", [])
    from ops import api

    starters.append(api.start)

    async def first_scan(svc) -> None:
        from core.library import Library

        if Library(svc.db).count() == 0:
            await svc.extra["hooks"]["scan"]()

    starters.append(first_scan)

    async def telegram_listener(svc) -> None:
        # On a server the listener is its own systemd unit (TELETHON_EXTERNAL=1); otherwise it lives inside Ops.
        if os.environ.get("TELETHON_EXTERNAL") == "1":
            return
        from ops import telethon_main

        svc.extra["telegram_task"] = asyncio.create_task(telethon_main.listen(svc.settings, svc.db))

    starters.append(telegram_listener)
    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True,
                                        allowed_updates=["message", "callback_query", "my_chat_member"])
        sched.start()
        scheduler.register_boot(sched, s)
        for start in starters:
            await start(s)
        log.info("ops running")
        try:
            await asyncio.Event().wait()
        finally:
            task = s.extra.get("telegram_task")
            if task:
                task.cancel()
            sched.shutdown(wait=False)
            await app.updater.stop()
            await app.stop()
            await s.sessions.close()


if __name__ == "__main__":
    asyncio.run(amain())
