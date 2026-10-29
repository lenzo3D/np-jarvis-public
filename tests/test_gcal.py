import json

import pytest
import respx

from core.gcal import API, CalendarAuthRequired, EventGone, GCal


class Tokens:
    def __init__(self, *toks):
        self.toks = list(toks)
        self.calls = 0

    async def __call__(self, force_refresh=False):
        self.calls += 1
        return self.toks.pop(0) if len(self.toks) > 1 else self.toks[0]


@pytest.fixture
def api():
    with respx.mock(base_url=API, assert_all_called=False) as m:
        yield m


async def test_insert_returns_id_and_sends_bearer(api):
    route = api.post("/calendars/primary/events").respond(json={"id": "ev1"})
    g = GCal(token_getter=Tokens("T1"))
    assert await g.insert({"summary": "x"}) == "ev1"
    assert route.calls.last.request.headers["Authorization"] == "Bearer T1"
    assert json.loads(route.calls.last.request.content) == {"summary": "x"}


async def test_patch_and_delete(api):
    p = api.patch("/calendars/primary/events/ev1").respond(json={"id": "ev1"})
    d = api.delete("/calendars/primary/events/ev1").respond(204)
    g = GCal(token_getter=Tokens("T1"))
    await g.patch("ev1", {"summary": "y"})
    await g.delete("ev1")
    assert p.called and d.called


async def test_gone_events(api):
    api.patch("/calendars/primary/events/ev1").respond(404)
    api.delete("/calendars/primary/events/ev2").respond(410)
    g = GCal(token_getter=Tokens("T1"))
    with pytest.raises(EventGone):
        await g.patch("ev1", {})
    with pytest.raises(EventGone):
        await g.delete("ev2")


async def test_cancelled_event_counts_as_gone(api):
    api.patch("/calendars/primary/events/ev1").respond(json={"id": "ev1", "status": "cancelled"})
    with pytest.raises(EventGone):
        await GCal(token_getter=Tokens("T1")).patch("ev1", {})


async def test_401_refreshes_once_then_auth_required(api):
    route = api.post("/calendars/primary/events")
    route.side_effect = [respx.MockResponse(401), respx.MockResponse(200, json={"id": "ev9"})]
    toks = Tokens("old", "new")
    assert await GCal(token_getter=toks).insert({}) == "ev9"
    route.side_effect = None
    route.respond(401)
    with pytest.raises(CalendarAuthRequired):
        await GCal(token_getter=Tokens("bad")).insert({})


async def test_missing_token_file_is_auth_required(tmp_path):
    g = GCal(token_path=tmp_path / "google_token.json", client_path=tmp_path / "google_client.json")
    with pytest.raises(CalendarAuthRequired):
        await g.insert({})



async def test_insert_conflict_raises_event_exists(api):
    from core.gcal import EventExists
    api.post("/calendars/primary/events").respond(409)
    with pytest.raises(EventExists):
        await GCal(token_getter=Tokens("T1")).insert({"id": "jvabc"})


def test_token_written_atomically(tmp_path):
    from core.gcal import _write_private
    p = tmp_path / "google_token.json"
    _write_private(p, '{"a": 1}')
    assert p.read_text() == '{"a": 1}' and oct(p.stat().st_mode & 0o777) == "0o600"
    assert not list(tmp_path.glob("*.tmp"))


async def test_corrupt_token_file_is_auth_required(tmp_path):
    p = tmp_path / "google_token.json"
    p.write_text("{not json")
    with pytest.raises(CalendarAuthRequired):
        await GCal(token_path=p).insert({})
