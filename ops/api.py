"""Local HTTP API on a Unix socket, used by school_mcp (which runs as the Claude user).

Read endpoints + propose_action + send_file only. There is deliberately NO approve or
execute route: approval happens only through the Ops bot buttons.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import timedelta
from pathlib import Path

from aiohttp import web

from core import safefs
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
        name, data = await self.s.bs.fetch_file(c.ou, int(body["topic_id"]))
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
        days = int(req.query.get("days", "7"))
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
        events = await self.s.bs.calendar_range_days(int(req.query.get("days", "7")))
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
        if not self.s.teams:
            raise web.HTTPServiceUnavailable(text="Teams not configured")
        msgs = await self.s.teams.read(req.query["id"], int(req.query.get("limit", "30")))
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

        msgs = tg_group.search(self.s.db, req.query.get("q", ""), int(req.query.get("limit", "20")),
                               self._group_filter(req))
        return web.json_response([self._gm(m) for m in msgs])

    async def get_group_recent(self, req):
        from core import tg_group

        since = self.s.clock() - timedelta(hours=int(req.query.get("hours", "24")))
        msgs = tg_group.messages_since(self.s.db, self._group_filter(req), since)
        return web.json_response([self._gm(m) for m in msgs])

    def _gm(self, m) -> dict:
        from core import groups

        return {**_gm(m, self.s.settings.tz), "group": groups.label_for(self.s.settings, self.s.db, m.chat_id)}

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

        since = self.s.clock() - timedelta(hours=int(req.query.get("hours", "48")))
        rows = MailStore(self.s.db).recent(int(req.query.get("limit", "15")), since, req.query.get("unread") == "1",
                                           req.query.get("category"))
        return web.json_response([self._mail_json(m) for m in rows])

    async def get_mail_search(self, req):
        from core.mail import MailStore

        rows = MailStore(self.s.db).search(req.query.get("q", ""), int(req.query.get("limit", "10")))
        return web.json_response([self._mail_json(m) for m in rows])

    async def get_mail(self, req):
        from core.mail import MailStore

        m = MailStore(self.s.db).get(req.query.get("id", ""))
        if m is None:
            raise web.HTTPNotFound(text=json.dumps({"error": "no such message"}), content_type="application/json")
        return web.json_response(self._mail_json(m, body=True))

    async def get_deadlines(self, req):
        from core.deadlines import DeadlineStore, local_date

        tz = self.s.settings.tz
        course = (await self.course(req.query["course"])).code if req.query.get("course") else None
        out = []
        for d in DeadlineStore(self.s.db).upcoming(self.s.clock(), int(req.query.get("days", "60")), tz):
            if course and d.course_code != course:
                continue
            if d.all_day:
                when = local_date(d, tz).strftime("%a %d %b")
            else:
                when = fmt_dt(parse_iso(d.end_utc or d.start_utc), tz)
            out.append({"key": d.key, "title": U(d.title), "course": d.course_code, "kind": d.kind,
                        "when_sgt": when, "all_day": d.all_day, "confidence": d.confidence, "source": d.source,
                        "quote": U(d.quote) if d.quote else None, "link": d.source_ref or None})
        return web.json_response(out)

    async def get_material_search(self, req):
        from core.library import Library

        course = (await self.course(req.query["course"])).code if req.query.get("course") else None
        hits = Library(self.s.db).search(req.query.get("q", ""), course, int(req.query.get("limit", "10")))
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
        return web.json_response(await handler(int(req.match_info["id"])))

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
    except safefs.UnsafePath as e:
        return web.json_response({"error": "unsafe path", "detail": str(e)}, status=400)
    except AuthRequired as e:
        return web.json_response({"error": "login required", "detail": str(e),
                                  "hint": "The user has been asked to log in via the Ops bot."}, status=401)
    except Exception as e:  # noqa: BLE001
        log.exception("api error")
        return web.json_response({"error": type(e).__name__, "detail": str(e)[:300]}, status=500)


def make_app(services) -> web.Application:
    api = Api(services)
    app = web.Application(middlewares=[_errors])
    r = app.router
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
    r.add_get("/deadlines", api.get_deadlines)
    r.add_get("/material/search", api.get_material_search)
    r.add_get("/material/get", api.get_material)
    r.add_get("/material/list", api.get_material_list)
    r.add_post("/actions/propose", api.post_propose)
    r.add_get("/actions/{id:\\d+}", api.get_action)
    r.add_post("/send_file", api.post_send_file)
    return app


async def start(services) -> web.AppRunner:
    sock = services.settings.ops_socket
    sock.parent.mkdir(parents=True, exist_ok=True)
    if sock.exists():
        sock.unlink()
    runner = web.AppRunner(make_app(services))
    await runner.setup()
    await web.UnixSite(runner, str(sock)).start()
    os.chmod(sock, 0o660)
    services.extra["api_runner"] = runner
    log.info("local API on %s", sock)
    return runner
