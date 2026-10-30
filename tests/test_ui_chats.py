import json
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from core import groups, tg_group
from core.config import load_settings
from core.db import DB
from core.models import GroupMessage
from ops import telethon_main
from ui import chats, data
from ui.server import make_app
from ui.supervisor import OpsSupervisor, Prefs
from ui.tglogin import TelegramLogin

H = {"Host": "127.0.0.1:8765", "X-JARVIS-UI": "1"}
NOW = datetime.now(timezone.utc)
CLASS, PROJECT = -1001, -1002


class PasswordNeeded(Exception):
    pass


PasswordNeeded.__name__ = "SessionPasswordNeededError"


class BadCode(Exception):
    pass


BadCode.__name__ = "PhoneCodeInvalidError"


class FakeTg:
    def __init__(self, want_password=False, good_code="12345", authorised=False):
        self.want_password, self.good_code, self.authorised = want_password, good_code, authorised
        self.calls, self.connected = [], False

    async def connect(self):
        self.connected = True

    async def is_user_authorized(self):
        return self.authorised

    async def disconnect(self):
        self.connected = False

    async def send_code_request(self, phone):
        self.calls.append(("send", phone))
        return SimpleNamespace(phone_code_hash="HASH")

    async def sign_in(self, phone=None, code=None, phone_code_hash=None, password=None):
        self.calls.append(("sign_in", phone, code, phone_code_hash, password))
        if password is not None:
            return
        if code != self.good_code:
            raise BadCode()
        if self.want_password:
            raise PasswordNeeded()


@pytest.fixture
def settings(env_file):
    env_file.write_text(env_file.read_text() + "TELETHON_API_ID=1\nTELETHON_API_HASH=h\n")
    s = load_settings(env_file)
    s.state_dir.mkdir(parents=True, exist_ok=True)
    return s


# ---------- login wizard ----------
async def test_login_happy_path_never_stores_phone_or_code_and_clears_the_lock(settings):
    fake = FakeTg()
    w = TelegramLogin(settings, client_factory=lambda s: fake)
    assert (await w.start("+65 9123-4567"))["stage"] == "code_sent"
    assert telethon_main.login_lock_path(settings).exists() and fake.calls[0] == ("send", "+6591234567")
    assert (await w.code("1 2345"))["stage"] == "done"
    assert fake.calls[1] == ("sign_in", "+6591234567", "12345", "HASH", None)
    assert not telethon_main.login_lock_path(settings).exists() and fake.connected is False
    assert w._phone == "" and w._hash == ""


async def test_wrong_code_then_two_step_password(settings):
    fake = FakeTg(want_password=True)
    w = TelegramLogin(settings, client_factory=lambda s: fake)
    await w.start("+6591234567")
    bad = await w.code("00000")
    assert bad["stage"] == "code_sent" and "not right" in bad["error"]
    assert (await w.code("12345"))["stage"] == "password_needed"
    assert (await w.password("hunter2"))["stage"] == "done"
    assert fake.calls[-1][-1] == "hunter2"


async def test_login_validation_and_missing_keys(settings, env_file):
    w = TelegramLogin(settings, client_factory=lambda s: FakeTg())
    assert "international format" in (await w.start("hello"))["error"]
    assert (await w.code("1"))["error"] == "Ask for a code first."
    bare = load_settings(env_file)
    object.__setattr__(bare, "telethon_api_hash", None)
    assert "TELETHON_API_ID" in (await TelegramLogin(bare, client_factory=lambda s: FakeTg()).start("+6591234567"))["error"]


async def test_already_logged_in_and_cancel(settings):
    w = TelegramLogin(settings, client_factory=lambda s: FakeTg(authorised=True))
    assert (await w.start("+6591234567"))["stage"] == "done"
    w2 = TelegramLogin(settings, client_factory=lambda s: FakeTg())
    await w2.start("+6591234567")
    assert (await w2.cancel())["stage"] == "idle" and not telethon_main.login_lock_path(settings).exists()


# ---------- stages, messages, saving ----------
def db_with_groups(settings):
    db = DB(settings.db_path)
    db.init()
    return db


def put(db, chat, i, text, mins=-5, sender="Ben"):
    tg_group.store_message(db, GroupMessage(chat, i, sender, text, NOW + timedelta(minutes=mins)))


