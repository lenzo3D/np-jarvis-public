import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from core import groups, tg_group
from core.config import _parse_groups, load_settings
from core.models import Course, GroupMessage
from ops import briefing, telethon_main
from ops.api import make_app
from ops.deadline_sources import extract_chats
from ops.watcher import Services
from tests.fakes import Clock, FakeBS, FakeNotifier

NOW = datetime(2026, 10, 26, 23, 0, tzinfo=timezone.utc)
CLASS, PROJECT = -1001, -1002


def settings_with(env_file, extra=""):
    env_file.write_text(env_file.read_text() + extra)
    return load_settings(env_file)


def put(db, chat, i, text, mins=-30, sender="Ben"):
    tg_group.store_message(db, GroupMessage(chat, i, sender, text, NOW + timedelta(minutes=mins)))


# ---------- config + registry ----------
def test_group_ids_parse_with_optional_labels():
    assert _parse_groups("") == ([], {})
    assert _parse_groups("-100, -200:Project team ,300") == ([-100, -200, 300], {-200: "Project team"})


def test_configured_falls_back_to_secrets_then_db_overrides(db, env_file):
    s = settings_with(env_file, "GROUP_CHAT_IDS=-1001,-1002:FYP squad\n")
    assert groups.configured(s, db) == [{"id": -1001, "label": "Class chat", "kind": "class"},
                                        {"id": -1002, "label": "FYP squad", "kind": "other"}]
    groups.save(db, [{"id": -1002, "label": " Project team ", "kind": "project"}, {"id": "x"}, {"id": -1002, "label": "dup"},
                     {"id": -1001, "label": "", "kind": "nonsense"}])
    got = groups.configured(s, db)
    assert got == [{"id": -1002, "label": "Project team", "kind": "project"}, {"id": -1001, "label": "Group 1", "kind": "other"}]
    assert groups.find(s, db, "project")["id"] == -1002 and groups.find(s, db, "PROJECT TEAM")["id"] == -1002
    assert groups.find(s, db, "-1001")["id"] == -1001 and groups.find(s, db, "nope") is None
    assert groups.label_for(s, db, -1002) == "Project team" and groups.label_for(s, db, -5) == "Group 5"


def test_search_recent_and_stats_are_per_group(db):
    put(db, CLASS, 1, "Tutorial 5 is due Friday")
    put(db, PROJECT, 1, "Tutorial slides for the project demo")
    put(db, PROJECT, 2, "demo at 3pm")
    assert [m.chat_id for m in tg_group.search(db, "tutorial")] == [PROJECT, CLASS] or len(tg_group.search(db, "tutorial")) == 2
    assert [m.chat_id for m in tg_group.search(db, "tutorial", chat_id=PROJECT)] == [PROJECT]
    assert [m.msg_id for m in tg_group.latest(db, PROJECT)] == [1, 2]
    assert tg_group.stats(db)[PROJECT]["count"] == 2 and tg_group.count_since(db, NOW - timedelta(hours=1), CLASS) == 1


# ---------- the live listener ----------
class FakeTg:
    def __init__(self, authorised=True, history=None):
        self.authorised, self.history, self.handlers, self.connected = authorised, history or {}, [], False

    async def connect(self):
        self.connected = True

    def is_connected(self):
        return self.connected

    async def is_user_authorized(self):
        return self.authorised

    async def disconnect(self):
        self.connected = False

    async def iter_messages(self, chat, limit):
        for m in self.history.get(chat, []):
            yield m

    async def iter_dialogs(self, limit):
        for cid, name in ((CLASS, "NP Class 2A"), (PROJECT, "FYP Team"), (7, "Dave")):
            yield SimpleNamespace(id=cid, name=name, is_group=cid != 7, is_channel=False)

    def add_event_handler(self, cb, _event):
        self.handlers.append(cb)


def tg_msg(i, text, sender="Ben"):
    return SimpleNamespace(id=i, message=text, date=NOW, sender=SimpleNamespace(first_name=sender, last_name=None))


