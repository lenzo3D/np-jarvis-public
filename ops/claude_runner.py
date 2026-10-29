"""Run headless Claude Code (`claude -p`) for summaries (no tools) and agent jobs."""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator, Callable

log = logging.getLogger(__name__)

PROMPTS = Path(__file__).parent / "prompts"


class ClaudeError(Exception):
    pass


# ---- usage log: every Claude call is recorded with the job that caused it ----
_job: contextvars.ContextVar[str] = contextvars.ContextVar("claude_job", default="other")
_sink: Callable[..., None] | None = None


def set_usage_sink(fn: Callable[..., None] | None) -> None:
    """fn(job=, kind=, model=, ok=, seconds=, in_tokens=, out_tokens=, cache_tokens=, estimated=, turns=, cost_usd=)"""
    global _sink
    _sink = fn


@contextlib.contextmanager
def labelled(job: str):
    token = _job.set(job)
    try:
        yield
    finally:
        _job.reset(token)


USAGE_KEEP = timedelta(days=90)


def attach_db(db, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
    """Log every Claude call of this process into the shared database (and drop entries older than 90 days)."""
    set_usage_sink(lambda **kw: db.record_usage(clock(), **kw))
    try:
        db.prune_usage(clock() - USAGE_KEEP)
    except Exception:  # noqa: BLE001
        log.debug("usage prune failed", exc_info=True)


def record_usage(**fields) -> None:
    if _sink is None:
        return
    try:
        _sink(job=_job.get(), **fields)
    except Exception:  # noqa: BLE001 - bookkeeping must never break a real job
        log.debug("usage record failed", exc_info=True)


def usage_from_result(ev: dict) -> dict:
    """Token counts and turns from a stream-json 'result' event (real numbers, not estimates)."""
    u = ev.get("usage") or {}
    cache = (u.get("cache_creation_input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
    return {"in_tokens": u.get("input_tokens"), "out_tokens": u.get("output_tokens"), "cache_tokens": cache or None,
            "turns": ev.get("num_turns"), "cost_usd": ev.get("total_cost_usd")}


def load_prompt(name: str, **fields) -> str:
    text = (PROMPTS / f"{name}.md").read_text()
    for k, v in fields.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text


def _base_cmd(run_as: str | None) -> list[str]:
    exe = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude"
    return ["sudo", "-n", "-H", "-u", run_as, exe] if run_as else [exe]


async def run_text(prompt: str, input_text: str, model: str = "sonnet", timeout: float = 180,
                   run_as: str | None = None) -> str:
    """One-shot text completion with every tool and MCP server disabled. Input goes on stdin."""
    cmd = _base_cmd(run_as) + ["-p", prompt, "--model", model, "--output-format", "text",
                               "--tools", "", "--strict-mcp-config"]
    started, in_chars, text = time.monotonic(), len(prompt) + len(input_text), ""
    try:
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE,
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(proc.communicate(input_text.encode()), timeout=timeout)
        except (asyncio.TimeoutError, FileNotFoundError, OSError) as e:
            raise ClaudeError(f"{type(e).__name__}: {e}") from e
        if proc.returncode != 0:
            raise ClaudeError(err.decode(errors="replace")[-500:] or f"exit {proc.returncode}")
        text = out.decode(errors="replace").strip()
        return text
    finally:  # plain-text mode reports no token counts, so these are estimates (about 4 characters per token)
        record_usage(kind="text", model=model, ok=bool(text), seconds=time.monotonic() - started,
                     in_tokens=in_chars // 4, out_tokens=len(text) // 4, estimated=True)


AGENT_TIMEOUT = 30 * 60


async def run_agent(prompt: str, cwd: Path, run_as: str | None = None,
                    timeout: float = AGENT_TIMEOUT) -> AsyncIterator[str]:
    """Headless Claude Code agent run with the user's MCP servers, skills and permission settings.
    Yields assistant text blocks as they stream; the final result text last."""
    cmd = _base_cmd(run_as) + ["-p", prompt, "--output-format", "stream-json", "--verbose"]
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(cwd), stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024)
    started, usage, ok = time.monotonic(), {}, False
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise ClaudeError("agent run hit the 30 minute limit")
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
            if not line:
                break
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "assistant":
                for block in (ev.get("message") or {}).get("content", []):
                    if block.get("type") == "text" and block.get("text", "").strip():
                        yield block["text"]
            elif ev.get("type") == "result":
                usage = usage_from_result(ev)
                if ev.get("is_error"):
                    raise ClaudeError(str(ev.get("result") or ev.get("subtype"))[:500])
                ok = True
        await proc.wait()
        if proc.returncode:
            err = (await proc.stderr.read()).decode(errors="replace")[-500:]
            raise ClaudeError(err or f"exit {proc.returncode}")
    except asyncio.TimeoutError as e:
        raise ClaudeError("agent run hit the 30 minute limit") from e
    finally:
        record_usage(kind="agent", model=None, ok=ok, seconds=time.monotonic() - started, **usage)
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
