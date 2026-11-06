"""Browser chat with JARVIS: the same agent, tools and approval rules as Telegram, one conversation at a time."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import AsyncIterator, Callable

from ops.chat_agent import IDLE_RESET, stream_claude
from ops.claude_runner import ClaudeAuthError, ClaudeError, labelled
from ops import persona
from ops.textfmt import plain

Runner = Callable[..., AsyncIterator[tuple[str, str]]]


class WebChat:
    def __init__(self, settings, runner: Runner = stream_claude, now: Callable[[], float] = time.time):
        self.settings = settings
        self.runner = runner
        self.now = now
        self.path = Path(settings.state_dir) / "web_chat.json"
        self.lock = asyncio.Lock()

    @property
    def busy(self) -> bool:
        return self.lock.locked()

    def _session(self) -> str | None:
        try:
            s = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not s.get("id") or self.now() - s.get("at", 0) > IDLE_RESET.total_seconds():
            return None
        return s["id"]

    def _remember(self, session_id: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"id": session_id, "at": self.now()}), encoding="utf-8")

    def reset(self) -> None:
        self.path.unlink(missing_ok=True)

    async def _once(self, text: str, session: str | None) -> AsyncIterator[dict]:
        final = last = ""
        async for kind, value in self.runner(self.settings, text, session):
            if kind == "session":
                self._remember(value)
            elif kind == "text":
                last = value
                yield {"type": "progress", "text": plain(value)}
            elif kind == "result":
                final = value
        yield {"type": "final", "text": plain(final or last or persona.done(self.settings))}

    async def run(self, text: str) -> AsyncIterator[dict]:
        """Yields {'type': 'progress'|'final'|'error', 'text': ...}. Caller must check .busy first."""
        async with self.lock:
            with labelled("chat_web"):
                async for ev in self._run_locked(text):
                    yield ev

    async def _run_locked(self, text: str) -> AsyncIterator[dict]:
        session = self._session()
        try:
            try:
                async for ev in self._once(text, session):
                    yield ev
            except ClaudeError as e:
                if not session or isinstance(e, ClaudeAuthError):
                    raise
                async for ev in self._once(text, None):  # the old conversation is gone: start fresh
                    yield ev
        except Exception as e:  # noqa: BLE001
            text = str(e) if isinstance(e, ClaudeAuthError) else f"{type(e).__name__}: {e}"
            yield {"type": "error", "text": text[:600]}
