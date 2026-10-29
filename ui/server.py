"""The panel's HTTP server. Binds to 127.0.0.1 only; every request is checked against DNS-rebinding and CSRF."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import web

from ops import persona
from ui import data
from ui import setup as setup_flow
from core.config import env_path
from core import groups as core_groups
from core.db import DB
from ui import chats as chats_view
from ui import inbox as inbox_view
from ui.calendar import CalendarView
from ui.chat import WebChat
from ui.jobs import Jobs
from ui.supervisor import OpsSupervisor
from ui.tglogin import TelegramLogin
from ui.updater import Updater

STATIC = Path(__file__).parent / "static"


def _version() -> str:
    """Short git commit of the code this panel is running (shown in the sidebar)."""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=Path(__file__).parent, capture_output=True,
                             text=True, timeout=3)
        return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else "dev"
    except (OSError, subprocess.SubprocessError):
        return "dev"


VERSION = _version()
CSRF_HEADER = "X-JARVIS-UI"


def make_middleware(port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    @web.middleware
    async def guard(request: web.Request, handler):
        if request.headers.get("Host", "") not in allowed_hosts:
            raise web.HTTPForbidden(text="forbidden host")
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("Origin")
            if origin and urlparse(origin).netloc not in allowed_hosts:
                raise web.HTTPForbidden(text="forbidden origin")
            if request.headers.get(CSRF_HEADER) != "1":
                raise web.HTTPForbidden(text="missing header")
        resp = await handler(request)
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    return guard


class Panel:
    def __init__(self, settings, supervisor: OpsSupervisor, chat: WebChat, jobs: Jobs, updater: Updater,
                 on_restart=None, calendar: CalendarView | None = None, db: DB | None = None,
                 tglogin: TelegramLogin | None = None, env_file=None):
        self.settings, self.sup, self.chat, self.jobs = settings, supervisor, chat, jobs
        self.calendar = calendar or CalendarView(settings)
        self.db = db or DB(settings.db_path)
        self.db.init()
        self.tglogin = tglogin or TelegramLogin(settings)
        self.jobs.on_result = self._job_result
        self.updater, self.on_restart = updater, on_restart
        self.env_file = env_file or env_path()

    async def index(self, request):
        return web.FileResponse(STATIC / "index.html")

    async def state(self, request):
        ov = data.overview(self.settings)
        ops = self.sup.info()
        return web.json_response({"ops": ops, "claude": shutil.which("claude") is not None,
                                  "chat_busy": self.chat.busy, "job": self.jobs.status(),
                                  "connections": data.connections(self.settings, ops["mode"]), "version": VERSION,
                                  "identity": {"name": persona.user_name(self.settings), "address": persona.address(self.settings)}, **ov})

    async def setup_status(self, request):
        return web.json_response(setup_flow.status(self.settings))

    async def setup_step(self, request):
        action = request.match_info["action"]
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="bad json")
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="bad json")
        if action == "token":
            return web.json_response(await setup_flow.check_token(str(body.get("token", ""))))
        if action == "owner":
            return web.json_response(await setup_flow.find_owner(str(body.get("token", ""))))
        if action == "save":
            res = setup_flow.save(self.env_file, body)
            if res["ok"] and self.on_restart:
                asyncio.get_running_loop().call_later(0.8, self.on_restart)
            return web.json_response(res)
        raise web.HTTPNotFound()

    async def deadlines(self, request):
        try:
            days = max(1, min(365, int(request.query.get("days", "60"))))
        except ValueError:
            days = 60
        return web.json_response(data.deadlines(self.settings, days))

    async def usage(self, request):
        try:
            days = max(1, min(90, int(request.query.get("days", "7"))))
        except ValueError:
            days = 7
        return web.json_response(data.usage(self.settings, days))

    async def calendar_month(self, request):
        today = datetime.now(self.settings.tz).date()
        try:
            year, month = int(request.query.get("year", today.year)), int(request.query.get("month", today.month))
            if not (1970 <= year <= 2100 and 1 <= month <= 12):
                raise ValueError
        except ValueError:
            raise web.HTTPBadRequest(text=json.dumps({"error": "bad year or month"}), content_type="application/json")
        return web.json_response(await self.calendar.month(year, month, refresh=request.query.get("refresh") == "1"))

    def _job_result(self, key: str, ok: bool) -> None:
        """A login finished successfully: the old "session expired" flag is stale, so clear it now instead of
        waiting up to 15 minutes for JARVIS's next check to notice."""
        if ok:
            self.db.set_setting(key, "0")

    def _ops_running(self) -> bool:
        return self.sup.mode() in ("running", "external")

    async def mail_list(self, request):
        kind = request.query.get("filter", "all")
        try:
            limit = max(1, min(int(request.query.get("limit", "60")), 150))
        except ValueError:
            limit = 60
        return web.json_response({"state": inbox_view.state(self.settings, self.db),
                                  "messages": inbox_view.messages(self.settings, self.db, kind if kind in ("all", "unread", "important") else "all",
                                                                  request.query.get("q", ""), limit)})

    async def mail_item(self, request):
        m = inbox_view.message(self.settings, self.db, request.query.get("id", ""))
        if m is None:
            raise web.HTTPNotFound(text=json.dumps({"error": "no such message"}), content_type="application/json")
        return web.json_response(m)

    async def chats_overview(self, request):
        return web.json_response({**chats_view.overview(self.settings, self.db, self._ops_running()),
                                  "login": self.tglogin.status()})

    async def chats_messages(self, request):
        try:
            chat_id, limit = int(request.query["group"]), int(request.query.get("limit", "150"))
        except (KeyError, ValueError):
            raise web.HTTPBadRequest(text=json.dumps({"error": "group must be a chat id"}), content_type="application/json")
        return web.json_response(chats_view.messages(self.settings, self.db, chat_id, request.query.get("q", ""), limit))

    async def chats_save(self, request):
        body = await request.json()
        listed = body.get("groups")
        if not isinstance(listed, list):
            raise web.HTTPBadRequest(text=json.dumps({"error": "groups must be a list"}), content_type="application/json")
        known = {d["id"] for d in core_groups.dialogs(self.db)}
        picked = [g for g in listed if isinstance(g, dict) and g.get("id") in known]  # only groups from your own Telegram
        return web.json_response({"ok": True, "groups": core_groups.save(self.db, picked)})

    async def tg_login(self, request):
        action = request.match_info["action"]
        body = await request.json() if request.can_read_body else {}
        fn = {"start": lambda: self.tglogin.start(str(body.get("phone", ""))),
              "code": lambda: self.tglogin.code(str(body.get("code", ""))),
              "password": lambda: self.tglogin.password(str(body.get("password", ""))),
              "cancel": self.tglogin.cancel}.get(action)
        if fn is None:
            raise web.HTTPNotFound()
        return web.json_response(await fn())

    async def update_check(self, request):
        try:
            return web.json_response(await self.updater.check())
        except Exception as e:  # noqa: BLE001 - always answer in JSON so the page can show the reason
            return web.json_response({"ok": False, "message": f"Update check failed: {type(e).__name__}: {e}"})

    async def update_apply(self, request):
        if self.chat.busy or self.jobs.running:
            return web.json_response({"ok": False, "message": "Wait for the running chat or task to finish first."},
                                     status=409)
        try:
            res = await self.updater.apply()
        except Exception as e:  # noqa: BLE001
            return web.json_response({"ok": False, "restart": False,
                                      "message": f"Update failed: {type(e).__name__}: {e}"}, status=500)
        if res["ok"] and res["restart"] and self.on_restart:
            self.sup.prefs.set("resume_ops", self.sup.managed_running)
            asyncio.get_running_loop().call_later(0.5, self.on_restart)
        return web.json_response(res, status=200 if res["ok"] else 409)

    async def logs(self, request):
        try:
            n = max(10, min(1000, int(request.query.get("n", "200"))))
        except ValueError:
            n = 200
        return web.json_response({"lines": self.sup.tail(n)})

    async def ops_control(self, request):
        action = request.match_info["action"]
        fn = {"start": self.sup.start, "stop": self.sup.stop, "restart": self.sup.restart}.get(action)
        if fn is None:
            raise web.HTTPNotFound()
        res = await fn()
        return web.json_response(res, status=200 if res["ok"] else 409)

    async def prefs(self, request):
        body = await request.json()
        if "autostart" in body:
            self.sup.prefs.set("autostart", bool(body["autostart"]))
        return web.json_response({"ok": True})

    async def chat_send(self, request):
        body = await request.json()
        text = str(body.get("text", "")).strip()
        if not text:
            raise web.HTTPBadRequest(text="empty message")
        if self.sup.mode() not in ("running", "external"):
            raise web.HTTPConflict(text=json.dumps({"error": "JARVIS is not running. Start it from the Overview tab first."}),
                                   content_type="application/json")
        if self.chat.busy:
            raise web.HTTPConflict(text=json.dumps({"error": "Still working on the previous message."}),
                                   content_type="application/json")
        resp = web.StreamResponse(headers={"Content-Type": "application/x-ndjson", "Cache-Control": "no-store"})
        await resp.prepare(request)
        try:
            async for ev in self.chat.run(text):
                await resp.write((json.dumps(ev) + "\n").encode())
        except (ConnectionResetError, web.HTTPException):
            pass
        return resp

    async def chat_reset(self, request):
        self.chat.reset()
        return web.json_response({"ok": True})

    async def job_catalog(self, request):
        return web.json_response({"jobs": self.jobs.catalog(), "current": self.jobs.status()})

    async def job_start(self, request):
        body = await request.json()
        job_id = str(body.get("id", ""))
        if not self.jobs.exclusive(job_id) or self.jobs.running:
            res = await self.jobs.start(job_id)
            return web.json_response(res, status=200 if res["ok"] else 409)
        # This job needs a browser profile that JARVIS keeps open: pause JARVIS, run the job, bring JARVIS back.
        if self.sup.mode() == "external":
            return web.json_response({"ok": False, "message": "JARVIS was started outside this panel (for example in a "
                                      "terminal), so I cannot pause it. Stop it there first, then try again."}, status=409)
        was_running = self.sup.managed_running
        if was_running:
            await self.sup.stop()

        async def resume():
            if not was_running:
                return ""
            res = await self.sup.start()
            return "JARVIS restarted." if res["ok"] else res["message"]

        note = "Pausing JARVIS while the browser window is open; it restarts by itself afterwards.\n" if was_running else ""
        res = await self.jobs.start(job_id, on_finish=resume if was_running else None, note=note)
        if not res["ok"] and was_running:
            await self.sup.start()
        return web.json_response(res, status=200 if res["ok"] else 409)

    async def job_cancel(self, request):
        await self.jobs.cancel()
        return web.json_response({"ok": True})


