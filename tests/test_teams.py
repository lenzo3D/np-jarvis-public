"""Graph parts of the Teams client: profile, channel sends and names (reading is in test_teams_chatsvc.py)."""
from datetime import datetime, timezone

import pytest
import respx

from core.auth import AuthRequired
from core.teams import AUTHZ_URL, GRAPH, Teams

SINCE = datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc)
CHAT = "https://apac.ng.msg.teams.microsoft.com"
ME = {"id": "me-1", "displayName": "Jake"}


def msg(mid, ts, text, sender="Mr Tan", sender_id="u-9", mentions=None):
    return {"id": mid, "createdDateTime": ts, "messageType": "message",
            "from": {"user": {"id": sender_id, "displayName": sender}},
            "body": {"contentType": "html", "content": text}, "mentions": mentions or []}


@pytest.fixture
def graph():
    with respx.mock(assert_all_called=False) as m:
        m.post(AUTHZ_URL).respond(json={"tokens": {"skypeToken": "SKY", "expiresIn": 86400},
                                        "regionGtms": {"chatService": CHAT}})
        m.get(f"{CHAT}/v1/users/ME/conversations").respond(json={"conversations": []})
        m.get(f"{GRAPH}/me").respond(json=ME)
        m.get(f"{GRAPH}/me/joinedTeams").respond(json={"value": [{"id": "t1", "displayName": "INV2101 TF05"}]})
        m.get(f"{GRAPH}/teams/t1/channels").respond(json={"value": [{"id": "ch1", "displayName": "General"}]})
        m.get(f"{GRAPH}/teams/t1/channels/ch1/messages").respond(json={"value": [
            msg("p0", "2026-10-25T02:00:00Z", "old"),
            msg("p1", "2026-10-26T02:00:00Z", "<p>Quiz moved</p>"),
            msg("p2", "2026-10-26T02:30:00Z", "<p><at id=\"0\">Jake</at> please present</p>",
                mentions=[{"id": 0, "mentioned": {"user": {"id": "me-1", "displayName": "Jake"}}}]),
            msg("p3", "2026-10-26T02:40:00Z", "mine", "Jake", "me-1"),
        ]})
        yield m


def teams():
    async def token(aud):
        return "tok"
    return Teams(session=None, token_provider=token)


async def test_graph_token_expired_raises_auth_required(graph):
    graph.get(f"{GRAPH}/me").respond(401)
    with pytest.raises(AuthRequired):
        await teams().recent(SINCE)


async def test_send_to_channel_uses_graph(graph):
    route = graph.post(f"{GRAPH}/teams/t1/channels/ch1/messages").respond(201, json={"id": "m9"})
    res = await teams().send("t1/ch1", "hello")
    assert route.called and res["id"] == "m9"


async def test_describe_channel(graph):
    graph.get(f"{GRAPH}/teams/t1").respond(json={"displayName": "INV2101 TF05"})
    graph.get(f"{GRAPH}/teams/t1/channels/ch1").respond(json={"displayName": "General"})
    assert await teams().describe("t1/ch1") == "channel INV2101 TF05 › General"


def test_ui_ids_stable_across_processes():
    import subprocess
    import sys
    code = "from core.teams import ui_key; print(ui_key('Group chat', 'see you at 3'))"
    outs = {subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=".",
                           env={"PYTHONHASHSEED": seed, "PATH": ""}).stdout for seed in ("1", "2")}
    assert len(outs) == 1 and outs.pop().strip()


def test_conv_id_validation():
    from core.teams import valid_conv_id
    assert valid_conv_id("19:abc_def@thread.v2")
    assert valid_conv_id("6a1b-22/19:xyz@thread.tacv2")
    for bad in ["../me", "c1?x=1", "c1#f", "a/b/c", "", "c 1"]:
        assert not valid_conv_id(bad)
