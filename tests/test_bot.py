import os
from types import SimpleNamespace

from telegram import Update

from core.config import load_settings
from ops.bot import CallbackRouter, Commands, Notifier, build_app
from ops.telegram_out import chunk


def test_chunk_respects_limit():
    text = "\n\n".join(("para %d " % i) * 60 for i in range(40))
    parts = chunk(text)
    assert all(len(p) <= 4096 for p in parts)
    assert len(parts) > 1
    assert "\n\n".join(parts) == text


def test_chunk_hard_splits_long_line():
    parts = chunk("x" * 9000)
    assert [len(p) for p in parts] == [4096, 4096, 808]


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.sent.append(("text", text, reply_markup))
        return SimpleNamespace(message_id=len(self.sent))

    async def send_document(self, chat_id, document, caption="", **kw):
        self.sent.append(("doc", caption, None))


async def test_send_splits_long_text():
    bot = FakeBot()
    await Notifier(bot, 42).send("y" * 5000)
    assert len(bot.sent) == 2


async def test_send_file_too_large_sends_text(tmp_path):
    big = tmp_path / "big.zip"
    with open(big, "wb") as f:
        f.truncate(51 * 1024 * 1024)
    bot = FakeBot()
    await Notifier(bot, 42).send_file(big)
    kind, text, _ = bot.sent[0]
    assert kind == "text" and "too large" in text and str(big) in text


async def test_buttons_become_inline_keyboard():
    bot = FakeBot()
    await Notifier(bot, 42).send("hi", buttons=[[("✅ Submit", "act:approve:1"), ("❌ Cancel", "act:cancel:1")]])
    markup = bot.sent[0][2]
    assert markup.inline_keyboard[0][0].callback_data == "act:approve:1"


def _update(user_id, text, bot=None):
    return Update.de_json({
        "update_id": 1,
        "message": {"message_id": 1, "date": 0, "text": text,
                    "entities": [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}],
                    "chat": {"id": user_id, "type": "private"},
                    "from": {"id": user_id, "is_bot": False, "first_name": "x"}},
    }, bot)


def test_non_allowed_user_ignored(env_file, db):
    app = build_app(load_settings(env_file), db, hooks={})
    from telegram import User
    app.bot._bot_user = User(1, "JarvisOps", True, username="jarvis_ops_bot")
    handlers = [h for group in app.handlers.values() for h in group]
    assert handlers
    stranger = _update(999, "/status", app.bot)
    assert not any(h.check_update(stranger) for h in handlers)
    owner = _update(42, "/status", app.bot)
    assert any(h.check_update(owner) for h in handlers)


async def test_quiet_command_sets_setting(db):
    cmds = Commands(db, hooks={})
    reply = await cmds.run("quiet", ["23-7"])
    assert db.get_setting("quiet") == "23-7"
    assert "23" in reply


async def test_quiet_rejects_bad_value(db):
    cmds = Commands(db, hooks={})
    reply = await cmds.run("quiet", ["late"])
    assert db.get_setting("quiet") is None
    assert "Usage" in reply


async def test_mute_unmute(db):
    cmds = Commands(db, hooks={})
    await cmds.run("mute", ["inv2101"])
    assert db.get_json_setting("muted", []) == ["INV2101"]
    await cmds.run("unmute", ["INV2101"])
    assert db.get_json_setting("muted", []) == []


async def test_briefing_time_validated(db):
    cmds = Commands(db, hooks={})
    await cmds.run("briefing", ["08:30"])
    assert db.get_setting("briefing_time") == "08:30"
    assert "Usage" in await cmds.run("briefing", ["25:00"])


async def test_pause_resume(db):
    cmds = Commands(db, hooks={})
    await cmds.run("pause", [])
    assert db.get_setting("paused") == "1"
    await cmds.run("resume", [])
    assert db.get_setting("paused") == "0"


async def test_callback_router_dispatch():
    seen = []
    r = CallbackRouter()

    async def approve(arg):
        seen.append(("approve", arg))
        return "ok"

    async def dl(arg):
        seen.append(("dl", arg))

    r.register("act:approve", approve)
    r.register("dl", dl)
    assert await r.dispatch("act:approve:12") == "ok"
    await r.dispatch("dl:1001:702")
    assert await r.dispatch("nope:1") is None
    assert seen == [("approve", "12"), ("dl", "1001:702")]


async def test_send_bytes_too_large_returns_false():
    bot = FakeBot()
    ok = await Notifier(bot, 42).send_bytes("big.zip", b"x" * (51 * 1024 * 1024))
    assert ok is False and "too large" in bot.sent[0][1]


def test_chunk_counts_utf16_units():
    from ops.telegram_out import utf16_len
    parts = chunk("🔴" * 3000)
    assert len(parts) == 2
    assert all(utf16_len(p) <= 4096 for p in parts)
    assert "".join(parts) == "🔴" * 3000



def _text_update(user_id, text, bot):
    return Update.de_json({"update_id": 2, "message": {"message_id": 2, "date": 0, "text": text,
                           "chat": {"id": user_id, "type": "private"},
                           "from": {"id": user_id, "is_bot": False, "first_name": "x"}}}, bot)


def test_plain_text_goes_to_ask_only_for_owner(env_file, db):
    from telegram import User
    app = build_app(load_settings(env_file), db, hooks={})
    app.bot._bot_user = User(1, "JarvisOps", True, username="jarvis_ops_bot")
    handlers = [h for group in app.handlers.values() for h in group]
    assert any(h.check_update(_text_update(42, "when are my assignments due", app.bot)) for h in handlers)
    assert not any(h.check_update(_text_update(999, "when are my assignments due", app.bot)) for h in handlers)


async def test_deadlines_and_scan_commands_use_hooks(db):
    async def deadlines():
        return "list"

    async def scan():
        return "scanning"

    cmds = Commands(db, hooks={"deadlines": deadlines, "scan": scan})
    assert await cmds.run("deadlines", []) == "list"
    assert await cmds.run("scan", []) == "scanning"
