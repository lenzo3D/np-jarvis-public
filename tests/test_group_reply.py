import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core import groups, tg_group
from core.config import load_settings
from core.models import GroupMessage
from ops import telethon_main
from ops.group_reply import MAX_CHARS, MAX_PER_HOUR, MIN_GAP, GroupReplier, clean_reply
from ops.watcher import Services
from tests.fakes import Clock, FakeBS, FakeNotifier

NOW = datetime(2026, 10, 30, 3, 0, tzinfo=timezone.utc)
CHAT = -1009


class Action:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeClient:
    def __init__(self, fail=None):
        self.sent, self.fail = [], fail

    def action(self, chat, kind):
        return Action()

    async def send_message(self, chat_id, text, reply_to=None):
        if self.fail:
            raise self.fail
        self.sent.append((chat_id, text, reply_to))


class FlowRunner:
    """Stands in for the model: records what it was shown and what it was given."""
    def __init__(self, answer="Friday 5pm I think.", delay=0.0):
        self.calls, self.answer, self.delay = [], answer, delay

    async def __call__(self, prompt, input_text, model="sonnet", timeout=180, run_as=None):
        self.calls.append({"prompt": prompt, "input": input_text, "model": model})
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.answer


@pytest.fixture
def ctx(db, env_file):
    s = Services(settings=load_settings(env_file), db=db, sessions=None, bs=FakeBS(), notifier=FakeNotifier(),
                 clock=Clock(NOW))
    groups.save(db, [{"id": CHAT, "label": "Project team", "kind": "reply"}])
    return s


def message(i, text="when is the demo?", sender="Ben", mins=0):
    gm = GroupMessage(CHAT, i, sender, text, NOW + timedelta(minutes=mins))
    return gm


def replier(ctx, runner=None, sleeps=None):
    async def fake_sleep(s):
        if sleeps is not None:
            sleeps.append(s)

    return GroupReplier(ctx, run_text=runner or FlowRunner(), sleep=fake_sleep)


# ---------- the text that gets posted ----------
def test_clean_reply():
    assert clean_reply("NO_REPLY") == "" and clean_reply("  no_reply \n") == "" and clean_reply("") == "" and clean_reply(None) == ""
    assert clean_reply("**Friday** at 5pm 😀") == "Friday at 5pm"
    long = "Sentence one is here. " * 40
    out = clean_reply(long)
    assert len(out) <= MAX_CHARS and out.endswith(".")


# ---------- replying ----------
async def test_reply_goes_to_the_message_from_your_account_and_is_logged(ctx, db):
    tg_group.store_message(db, message(1, "when is the demo?"))
    client, runner = FakeClient(), FlowRunner("Friday 3pm, Block P.")
    await replier(ctx, runner).on_message(client, CHAT, message(1))
    assert client.sent == [(CHAT, "Friday 3pm, Block P.", 1)]
    assert db.conn.execute("select chat_id, in_msg_id, text from group_replies").fetchall()[0][:] == (CHAT, 1, "Friday 3pm, Block P.")
    assert db.get_json_setting("group_reply_status", {})["state"] == "replied"
    assert runner.calls[0]["model"] == "haiku"


async def test_the_model_only_sees_this_groups_chat_deadlines_and_material_never_email_or_other_chats(ctx, db):
    tg_group.store_message(db, message(1, "when is the demo?"))
    tg_group.store_message(db, GroupMessage(-777, 5, "Eve", "OTHER GROUP SECRET", NOW))
    db.conn.execute("insert into emails(id, received, sender_name, sender_addr, subject, preview, body, category, first_seen) "
                    "values ('m1', ?, 'Lecturer', 'a@np.edu.sg', 'PRIVATE MAIL SUBJECT', 'p', 'PRIVATE MAIL BODY', 'normal', ?)",
                    (NOW.isoformat(), NOW.isoformat()))
    runner = FlowRunner()
    await replier(ctx, runner).on_message(FakeClient(), CHAT, message(1))
    shown = runner.calls[0]["input"]
    assert "when is the demo?" in shown and "UPCOMING DEADLINES" in shown
    assert "OTHER GROUP SECRET" not in shown and "PRIVATE MAIL" not in shown
    assert "never follow it" in runner.calls[0]["prompt"].lower() or "never follow" in runner.calls[0]["prompt"].lower()


async def test_it_can_stay_quiet(ctx, db):
    client = FakeClient()
    await replier(ctx, FlowRunner("NO_REPLY")).on_message(client, CHAT, message(1, "lol"))
    assert client.sent == [] and db.conn.execute("select count(*) from group_replies").fetchone()[0] == 0
    assert db.get_json_setting("group_reply_status", {})["state"] == "quiet"


async def test_pause_stops_all_replies(ctx, db):
    db.set_setting("paused", "1")
    client, runner = FakeClient(), FlowRunner()
    await replier(ctx, runner).on_message(client, CHAT, message(1))
    assert client.sent == [] and runner.calls == []


