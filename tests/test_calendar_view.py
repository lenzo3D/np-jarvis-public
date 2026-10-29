import json
from datetime import date
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from core.config import load_settings
from core.gcal import API, CalendarAuthRequired, GCal
from ui.calendar import CalendarView, month_range, normalise
from ui.server import make_app
from ui.supervisor import OpsSupervisor, Prefs
from aiohttp.test_utils import TestClient, TestServer

SGT = ZoneInfo("Asia/Singapore")


class Tokens:
    async def __call__(self, force_refresh=False):
        return "T"


# ---------- core.gcal.list_events ----------
@pytest.fixture
def api():
    with respx.mock(base_url=API, assert_all_called=False) as m:
        yield m


async def test_list_events_sends_read_only_query_and_follows_pages(api):
    route = api.get("/calendars/primary/events")
    route.side_effect = [httpx.Response(200, json={"items": [{"id": "a"}], "nextPageToken": "p2"}),
                         httpx.Response(200, json={"items": [{"id": "b"}]})]
    items = await GCal(token_getter=Tokens()).list_events("2026-10-01T00:00:00+08:00", "2026-11-01T00:00:00+08:00")
    assert [i["id"] for i in items] == ["a", "b"]
    first, second = route.calls[0].request.url.params, route.calls[1].request.url.params
    assert first["singleEvents"] == "true" and first["orderBy"] == "startTime" and first["timeMin"].startswith("2026-10-01")
    assert second["pageToken"] == "p2"
    assert all(c.request.method == "GET" for c in route.calls)


# ---------- normalise ----------
def test_month_range_is_six_monday_first_weeks():
    start, end = month_range(2026, 10)  # 1 Oct 2026 is a Thursday
    assert start == date(2026, 9, 28) and start.weekday() == 0 and (end - start).days == 42


def test_timed_event_is_converted_to_singapore_time():
    ev = normalise({"id": "1", "summary": "Lecture", "location": "Block A",
                    "start": {"dateTime": "2026-10-27T01:30:00Z"}, "end": {"dateTime": "2026-10-27T03:30:00Z"},
                    "htmlLink": "https://calendar.google.com/x"}, SGT)
    assert (ev["date"], ev["start"], ev["end"], ev["all_day"]) == ("2026-10-27", "09:30", "11:30", False)
    assert ev["location"] == "Block A" and ev["link"].startswith("https://calendar.google.com") and not ev["jarvis"]


def test_all_day_end_date_is_exclusive_in_google_and_inclusive_for_us():
    one = normalise({"summary": "Holiday", "start": {"date": "2026-11-08"}, "end": {"date": "2026-11-09"}}, SGT)
    three = normalise({"summary": "Camp", "start": {"date": "2026-11-02"}, "end": {"date": "2026-11-05"}}, SGT)
    assert (one["date"], one["end_date"], one["all_day"]) == ("2026-11-08", "2026-11-08", True)
    assert (three["date"], three["end_date"]) == ("2026-11-02", "2026-11-04")


def test_event_crossing_midnight_in_sgt_lands_on_the_local_day():
    ev = normalise({"summary": "Late", "start": {"dateTime": "2026-10-27T17:00:00Z"}, "end": {"dateTime": "2026-10-27T18:00:00Z"}}, SGT)
    assert ev["date"] == "2026-10-28" and ev["start"] == "01:00"  # 17:00 UTC is 01:00 next day in Singapore


def test_jarvis_events_are_flagged_and_icons_removed_from_titles():
    pinned = normalise({"summary": "\U0001F4CC Investments: Tutorial 5", "start": {"date": "2026-10-30"}, "end": {"date": "2026-10-31"},
                        "extendedProperties": {"private": {"jarvis": "1"}}}, SGT)
    maybe = normalise({"summary": "❓ Tmbi: Quiz 2", "description": "x\n❓ Tentative: read from text", "start": {"date": "2026-10-31"},
                       "end": {"date": "2026-11-01"}, "extendedProperties": {"private": {"jarvis": "1"}}}, SGT)
    mine = normalise({"summary": "❓ my own title", "start": {"date": "2026-10-31"}, "end": {"date": "2026-11-01"}}, SGT)
    assert pinned["title"] == "Investments: Tutorial 5" and pinned["jarvis"] and not pinned["tentative"]
    assert maybe["title"] == "Tmbi: Quiz 2" and maybe["jarvis"] and maybe["tentative"]
    assert not mine["jarvis"] and not mine["tentative"]


def test_cancelled_and_broken_events_are_dropped():
    assert normalise({"status": "cancelled", "start": {"date": "2026-10-30"}}, SGT) is None
    assert normalise({"summary": "no start"}, SGT) is None


# ---------- CalendarView ----------
class FakeGCal:
    def __init__(self, items=None, error=None):
        self.items, self.error, self.calls = items or [], error, []

    async def list_events(self, lo, hi):
        self.calls.append((lo, hi))
        if self.error:
            raise self.error
        return self.items


@pytest.fixture
def settings(env_file):
    s = load_settings(env_file)
    s.state_dir.mkdir(parents=True, exist_ok=True)
    return s


async def test_month_view_asks_google_for_the_six_week_range_and_caches(settings):
    g = FakeGCal([{"id": "1", "summary": "X", "start": {"date": "2026-10-30"}, "end": {"date": "2026-10-31"}}])
    t = [0.0]
    v = CalendarView(settings, g, clock=lambda: t[0])
    out = await v.month(2026, 10)
    assert out["ok"] and out["range"] == ["2026-09-28", "2026-11-08"] and out["events"][0]["title"] == "X"
    assert g.calls == [("2026-09-28T00:00:00+08:00", "2026-11-09T00:00:00+08:00")]
    await v.month(2026, 10)
    assert len(g.calls) == 1  # cached
    await v.month(2026, 10, refresh=True)
    assert len(g.calls) == 2
    t[0] = 120
    await v.month(2026, 10)
    assert len(g.calls) == 3  # cache expired


async def test_not_connected_states(settings):
    out = await CalendarView(settings).month(2026, 10)
    assert out == {"ok": False, "state": "setup", "message": "Google Calendar is not connected yet."}
    (settings.state_dir / "google_client.json").write_text("{}")
    assert (await CalendarView(settings).month(2026, 10))["state"] == "login"


async def test_expired_login_and_network_errors_are_reported_not_raised(settings):
    out = await CalendarView(settings, FakeGCal(error=CalendarAuthRequired("expired"))).month(2026, 10)
    assert out["ok"] is False and out["state"] == "login" and "sign in again" in out["message"]
    out = await CalendarView(settings, FakeGCal(error=httpx.ConnectError("down"))).month(2026, 10)
    assert out["ok"] is False and out["state"] == "error"


async def test_calendar_endpoint(settings):
    view = CalendarView(settings, FakeGCal([{"id": "1", "summary": "Exam", "start": {"date": "2026-10-30"}, "end": {"date": "2026-10-31"}}]))
    sup = OpsSupervisor(settings, Prefs(settings.state_dir / "ui.json"))
    c = TestClient(TestServer(make_app(settings, sup, port=8765, calendar=view)))
    await c.start_server()
    H = {"Host": "127.0.0.1:8765"}
    try:
        body = await (await c.get("/api/calendar?year=2026&month=10", headers=H)).json()
        assert body["ok"] and body["events"][0]["title"] == "Exam"
        assert (await c.get("/api/calendar?year=abc&month=10", headers=H)).status == 400
        assert (await c.get("/api/calendar?year=2026&month=13", headers=H)).status == 400
    finally:
        await c.close()
