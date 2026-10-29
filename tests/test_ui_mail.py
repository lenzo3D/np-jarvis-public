import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest
from aiohttp.test_utils import TestClient, TestServer

from core.config import load_settings
from core.db import DB
from core.mail import Mail, MailStore
from ui import data, inbox
from ui.jobs import Jobs
from ui.server import make_app
from ui.supervisor import OpsSupervisor, Prefs

H = {"Host": "127.0.0.1:8765", "X-JARVIS-UI": "1"}
NOW = datetime(2026, 10, 27, 4, 0, tzinfo=timezone.utc)  # 12:00 in Singapore


@pytest.fixture
def settings(env_file):
    s = load_settings(env_file)
    (s.state_dir / "sessions").mkdir(parents=True, exist_ok=True)
    return s


def logged_in(settings):
    (settings.state_dir / "sessions" / "outlook.cookies.json").write_text("[]")


def m(i, subject, read=False, minutes=10, cat="normal", **kw):
    mail = Mail(id=i, received=NOW - timedelta(minutes=minutes), sender_name=kw.get("sender", "Mr Tan"),
                sender_addr=kw.get("addr", "tan@np.edu.sg"), subject=subject, preview=kw.get("preview", "preview"),
                body=kw.get("body", ""), is_read=read, web_link="https://outlook.office.com/m/" + i)
    return mail, cat


def fill(db, *items):
    store = MailStore(db)
    for mail, cat in items:
        store.upsert(mail, cat, NOW)


def conn(settings, mode="running", now=NOW):
    return {c["id"]: c for c in data.connections(settings, mode, now)}["outlook"]


# ---------- stages ----------
def test_stage_progression(settings):
    db = DB(settings.db_path)
    db.init()
    assert inbox.state(settings, db)["stage"] == "not_set_up"
    logged_in(settings)
    assert inbox.state(settings, db)["stage"] == "waiting"
    db.set_setting("auth_prompted:outlook", "1")
    assert inbox.state(settings, db)["stage"] == "login"
    db.set_setting("auth_prompted:outlook", "0")
    db.set_json_setting("outlook_status", {"state": "no_access", "message": "graph: HTTP 403"})
    st = inbox.state(settings, db)
    assert st["stage"] == "no_access" and "403" in st["message"]
    db.set_json_setting("outlook_status", {"state": "ok", "strategy": "graph"})
    assert inbox.state(settings, db)["stage"] == "ready"


def test_messages_filters_search_and_local_time(settings):
    db = DB(settings.db_path)
    db.init()
    fill(db, m("a", "Quiz 2 venue changed", cat="important", minutes=5), m("b", "Lunch?", read=True, minutes=30, sender="Ben", addr="b@x.com"),
         m("c", "Newsletter", minutes=60, cat="bulk", body="weekly digest of campus news"))
    assert [x["id"] for x in inbox.messages(settings, db)] == ["a", "b", "c"]
    assert [x["id"] for x in inbox.messages(settings, db, "unread")] == ["a", "c"]
    assert [x["id"] for x in inbox.messages(settings, db, "important")] == ["a"]
    assert [x["id"] for x in inbox.messages(settings, db, query="digest")] == ["c"]
    a = inbox.messages(settings, db)[0]
    assert a["when"] == "Tue 27 Oct 11:55" and a["received"].endswith("+08:00")  # Singapore time
    assert inbox.message(settings, db, "a")["web_link"].startswith("https://outlook.office.com/")
    assert inbox.message(settings, db, "zzz") is None


# ---------- connection indicator ----------
def test_outlook_connection_states(settings):
    db = DB(settings.db_path)
    db.init()
    row = conn(settings)
    assert row["state"] == "unconfigured" and row["action"] == "login_outlook" and row["action_label"] == "Connect" and row["color"] == "grey"
    logged_in(settings)
    assert conn(settings)["state"] == "stale" and "first check" in conn(settings)["detail"]
    db.conn.execute("insert into source_health values ('outlook', ?, 0)", ((NOW - timedelta(minutes=4)).isoformat(),))
    fill(db, m("a", "x"))
    row = conn(settings)
    assert row["state"] == "live" and row["color"] == "green" and "1 messages stored" in row["detail"]
    assert conn(settings, mode="stopped")["state"] == "offline"
    db.conn.execute("update source_health set last_ok=? where source='outlook'", ((NOW - timedelta(minutes=90)).isoformat(),))
    assert conn(settings)["state"] == "stale"
    db.set_json_setting("outlook_status", {"state": "no_access"})
    row = conn(settings)
    assert row["state"] == "failing" and row["action"] == "probe_outlook" and row["action_label"] == "Check access"
    db.set_json_setting("outlook_status", {"state": "ok"})
    db.set_setting("auth_prompted:outlook", "1")
    assert conn(settings)["state"] == "login" and conn(settings)["action"] == "login_outlook"


