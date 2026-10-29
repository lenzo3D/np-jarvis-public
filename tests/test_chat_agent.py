import asyncio
import json
from datetime import datetime, timedelta, timezone

from core.config import load_settings
from core.models import Course
from ops.chat_agent import ChatAgent, build_agent_command
from ops.watcher import Services
from tests.fakes import Clock, FakeBS, FakeNotifier

NOW = datetime(2026, 10, 27, 2, 0, tzinfo=timezone.utc)


def svc(db, env_file):
    s = Services(settings=load_settings(env_file), db=db, sessions=None, bs=FakeBS(), notifier=FakeNotifier(),
                 clock=Clock(NOW))
    s.bs.course_list = [Course(1, "26S2-731IV_013686", "INVESTMENTS(731IV_013686)")]
    return s


def test_command_has_full_school_tools_and_workspace_access(env_file):
    s = load_settings(env_file)
    cmd = build_agent_command("do my investments tutorial", s, session_id=None)
    allowed = cmd[cmd.index("--allowedTools") + 1]
    assert "mcp__school" in allowed.split(",")
    assert f"Write({s.workspace_dir}/**)" in allowed and "Skill" in allowed and "Bash(python3:*)" in allowed
    denied = cmd[cmd.index("--disallowedTools") + 1]
    assert "Bash(sudo:*)" in denied and f"Read({s.state_dir}/**)" in denied and "secrets.env" in denied
    cfg = json.loads(cmd[cmd.index("--mcp-config") + 1])
    assert "SCHOOL_MCP_READONLY" not in cfg["mcpServers"]["school"]["env"]
    assert "--strict-mcp-config" in cmd and "--resume" not in cmd
    rules = cmd[cmd.index("--append-system-prompt") + 1]
    assert "submit_assignment" in rules and "send_file_to_me" in rules and "untrusted" in rules
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"


def test_command_resumes_session(env_file):
    cmd = build_agent_command("and the next one?", load_settings(env_file), session_id="abc-123")
    assert cmd[cmd.index("--resume") + 1] == "abc-123"


class Script:
    """Fake agent runner: yields ('session', id), ('text', t)... ('result', t)."""

    def __init__(self, events, gate=None, fail_resume=False):
        self.events, self.gate, self.fail_resume = events, gate, fail_resume
        self.calls = []

    async def __call__(self, prompt, session_id):
        self.calls.append((prompt, session_id))
        if self.gate:
            await self.gate.wait()
        if self.fail_resume and session_id:
            from ops.claude_runner import ClaudeError
            raise ClaudeError("No conversation found with session ID")
        for e in self.events:
            yield e


async def test_message_runs_agent_and_replies(db, env_file):
    s = svc(db, env_file)
    r = Script([("session", "S1"), ("text", "Looking…"), ("result", "Tutorial 5 is due Fri 30 Oct 23:59.")])
    a = ChatAgent(s, runner=r)
    assert await a.handle("when is tutorial 5 due") is None
    await a.wait()
    assert r.calls == [("when is tutorial 5 due", None)]
    assert s.notifier.messages[-1][0] == "Tutorial 5 is due Fri 30 Oct 23:59."


async def test_conversation_memory_and_new(db, env_file):
    s = svc(db, env_file)
    r = Script([("session", "S1"), ("result", "ok")])
    a = ChatAgent(s, runner=r)
    await a.handle("q1")
    await a.wait()
    await a.handle("q2")
    await a.wait()
    assert r.calls[1] == ("q2", "S1")
    assert "fresh" in a.reset()
    await a.handle("q3")
    await a.wait()
    assert r.calls[2] == ("q3", None)


async def test_idle_conversation_expires(db, env_file):
    s = svc(db, env_file)
    r = Script([("session", "S1"), ("result", "ok")])
    a = ChatAgent(s, runner=r)
    await a.handle("q1")
    await a.wait()
    s.clock.now = NOW + timedelta(hours=7)
    await a.handle("q2")
    await a.wait()
    assert r.calls[1] == ("q2", None)


async def test_lost_session_retries_fresh(db, env_file):
    s = svc(db, env_file)
    db.set_json_setting("chat_session", {"id": "OLD", "at": NOW.isoformat()})
    r = Script([("session", "S2"), ("result", "ok")], fail_resume=True)
    a = ChatAgent(s, runner=r)
    await a.handle("q")
    await a.wait()
    assert r.calls == [("q", "OLD"), ("q", None)] and s.notifier.messages[-1][0] == "ok"


async def test_busy_reply(db, env_file):
    s = svc(db, env_file)
    gate = asyncio.Event()
    a = ChatAgent(s, runner=Script([("result", "done")], gate=gate))
    await a.handle("long job")
    await asyncio.sleep(0)
    assert "still working" in await a.handle("another")
    gate.set()
    await a.wait()


async def test_progress_edits_throttled(db, env_file):
    s = svc(db, env_file)
    t = {"now": 0.0}
    events = [("text", "a"), ("text", "b"), ("text", "c"), ("result", "final")]

    class Timed(Script):
        async def __call__(self, prompt, session_id):
            for i, e in enumerate(self.events):
                t["now"] = [0, 3, 11, 12][i]
                yield e

    a = ChatAgent(s, runner=Timed(events), time_fn=lambda: t["now"])
    await a.handle("q")
    await a.wait()
    assert len(s.notifier.edits) == 1 and s.notifier.messages[-1][0] == "final"


async def test_failure_reported(db, env_file):
    s = svc(db, env_file)

    async def broken(prompt, session_id):
        raise RuntimeError("claude not logged in")
        yield

    a = ChatAgent(s, runner=broken)
    await a.handle("q")
    await a.wait()
    assert "claude not logged in" in s.notifier.messages[-1][0]