def test_setup_stage_progression(settings, env_file):
    db = db_with_groups(settings)
    bare = load_settings(env_file)
    object.__setattr__(bare, "telethon_api_id", None)
    st = chats.telegram_state(bare, db, True)
    assert (st["stage"], st["telethon"], st["health"]) == ("setup", "no_api_keys", "setup")
    assert chats.telegram_state(settings, db, True)["telethon"] == "login"
    telethon_main.session_path(settings).write_text("x")
    assert chats.telegram_state(settings, db, True)["telethon"] == "ready" and chats.telegram_state(settings, db, True)["stage"] == "setup"
    groups.add_bot_group(db, PROJECT, "FYP Team")  # the owner added the bot to a group: it appears to be ticked
    assert chats.telegram_state(settings, db, True)["stage"] == "pick"
    groups.save(db, [{"id": PROJECT, "label": "Project", "kind": "project"}])
    st = chats.telegram_state(settings, db, True)
    assert st["stage"] == "ready" and st["health"] == "live" and st["bot_groups"] == 1  # the bot in the group is enough
    assert chats.telegram_state(settings, db, False)["health"] == "offline"


def test_own_account_reader_health_when_the_bot_is_not_in_the_group(settings):
    db = db_with_groups(settings)
    telethon_main.session_path(settings).write_text("x")
    groups.save_dialogs(db, [{"id": PROJECT, "name": "FYP Team"}])
    groups.save(db, [{"id": PROJECT, "label": "Project", "kind": "project"}])
    assert chats.telegram_state(settings, db, True)["health"] == "stale"  # nothing has reported yet
    db.set_json_setting("telethon_status", {"state": "listening", "at": datetime.now(timezone.utc).isoformat()})
    assert chats.telegram_state(settings, db, True)["health"] == "live"
    db.set_json_setting("telethon_status", {"state": "login_needed", "at": datetime.now(timezone.utc).isoformat()})
    assert chats.telegram_state(settings, db, True)["health"] == "login"
    db.set_json_setting("telethon_status", {"state": "error", "at": datetime.now(timezone.utc).isoformat()})
    assert chats.telegram_state(settings, db, True)["health"] == "failing"


def test_messages_search_and_access_limits(settings):
    db = db_with_groups(settings)
    groups.save(db, [{"id": PROJECT, "label": "Project", "kind": "project"}])
    put(db, PROJECT, 1, "demo on Friday", mins=-60)
    put(db, PROJECT, 2, "who has the slides?", mins=-30, sender="Aisha")
    put(db, CLASS, 1, "secret class message")
    out = chats.messages(settings, db, PROJECT)
    assert out["ok"] and [m["text"] for m in out["messages"]] == ["demo on Friday", "who has the slides?"]
    assert out["messages"][1]["sender"] == "Aisha" and out["label"] == "Project"
    assert [m["text"] for m in chats.messages(settings, db, PROJECT, "slides")["messages"]] == ["who has the slides?"]
    assert chats.messages(settings, db, CLASS)["ok"] is False  # not one of the chosen groups


def test_connections_row_for_telegram(settings):
    db = db_with_groups(settings)
    row = {c["id"]: c for c in data.connections(settings, "running")}["telegram"]
    assert row["state"] == "unconfigured" and row["goto"] == "chats" and "JARVIS bot" in row["detail"]
    groups.add_bot_group(db, PROJECT, "FYP Team")
    assert {c["id"]: c for c in data.connections(settings, "running")}["telegram"]["state"] == "pick"
    groups.save(db, [{"id": PROJECT, "label": "Project", "kind": "project"}, {"id": CLASS, "label": "Class", "kind": "class"}])
    row = {c["id"]: c for c in data.connections(settings, "running")}["telegram"]
    assert row["state"] == "live" and row["color"] == "green" and "2 group" in row["detail"] and row["goto"] is None
    assert "never posts" in row["detail"]


