"""Brightspace (POLITEMall) client using the browser session's cookies against Valence routes."""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from core.auth import AuthRequired, is_login_page
from core.models import Assignment, Course, Item
from core.text import html_to_text


class FileTooLarge(Exception):
    pass


MAX_FETCH_BYTES = 25 * 1024 * 1024
SEMESTER_RE = re.compile(r"^(\d{2})S(\d)-")


def latest_semester(codes: list[str]) -> str | None:
    """NP course codes start with a semester tag like '26S2-'. Return the newest tag seen."""
    tags = {(int(m.group(1)), int(m.group(2))) for c in codes if (m := SEMESTER_RE.match(c))}
    if not tags:
        return None
    y, t = max(tags)
    return f"{y:02d}S{t}"


def _iso(value: str | None) -> str | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()


def _dt(value: str | None) -> datetime | None:
    v = _iso(value)
    return datetime.fromisoformat(v) if v else None


class Brightspace:
    def __init__(self, session, base: str, course_prefix: str | None = None):
        """course_prefix: None = latest semester tag (e.g. 26S2), '*' = every active enrolment,
        anything else = codes starting with that prefix."""
        self.session = session
        self.base = base.rstrip("/")
        self.course_prefix = course_prefix
        self._versions: dict[str, str] | None = None

    # ---------- http ----------
    async def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base, cookies=await self.session.cookies("brightspace"),
                                 follow_redirects=False, timeout=30)

    @staticmethod
    def _check(r: httpx.Response) -> None:
        # Only 401 / redirect-to-login mean the session is gone. 403 = a tool is off in that course.
        if r.status_code == 401:
            raise AuthRequired("brightspace: HTTP 401 (the server no longer accepts the saved session)")
        if r.is_redirect and is_login_page(r.headers.get("location", "")):
            raise AuthRequired("brightspace: redirected to the sign-in page")
        r.raise_for_status()

    async def _session_alive(self) -> bool:
        async with await self._client() as c:
            r = await c.get(f"/d2l/api/lp/{(self._versions or {}).get('lp', '1.0')}/users/whoami")
            return r.status_code == 200

    async def _check_async(self, r: httpx.Response) -> None:
        """Like _check, but a 403 is only 'logged out' if whoami also fails (Brightspace answers 403, not 401,
        when there is no session; it also answers 403 when a tool is turned off in one course)."""
        if r.status_code == 403 and not await self._session_alive():
            raise AuthRequired("brightspace")
        self._check(r)

    async def _get(self, path: str, **params) -> Any:
        async with await self._client() as c:
            r = await c.get(path, params=params or None)
        absorb = getattr(self.session, "absorb", None)
        if absorb is not None:
            await absorb("brightspace", c.cookies)
        await self._check_async(r)
        return r.json()

    async def versions(self) -> dict[str, str]:
        if self._versions is None:
            data = await self._get("/d2l/api/versions/")
            self._versions = {v["ProductCode"]: v["LatestVersion"] for v in data}
        return self._versions

    async def _lp(self) -> str:
        return (await self.versions()).get("lp", "1.0")

    async def _le(self) -> str:
        return (await self.versions()).get("le", "1.0")

    # ---------- read ----------
    async def whoami(self) -> dict:
        return await self._get(f"/d2l/api/lp/{await self._lp()}/users/whoami")

    async def courses(self) -> list[Course]:
        lp = await self._lp()
        out: dict[int, Course] = {}
        bookmark = None
        while True:
            params = {"orgUnitTypeId": 3}
            if bookmark:
                params["bookmark"] = bookmark
            data = await self._get(f"/d2l/api/lp/{lp}/enrollments/myenrollments/", **params)
            for it in data.get("Items", []):
                acc = it.get("Access", {})
                if not acc.get("IsActive", True) or not acc.get("CanAccess", True):
                    continue
                ou = it["OrgUnit"]
                out.setdefault(ou["Id"], Course(ou["Id"], ou.get("Code") or str(ou["Id"]), ou.get("Name", "")))
            paging = data.get("PagingInfo") or {}
            if not paging.get("HasMoreItems"):
                break
            bookmark = paging.get("Bookmark")
        courses = list(out.values())
        if self.course_prefix == "*":
            return courses
        prefix = self.course_prefix or latest_semester([c.code for c in courses])
        if not prefix:
            return courses
        return [c for c in courses if c.code.startswith(prefix)]

    async def content_items(self, ou: int, course_code: str) -> list[Item]:
        toc = await self._get(f"/d2l/api/le/{await self._le()}/{ou}/content/toc")
        items: list[Item] = []

        def walk(modules, trail):
            for m in modules:
                path = trail + [m.get("Title", "")]
                for t in m.get("Topics", []):
                    items.append(Item("brightspace", f"bs:content:{t['TopicId']}", course_code, "content",
                                      t.get("Title", ""), {
                                          "ou": ou, "topic_id": t["TopicId"], "url": t.get("Url"),
                                          "type": t.get("TypeIdentifier"), "module": " › ".join(path),
                                          "last_modified": _iso(t.get("LastModifiedDate")),
                                      }))
                walk(m.get("Modules", []), path)

        walk(toc.get("Modules", []), [])
        return items

    async def fetch_file(self, ou: int, topic_id: int, max_redirects: int = 5,
                         max_bytes: int = MAX_FETCH_BYTES) -> tuple[str, bytes]:
        """Download a content topic's file, streaming, aborting past max_bytes. Returns (filename, bytes)."""
        url = f"/d2l/api/le/{await self._le()}/{ou}/content/topics/{topic_id}/file"
        async with await self._client() as c:
            for _ in range(max_redirects + 1):
                async with c.stream("GET", url) as r:
                    if r.is_redirect and not is_login_page(r.headers.get("location", "")):
                        url = r.headers["location"]
                        continue
                    if r.status_code != 200:
                        await r.aread()
                        await self._check_async(r)
                    if int(r.headers.get("content-length") or 0) > max_bytes:
                        raise FileTooLarge(f"{int(r.headers['content-length']) // 2**20} MB")
                    buf = bytearray()
                    async for part in r.aiter_bytes():
                        buf += part
                        if len(buf) > max_bytes:
                            raise FileTooLarge(f"over {max_bytes // 2**20} MB")
                    name = _filename(r.headers.get("content-disposition", "")) or f"topic-{topic_id}"
                    return name, bytes(buf)
        raise httpx.TooManyRedirects("too many redirects downloading topic", request=None)

    async def assignments(self, ou: int, course_code: str) -> list[Assignment]:
        folders = await self._get(f"/d2l/api/le/{await self._le()}/{ou}/dropbox/folders/")
        out = []
        for f in folders:
            avail = f.get("Availability") or {}
            out.append(Assignment(ou, f["Id"], course_code, f.get("Name", ""), _dt(f.get("DueDate")),
                                  _dt(avail.get("EndDate"))))
        return out

    async def folder_instructions(self, ou: int) -> dict[int, tuple[str, str]]:
        """{folder_id: (name, instructions as plain text)} — for the semester library."""
        folders = await self._get(f"/d2l/api/le/{await self._le()}/{ou}/dropbox/folders/")
        out = {}
        for f in folders:
            ins = f.get("CustomInstructions") or {}
            out[f["Id"]] = (f.get("Name", ""), html_to_text(ins.get("Html") or ins.get("Text") or "", limit=20000))
        return out

    async def assignment_items(self, ou: int, course_code: str) -> list[Item]:
        return [
            Item("brightspace", f"bs:assignment:{ou}:{a.folder_id}", course_code, "assignment", a.title, {
                "ou": ou, "folder_id": a.folder_id,
                "due_at": a.due_at.isoformat() if a.due_at else None,
                "end_at": a.end_at.isoformat() if a.end_at else None,
            })
            for a in await self.assignments(ou, course_code)
        ]

    async def submitted(self, ou: int, folder_id: int) -> bool:
        data = await self._get(
            f"/d2l/api/le/{await self._le()}/{ou}/dropbox/folders/{folder_id}/submissions/mysubmissions/")
        return any(entry.get("Submissions") for entry in data or [])

    async def announcements(self, ou: int, course_code: str) -> list[Item]:
        news = await self._get(f"/d2l/api/le/{await self._le()}/{ou}/news/")
        out = []
        for n in news:
            if n.get("IsPublished") is False:
                continue
            body = n.get("Body") or {}
            out.append(Item("brightspace", f"bs:announcement:{ou}:{n['Id']}", course_code, "announcement",
                            n.get("Title", ""), {
                                "ou": ou, "posted_at": _iso(n.get("StartDate")),
                                "body_text": html_to_text(body.get("Html") or body.get("Text") or "", limit=2000),
                            }))
        return out

    async def grades(self, ou: int, course_code: str) -> list[Item]:
        data = await self._get(f"/d2l/api/le/{await self._le()}/{ou}/grades/values/myGradeValues/")
        out = []
        for g in data or []:
            comments = g.get("Comments") or {}
            out.append(Item("brightspace", f"bs:grade:{ou}:{g['GradeObjectIdentifier']}", course_code, "grade",
                            g.get("GradeObjectName", ""), {
                                "value": g.get("PointsNumerator"), "out_of": g.get("PointsDenominator"),
                                "displayed": g.get("DisplayedGrade"),
                                "feedback_text": html_to_text(comments.get("Html") or comments.get("Text") or "",
                                                              limit=1000),
                            }))
        return out

    async def calendar(self, start: datetime, end: datetime, ou_ids: list[int] | None = None) -> list[dict]:
        """Events across the given courses (default: current courses). NP requires orgUnitIdsCSV."""
        fmt = "%Y-%m-%dT%H:%M:%S.000Z"
        ids = ou_ids if ou_ids is not None else [c.ou for c in await self.courses()]
        if not ids:
            return []
        events: list[dict] = []
        bookmark = None
        while True:
            params = {"startDateTime": start.astimezone(timezone.utc).strftime(fmt),
                      "endDateTime": end.astimezone(timezone.utc).strftime(fmt),
                      "orgUnitIdsCSV": ",".join(str(i) for i in ids)}
            if bookmark:
                params["bookmark"] = bookmark
            data = await self._get(f"/d2l/api/le/{await self._le()}/calendar/events/myEvents/", **params)
            if not isinstance(data, dict):
                return events + list(data)
            events += data.get("Objects", [])
            paging = data.get("PagingInfo") or {}
            if not paging.get("HasMoreItems"):
                return events
            bookmark = paging.get("Bookmark")

    async def calendar_range_days(self, days: int, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(timezone.utc)
        return await self.calendar(now, now + timedelta(days=days))

    async def discussion_names(self, ou: int, forum_id: int, topic_id: int) -> tuple[str, str]:
        le = await self._le()
        forum = await self._get(f"/d2l/api/le/{le}/{ou}/discussions/forums/{forum_id}")
        topic = await self._get(f"/d2l/api/le/{le}/{ou}/discussions/forums/{forum_id}/topics/{topic_id}")
        return forum.get("Name", str(forum_id)), topic.get("Name", str(topic_id))

    # ---------- write (only via core.actions.execute) ----------
    async def _xsrf(self, c: httpx.AsyncClient) -> str:
        r = await c.get("/d2l/lp/auth/xsrf-tokens")
        self._check(r)
        return r.json()["referrerToken"]

    async def submit(self, ou: int, folder_id: int, file: Path, comment: str = "") -> dict:
        boundary = uuid.uuid4().hex
        meta = json.dumps({"Text": comment, "Html": None}).encode()
        body = b"".join([
            f"--{boundary}\r\nContent-Type: application/json\r\n\r\n".encode(), meta, b"\r\n",
            f'--{boundary}\r\nContent-Disposition: form-data; name=""; filename="{file.name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n".encode(), file.read_bytes(), b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ])
        async with await self._client() as c:
            token = await self._xsrf(c)
            r = await c.post(
                f"/d2l/api/le/{await self._le()}/{ou}/dropbox/folders/{folder_id}/submissions/mysubmissions/",
                content=body,
                headers={"Content-Type": f"multipart/mixed; boundary={boundary}", "X-Csrf-Token": token},
            )
            self._check(r)
        return {"status": r.status_code, "file": file.name, "submitted_at": datetime.now(timezone.utc).isoformat()}

    async def post_discussion(self, ou: int, forum_id: int, topic_id: int, subject: str, text: str) -> dict:
        async with await self._client() as c:
            token = await self._xsrf(c)
            r = await c.post(
                f"/d2l/api/le/{await self._le()}/{ou}/discussions/forums/{forum_id}/topics/{topic_id}/posts/",
                json={"ParentPostId": None, "Subject": subject, "Message": {"Content": text, "Type": "Text"},
                      "IsAnonymous": False},
                headers={"X-Csrf-Token": token},
            )
            self._check(r)
            return r.json()


def _filename(disposition: str) -> str | None:
    m = re.search(r"filename\*=UTF-8''([^;]+)", disposition) or re.search(r'filename="?([^";]+)"?', disposition)
    if not m:
        return None
    from urllib.parse import unquote

    return Path(unquote(m.group(1))).name
