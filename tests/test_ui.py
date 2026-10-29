import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest
from aiohttp.test_utils import TestClient, TestServer

from core.config import load_settings
from core.db import DB
from ui import data
from ui.chat import WebChat
from ui.jobs import Jobs
from ui.server import make_app
from ui.supervisor import OpsSupervisor, Prefs

PORT = 8765
H = {"Host": f"127.0.0.1:{PORT}", "X-JARVIS-UI": "1"}
SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]


@pytest.fixture
def settings(env_file):
    s = load_settings(env_file)
    s.state_dir.mkdir(parents=True, exist_ok=True)
    return s


def supervisor(settings, command=SLEEP):
    return OpsSupervisor(settings, Prefs(settings.state_dir / "ui.json"), command=command)


async def fake_runner(settings, prompt, session_id):
    yield "session", "sess-1"
    yield "text", "**Checking** the calendar"
    yield "result", "## Done\n**Quiz 2** is on Friday."


async def client(settings, sup, chat=None, jobs=None):
    app = make_app(settings, sup, chat or WebChat(settings, runner=fake_runner), jobs or Jobs(), port=PORT)
    c = TestClient(TestServer(app))
    await c.start_server()
    return c


async def test_guard_rejects_foreign_host_origin_and_missing_header(settings):
    c = await client(settings, supervisor(settings))
    try:
        assert (await c.get("/api/state", headers={"Host": "evil.example"})).status == 403
        assert (await c.get("/api/state", headers={"Host": H["Host"]})).status == 200
        assert (await c.post("/api/chat/reset", headers={"Host": H["Host"]})).status == 403  # no CSRF header
        bad = {**H, "Origin": "http://evil.example"}
        assert (await c.post("/api/chat/reset", headers=bad)).status == 403
        ok = {**H, "Origin": f"http://127.0.0.1:{PORT}"}
        assert (await c.post("/api/chat/reset", headers=ok)).status == 200
    finally:
        await c.close()


async def test_index_and_static_are_served(settings):
    c = await client(settings, supervisor(settings))
    try:
        r = await c.get("/", headers={"Host": H["Host"]})
        assert r.status == 200 and "J.A.R.V.I.S. Command Center" in await r.text()
        assert (await c.get("/static/app.js", headers={"Host": H["Host"]})).status == 200
    finally:
        await c.close()


async def test_start_stop_lifecycle_and_log(settings):
    sup = supervisor(settings)
    try:
        assert sup.info()["mode"] == "stopped"
        res = await sup.start()
        assert res["ok"] and sup.managed_running and sup.info()["pid"]
        assert (await sup.start())["message"] == "JARVIS is already running."
        assert sup.mode() in ("starting", "unresponsive")  # no ops socket in this test
        assert (await sup.stop())["ok"] and not sup.managed_running and sup.info()["mode"] == "stopped"
        assert not sup.pid_path.exists()
    finally:
        await sup.stop()


async def test_start_refuses_when_something_else_owns_the_socket(settings, monkeypatch):
    sup = supervisor(settings)
    monkeypatch.setattr("ui.supervisor.socket_alive", lambda p: True)
    res = await sup.start()
    assert not res["ok"] and "outside this panel" in res["message"]
    assert sup.info()["mode"] == "external"
    assert not (await sup.stop())["ok"]


async def test_crash_is_restarted_when_wanted(settings, monkeypatch):
    sup = supervisor(settings, [sys.executable, "-c", "import sys; sys.exit(3)"])
    monkeypatch.setattr("ui.supervisor.asyncio.sleep", _fast_sleep())
    await sup.start()
    for _ in range(200):
        if sup.restarts >= 1:
            break
        await asyncio.sleep(0)
    assert sup.restarts >= 1
    await sup.stop()


def _fast_sleep():
    real = asyncio.sleep

    async def sleep(_t, *a):
        await real(0.01)
    return sleep


async def test_adopt_takes_back_a_running_ops(settings, monkeypatch):
    first = supervisor(settings)
    await first.start()
    second = supervisor(settings)
    monkeypatch.setattr("ui.supervisor.socket_alive", lambda p: True)
    second.adopt()
    assert second.managed_running and second.desired
    monkeypatch.undo()
    await second.stop()
    assert not first.managed_running


