"""Calendar events the user asks for in chat: validation, the API route, the Google event shape, removal."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from aiohttp.test_utils import TestClient, TestServer

from core import my_events
from core.config import load_settings
from core.deadlines import DeadlineStore
from ops.api import make_app
from ops.gcal_events import event_body
from ops.watcher import Services
from tests.fakes import Clock, FakeBS, FakeNotifier

SGT = ZoneInfo("Asia/Singapore")
NOW = datetime(2026, 11, 6, 6, 0, tzinfo=timezone.utc)  # Fri 6 Nov, 14:00 SGT


def test_build_reads_times_as_singapore_and_validates():
    d = my_events.build("Meeting with Mr Valerian Yap", "2026-11-10T16:00", NOW, SGT, kind="meeting", note="Room 72-05")
    assert d.start_utc == "2026-11-10T16:00:00+08:00" and d.end_utc is None and d.kind == "meeting"
    assert d.source == "mine" and d.confidence == "confirmed" and d.key.startswith("mine:") and d.as_event
    assert my_events.build("Meeting with Mr Valerian Yap", "2026-11-10T16:00", NOW, SGT).key == d.key  # same ask, same row
    with pytest.raises(my_events.EventError):
        my_events.build("x", "next tuesday", NOW, SGT)
    with pytest.raises(my_events.EventError):
        my_events.build("x", "2026-11-01T10:00", NOW, SGT)  # in the past
    with pytest.raises(my_events.EventError):
        my_events.build("x", "2026-11-10T16:00", NOW, SGT, end="2026-11-10T15:00")
    with pytest.raises(my_events.EventError):
        my_events.build("", "2026-11-10T16:00", NOW, SGT)
    with pytest.raises(my_events.EventError):
        my_events.build("x", "2026-11-10T16:00", NOW, SGT, kind="submission")
    allday = my_events.build("Deepavali", "2026-11-09", NOW, SGT, all_day=True)
    assert allday.all_day and allday.start_utc == "2026-11-09"


def test_links_are_stripped_and_google_event_looks_personal():
    d = my_events.build("Call http://evil.example now", "2026-11-10T16:00", NOW, SGT, kind="meeting", note="see https://x.y")
    assert "http" not in d.title and "http" not in d.quote
    body = event_body(d, "", SGT)
    assert body["summary"].startswith("\U0001F4CC Call") and "Personal" in body["description"]
    assert "Note: see" in body["description"] and "Tentative" not in body["description"]
    assert body["start"]["dateTime"].startswith("2026-11-10T16:00") and body["end"]["dateTime"].startswith("2026-11-10T17:00")


def test_removal_only_touches_chat_events(db):
    d = my_events.build("Dentist", "2026-11-12T09:00", NOW, SGT)
    my_events.add(db, d, NOW)
    assert [x.key for x in my_events.listing(db)] == [d.key]
    assert not my_events.remove(db, "bs:assignment:1:2")
    assert my_events.remove(db, d.key) and my_events.listing(db) == []
    assert not my_events.remove(db, d.key)  # already gone


def test_adding_again_after_a_google_delete_brings_it_back(db):
    d = my_events.build("Dentist", "2026-11-12T09:00", NOW, SGT)
    my_events.add(db, d, NOW)
    DeadlineStore(db).mark_user_deleted(d.key)
    my_events.add(db, d, NOW)
    assert DeadlineStore(db).get(d.key).status == "active"


@pytest.fixture
async def client(db, env_file):
    settings = load_settings(env_file)
    s = Services(settings=settings, db=db, sessions=None, bs=FakeBS(), notifier=FakeNotifier(), clock=Clock(NOW))
    c = TestClient(TestServer(make_app(s)))
    await c.start_server()
    c.services = s
    yield c
    await c.close()


async def test_api_adds_lists_and_removes(client, db):
    r = await client.post("/calendar/mine", json={"title": "Meeting with Mr Valerian Yap", "start": "2026-11-10T16:00",
                                                  "kind": "meeting"})
    body = await r.json()
    assert r.status == 200 and body["when"].startswith("Tue 10 Nov") and "not connected" in body["google_calendar"]
    assert "Added to your calendar" in client.services.notifier.messages[-1][0]
    listed = await (await client.get("/calendar/mine")).json()
    assert [e["key"] for e in listed] == [body["key"]]
    assert (await client.post("/calendar/mine", json={"title": "x", "start": "soon"})).status == 400
    assert (await client.post("/calendar/mine", data="[1]")).status == 400
    assert (await client.post("/calendar/mine/remove", json={"key": "bs:assignment:1:2"})).status == 400
    r = await client.post("/calendar/mine/remove", json={"key": body["key"]})
    assert r.status == 200 and await (await client.get("/calendar/mine")).json() == []


async def test_api_pushes_to_google_when_connected(client, db):
    from tests.test_calsync import FakeCal

    gcal = FakeCal()
    client.services.extra["gcal"] = gcal
    r = await client.post("/calendar/mine", json={"title": "Dentist", "start": "2026-11-12T09:00"})
    assert (await r.json())["google_calendar"] == "done"
    assert [e["summary"] for e in gcal.events.values()] == ["\U0001F4CC Dentist"]
    key = (await (await client.get("/calendar/mine")).json())[0]["key"]
    await client.post("/calendar/mine/remove", json={"key": key})
    assert gcal.events == {}  # withdrawn from Google straight away


async def test_mcp_has_calendar_tools_and_readonly_strips_them():
    import os
    import subprocess
    import sys

    from school_mcp.server import server

    names = {t.name for t in await server.list_tools()}
    assert {"add_to_my_calendar", "get_my_events", "remove_from_my_calendar"} <= names
    code = ("import asyncio; from school_mcp.server import server; "
            "print(sorted(t.name for t in asyncio.run(server.list_tools())))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8",
                         env={**os.environ, "SCHOOL_MCP_READONLY": "1"}).stdout
    assert "get_my_events" in out and "add_to_my_calendar" not in out and "remove_from_my_calendar" not in out
