"""Approval cards and button handlers. The only path from a proposed action to execution."""
from __future__ import annotations

import json
from pathlib import Path

from core import actions
from core.actions import ActionNotPending
from ops.api import find_course
from ops.format import fmt_dt, parse_iso

TITLES = {"brightspace_submit": "Submit", "brightspace_discussion_post": "Post to discussion",
          "teams_send_message": "Send Teams message"}


def card(action, ctx: dict) -> tuple[str, list[list[tuple[str, str]]]]:
    p = action.params
    lines = [f"Approval #{action.id}: {TITLES[action.kind]}"]
    if ctx.get("dev"):
        lines[0] += " (dev: actions disabled)"
    if action.kind == "brightspace_submit":
        lines.append(f"File: {p['file_name']} ({max(1, p['size'] // 1024)} KB, sha256 {p['sha256'][:12]})")
        lines.append(f"To: {ctx.get('course', '')} › {ctx.get('target') or 'folder ' + str(p['folder_id'])}")
        if ctx.get("due"):
            lines.append(f"Due: {ctx['due']}")
        if p.get("comment"):
            lines.append(f"Comment: {p['comment'][:300]}")
    elif action.kind == "brightspace_discussion_post":
        where = ctx.get("target") or f"forum {p['forum_id']} / topic {p['topic_id']}"
        lines.append(f"Where: {ctx.get('course', '')} › {where}")
        lines.append(f"Subject: {p.get('subject', '')}")
        lines.append(f"Text:\n{p['text'][:1500]}")
    else:
        lines.append(f"To: {ctx.get('target') or 'UNKNOWN conversation ' + p['conv_id']}")
        lines.append(f"Text:\n{p['text'][:1500]}")
    if action.summary:
        lines.append(f"Why: {action.summary[:300]}")
    lines.append("Expires in 24h.")
    verb = "Submit" if action.kind == "brightspace_submit" else "Send"
    buttons = [[(verb, f"act:approve:{action.id}"), ("Cancel", f"act:cancel:{action.id}")]]
    if action.kind == "brightspace_submit":
        buttons[0].append(("Preview", f"act:preview:{action.id}"))
    return "\n".join(lines), buttons


class Approvals:
    def __init__(self, services):
        self.s = services

    async def _course(self, q: str):
        courses = self.s.extra.get("courses")
        courses = courses[1] if courses else await self.s.bs.courses()
        m = find_course(courses, q)
        if len(m) != 1:
            raise ValueError(f"course '{q}' {'not found' if not m else 'is ambiguous'}")
        return m[0]

    def _context(self, action, course_code: str | None) -> dict:
        ctx: dict = {"course": course_code or "", "dev": not self.s.settings.actions_enabled}
        if action.kind == "brightspace_submit":
            key = f"bs:assignment:{action.params['ou']}:{action.params['folder_id']}"
            it = self.s.db.get_item("brightspace", key)
            if it:
                ctx["target"] = it["title"]
                ctx["course"] = it["course_code"]
                due = parse_iso(json.loads(it["payload_json"]).get("due_at"))
                if due:
                    ctx["due"] = fmt_dt(due, self.s.settings.tz)
        return ctx

    # ----- API hook (called on behalf of Claude) -----
    async def propose(self, kind: str, raw: dict, summary: str) -> dict:
        """Build params from an explicit allow-list of fields; never pass Claude's dict through."""
        code = None
        ws = self.s.settings.workspace_dir
        outbox = self.s.settings.state_dir / "outbox"
        try:
            if kind == "brightspace_submit":
                c = await self._course(str(raw.get("course", "")))
                code = c.code
                params = {"ou": c.ou, "folder_id": int(raw["folder_id"]), "comment": str(raw.get("comment") or ""),
                          **actions.prepare_submission(ws, outbox, raw["file_path"])}
            elif kind == "brightspace_discussion_post":
                c = await self._course(str(raw.get("course", "")))
                code = c.code
                params = {"ou": c.ou, "forum_id": int(raw["forum_id"]), "topic_id": int(raw["topic_id"]),
                          "subject": str(raw.get("subject") or ""), "text": str(raw.get("text") or "")}
            elif kind == "teams_send_message":
                params = {"conv_id": str(raw.get("conv_id") or ""), "text": str(raw.get("text") or "")}
            else:
                raise ValueError(f"unknown action kind: {kind}")
            a = actions.propose(self.s.db, kind, params, summary, self.s.clock(), ws, outbox=outbox)
        except (ValueError, KeyError, TypeError) as e:
            return {"status": "rejected", "error": str(e)}
        ctx = self._context(a, code)
        ctx["target"] = ctx.get("target") or await self._recipient(a)
        text, buttons = card(a, ctx)
        await self.s.notifier.send(text, buttons)
        return {"status": "awaiting approval", "action_id": a.id,
                "message": f"Awaiting the user's approval #{a.id} in the JARVIS Ops bot. Check with action_status."}

    async def _recipient(self, a) -> str | None:
        """Resolve who/where an action goes, so the user sees it before approving."""
        try:
            if a.kind == "teams_send_message" and self.s.teams:
                return await self.s.teams.describe(a.params["conv_id"])
            if a.kind == "brightspace_discussion_post":
                forum, topic = await self.s.bs.discussion_names(a.params["ou"], a.params["forum_id"],
                                                                a.params["topic_id"])
                return f"{forum} \u203a {topic}"
        except Exception as e:  # noqa: BLE001 - card then says UNKNOWN / shows raw ids
            import logging
            logging.getLogger(__name__).warning("could not resolve recipient: %s", e)
        return None

    async def status(self, action_id: int) -> dict:
        try:
            a = actions.get(self.s.db, action_id)
        except ActionNotPending as e:
            return {"error": str(e)}
        return {"id": a.id, "kind": a.kind, "status": a.status, "result": a.result}

    # ----- button handlers (Ops bot only) -----
    async def on_approve(self, arg: str) -> str:
        if not self.s.settings.actions_enabled:
            return "Actions are disabled on this machine (ACTIONS_ENABLED=false). Nothing was sent."
        try:
            a = actions.approve(self.s.db, int(arg), self.s.clock())
        except ActionNotPending as e:
            return f"#{arg} not pending: {e}"
        result = await actions.execute(self.s, a)
        if "error" in result:
            return f"#{a.id} failed: {result['error']}"
        return f"#{a.id} done. Receipt: {json.dumps(result, default=str)[:500]}"

    async def on_cancel(self, arg: str) -> str:
        return f"#{arg} cancelled." if actions.cancel(self.s.db, int(arg), self.s.clock()) else f"#{arg} not pending."

    async def on_preview(self, arg: str) -> str | None:
        try:
            a = actions.get(self.s.db, int(arg))
        except ActionNotPending as e:
            return str(e)
        if a.kind == "brightspace_submit" and Path(a.params["snapshot"]).is_file():
            await self.s.notifier.send_file(Path(a.params["snapshot"]), f"Preview for approval #{a.id}")
            return None
        return "Nothing to preview."

    async def pending_text(self) -> str:
        items = actions.list_pending(self.s.db)
        if not items:
            return "No pending approvals."
        return "\n".join(f"#{a.id} {TITLES[a.kind]}: {a.summary or a.params.get('file_name', '')}"[:200]
                         for a in items)

    async def expire_job(self) -> None:
        actions.expire(self.s.db, self.s.clock())