async def test_ops_endpoints_and_prefs(settings):
    sup = supervisor(settings)
    c = await client(settings, sup)
    try:
        r = await c.post("/api/ops/start", headers=H)
        assert r.status == 200 and (await r.json())["ok"]
        assert (await c.post("/api/ops/nonsense", headers=H)).status == 404
        state = await (await c.get("/api/state", headers=H)).json()
        assert state["ops"]["pid"] and "checks" in state and state["ops"]["autostart"] is True
        assert isinstance(state["version"], str) and state["version"]
        assert state["identity"]["address"] == "Sir" and "name" in state["identity"]
        await c.post("/api/prefs", headers=H, data=json.dumps({"autostart": False}))
        assert sup.info()["autostart"] is False
        assert (await c.post("/api/ops/stop", headers=H)).status == 200
    finally:
        await sup.stop()
        await c.close()


async def test_chat_requires_running_ops_then_streams_clean_text(settings, monkeypatch):
    sup = supervisor(settings)
    c = await client(settings, sup)
    try:
        r = await c.post("/api/chat", headers=H, data=json.dumps({"text": "hi"}))
        assert r.status == 409 and "not running" in (await r.json())["error"]
        monkeypatch.setattr(sup, "mode", lambda: "running")
        r = await c.post("/api/chat", headers=H, data=json.dumps({"text": "when is quiz 2?"}))
        events = [json.loads(line) for line in (await r.text()).splitlines()]
        assert events[0] == {"type": "progress", "text": "Checking the calendar"}
        assert events[-1] == {"type": "final", "text": "Done\nQuiz 2 is on Friday."}
        assert (settings.state_dir / "web_chat.json").exists()
        assert (await c.post("/api/chat", headers=H, data=json.dumps({"text": " "}))).status == 400
    finally:
        await c.close()


async def test_chat_resumes_then_falls_back_when_conversation_is_gone(settings):
    calls = []

    async def runner(s, prompt, session_id):
        calls.append(session_id)
        if session_id:
            from ops.claude_runner import ClaudeError
            raise ClaudeError("No conversation found")
        yield "session", "new"
        yield "result", "fresh answer"

    chat = WebChat(settings, runner=runner, now=lambda: 1000.0)
    chat._remember("old")
    out = [e async for e in chat.run("hello")]
    assert calls == ["old", None] and out[-1] == {"type": "final", "text": "fresh answer"}
    chat.reset()
    assert chat._session() is None


async def test_chat_session_expires_after_idle(settings):
    t = [1000.0]
    chat = WebChat(settings, runner=fake_runner, now=lambda: t[0])
    chat._remember("abc")
    assert chat._session() == "abc"
    t[0] += 7 * 3600
    assert chat._session() is None


async def test_jobs_run_whitelisted_commands_only(settings):
    jobs = Jobs({"hello": {"label": "Say hello", "cmd": [sys.executable, "-c", "print('hi from job')"]}})
    assert (await jobs.start("rm -rf /"))["ok"] is False
    assert (await jobs.start("hello"))["ok"]
    assert (await jobs.start("hello"))["ok"] is False  # one at a time
    await jobs._task
    st = jobs.status()
    assert st["state"] == "done" and "hi from job" in st["output"]


def seed(settings):
    db = DB(settings.db_path)
    db.init()
    db.set_json_setting("course_names", {"26S2-731IV_013686": "INVESTMENTS(731IV_013686)"})
    db.set_setting("paused", "1")
    db.conn.execute("insert into source_health values ('brightspace','2026-10-27T01:00:00+00:00',0)")
    for key, title, conf, start in (("a", "Tutorial 5", "confirmed", "2026-10-30T15:29:00+00:00"),
                                    ("b", "Quiz 2", "tentative", "2026-11-03T00:00:00+00:00"),
                                    ("c", "Far away", "confirmed", "2027-06-01T00:00:00+00:00")):
        db.conn.execute(
            "insert into deadlines(key,title,course_code,kind,start_utc,end_utc,all_day,source,source_ref,confidence,"
            "quote,status,updated_at) values (?,?,?,?,?,?,?,?,?,?,?,'active','x')",
            (key, title, "26S2-731IV_013686", "assignment", start, None, int(key == "b"), "brightspace", "", conf, None))
    db.conn.execute("insert into pending_actions(kind,params_json,summary,status,created_at,expires_at) "
                    "values ('brightspace_submit','{}','Submit T5','pending','x','y')")
    return db


def test_data_views_are_read_only_and_correct(settings):
    seed(settings)
    ov = data.overview(settings)
    assert ov["controls"]["paused"] is True and ov["health"][0]["source"] == "brightspace"
    assert ov["pending"][0]["summary"] == "Submit T5" and ov["counts"]["deadlines"] == 3
    names = {c["name"]: c["ok"] for c in ov["checks"]}
    assert names["Semester start date set"] is False and "Google Calendar connected" not in names
    rows = data.deadlines(settings, 30, now=datetime(2026, 10, 27, tzinfo=timezone.utc))
    assert [r["title"] for r in rows] == ["Tutorial 5", "Quiz 2"]
    assert rows[0]["course"] == "Investments" and rows[0]["confirmed"] and rows[0]["time"] == "23:29"
    assert rows[1]["time"] is None and not rows[1]["confirmed"]