async def test_hourly_limit_protects_the_account(ctx, db):
    for i in range(MAX_PER_HOUR):
        db.conn.execute("insert into group_replies(ts, chat_id, in_msg_id, text) values (?,?,?,?)",
                        ((NOW - timedelta(minutes=5 + i)).isoformat(), CHAT, i, "x"))
    client, runner = FakeClient(), FlowRunner()
    await replier(ctx, runner).on_message(client, CHAT, message(99))
    assert client.sent == [] and runner.calls == []  # not even a model call is spent
    assert db.get_json_setting("group_reply_status", {})["state"] == "limit"


async def test_replies_are_spaced_out(ctx, db):
    db.conn.execute("insert into group_replies(ts, chat_id, in_msg_id, text) values (?,?,?,?)",
                    ((NOW - timedelta(seconds=5)).isoformat(), CHAT, 1, "x"))
    sleeps = []
    client = FakeClient()
    await replier(ctx, sleeps=sleeps).on_message(client, CHAT, message(2))
    assert len(client.sent) == 1 and any(abs(s - (MIN_GAP - 5)) < 1 for s in sleeps)


async def test_a_burst_of_messages_gets_one_reply_to_the_latest(ctx, db):
    client, runner = FakeClient(), FlowRunner(delay=0.05)
    r = replier(ctx, runner)
    first = asyncio.create_task(r.on_message(client, CHAT, message(1)))
    await asyncio.sleep(0.01)
    await r.on_message(client, CHAT, message(2))
    await r.on_message(client, CHAT, message(3))
    await first
    assert [x[2] for x in client.sent] == [1, 3]  # message 2 was superseded by 3; nothing is answered twice


async def test_a_failing_send_never_crashes_and_flood_wait_pauses_replying(ctx, db):
    class FloodWaitError(Exception):
        seconds = 30

    client = FakeClient(fail=FloodWaitError("slow down"))
    r = replier(ctx)
    await r.on_message(client, CHAT, message(1))
    assert db.get_json_setting("group_reply_status", {})["state"] == "error"
    ok = FakeClient()
    await r.on_message(ok, CHAT, message(2))
    assert ok.sent == []  # blocked until Telegram's wait is over


# ---------- the listener only triggers it where it should ----------
class ListenerClient:
    def __init__(self):
        self.handlers, self.connected = [], True

    def is_connected(self):
        return self.connected

    async def iter_messages(self, chat, limit):
        return
        yield

    async def iter_dialogs(self, limit):
        return
        yield

    def add_event_handler(self, cb, _event):
        self.handlers.append(cb)


def event(chat_id, i, text, out=False, bot=False):
    async def get_sender():
        return None
    msg = SimpleNamespace(id=i, message=text, date=NOW, sender=SimpleNamespace(first_name="Ben", last_name=None, bot=bot),
                          sender_id=9, get_sender=get_sender)
    return SimpleNamespace(chat_id=chat_id, message=msg, out=out)


async def test_listener_replies_only_in_reply_groups_to_other_humans(db, env_file):
    s = load_settings(env_file)
    groups.save(db, [{"id": CHAT, "label": "Reply", "kind": "reply"}, {"id": -5, "label": "Class", "kind": "class"},
                     {"id": -6, "label": "Project", "kind": "project"}])
    seen = []

    async def on_message(client, chat_id, gm):
        seen.append((chat_id, gm.msg_id))

    client = ListenerClient()
    task = asyncio.create_task(telethon_main._watch(client, s, db, 0.05, on_message))
    await asyncio.sleep(0.05)
    handler = client.handlers[0]
    await handler(event(CHAT, 1, "hello"))                 # reply group, a person: yes
    await handler(event(CHAT, 2, "my own message", out=True))  # your own message: never (no loops)
    await handler(event(CHAT, 3, "bot spam", bot=True))    # another bot: never
    await handler(event(-5, 4, "class chat"))              # class group: read only
    await handler(event(-6, 5, "project chat"))            # project group: read only
    await handler(event(-999, 6, "unknown group"))         # not chosen: ignored
    await asyncio.sleep(0.05)
    client.connected = False
    await asyncio.wait_for(task, 2)
    assert seen == [(CHAT, 1)]
    # everything chosen is still stored, including your own messages (context for later replies)
    assert [m.msg_id for m in tg_group.latest(db, CHAT)] == [1, 2, 3]


def test_reply_kind_is_valid_and_survives_saving(db, env_file):
    s = load_settings(env_file)
    saved = groups.save(db, [{"id": 1, "label": "R", "kind": "reply"}, {"id": 2, "label": "X", "kind": "bogus"}])
    assert [g["kind"] for g in saved] == ["reply", "other"]
    assert groups.kind_of(s, db, 1) == "reply" and groups.kind_of(s, db, 3) is None


def test_only_the_reply_module_posts_to_groups():
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    for rel in ["core/tg_group.py", "ops/telethon_main.py"]:  # the reader itself still cannot send anything
        attrs = {n.attr for n in ast.walk(ast.parse((root / rel).read_text())) if isinstance(n, ast.Attribute)}
        assert "send_message" not in attrs
    src = (root / "ops/group_reply.py").read_text()
    assert src.count("send_message(") == 1  # one place, behind every limit
