"""Automatic replies in the groups the user marked "reply".

Replies are posted from the user's own Telegram account (the login that already reads the group). The Ops bot itself still
never posts in any group.

Everyone in the group can write text that reaches this code, so the model runs with every tool switched off and sees only
that group's recent chat, upcoming deadlines and a few matching course-material snippets. It never sees email, other chats,
or secrets. Hard limits keep it from spamming and from getting the account restricted by Telegram.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from datetime import datetime, timedelta

from core import groups, tg_group
from core.deadlines import DeadlineStore, local_date
from core.library import Library
from ops import claude_runner, persona
from ops.textfmt import plain

log = logging.getLogger(__name__)

MODEL = "haiku"
CONTEXT_MESSAGES = 25
MIN_GAP = 20.0        # seconds between two replies in the same group
MAX_PER_HOUR = 20     # per group
MAX_PER_DAY = 100     # per group
MAX_CHARS = 500
NO_REPLY = "NO_REPLY"
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


def clean_reply(raw: str | None) -> str:
    """The text to post, or "" when the model chose to stay quiet."""
    text = plain(raw or "").strip()
    if not text or text.upper().startswith(NO_REPLY):
        return ""
    if len(text) > MAX_CHARS:
        cut = text[:MAX_CHARS]
        ends = [m.end() for m in _SENTENCE_END.finditer(cut)]
        text = cut[:ends[-1]] if ends and ends[-1] > MAX_CHARS // 2 else cut.rstrip() + "..."
    return text


class GroupReplier:
    def __init__(self, ctx, run_text=None, sleep=asyncio.sleep, typing_delay=True):
        self.ctx = ctx
        self._run = run_text or claude_runner.run_text
        self._sleep = sleep
        self._typing_delay = typing_delay
        self._latest: dict[int, object] = {}
        self._busy: set[int] = set()
        self._blocked_until = 0.0  # monotonic time; set when Telegram says "slow down"

    # ---------- entry point (called by the Telegram reader) ----------
    async def on_message(self, client, chat_id: int, msg) -> None:
        """A new message arrived in a reply group (never your own, never a bot's). Bursts collapse: while one reply is being
        written, newer messages replace each other, so one answer covers the latest state of the conversation."""
        if self.ctx.db.get_setting("paused") == "1":
            return
        self._latest[chat_id] = msg
        if chat_id in self._busy:
            return
        self._busy.add(chat_id)
        try:
            while chat_id in self._latest:
                current = self._latest.pop(chat_id)
                try:
                    await self._respond(client, chat_id, current)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 - one failed reply must never stop the reader
                    self._handle_error(e)
        finally:
            self._busy.discard(chat_id)

    # ---------- steps ----------
    async def _respond(self, client, chat_id: int, msg) -> None:
        if time.monotonic() < self._blocked_until:
            return
        now = self.ctx.clock()
        per_hour, per_day, last = self._usage(chat_id, now)
        if per_hour >= MAX_PER_HOUR or per_day >= MAX_PER_DAY:
            self._status("limit", f"reply limit reached ({per_hour} in the last hour, {per_day} today)")
            return
        if last is not None:
            wait = MIN_GAP - (now - last).total_seconds()
            if wait > 0:
                await self._sleep(wait)
                msg = self._latest.pop(chat_id, msg)  # something newer arrived while waiting: answer that
        if self.ctx.db.get_setting("paused") == "1":
            return
        text = clean_reply(await self._write(chat_id, msg))
        if not text:
            self._status("quiet", "chose not to reply")
            return
        if self._typing_delay:
            await self._pretend_to_type(client, chat_id, text)
        await client.send_message(chat_id, text, reply_to=msg.msg_id)
        self.ctx.db.conn.execute("insert into group_replies(ts, chat_id, in_msg_id, text) values (?,?,?,?)",
                                 (self.ctx.clock().isoformat(), chat_id, msg.msg_id, text))
        self._status("replied", "")

    async def _write(self, chat_id: int, msg) -> str:
        s, db = self.ctx.settings, self.ctx.db
        tz = s.tz
        recent = tg_group.latest(db, chat_id, CONTEXT_MESSAGES)
        chat = "\n".join(f"[{m.ts.astimezone(tz):%H:%M}] {m.sender or '?'}: {m.text[:500]}" for m in recent)
        data = (f"=== CHAT (newest last) ===\n{chat}\n\n=== UPCOMING DEADLINES (next 14 days) ===\n{self._deadlines()}\n\n"
                f"=== COURSE MATERIAL MATCHING THE NEWEST MESSAGE ===\n{self._material(msg.text)}\n")
        prompt = claude_runner.load_prompt("group_reply", group=groups.label_for(s, db, chat_id), **persona.fields(s))
        return await self._run(prompt, data, model=MODEL, timeout=90)

    def _deadlines(self) -> str:
        s, now = self.ctx.settings, self.ctx.clock()
        rows = DeadlineStore(self.ctx.db).upcoming(now, 14, s.tz)
        if not rows:
            return "None in the next 14 days."
        out = []
        for d in rows[:25]:
            when = local_date(d, s.tz).strftime("%a %d %b")
            if not d.all_day:
                when = datetime.fromisoformat(d.end_utc or d.start_utc).astimezone(s.tz).strftime("%a %d %b %H:%M")
            tag = "" if d.confidence == "confirmed" else " [tentative]"
            out.append(f"{when} - {d.course_code or ''}: {d.title}{tag}")
        return "\n".join(out)

    def _material(self, text: str) -> str:
        try:
            hits = Library(self.ctx.db).search(text or "", limit=3)
        except Exception:  # noqa: BLE001 - an odd message must not break replying
            hits = []
        if not hits:
            return "Nothing relevant."
        return "\n".join(f"{h['course_code'] or ''} {h['title']}: {h['snippet']}"[:400] for h in hits)

    async def _pretend_to_type(self, client, chat_id: int, text: str) -> None:
        """A short pause with Telegram's "typing..." indicator, so a reply does not land the instant a message does."""
        seconds = min(6.0, 1.5 + len(text) / 40 + random.random())
        try:
            async with client.action(chat_id, "typing"):
                await self._sleep(seconds)
        except Exception:  # noqa: BLE001 - the indicator is cosmetic
            await self._sleep(seconds)

    # ---------- limits, audit and status ----------
    def _usage(self, chat_id: int, now: datetime) -> tuple[int, int, datetime | None]:
        c = self.ctx.db.conn
        hour = c.execute("select count(*) from group_replies where chat_id=? and ts>=?",
                         (chat_id, (now - timedelta(hours=1)).isoformat())).fetchone()[0]
        day = c.execute("select count(*) from group_replies where chat_id=? and ts>=?",
                        (chat_id, (now - timedelta(days=1)).isoformat())).fetchone()[0]
        row = c.execute("select max(ts) from group_replies where chat_id=?", (chat_id,)).fetchone()
        return hour, day, datetime.fromisoformat(row[0]) if row and row[0] else None

    def _handle_error(self, e: Exception) -> None:
        name = type(e).__name__
        seconds = getattr(e, "seconds", None)
        if name == "FloodWaitError" and isinstance(seconds, int):
            self._blocked_until = time.monotonic() + seconds + 5
        log.warning("group reply failed: %s: %s", name, e)
        self._status("error", name)

    def _status(self, state: str, message: str) -> None:
        self.ctx.db.set_json_setting("group_reply_status", {"state": state, "message": message[:120],
                                                            "at": self.ctx.clock().isoformat()})