def test_data_without_database_is_empty(settings):
    assert data.deadlines(settings) == [] and data.overview(settings)["health"] == []


async def test_usage_endpoint(settings):
    db = seed(settings)
    db.record_usage(datetime.now(timezone.utc), "briefing", "text", "sonnet", True, 3.0, 100, 50, estimated=True)
    c = await client(settings, supervisor(settings))
    try:
        body = await (await c.get("/api/usage?days=7", headers=H)).json()
        assert body["total"]["calls"] == 1 and body["by_job"][0]["job"] == "briefing"
        assert (await c.get("/api/usage?days=abc", headers=H)).status == 200
    finally:
        await c.close()


NOW_CONN = datetime(2026, 10, 27, 4, 0, tzinfo=timezone.utc)  # 12:00 in Singapore


def conn_states(settings, mode="running", now=NOW_CONN):
    return {c["id"]: c for c in data.connections(settings, mode, now)}


def health(db, source, minutes_ago, failures=0):
    last = (NOW_CONN - timedelta(minutes=minutes_ago)).isoformat() if minutes_ago is not None else None
    db.conn.execute("insert or replace into source_health values (?,?,?)", (source, last, failures))


def test_connection_is_green_only_when_logged_in_and_recently_checked(settings):
    db = DB(settings.db_path)
    db.init()
    health(db, "brightspace", 5)
    health(db, "teams", 12)
    c = conn_states(settings)
    assert c["brightspace"]["state"] == "live" and c["brightspace"]["color"] == "green" and c["brightspace"]["action"] is None
    assert c["teams"]["color"] == "green" and "Last checked 12 min ago" in c["teams"]["detail"]


def test_stale_check_is_amber_and_stopped_jarvis_is_grey(settings):
    db = DB(settings.db_path)
    db.init()
    health(db, "brightspace", 90)
    health(db, "teams", 5)
    c = conn_states(settings)
    assert c["brightspace"]["state"] == "stale" and c["brightspace"]["color"] == "amber"
    off = conn_states(settings, mode="stopped")
    assert off["teams"]["state"] == "offline" and off["teams"]["color"] == "grey" and "Last confirmed" in off["teams"]["detail"]
    assert conn_states(settings, mode="starting")["teams"]["state"] == "starting"


def test_expired_session_or_repeated_failures_are_red_with_a_login_action(settings):
    db = DB(settings.db_path)
    db.init()
    health(db, "brightspace", 5)
    db.set_setting("auth_prompted:brightspace", "1")
    health(db, "teams", 5, failures=3)
    c = conn_states(settings)
    assert c["brightspace"]["state"] == "login" and c["brightspace"]["action"] == "login_brightspace"
    assert c["teams"]["state"] == "failing" and c["teams"]["action"] == "login_teams"


def test_night_allows_the_hourly_check_and_never_logged_in_is_red(settings):
    db = DB(settings.db_path)
    db.init()
    night = datetime(2026, 10, 26, 19, 0, tzinfo=timezone.utc)  # 03:00 in Singapore
    db.conn.execute("insert into source_health values ('brightspace', ?, 0)", ((night - timedelta(minutes=70)).isoformat(),))
    assert conn_states(settings, now=night)["brightspace"]["state"] == "live"
    assert conn_states(settings)["teams"]["state"] == "login" and "No login saved" in conn_states(settings)["teams"]["detail"]


def test_google_calendar_states(settings):
    assert conn_states(settings)["google"]["state"] == "unconfigured"
    (settings.state_dir / "google_client.json").write_text("{}")
    assert conn_states(settings)["google"]["action"] == "login_google"
    (settings.state_dir / "google_token.json").write_text("{}")
    assert conn_states(settings)["google"]["color"] == "green"
    db = DB(settings.db_path)
    db.init()
    db.set_setting("gcal_warned", "1")
    assert conn_states(settings)["google"]["color"] == "red"


async def test_state_endpoint_includes_connections(settings):
    c = await client(settings, supervisor(settings))
    try:
        body = await (await c.get("/api/state", headers=H)).json()
        assert [x["id"] for x in body["connections"]] == ["brightspace", "teams", "google", "telegram", "outlook"]
    finally:
        await c.close()