# ---------- endpoints ----------
async def test_mail_endpoints(settings):
    db = DB(settings.db_path)
    db.init()
    logged_in(settings)
    db.set_json_setting("outlook_status", {"state": "ok"})
    fill(db, m("a", "Quiz 2", body="Tuesday 3 Nov, Block L"))
    sup = OpsSupervisor(settings, Prefs(settings.state_dir / "ui.json"))
    c = TestClient(TestServer(make_app(settings, sup, port=8765, db=db)))
    await c.start_server()
    try:
        body = await (await c.get("/api/mail?filter=unread", headers=H)).json()
        assert body["state"]["stage"] == "ready" and [x["id"] for x in body["messages"]] == ["a"]
        weird = await (await c.get("/api/mail?filter=../../etc&limit=abc", headers=H)).json()
        assert [x["id"] for x in weird["messages"]] == ["a"]  # bad input falls back to safe defaults
        one = await (await c.get("/api/mail/item?id=a", headers=H)).json()
        assert one["body"] == "Tuesday 3 Nov, Block L"
        assert (await c.get("/api/mail/item?id=nope", headers=H)).status == 404
        assert "outlook" in [x["id"] for x in (await (await c.get("/api/state", headers=H)).json())["connections"]]
    finally:
        await c.close()


# ---------- jobs that need JARVIS paused ----------
ECHO = {"hello": {"label": "Hello", "cmd": [sys.executable, "-c", "print('hi')"], "exclusive": True},
        "plain": {"label": "Plain", "cmd": [sys.executable, "-c", "print('plain')"]}}


async def test_exclusive_job_pauses_jarvis_and_restarts_it(settings):
    sup = OpsSupervisor(settings, Prefs(settings.state_dir / "ui.json"), command=[sys.executable, "-c", "import time; time.sleep(60)"])
    jobs = Jobs(ECHO)
    c = TestClient(TestServer(make_app(settings, sup, jobs=jobs, port=8765, db=DB(settings.db_path))))
    await c.start_server()
    try:
        await sup.start()
        first_pid = sup.pid
        r = await c.post("/api/jobs/start", headers=H, data=json.dumps({"id": "hello"}))
        assert r.status == 200
        assert jobs.status()["output"].startswith("Pausing JARVIS")
        await jobs._task
        assert "hi" in jobs.status()["output"] and "JARVIS restarted." in jobs.status()["output"]
        assert sup.managed_running and sup.pid != first_pid  # it came back, as a new process
        # a job that does not touch the browser profile leaves JARVIS alone
        pid = sup.pid
        assert (await c.post("/api/jobs/start", headers=H, data=json.dumps({"id": "plain"}))).status == 200
        await jobs._task
        assert sup.pid == pid
    finally:
        await sup.stop()
        await c.close()


async def test_exclusive_job_when_jarvis_was_not_running_does_not_start_it(settings):
    sup = OpsSupervisor(settings, Prefs(settings.state_dir / "ui.json"), command=[sys.executable, "-c", "import time; time.sleep(60)"])
    jobs = Jobs(ECHO)
    c = TestClient(TestServer(make_app(settings, sup, jobs=jobs, port=8765, db=DB(settings.db_path))))
    await c.start_server()
    try:
        assert (await c.post("/api/jobs/start", headers=H, data=json.dumps({"id": "hello"}))).status == 200
        await jobs._task
        assert not sup.managed_running and "restarted" not in jobs.status()["output"]
    finally:
        await c.close()


async def test_exclusive_job_refuses_when_jarvis_runs_outside_the_panel(settings, monkeypatch):
    sup = OpsSupervisor(settings, Prefs(settings.state_dir / "ui.json"))
    monkeypatch.setattr(sup, "mode", lambda: "external")
    c = TestClient(TestServer(make_app(settings, sup, jobs=Jobs(ECHO), port=8765, db=DB(settings.db_path))))
    await c.start_server()
    try:
        r = await c.post("/api/jobs/start", headers=H, data=json.dumps({"id": "hello"}))
        assert r.status == 409 and "outside this panel" in (await r.json())["message"]
    finally:
        await c.close()
