"""In group chats the bot is a silent reader: it records chosen groups and does nothing else."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from telegram import Update, User

from core import groups, tg_group
from core.config import load_settings
from ops.bot import Notifier, build_app

OWNER, STRANGER = 42, 999
CLASS, PROJECT, OTHER = -1001, -1002, -1009


@pytest.fixture
def app(env_file, db):
    a = build_app(load_settings(env_file), db, hooks={"ask": _never, "status": _never}, token="123:abc")
    a.bot._bot_user = User(1, "JarvisOps", True, username="jarvis_ops_bot")
    groups.save(db, [{"id": CLASS, "label": "Class", "kind": "class"}, {"id": PROJECT, "label": "Project", "kind": "project"}])
    return a


async def _never(*a, **k):
    raise AssertionError("the bot must not run commands or answer questions from a group")


def handlers(app, group=None):
    return [h for g, hs in app.handlers.items() if group in (None, g) for h in hs]


def msg(app, chat_id, chat_type, user_id, text, mid=7, **extra):
    entities = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}] if text.startswith("/") else []
    if "@jarvis_ops_bot" in text:
        entities.append({"type": "mention", "offset": text.index("@"), "length": len("@jarvis_ops_bot")})
    body = {"message_id": mid, "date": int(datetime(2026, 10, 28, tzinfo=timezone.utc).timestamp()), "text": text, "entities": entities,
            "chat": {"id": chat_id, "type": chat_type, "title": "FYP Team"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Aisha", "last_name": "Tan"}, **extra}
    return Update.de_json({"update_id": 1, "message": body}, app.bot)


GROUP_TEXTS = ["/status", "/deadlines@jarvis_ops_bot", "/new", "what is due this week?", "@jarvis_ops_bot what is due?",
               "hey jarvis tell me my grades", "/login brightspace"]


@pytest.mark.parametrize("who", [OWNER, STRANGER])
@pytest.mark.parametrize("chat_type", ["group", "supergroup"])
def test_no_command_or_question_handler_ever_fires_in_a_group(app, who, chat_type):
    """Even the owner cannot make the bot act (and so reply) inside a group."""
    acting = handlers(app, group=0)  # group 0 holds everything that can answer
    for text in GROUP_TEXTS:
        assert not any(h.check_update(msg(app, CLASS, chat_type, who, text)) for h in acting), text
    reply = msg(app, CLASS, chat_type, who, "yes please",
                reply_to_message={"message_id": 3, "date": 0, "chat": {"id": CLASS, "type": chat_type},
                                  "from": {"id": 1, "is_bot": True, "first_name": "JarvisOps"}, "text": "x"})
    assert not any(h.check_update(reply) for h in acting)


def test_the_same_requests_still_work_in_the_private_chat(app):
    acting = handlers(app, group=0)
    assert any(h.check_update(msg(app, OWNER, "private", OWNER, "/status")) for h in acting)
    assert any(h.check_update(msg(app, OWNER, "private", OWNER, "what is due this week?")) for h in acting)
    assert not any(h.check_update(msg(app, STRANGER, "private", STRANGER, "/status")) for h in acting)


async def test_group_buttons_do_nothing(app):
    router_calls, answered = [], []

    class Q:
        data = "act:approve:1"
        from_user = SimpleNamespace(id=OWNER)
        message = SimpleNamespace(chat=SimpleNamespace(type="supergroup"))

        async def answer(self):
            answered.append(1)

    async def dispatch(data):
        router_calls.append(data)

    app.bot_data["router"].dispatch = dispatch
    cb = next(h for h in handlers(app, 0) if type(h).__name__ == "CallbackQueryHandler")
    await cb.callback(SimpleNamespace(callback_query=Q()), SimpleNamespace())
    assert router_calls == [] and answered == []  # not even acknowledged


# ---------- reading ----------
async def record(app, update):
    rec = next(h for h in handlers(app, 1) if type(h).__name__ == "MessageHandler")
    assert rec.check_update(update)
    await rec.callback(update, SimpleNamespace(bot=None))


async def test_messages_in_chosen_groups_are_recorded_with_the_sender_name(app, db):
    await record(app, msg(app, PROJECT, "supergroup", STRANGER, "Demo moved to Friday 3pm"))
    await record(app, msg(app, PROJECT, "supergroup", OWNER, "Noted, thanks", mid=8))
    (a, b) = tg_group.latest(db, PROJECT)
    assert (a.sender, a.text, a.msg_id) == ("Aisha Tan", "Demo moved to Friday 3pm", 7) and b.text == "Noted, thanks"
    await record(app, msg(app, PROJECT, "supergroup", STRANGER, "Demo moved to Friday 3pm"))  # same message again
    assert len(tg_group.latest(db, PROJECT)) == 2  # never stored twice


async def test_unchosen_groups_and_private_chats_are_not_recorded(app, db):
    await record(app, msg(app, OTHER, "group", STRANGER, "gym buddies chat"))
    assert tg_group.stats(db) == {}
    private = msg(app, OWNER, "private", OWNER, "a private note")
    assert not any(h.check_update(private) for h in handlers(app, group=1) if type(h).__name__ == "MessageHandler")


async def test_captions_are_read_and_empty_or_service_messages_are_skipped(app, db):
    cap = Update.de_json({"update_id": 2, "message": {"message_id": 9, "date": 0, "caption": "Slides for Thursday",
                         "photo": [{"file_id": "x", "file_unique_id": "y", "width": 1, "height": 1}],
                         "chat": {"id": PROJECT, "type": "group", "title": "t"}, "from": {"id": STRANGER, "is_bot": False, "first_name": "Ben"}}}, app.bot)
    await record(app, cap)
    assert [m.text for m in tg_group.latest(db, PROJECT)] == ["Slides for Thursday"]
    joined = Update.de_json({"update_id": 3, "message": {"message_id": 10, "date": 0, "new_chat_members": [{"id": 5, "is_bot": False, "first_name": "Z"}],
                            "chat": {"id": PROJECT, "type": "group", "title": "t"}, "from": {"id": STRANGER, "is_bot": False, "first_name": "Ben"}}}, app.bot)
    rec = next(h for h in handlers(app, 1) if type(h).__name__ == "MessageHandler")
    assert not rec.check_update(joined)


# ---------- who may add the bot to a group ----------
def membership(app, chat_id, status, adder):
    return Update.de_json({"update_id": 5, "my_chat_member": {
        "chat": {"id": chat_id, "type": "supergroup", "title": "FYP Team"}, "from": {"id": adder, "is_bot": False, "first_name": "x"},
        "date": 0, "old_chat_member": {"status": "left", "user": {"id": 1, "is_bot": True, "first_name": "J"}},
        "new_chat_member": {"status": status, "user": {"id": 1, "is_bot": True, "first_name": "J"}, **({"until_date": 0} if status == "kicked" else {})}}}, app.bot)


async def run_membership(app, update):
    left = []

    async def leave_chat(chat_id):
        left.append(chat_id)

    h = next(h for h in handlers(app, 1) if type(h).__name__ == "ChatMemberHandler")
    assert h.check_update(update)
    await h.callback(update, SimpleNamespace(bot=SimpleNamespace(leave_chat=leave_chat)))
    return left


async def test_bot_added_by_the_owner_is_remembered_and_stays(app, db):
    assert await run_membership(app, membership(app, PROJECT, "member", OWNER)) == []
    assert groups.bot_groups(db) == [{"id": PROJECT, "name": "FYP Team"}]
    assert [d["id"] for d in groups.dialogs(db)] == [PROJECT]  # now selectable in the panel


async def test_bot_added_by_anyone_else_leaves_at_once_and_is_not_remembered(app, db):
    assert await run_membership(app, membership(app, OTHER, "member", STRANGER)) == [OTHER]
    assert groups.bot_groups(db) == []


async def test_removed_from_a_group_forgets_it(app, db):
    await run_membership(app, membership(app, PROJECT, "member", OWNER))
    assert await run_membership(app, membership(app, PROJECT, "kicked", STRANGER)) == []
    assert groups.bot_groups(db) == []


# ---------- sending ----------
async def test_everything_the_bot_sends_goes_to_the_owners_private_chat_only():
    sent = []

    class Bot:
        async def send_message(self, chat_id, *a, **k):
            sent.append(chat_id)
            return SimpleNamespace(message_id=1)

        async def edit_message_text(self, text, chat_id=None, **k):
            sent.append(chat_id)

        async def send_document(self, chat_id, *a, **k):
            sent.append(chat_id)

    n = Notifier(Bot(), OWNER)
    await n.send("alert")
    await n.edit(1, "progress")
    await n.send_bytes("a.txt", b"x")
    assert sent == [OWNER, OWNER, OWNER]
    assert n.chat_id == OWNER and OWNER > 0  # a user id (private chat), never a group id