def make_app(settings, supervisor: OpsSupervisor, chat: WebChat | None = None, jobs: Jobs | None = None,
             port: int = 8765, updater: Updater | None = None, on_restart=None,
             calendar: CalendarView | None = None, db: DB | None = None,
             tglogin: TelegramLogin | None = None, env_file=None) -> web.Application:
    panel = Panel(settings, supervisor, chat or WebChat(settings), jobs or Jobs(), updater or Updater(), on_restart,
                  calendar, db, tglogin, env_file)
    app = web.Application(middlewares=[make_middleware(port)])
    r = app.router
    r.add_get("/", panel.index)
    r.add_static("/static/", STATIC)
    r.add_get("/api/state", panel.state)
    r.add_get("/api/setup", panel.setup_status)
    r.add_post("/api/setup/{action}", panel.setup_step)
    r.add_get("/api/deadlines", panel.deadlines)
    r.add_get("/api/logs", panel.logs)
    r.add_get("/api/usage", panel.usage)
    r.add_get("/api/calendar", panel.calendar_month)
    r.add_get("/api/mail", panel.mail_list)
    r.add_get("/api/mail/item", panel.mail_item)
    r.add_get("/api/chats", panel.chats_overview)
    r.add_get("/api/chats/messages", panel.chats_messages)
    r.add_post("/api/chats/groups", panel.chats_save)
    r.add_post("/api/telegram/login/{action}", panel.tg_login)
    r.add_post("/api/update/check", panel.update_check)
    r.add_post("/api/update/apply", panel.update_apply)
    r.add_post("/api/ops/{action}", panel.ops_control)
    r.add_post("/api/prefs", panel.prefs)
    r.add_post("/api/chat", panel.chat_send)
    r.add_post("/api/chat/reset", panel.chat_reset)
    r.add_get("/api/jobs", panel.job_catalog)
    r.add_post("/api/jobs/start", panel.job_start)
    r.add_post("/api/jobs/cancel", panel.job_cancel)
    return app