# ---------- endpoints ----------
async def test_endpoints_save_only_your_own_dialogs_and_serve_messages(settings):
    db = db_with_groups(settings)
    groups.save_dialogs(db, [{"id": PROJECT, "name": "FYP Team"}, {"id": 5, "name": "Dave"}])
    put(db, PROJECT, 1, "hello team")
    sup = OpsSupervisor(settings, Prefs(settings.state_dir / "ui.json"))
    c = TestClient(TestServer(make_app(settings, sup, port=8765, db=db, tglogin=TelegramLogin(settings, client_factory=lambda s: FakeTg()))))
    await c.start_server()
    try:
        ov = await (await c.get("/api/chats", headers=H)).json()
        assert ov["stage"] == "pick" and ov["telethon"] == "login" and [d["name"] for d in ov["dialogs"]] == ["FYP Team", "Dave"] and ov["login"]["stage"] == "idle"
        post = lambda path, body: c.post(path, headers=H, data=json.dumps(body))  # noqa: E731
        r = await (await post("/api/chats/groups", {"groups": [{"id": PROJECT, "label": "Team", "kind": "project"},
                                                               {"id": 999, "label": "Not mine", "kind": "class"}]})).json()
        assert [g["id"] for g in r["groups"]] == [PROJECT]  # an id that is not in your own Telegram list is ignored
        assert (await post("/api/chats/groups", {"groups": "x"})).status == 400
        msgs = await (await c.get(f"/api/chats/messages?group={PROJECT}", headers=H)).json()
        assert msgs["ok"] and msgs["messages"][0]["text"] == "hello team"
        assert (await c.get("/api/chats/messages?group=abc", headers=H)).status == 400
        assert (await (await post("/api/telegram/login/start", {"phone": "+6591234567"})).json())["stage"] == "code_sent"
        assert (await (await post("/api/telegram/login/code", {"code": "12345"})).json())["stage"] == "done"
        assert (await post("/api/telegram/login/nonsense", {})).status == 404
    finally:
        await c.close()


def test_listener_message_is_reported_so_the_panel_can_say_why(settings):
    db = db_with_groups(settings)
    telethon_main.session_path(settings).write_text("x")
    db.set_json_setting("telethon_status", {"state": "error", "message": "FloodWaitError", "at": datetime.now(timezone.utc).isoformat()})
    st = chats.telegram_state(settings, db, True)
    assert st["listener"] == "error" and st["listener_message"] == "FloodWaitError"
    db.set_json_setting("telethon_status", {"state": "login_needed", "at": datetime.now(timezone.utc).isoformat()})
    st = chats.telegram_state(settings, db, True)
    assert st["telethon"] == "ready" and st["listener"] == "login_needed" and st["listener_message"] == ""


def test_overview_reports_reply_activity_only_for_reply_groups(settings):
    db = db_with_groups(settings)
    groups.save_dialogs(db, [{"id": PROJECT, "name": "FYP Team"}, {"id": CLASS, "name": "Class"}])
    groups.save(db, [{"id": PROJECT, "label": "Team", "kind": "reply"}, {"id": CLASS, "label": "Class", "kind": "class"}])
    db.conn.execute("insert into group_replies(ts, chat_id, in_msg_id, text) values (?,?,?,?)", (NOW.isoformat(), PROJECT, 1, "ok"))
    db.conn.execute("insert into group_replies(ts, chat_id, in_msg_id, text) values (?,?,?,?)",
                    ((NOW - timedelta(days=3)).isoformat(), PROJECT, 2, "old"))
    out = chats.overview(settings, db, True)
    by_id = {g["id"]: g for g in out["chosen"]}
    assert by_id[PROJECT]["replies_today"] == 1 and by_id[CLASS]["replies_today"] == 0
    assert out["paused"] is False and out["reply_status"] == {}
    db.set_setting("paused", "1")
    assert chats.overview(settings, db, True)["paused"] is True


async def test_endpoint_accepts_the_reply_kind(settings):
    db = db_with_groups(settings)
    groups.save_dialogs(db, [{"id": PROJECT, "name": "FYP Team"}])
    sup = OpsSupervisor(settings, Prefs(settings.state_dir / "ui.json"))
    c = TestClient(TestServer(make_app(settings, sup, port=8765, db=db)))
    await c.start_server()
    try:
        r = await (await c.post("/api/chats/groups", headers=H, data=json.dumps({"groups": [{"id": PROJECT, "label": "T", "kind": "reply"}]}))).json()
        assert r["groups"][0]["kind"] == "reply"
    finally:
        await c.close()
