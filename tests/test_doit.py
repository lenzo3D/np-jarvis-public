import asyncio
from datetime import datetime, timezone

from core.config import load_settings
from ops.diff import content_hash
from ops.doit import DoIt
from ops.watcher import Services
from tests.fakes import Clock, FakeBS, FakeNotifier, assignment

NOW = datetime(2026, 10, 26, 2, 0, tzinfo=timezone.utc)


def svc(db, env_file):
    s = Services(settings=load_settings(env_file), db=db, sessions=None, bs=FakeBS(), notifier=FakeNotifier(),
                 clock=Clock(NOW))
    it = assignment(55, due="2026-10-30T15:59:00+00:00")
    db.upsert_item(it, content_hash(it), NOW)
    return s


class ScriptedRunner:
    def __init__(self, chunks, gate=None):
        self.chunks = chunks  # list of (t, text)
        self.gate = gate
        self.prompts = []
        self.t = 0.0

    def now(self):
        return self.t

    async def __call__(self, prompt, cwd, run_as=None):
        self.prompts.append((prompt, cwd))
        if self.gate:
            await self.gate.wait()
        for t, text in self.chunks:
            self.t = t
            yield text


async def test_doit_prompt_contains_course_and_title(db, env_file):
    s = svc(db, env_file)
    r = ScriptedRunner([(0, "Done. Sent the file.")])
    d = DoIt(s, runner=r, time_fn=r.now)
    reply = await d.on_doit("bs:assignment:1001:55")
    await d.wait()
    prompt, cwd = r.prompts[0]
    assert "INV2101" in prompt and "Tutorial 55" in prompt and "Fri 30 Oct 23:59" in prompt
    assert "folder_id 55" in prompt and "do not submit" in prompt.lower()
    assert cwd == s.settings.workspace_dir
    assert "Working on" in reply
    assert s.notifier.messages[-1][0].endswith("Done. Sent the file.")


async def test_second_doit_while_busy_rejected(db, env_file):
    s = svc(db, env_file)
    gate = asyncio.Event()
    r = ScriptedRunner([(0, "ok")], gate=gate)
    d = DoIt(s, runner=r, time_fn=r.now)
    await d.on_doit("bs:assignment:1001:55")
    reply = await d.on_doit("bs:assignment:1001:55")
    assert reply.startswith("Busy with")
    gate.set()
    await d.wait()
    assert len(r.prompts) == 1


async def test_progress_edit_throttled(db, env_file):
    s = svc(db, env_file)
    r = ScriptedRunner([(0, "a"), (3, "b"), (6, "c"), (11, "d"), (12, "e")])
    d = DoIt(s, runner=r, time_fn=r.now)
    await d.on_doit("bs:assignment:1001:55")
    await d.wait()
    assert len(s.notifier.edits) == 1


async def test_unknown_item(db, env_file):
    s = svc(db, env_file)
    d = DoIt(s, runner=ScriptedRunner([]))
    assert "not found" in await d.on_doit("bs:assignment:1:1")


async def test_runner_failure_reported(db, env_file):
    s = svc(db, env_file)

    async def broken(prompt, cwd, run_as=None):
        raise RuntimeError("claude not logged in")
        yield  # pragma: no cover

    d = DoIt(s, runner=broken)
    await d.on_doit("bs:assignment:1001:55")
    await d.wait()
    assert "claude not logged in" in s.notifier.messages[-1][0]


async def test_dl_sends_file(db, env_file):
    s = svc(db, env_file)
    s.settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    d = DoIt(s, runner=ScriptedRunner([]))
    await d.on_dl("1001:702")
    saved = s.settings.workspace_dir / "INV2101" / "downloads" / "topic-702.docx"
    assert saved.read_bytes() == b"PK-course-file"
    assert s.notifier.files == [(saved, "topic-702.docx")]
