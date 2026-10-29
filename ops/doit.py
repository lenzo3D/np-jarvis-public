"""'Do it' and 'Download' button handlers. One agent run at a time."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from core import safefs
from ops import claude_runner, persona
from ops.format import fmt_dt, parse_iso

log = logging.getLogger(__name__)
EDIT_EVERY = 10.0


def _slug(s: str) -> str:
    return re.sub(r"[^\w.\- ]+", "_", s).strip()[:60] or "task"


class DoIt:
    def __init__(self, services, runner=None, time_fn=time.monotonic):
        self.s = services
        self.runner = runner or claude_runner.run_agent
        self.time_fn = time_fn
        self.busy: str | None = None
        self._task: asyncio.Task | None = None

    async def wait(self) -> None:
        if self._task:
            await self._task

    def build_prompt(self, item) -> str:
        p = json.loads(item["payload_json"])
        due = parse_iso(p.get("due_at")) or parse_iso(p.get("end_at"))
        return claude_runner.load_prompt(
            "doit_assignment", **persona.fields(self.s.settings), course=item["course_code"], title=item["title"],
            due=fmt_dt(due, self.s.settings.tz) if due else "no due date",
            ou=p.get("ou"), folder_id=p.get("folder_id"), slug=_slug(item["title"]))

    async def on_doit(self, item_key: str) -> str:
        if self.busy:
            return f"Busy with {self.busy}. Try again when it finishes."
        item = self.s.db.conn.execute("select * from items where stable_id=?", (item_key,)).fetchone()
        if not item:
            return "That item was not found (it may have been removed)."
        self.busy = item["title"]
        prompt = self.build_prompt(item)
        self._task = asyncio.get_running_loop().create_task(self._run(item["title"], prompt))
        return f"Working on {item['title']}… I'll post progress here."

    async def _run(self, title: str, prompt: str) -> None:
        with claude_runner.labelled("do_it"):
            await self._run_labelled(title, prompt)

    async def _run_labelled(self, title: str, prompt: str) -> None:
        msg = await self.s.notifier.send(f"{title}: starting…")
        last_edit = self.time_fn()
        texts: list[str] = []
        try:
            async for text in self.runner(prompt, self.s.settings.workspace_dir, run_as=self.s.settings.claude_run_as):
                texts.append(text)
                now = self.time_fn()
                if now - last_edit >= EDIT_EVERY and msg is not None:
                    await self.s.notifier.edit(msg.message_id, f"{title}: …{text[-500:]}")
                    last_edit = now
            final = texts[-1] if texts else "(no output)"
            await self.s.notifier.send(f"{title} finished.\n\n{final}")
        except Exception as e:  # noqa: BLE001
            log.exception("do-it run failed")
            await self.s.notifier.send(f"{title} failed: {type(e).__name__}: {e}"[:1000])
        finally:
            self.busy = None

    async def on_dl(self, arg: str) -> str | None:
        ou_s, topic_s = arg.split(":", 1)
        ou, topic = int(ou_s), int(topic_s)
        courses = self.s.extra.get("courses")
        courses = courses[1] if courses else await self.s.bs.courses()
        code = next((c.code for c in courses if c.ou == ou), str(ou))
        name, data = await self.s.bs.fetch_file(ou, topic)
        path = safefs.write_file(self.s.settings.workspace_dir, [_slug(code), "downloads"], _slug(name), data)
        await self.s.notifier.send_file(path, path.name)
        return None
