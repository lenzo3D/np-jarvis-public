"""JARVIS Ops bot: alerts, MFA prompts, approvals and control commands.

Only ALLOWED_TELEGRAM_ID can trigger any handler, and only in a PRIVATE chat with the bot.

In group chats the bot is a silent reader: it records the text of the groups you chose (for summaries, search and
date extraction) and does nothing else. It never replies (replies in groups marked "reply" go out through the user's own account in ops/group_reply.py, not this bot), never answers commands, mentions or buttons there (even from
you, so nothing private can leak into a group), and it leaves any group that someone other than you adds it to.
Everything it sends goes to the owner's private chat only (see Notifier).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Awaitable, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, ChatMemberHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from core import groups, tg_group
from core.config import Settings
from core.db import DB
from core.models import GroupMessage
from ops.telegram_out import FILE_LIMIT, chunk
from ops.textfmt import plain

log = logging.getLogger(__name__)
Buttons = list[list[tuple[str, str]]]
COMMANDS = ["status", "login", "mute", "unmute", "quiet", "briefing", "pause", "resume", "pending", "deadlines",
            "scan", "new"]


class Notifier:
    def __init__(self, bot, chat_id: int, address: str = "Sir"):
        self.bot = bot
        self.chat_id = chat_id
        self.address = address

    @staticmethod
    def _markup(buttons: Buttons | None):
        if not buttons:
            return None
        return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in buttons])

    async def send(self, text: str, buttons: Buttons | None = None):
        parts = chunk(plain(text))
        msg = None
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            msg = await self.bot.send_message(self.chat_id, part, reply_markup=self._markup(buttons) if last else None,
                                              disable_web_page_preview=True)
        return msg

    async def edit(self, message_id: int, text: str) -> None:
        try:
            await self.bot.edit_message_text(chunk(plain(text))[-1], chat_id=self.chat_id, message_id=message_id)
        except Exception as e:  # noqa: BLE001 - "message is not modified" etc.
            log.debug("edit failed: %s", e)

    async def send_file(self, path: Path, caption: str = "") -> None:
        path = Path(path)
        size = path.stat().st_size
        if size > FILE_LIMIT:
            await self.send(f"File too large for Telegram ({size // (1024 * 1024)} MB). Saved at {path}")
            return
        with open(path, "rb") as fh:
            await self.bot.send_document(self.chat_id, fh, caption=caption[:1024], filename=path.name)

    async def send_bytes(self, name: str, data: bytes, caption: str = "") -> bool:
        if len(data) > FILE_LIMIT:
            await self.send(f"File too large for Telegram ({len(data) // (1024 * 1024)} MB): {name}. "
                            "It is saved in the workspace.")
            return False
        await self.bot.send_document(self.chat_id, data, caption=caption[:1024], filename=name)
        return True

    async def mfa_prompt(self, site: str, number: str) -> None:
        name = {"brightspace": "POLITEMall", "outlook": "Outlook"}.get(site, "Teams")
        await self.send(f"{self.address}, {name} requires your authorisation. Please approve {number} in Microsoft "
                        "Authenticator — I'll wait five minutes.")


class CallbackRouter:
    def __init__(self):
        self._routes: dict[str, Callable[[str], Awaitable[str | None]]] = {}

    def register(self, prefix: str, handler: Callable[[str], Awaitable[str | None]]) -> None:
        self._routes[prefix] = handler

    async def dispatch(self, data: str) -> str | None:
        for prefix in sorted(self._routes, key=len, reverse=True):
            if data.startswith(prefix + ":"):
                return await self._routes[prefix](data[len(prefix) + 1:])
        return None


class Commands:
    """Command logic, independent of telegram objects. hooks: optional async callables
    status() -> str, sweep() -> str, login(site) -> str, pending() -> str."""

    def __init__(self, db: DB, hooks: dict[str, Callable[..., Awaitable[str]]]):
        self.db = db
        self.hooks = hooks

    async def run(self, name: str, args: list[str]) -> str:
        return await getattr(self, f"cmd_{name}")(args)

    async def _hook(self, name: str, *a) -> str:
        fn = self.hooks.get(name)
        return await fn(*a) if fn else f"{name}: not available yet"

    async def cmd_status(self, args):
        if args and args[0] == "sweep":
            return await self._hook("sweep")
        return await self._hook("status")

    async def cmd_login(self, args):
        site = args[0] if args else "brightspace"
        if site not in ("brightspace", "teams", "outlook"):
            return "Usage: /login brightspace|teams|outlook"
        return await self._hook("login", site)

    async def cmd_mute(self, args):
        if not args:
            return "Usage: /mute <course code>"
        muted = set(self.db.get_json_setting("muted", []))
        muted.add(args[0].upper())
        self.db.set_json_setting("muted", sorted(muted))
        return f"Muted {args[0].upper()} (urgent alerts still come through)."

    async def cmd_unmute(self, args):
        if not args:
            return "Usage: /unmute <course code>"
        muted = [m for m in self.db.get_json_setting("muted", []) if m != args[0].upper()]
        self.db.set_json_setting("muted", muted)
        return f"Unmuted {args[0].upper()}."

    async def cmd_quiet(self, args):
        if args and args[0] == "off":
            self.db.set_setting("quiet", "")
            return "Quiet hours off."
        if not args or not re.fullmatch(r"([01]?\d|2[0-3])-([01]?\d|2[0-3])", args[0]):
            return "Usage: /quiet 23-7  (hours, 24h clock) or /quiet off"
        self.db.set_setting("quiet", args[0])
        return f"Quiet hours set to {args[0]}. Urgent alerts still come through."

    async def cmd_briefing(self, args):
        if not args or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", args[0]):
            return "Usage: /briefing 07:00"
        self.db.set_setting("briefing_time", args[0])
        return f"Daily briefing at {args[0]}."

    async def cmd_pause(self, args):
        self.db.set_setting("paused", "1")
        return "Alerts paused. /resume to turn back on."

    async def cmd_resume(self, args):
        self.db.set_setting("paused", "0")
        return "Alerts resumed."

    async def cmd_pending(self, args):
        return await self._hook("pending")

    async def cmd_deadlines(self, args):
        return await self._hook("deadlines", *([int(args[0])] if args and args[0].isdigit() else []))

    async def cmd_scan(self, args):
        return await self._hook("scan")

    async def cmd_new(self, args):
        return await self._hook("new")


def build_app(settings: Settings, db: DB, hooks: dict, router: CallbackRouter | None = None,
              token: str | None = None) -> Application:
    app = Application.builder().token(token or settings.ops_bot_token or "0:offline").build()
    only_me = filters.User(user_id=settings.allowed_telegram_id) & filters.ChatType.PRIVATE  # never in a group
    cmds = Commands(db, hooks)
    router = router or CallbackRouter()
    app.bot_data["commands"] = cmds
    app.bot_data["router"] = router

    def make(name: str):
        async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
            reply = await cmds.run(name, list(context.args or []))
            if reply:
                for part in chunk(plain(reply)):
                    await update.effective_message.reply_text(part)
        return handler

    for name in COMMANDS:
        app.add_handler(CommandHandler(name, make(name), filters=only_me))

    async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        if not q or not q.from_user or q.from_user.id != settings.allowed_telegram_id:
            return
        if not q.message or q.message.chat.type != "private":
            return  # buttons only work in the private chat
        await q.answer()
        try:
            reply = await router.dispatch(q.data or "")
        except Exception as e:  # noqa: BLE001
            log.exception("callback %s failed", q.data)
            reply = f"Error: {type(e).__name__}: {e}"[:500]
        if reply:
            await q.message.reply_text(plain(reply))

    app.add_handler(CallbackQueryHandler(on_callback))

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        ask = hooks.get("ask")
        if not ask:
            await update.effective_message.reply_text("Chat isn't set up yet.")
            return
        reply = await ask(update.effective_message.text or "")
        if reply:
            await update.effective_message.reply_text(plain(reply))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & only_me, on_text))

    # ---- groups: read-only. These handlers never send anything. ----
    async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg, chat = update.effective_message, update.effective_chat
        if msg is None or chat is None or chat.id not in groups.ids(settings, db):
            return  # only the groups you chose are recorded
        text = msg.text or msg.caption
        if not text or not text.strip():
            return
        user = update.effective_user
        sender = (user.full_name if user else None) or (msg.sender_chat.title if msg.sender_chat else None) or "?"
        tg_group.store_message(db, GroupMessage(chat.id, msg.message_id, sender, text, msg.date))

    app.add_handler(MessageHandler(filters.ChatType.GROUPS & (filters.TEXT | filters.CAPTION)
                                   & ~filters.StatusUpdate.ALL, on_group_message), group=1)

    async def on_membership(update: Update, context: ContextTypes.DEFAULT_TYPE):
        ev = update.my_chat_member
        if ev is None or ev.chat.type not in ("group", "supergroup"):
            return
        if ev.new_chat_member.status in ("member", "administrator"):
            if ev.from_user is None or ev.from_user.id != settings.allowed_telegram_id:
                log.warning("bot was added to %s by someone other than the owner; leaving", ev.chat.id)
                try:
                    await context.bot.leave_chat(ev.chat.id)  # silently: leaving posts nothing
                except Exception:  # noqa: BLE001
                    log.debug("leave_chat failed", exc_info=True)
                return
            groups.add_bot_group(db, ev.chat.id, ev.chat.title or "")
        elif ev.new_chat_member.status in ("left", "kicked"):
            groups.remove_bot_group(db, ev.chat.id)

    app.add_handler(ChatMemberHandler(on_membership, ChatMemberHandler.MY_CHAT_MEMBER), group=1)
    return app
