"""One bot: every non-command message to JARVIS Ops goes to a full Claude Code agent with conversation memory.

The agent gets the school MCP (all tools, including PROPOSE-only submit/post/send that still need approval in Ops),
the user's skills, and read/write access to the workspace. /new starts a fresh conversation; a conversation
idle for 6 hours also starts fresh.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import AsyncIterator

from ops import claude_runner, persona
from ops.claude_runner import ClaudeError, _base_cmd

log = logging.getLogger(__name__)
REPO = Path(__file__).resolve().parent.parent
IDLE_RESET = timedelta(hours=6)
AGENT_TIMEOUT = 30 * 60
EDIT_EVERY = 10.0


def _mcp_config_json(settings) -> str:
    return json.dumps({"mcpServers": {"school": {
        "command": sys.executable, "args": ["-m", "school_mcp.server"], "cwd": str(REPO),
        "env": {"OPS_SOCKET": str(settings.ops_socket), "PYTHONPATH": str(REPO)}}}})


def build_agent_command(prompt: str, settings, session_id: str | None) -> list[str]:
    ws, skills = settings.workspace_dir, Path.home() / ".claude" / "skills"
    allowed = ["mcp__school", f"Read({ws}/**)", f"Write({ws}/**)", f"Edit({ws}/**)", f"Read({skills}/**)",
               "Glob", "Grep", "Skill", "Bash(python3:*)", "Bash(soffice:*)", "Bash(pandoc:*)", "Bash(ls:*)",
               "Bash(mkdir:*)", "Bash(cp:*)", "Bash(mv:*)", "WebSearch", "WebFetch"]
    denied = ["Bash(sudo:*)", "Bash(curl:*)", f"Read({settings.state_dir}/**)", f"Read({REPO}/secrets.env)",
              f"Edit({REPO}/core/**)", f"Edit({REPO}/ops/**)", f"Edit({REPO}/school_mcp/**)"]
    cmd = _base_cmd(settings.claude_run_as) + [
        "-p", prompt, "--output-format", "stream-json", "--verbose",
        "--append-system-prompt", persona.rules(settings),
        "--strict-mcp-config", "--mcp-config", _mcp_config_json(settings),
        "--allowedTools", ",".join(allowed), "--disallowedTools", ",".join(denied)]
    if session_id:
        cmd += ["--resume", session_id]
    return cmd


async def stream_claude(s, prompt: str, session_id: str | None) -> AsyncIterator[tuple[str, str]]:
    """Run the JARVIS agent once. Yields ('session', id), ('text', block)... and finally ('result', text)."""
    proc = await asyncio.create_subprocess_exec(
        *build_agent_command(prompt, s, session_id), cwd=str(s.workspace_dir), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024, start_new_session=True)
    stderr_task = asyncio.create_task(proc.stderr.read())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + AGENT_TIMEOUT
    started, usage, ok = time.monotonic(), {}, False
    try:
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=max(1, deadline - loop.time()))
            if not line:
                break
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("session_id") and ev.get("type") in ("system", "result"):
                yield "session", ev["session_id"]
            if ev.get("type") == "assistant":
                for block in (ev.get("message") or {}).get("content", []):
                    if block.get("type") == "text" and block.get("text", "").strip():
                        yield "text", block["text"]
            elif ev.get("type") == "result":
                usage = claude_runner.usage_from_result(ev)
                if ev.get("is_error"):
                    raise ClaudeError(str(ev.get("result") or ev.get("subtype"))[:500])
                ok = True
                yield "result", str(ev.get("result") or "")
        await proc.wait()
        if proc.returncode:
            raise ClaudeError((await stderr_task).decode(errors="replace")[-500:] or f"exit {proc.returncode}")
    except asyncio.TimeoutError as e:
        raise ClaudeError("took longer than 30 minutes") from e
    finally:
        claude_runner.record_usage(kind="agent", model=None, ok=ok, seconds=time.monotonic() - started, **usage)
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
        stderr_task.cancel()


class ChatAgent:
    def __init__(self, ctx, runner=None, time_fn=time.monotonic):
        self.ctx = ctx
        self.runner = runner or self._run_claude
        self.time_fn = time_fn
        self._task: asyncio.Task | None = None

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    async def wait(self) -> None:
        if self._task:
            await self._task

    def reset(self) -> str:
        self.ctx.db.set_json_setting("chat_session", {})
        return persona.fresh_start(self.ctx.settings)

    def _session(self) -> str | None:
        s = self.ctx.db.get_json_setting("chat_session", {})
        if not s.get("id") or not s.get("at"):
            return None
        if self.ctx.clock() - datetime.fromisoformat(s["at"]) > IDLE_RESET:
            return None
        return s["id"]

    def _remember(self, session_id: str) -> None:
        self.ctx.db.set_json_setting("chat_session", {"id": session_id, "at": self.ctx.clock().isoformat()})

    async def handle(self, text: str) -> str | None:
        if self.busy:
            return persona.busy(self.ctx.settings)
        self._task = asyncio.get_running_loop().create_task(self._run(text))
        return None

    async def _stream(self, prompt: str, session_id: str | None, msg) -> str:
        final, last_edit, last_text = "", self.time_fn(), ""
        async for kind, value in self.runner(prompt, session_id):
            if kind == "session":
                self._remember(value)
            elif kind == "text":
                last_text = value
                now = self.time_fn()
                if msg is not None and now - last_edit >= EDIT_EVERY:
                    await self.ctx.notifier.edit(msg.message_id, f"Working… {value[-500:]}")
                    last_edit = now
            elif kind == "result":
                final = value
        return final or last_text or persona.done(self.ctx.settings)

    async def _run(self, prompt: str) -> None:
        with claude_runner.labelled("chat_telegram"):
            await self._run_labelled(prompt)

    async def _run_labelled(self, prompt: str) -> None:
        msg = await self.ctx.notifier.send(persona.ack(self.ctx.settings))
        session = self._session()
        try:
            try:
                answer = await self._stream(prompt, session, msg)
            except ClaudeError:
                if not session:
                    raise
                log.warning("could not resume conversation %s; starting fresh", session)
                answer = await self._stream(prompt, None, msg)
            await self.ctx.notifier.send(answer)
        except Exception as e:  # noqa: BLE001
            log.exception("chat agent failed")
            await self.ctx.notifier.send(persona.apology(self.ctx.settings, f"{type(e).__name__}: {e}")[:1000])

    def _run_claude(self, prompt: str, session_id: str | None):
        return stream_claude(self.ctx.settings, prompt, session_id)