async def until(cond, timeout=3.0):
    end = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def test_listener_waits_for_login_then_mirrors_chosen_groups_live(db, env_file):
    s = settings_with(env_file, "TELETHON_API_ID=1\nTELETHON_API_HASH=h\n")
    fake = FakeTg(authorised=False, history={PROJECT: [tg_msg(1, "old msg")]})
    saved = {"yes": False}
    task = asyncio.create_task(telethon_main.listen(s, db, client_factory=lambda _s: fake, poll=0.02,
                                                    ready=lambda _s: saved["yes"]))
    try:
        await until(lambda: (db.get_json_setting("telethon_status", {}) or {}).get("state") == "login_needed")
        assert fake.connected is False  # never touched the session before a login existed
        saved["yes"], fake.authorised = True, True  # the user finished the login in the panel
        groups.save(db, [{"id": PROJECT, "label": "Project", "kind": "project"}])
        await until(lambda: (db.get_json_setting("telethon_status", {}) or {}).get("state") == "listening")
        await until(lambda: tg_group.stats(db).get(PROJECT, {}).get("count") == 1)  # history backfilled
        assert [d["name"] for d in groups.dialogs(db)] == ["NP Class 2A", "FYP Team"]  # groups only, not private chats
        # a live message in the chosen group is stored, one in an unchosen group is ignored
        await fake.handlers[0](SimpleNamespace(chat_id=PROJECT, message=SimpleNamespace(
            id=2, message="demo moved to 4pm", date=NOW, sender=None, sender_id=9, get_sender=_noop)))
        await fake.handlers[0](SimpleNamespace(chat_id=CLASS, message=SimpleNamespace(
            id=3, message="should be ignored", date=NOW, sender=None, sender_id=9, get_sender=_noop)))
        assert [m.text for m in tg_group.latest(db, PROJECT)] == ["old msg", "demo moved to 4pm"]
        assert tg_group.stats(db).get(CLASS) is None
        # choosing another group later is picked up without a restart
        fake.history[CLASS] = [tg_msg(10, "class news")]
        groups.save(db, [{"id": PROJECT, "label": "Project", "kind": "project"}, {"id": CLASS, "label": "Class", "kind": "class"}])
        await until(lambda: tg_group.stats(db).get(CLASS, {}).get("count") == 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert fake.connected is False


async def _noop():
    return None


async def test_listener_reports_missing_api_keys(db, env_file):
    await telethon_main.listen(load_settings(env_file), db)
    assert db.get_json_setting("telethon_status", {})["state"] == "no_api_keys"


async def test_listener_survives_errors_and_retries(db, env_file):
    s = settings_with(env_file, "TELETHON_API_ID=1\nTELETHON_API_HASH=h\n")
    calls = []

    class Boom(FakeTg):
        async def connect(self):
            calls.append(1)
            raise ConnectionError("offline")

    task = asyncio.create_task(telethon_main.listen(s, db, client_factory=lambda _s: Boom(), poll=0.02, ready=lambda _s: True))
    try:
        await until(lambda: len(calls) >= 2)
        assert db.get_json_setting("telethon_status", {})["state"] == "error"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# ---------- summaries and extraction per group ----------
def svc(db, env_file, reply, extra=""):
    s = Services(settings=settings_with(env_file, extra), db=db, sessions=None, bs=FakeBS(), notifier=FakeNotifier(), clock=Clock(NOW))
    s.bs.course_list = [Course(1, "26S2-X", "X")]
    calls = []

    async def run_text(prompt, input_text, model="sonnet", timeout=180, run_as=None):
        calls.append((prompt, input_text))
        return reply if isinstance(reply, str) else json.dumps(reply)

    s.extra["run_text"], s.extra["calls"] = run_text, calls
    return s


async def test_each_group_is_summarised_separately_with_its_own_name(db, env_file):
    s = svc(db, env_file, {"summary": ["Demo moved"], "actions": [{"text": "Bring laptop", "due": "Fri"}]})
    groups.save(db, [{"id": CLASS, "label": "Class", "kind": "class"}, {"id": PROJECT, "label": "Project team", "kind": "project"}])
    put(db, CLASS, 1, "class chatter")
    put(db, PROJECT, 1, "project chatter")
    await briefing.scheduled_group_summary(s)
    texts = [t for t, _ in s.notifier.messages]
    assert any(t.startswith("Class summary") for t in texts) and any(t.startswith("Project team summary") for t in texts)
    (c1, c2) = s.extra["calls"]
    assert "class chatter" in c1[1] and "project chatter" not in c1[1] and 'called "Class"' in c1[0]
    assert "project chatter" in c2[1] and "class chatter" not in c2[1] and "project group" in c2[0]
    assert db.get_setting(f"last_summary:{PROJECT}") and db.get_setting(f"last_summary:{CLASS}")
    assert set(db.get_json_setting("last_group_summary", [])) == {"Class: Demo moved", "Project team: Demo moved"}
    assert [t for t in texts if "Bring laptop" in t]  # actions still become alerts


async def test_extraction_sends_each_group_as_its_own_labelled_block(db, env_file):
    s = svc(db, env_file, {"deadlines": []})
    groups.save(db, [{"id": CLASS, "label": "Class", "kind": "class"}, {"id": PROJECT, "label": "FYP team", "kind": "project"}])
    put(db, CLASS, 1, "quiz on 10 Nov")
    put(db, PROJECT, 1, "report due 20 Nov")
    await extract_chats(s)
    sent = s.extra["calls"][0][1]
    assert "Telegram class group: Class" in sent and "Telegram project group: FYP team" in sent
    assert "quiz on 10 Nov" in sent and "report due 20 Nov" in sent


# ---------- API ----------
async def test_group_api_filters_by_group_name_kind_or_id(db, env_file):
    s = svc(db, env_file, "{}")
    groups.save(db, [{"id": CLASS, "label": "Class", "kind": "class"}, {"id": PROJECT, "label": "Project team", "kind": "project"}])
    put(db, CLASS, 1, "tutorial for class")
    put(db, PROJECT, 1, "tutorial for project")
    c = TestClient(TestServer(make_app(s)))
    await c.start_server()
    try:
        chats = await (await c.get("/group/chats")).json()
        assert [(g["label"], g["messages"]) for g in chats] == [("Class", 1), ("Project team", 1)]
        both = await (await c.get("/group/search?q=tutorial")).json()
        assert {m["group"] for m in both} == {"Class", "Project team"}
        proj = await (await c.get("/group/search?q=tutorial&group=project")).json()
        assert [m["group"] for m in proj] == ["Project team"]
        recent = await (await c.get("/group/recent?hours=24&group=Class")).json()
        assert [m["group"] for m in recent] == ["Class"]
        bad = await c.get("/group/recent?group=gym")
        assert bad.status == 400 and (await bad.json())["groups"] == ["Class", "Project team"]
    finally:
        await c.close()


def test_session_ready_needs_a_session_file_and_no_fresh_login_lock(env_file):
    import os
    s = load_settings(env_file)
    s.state_dir.mkdir(parents=True, exist_ok=True)
    assert telethon_main.session_ready(s) is False
    telethon_main.session_path(s).write_text("x")
    assert telethon_main.session_ready(s) is True
    lock = telethon_main.login_lock_path(s)
    lock.write_text("x")
    assert telethon_main.session_ready(s) is False  # a login is in progress
    old = __import__("time").time() - 3600
    os.utime(lock, (old, old))
    assert telethon_main.session_ready(s) is True  # stale lock from a crashed login is ignored
