"""Local HTTP API used by school_mcp (which runs as the Claude user): a Unix socket on macOS/Linux, or
127.0.0.1 with a random bearer token on Windows (see core/localapi.py).

Read endpoints + propose_action + send_file, plus one local-only write: marking a project-group task done/dropped
(/project/tasks/status), which changes only JARVIS's own list and sends nothing anywhere. There is deliberately NO
approve or execute route: approval happens only through the Ops bot buttons.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
from datetime import timedelta
from pathlib import Path

from aiohttp import web

from core import hostos, localapi, safefs
from core.auth import AuthRequired
from core.models import Course
from ops.format import fmt_dt, parse_iso

log = logging.getLogger(__name__)
COURSE_TTL = timedelta(hours=6)


def U(text) -> dict:
    """Wrap third-party text so Claude treats it as data, not instructions."""
    return {"untrusted": True, "text": "" if text is None else str(text)}


def _norm(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", s.lower())


def find_course(courses: list[Course], q: str) -> list[Course]:
    toks = _norm(q)
    if not toks:
        return []
    exact = [c for c in courses if c.code.lower() == q.strip().lower()]
    if exact:
        return exact
    out = []
    for c in courses:
        words = _norm(c.code + " " + c.name)
        if all(any(w.startswith(t) or t in w for w in words) for t in toks):
            out.append(c)
    return out


def _bad(msg: str, **extra) -> web.HTTPBadRequest:
    return web.HTTPBadRequest(text=json.dumps({"error": msg, **extra}), content_type="application/json")


def _num(raw, name: str, default=None, cast=int, lo=None, hi=None):
    """A number parameter from the query string, a JSON body or the path. Anything that is not a plain finite
    number (or is outside lo..hi) is a 400 with a clear message, never a server error."""
    if raw is None or raw == "":
        if default is None:
            raise _bad(f"{name} is required (a number)")
        return default
    try:
        if isinstance(raw, bool):
            raise ValueError
        v = cast(raw)
    except (TypeError, ValueError, OverflowError):
        raise _bad(f"{name} must be a {'whole number' if cast is int else 'number'}", got=str(raw)[:40])
    if v != v or v in (float("inf"), float("-inf")):
        raise _bad(f"{name} must be a finite number", got=str(raw)[:40])
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise _bad(f"{name} must be between {lo} and {hi}", got=str(raw)[:40])
    return v


def _safe(name: str) -> str:
    return re.sub(r"[^\w.\- ]+", "_", name).strip()[:80] or "item"


class Api:
    def __init__(self, s):
        self.s = s

    # ---------- helpers ----------
    async def courses(self) -> list[Course]:
        cache = self.s.extra.get("courses")
        now = self.s.clock()
        if not cache or now - cache[0] > COURSE_TTL:
            cache = (now, await self.s.bs.courses())
            self.s.extra["courses"] = cache
        return cache[1]

    async def course(self, q: str) -> Course:
        matches = find_course(await self.courses(), q or "")
        if len(matches) != 1:
            raise web.HTTPBadRequest(text=json.dumps({
                "error": "course not found" if not matches else "ambiguous course",
                "candidates": [m.code for m in matches]}), content_type="application/json")
        return matches[0]

    def _items(self, kind: str, course: str | None = None, since: str | None = None):
        sql, args = "select * from items where kind=?", [kind]
        if course:
            sql += " and course_code=?"
            args.append(course)
        if since:
            sql += " and first_seen>=?"
            args.append(since)
        return self.s.db.conn.execute(sql + " order by first_seen desc limit 100", args).fetchall()

    def _due_row(self, r, row_due) -> dict:
        p = json.loads(r["payload_json"])
        due = parse_iso(row_due)
        return {"course": r["course_code"], "title": U(r["title"]), "id": r["stable_id"],
                "ou": p.get("ou"), "folder_id": p.get("folder_id"),
                "due_utc": due.isoformat() if due else None,
                "due_sgt": fmt_dt(due, self.s.settings.tz) if due else None}

    # ---------- routes ----------
    async def get_courses(self, req):
        return web.json_response([c.__dict__ for c in await self.courses()])

    async def get_find(self, req):
        return web.json_response([c.__dict__ for c in find_course(await self.courses(), req.query.get("q", ""))])

    async def get_content(self, req):
        c = await self.course(req.query.get("course", ""))
        items = await self.s.bs.content_items(c.ou, c.code)
        return web.json_response([{"topic_id": i.payload["topic_id"], "title": U(i.title),
                                   "module": U(i.payload.get("module")), "type": i.payload.get("type"),
                                   "last_modified": i.payload.get("last_modified")} for i in items])

    async def post_download(self, req):
        body = await req.json()
        c = await self.course(body.get("course", ""))
        name, data = await self.s.bs.fetch_file(c.ou, _num(body.get("topic_id"), "topic_id", lo=0, hi=2**53))
        path = safefs.write_file(self.s.settings.workspace_dir,
                                 [_safe(c.code), _safe(str(body.get("folder") or "downloads"))], _safe(name), data)
        return web.json_response({"path": str(path), "bytes": len(data)})

    async def get_assignments(self, req):
        course = None
        if req.query.get("course"):
            course = (await self.course(req.query["course"])).code
        rows = self._items("assignment", course)
        out = []
        for r in rows:
            d = self.s.db.conn.execute("select * from due_dates where item_key=?", (r["stable_id"],)).fetchone()
            row = self._due_row(r, d["due_at"] if d else None)
            row["submitted"] = bool(d["submitted"]) if d else None
            out.append(row)
        return web.json_response(out)

    async def get_due(self, req):
        days = _num(req.query.get("days"), "days", 7, lo=0, hi=3650)
        now = self.s.clock()
        due, nodue = [], []
        for d in self.s.db.conn.execute("select * from due_dates"):
            r = self.s.db.conn.execute("select * from items where stable_id=?", (d["item_key"],)).fetchone()
            if not r:
                continue
            row = self._due_row(r, d["due_at"])
            row["submitted"] = bool(d["submitted"])
            when = parse_iso(d["due_at"])
            if when is None:
                nodue.append(row)
            elif now <= when <= now + timedelta(days=days):
                due.append(row)
        due.sort(key=lambda x: x["due_utc"])
        return web.json_response({"now_sgt": fmt_dt(now, self.s.settings.tz), "due": due, "no_due_date": nodue})

    async def get_announcements(self, req):
        course = (await self.course(req.query["course"])).code if req.query.get("course") else None
        rows = self._items("announcement", course, req.query.get("since"))
        return web.json_response([{"course": r["course_code"], "title": U(r["title"]),
                                   "body": U(json.loads(r["payload_json"]).get("body_text")),
                                   "first_seen": r["first_seen"]} for r in rows])

    async def get_grades(self, req):
        course = (await self.course(req.query["course"])).code if req.query.get("course") else None
        out = []
        for r in self._items("grade", course):
            p = json.loads(r["payload_json"])
            out.append({"course": r["course_code"], "title": U(r["title"]), "value": p.get("value"),
                        "out_of": p.get("out_of"), "displayed": p.get("displayed"),
                        "feedback": U(p.get("feedback_text"))})
        return web.json_response(out)

    async def get_calendar(self, req):
        events = await self.s.bs.calendar_range_days(_num(req.query.get("days"), "days", 7, lo=0, hi=3650))
        tz = self.s.settings.tz
        return web.json_response([{
            "title": U(e.get("Title")), "start_sgt": fmt_dt(parse_iso(e["StartDateTime"]), tz) if e.get("StartDateTime") else None,
            "end_sgt": fmt_dt(parse_iso(e["EndDateTime"]), tz) if e.get("EndDateTime") else None,
            "location": U(e.get("Location")), "course": e.get("OrgUnitCode")} for e in events])

    async def get_teams_recent(self, req):
        if not self.s.teams:
            raise web.HTTPServiceUnavailable(text="Teams not configured")
        since = parse_iso(req.query.get("since")) or self.s.clock() - timedelta(days=2)
        items = await self.s.teams.recent(since)
        return web.json_response([{"id": i.stable_id, "kind": i.kind, "chat": U(i.payload.get("chat_name")),
                                   "sender": U(i.payload.get("sender")), "text": U(i.payload.get("body_text")),
                                   "conv_id": i.payload.get("conv_id"), "ts": i.payload.get("ts")} for i in items])

    async def get_teams_read(self, req):
        limit = _num(req.query.get("limit"), "limit", 30, lo=1, hi=500)
        if not req.query.get("id"):
            raise _bad("id is required")
        if not self.s.teams:
            raise web.HTTPServiceUnavailable(text="Teams not configured")
        msgs = await self.s.teams.read(req.query["id"], limit)
        return web.json_response([{"sender": U(m.get("sender")), "text": U(m.get("text")), "ts": m.get("ts")}
                                  for m in msgs])

    def _group_filter(self, req):
        """?group=project|class|<label>|<id> -> (chat_id or None). Unknown group names are a clear 400."""
        from core import groups

        ref = req.query.get("group")
        if not ref:
            return None
        g = groups.find(self.s.settings, self.s.db, ref)
        if g is None:
            known = [x["label"] for x in groups.configured(self.s.settings, self.s.db)]
            raise web.HTTPBadRequest(text=json.dumps({"error": "unknown group", "groups": known}),
                                     content_type="application/json")
        return g["id"]

    async def get_group_chats(self, req):
        from core import groups, tg_group

        stats = tg_group.stats(self.s.db)
        return web.json_response([{**g, "messages": stats.get(g["id"], {}).get("count", 0),
                                   "last_message": stats.get(g["id"], {}).get("last")}
                                  for g in groups.configured(self.s.settings, self.s.db)])

    async def get_group_search(self, req):
        from core import tg_group

        msgs = tg_group.search(self.s.db, req.query.get("q", ""), _num(req.query.get("limit"), "limit", 20, lo=1, hi=500),
                               self._group_filter(req))
        return web.json_response([self._gm(m) for m in msgs])

    async def get_group_recent(self, req):
        from core import tg_group

        since = self.s.clock() - timedelta(hours=_num(req.query.get("hours"), "hours", 24, lo=0, hi=24 * 365))
        msgs = tg_group.messages_since(self.s.db, self._group_filter(req), since)
        return web.json_response([self._gm(m) for m in msgs])

    def _gm(self, m) -> dict:
        from core import groups

        return {**_gm(m, self.s.settings.tz), "group": groups.label_for(self.s.settings, self.s.db, m.chat_id)}

    # ---------- project group tasks (text comes from the group chat, so it is third-party content) ----------
    @staticmethod
    def _task_out(t: dict) -> dict:
        return {**t, "task": U(t["task"]), "owner": U(t["owner"]) if t["owner"] else None,
                "source_quote": U(t["source_quote"]) if t["source_quote"] else None}

    async def get_project_tasks(self, req):
        from ops import project_tasks

        ref = req.query.get("group") or None
        known = project_tasks.pt.project_groups(self.s.settings, self.s.db)
        if ref and not any(project_tasks.group_matches(g, ref) for g in known):
            raise web.HTTPBadRequest(text=json.dumps({"error": "unknown project group", "groups": [g["label"] for g in known]}),
                                     content_type="application/json")
        rows = project_tasks.listing(self.s.settings, self.s.db, self.s.clock(), ref, req.query.get("mine") == "1")
        return web.json_response([self._task_out(t) for t in rows])

    async def post_project_task_status(self, req):
        from core import project_tasks as pt

        try:
            body = await req.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text=json.dumps({"error": "a JSON object is required"}), content_type="application/json")
        try:
            task_id, status = int(body.get("id")), str(body.get("status"))
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text=json.dumps({"error": "id and status are required"}), content_type="application/json")
        task = pt.TaskStore(self.s.db).get(task_id)
        project_ids = {g["id"] for g in pt.project_groups(self.s.settings, self.s.db)}
        if status not in pt.STATUSES or task is None or task.chat_id not in project_ids:
            raise web.HTTPBadRequest(text=json.dumps({"error": "unknown task or status", "statuses": list(pt.STATUSES)}),
                                     content_type="application/json")
        now = self.s.clock()
        pt.TaskStore(self.s.db).set_status(task_id, status, now)
        pt.sync_deadlines(self.s.settings, self.s.db, now)  # same calendar refresh as the panel
        return web.json_response({"id": task_id, "status": status})

    # ---------- school email (read-only; text is third-party content) ----------
    def _mail_json(self, m: dict, body: bool = False) -> dict:
        tz = self.s.settings.tz
        out = {"id": m["id"], "received_sgt": fmt_dt(parse_iso(m["received"]), tz), "from": U(m["sender_name"]),
               "address": m["sender_addr"], "subject": U(m["subject"]), "preview": U(m["preview"]),
               "unread": not m["is_read"], "attachments": m["has_attachments"], "category": m["category"]}
        if body:
            out["body"] = U(m.get("body") or m["preview"])
        return out

    async def get_mail_status(self, req):
        from core.mail import MailStore

        status = self.s.db.get_json_setting("outlook_status", {}) or {}
        return web.json_response({**MailStore(self.s.db).stats(), "state": status.get("state", "not_set_up"),
                                  "strategy": status.get("strategy")})

    async def get_mail_recent(self, req):
        from core.mail import MailStore

        since = self.s.clock() - timedelta(hours=_num(req.query.get("hours"), "hours", 48, lo=0, hi=24 * 365))
        rows = MailStore(self.s.db).recent(_num(req.query.get("limit"), "limit", 15, lo=1, hi=500), since, req.query.get("unread") == "1",
                                           req.query.get("category"))
        return web.json_response([self._mail_json(m) for m in rows])

    async def get_mail_search(self, req):
        from core.mail import MailStore

        rows = MailStore(self.s.db).search(req.query.get("q", ""), _num(req.query.get("limit"), "limit", 10, lo=1, hi=500))
        return web.json_response([self._mail_json(m) for m in rows])

    async def get_mail(self, req):
        from core.mail import MailStore

        m = MailStore(self.s.db).get(req.query.get("id", ""))
        if m is None:
            raise web.HTTPNotFound(text=json.dumps({"error": "no such message"}), content_type="application/json")
        return web.json_response(self._mail_json(m, body=True))

    async def get_deadlines(self, req):
        from core.deadlines import DeadlineStore, display_moment, local_date

        tz = self.s.settings.tz
        course = (await self.course(req.query["course"])).code if req.query.get("course") else None
        out = []
        for d in DeadlineStore(self.s.db).upcoming(self.s.clock(), _num(req.query.get("days"), "days", 60, lo=0, hi=3650), tz):
            if course and d.course_code != course:
                continue
            if d.all_day:
                when = local_date(d, tz).strftime("%a %d %b")
            else:
                when = fmt_dt(parse_iso(display_moment(d)), tz)
            out.append({"key": d.key, "title": U(d.title), "course": d.course_code, "kind": d.kind,
                        "when_sgt": when, "all_day": d.all_day, "confidence": d.confidence, "source": d.source,
                        "quote": U(d.quote) if d.quote else None, "link": d.source_ref or None})
        return web.json_response(out)

    async def get_material_search(self, req):
        from core.library import Library

        course = (await self.course(req.query["course"])).code if req.query.get("course") else None
        hits = Library(self.s.db).search(req.query.get("q", ""), course, _num(req.query.get("limit"), "limit", 10, lo=1, hi=500))
        return web.json_response([{**{k: h[k] for k in ("key", "course_code", "kind", "module")},
                                   "title": U(h["title"]), "snippet": U(h["snippet"])} for h in hits])

    async def get_material(self, req):
        from core.library import Library

        m = Library(self.s.db).get(req.query.get("key", ""))
        if not m:
            raise web.HTTPNotFound(text="no such material")
        return web.json_response({"key": m["key"], "course_code": m["course_code"], "kind": m["kind"],
                                  "title": U(m["title"]), "module": U(m["module"]),
                                  "text": U(m["text"][:20000]), "truncated": len(m["text"]) > 20000})

    async def get_material_list(self, req):
        from core.library import Library

        course = (await self.course(req.query["course"])).code if req.query.get("course") else None
        rows = Library(self.s.db).list(req.query.get("kind"), course)
        return web.json_response([{"key": r["key"], "course_code": r["course_code"], "kind": r["kind"],
                                   "title": U(r["title"]), "module": U(r["module"])} for r in rows])

    async def post_propose(self, req):
        handler = self.s.extra.get("propose")
        if not handler:
            raise web.HTTPNotImplemented(text="actions not available")
        body = await req.json()
        return web.json_response(await handler(body.get("kind"), body.get("params") or {}, body.get("summary") or ""))

    async def get_action(self, req):
        handler = self.s.extra.get("action_status")
        if not handler:
            raise web.HTTPNotImplemented(text="actions not available")
        return web.json_response(await handler(_num(req.match_info.get("id"), "id", lo=0, hi=2**53)))

    async def post_send_file(self, req):
        body = await req.json()
        path = str(body.get("path", ""))
        data = safefs.read_file(self.s.settings.workspace_dir, path)
        name = Path(path).name
        if await self.s.notifier.send_bytes(name, data, body.get("caption", "")):
            return web.json_response({"sent": name})
        return web.json_response({"sent": None, "note": "file too large for Telegram (50 MB); user was told it is "
                                  "in the workspace"})


def _gm(m, tz) -> dict:
    return {"chat_id": m.chat_id, "msg_id": m.msg_id, "sender": U(m.sender), "text": U(m.text),
            "ts_sgt": fmt_dt(m.ts, tz)}


@web.middleware
async def _errors(request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except json.JSONDecodeError:
        return web.json_response({"error": "the request body must be valid JSON"}, status=400)
    except safefs.UnsafePath as e:
        return web.json_response({"error": "unsafe path", "detail": str(e)}, status=400)
    except AuthRequired as e:
        return web.json_response({"error": "login required", "detail": str(e),
                                  "hint": "The user has been asked to log in via the Ops bot."}, status=401)
    except Exception as e:  # noqa: BLE001
        log.exception("api error")
        return web.json_response({"error": type(e).__name__, "detail": str(e)[:300]}, status=500)


def _bearer(token: str):
    """Reject any request that does not carry the per-start token (tcp transport only)."""
    want = f"Bearer {token}".encode()

    @web.middleware
    async def check(request, handler):
        if not hmac.compare_digest(request.headers.get("Authorization", "").encode(), want):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    return check


async def _health(_req):
    return web.json_response({"ok": True})


def make_app(services, token: str | None = None) -> web.Application:
    api = Api(services)
    app = web.Application(middlewares=[_bearer(token), _errors] if token else [_errors])
    r = app.router
    r.add_get("/health", _health)
    r.add_get("/courses", api.get_courses)
    r.add_get("/courses/find", api.get_find)
    r.add_get("/content", api.get_content)
    r.add_post("/download", api.post_download)
    r.add_get("/assignments", api.get_assignments)
    r.add_get("/due", api.get_due)
    r.add_get("/announcements", api.get_announcements)
    r.add_get("/grades", api.get_grades)
    r.add_get("/calendar", api.get_calendar)
    r.add_get("/teams/recent", api.get_teams_recent)
    r.add_get("/teams/read", api.get_teams_read)
    r.add_get("/mail/status", api.get_mail_status)
    r.add_get("/mail/recent", api.get_mail_recent)
    r.add_get("/mail/search", api.get_mail_search)
    r.add_get("/mail/get", api.get_mail)
    r.add_get("/group/chats", api.get_group_chats)
    r.add_get("/group/search", api.get_group_search)
    r.add_get("/group/recent", api.get_group_recent)
    r.add_get("/project/tasks", api.get_project_tasks)
    r.add_post("/project/tasks/status", api.post_project_task_status)
    r.add_get("/deadlines", api.get_deadlines)
    r.add_get("/material/search", api.get_material_search)
    r.add_get("/material/get", api.get_material)
    r.add_get("/material/list", api.get_material_list)
    r.add_post("/actions/propose", api.post_propose)
    r.add_get("/actions/{id:\\d+}", api.get_action)
    r.add_post("/send_file", api.post_send_file)
    return app


async def start(services, transport: str | None = None) -> web.AppRunner:
    """Serve the API and write the endpoint file that tells clients how to reach it."""
    cfg = services.settings
    transport = transport or hostos.api_transport(cfg.ops_api_transport)
    ep_path = localapi.endpoint_path(cfg)
    if transport == "tcp":
        token = localapi.new_token()
        runner = web.AppRunner(make_app(services, token=token))
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", cfg.ops_port).start()
        port = runner.addresses[0][1]
        localapi.write_endpoint(ep_path, {"transport": "tcp", "host": "127.0.0.1", "port": port, "token": token})
        log.info("local API on 127.0.0.1:%s (token in %s)", port, ep_path)
    else:
        sock = cfg.ops_socket
        sock.parent.mkdir(parents=True, exist_ok=True)
        if sock.exists():
            sock.unlink()
        runner = web.AppRunner(make_app(services))
        await runner.setup()
        await web.UnixSite(runner, str(sock)).start()
        os.chmod(sock, 0o660)
        try:
            localapi.write_endpoint(ep_path, {"transport": "unix", "path": str(sock)})
        except OSError:
            log.warning("could not write %s; clients will use the default socket path", ep_path)
        log.info("local API on %s", sock)
    services.extra["api_runner"] = runner
    services.extra["api_endpoint"] = ep_path
    return runner


def stop_cleanup(services) -> None:
    """Remove the endpoint file on a clean shutdown (a stale one is harmless: clients check it is alive)."""
    ep = services.extra.get("api_endpoint")
    if ep:
        localapi.clear_endpoint(ep)  # never raises
