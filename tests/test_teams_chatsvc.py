import json
from datetime import datetime, timezone

import pytest
import respx

from core.auth import AuthRequired
from core.teams import AUTHZ_URL, GRAPH, Teams

SINCE = datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc)
CHAT = "https://apac.ng.msg.teams.microsoft.com"
ME = "me-oid"


def conv(cid, thread_type, last, topic="", product=None, space_topic=None):
    tp = {"threadType": thread_type, "topic": topic}
    if space_topic:
        tp["spaceThreadTopic"] = space_topic
    if product:
        tp["productThreadType"] = product
    return {"id": cid, "threadProperties": tp,
            "lastMessage": {"composetime": last} if last else {}}


def m(mid, ts, html, sender="Alice", oid="other", mtype="RichText/Html", mentions=None):
    props = {"mentions": json.dumps(mentions)} if mentions else {}
    return {"id": mid, "composetime": ts, "content": html, "messagetype": mtype, "imdisplayname": sender,
            "from": f"{CHAT}/v1/users/ME/contacts/8:orgid:{oid}", "properties": props}


@pytest.fixture
def svc():
    with respx.mock(assert_all_called=False) as r:
        r.post(AUTHZ_URL).respond(json={"tokens": {"skypeToken": "SKY", "expiresIn": 86400},
                                        "region": "apac", "regionGtms": {"chatService": CHAT}})
        r.get(f"{GRAPH}/me").respond(json={"id": ME, "displayName": "Jake"})
        r.get(f"{GRAPH}/me/joinedTeams").respond(json={"value": []})
        r.get(f"{CHAT}/v1/users/ME/conversations").respond(json={"conversations": [
            conv("19:dm@unq.gbl.spaces", "chat", "2026-10-27T02:17:00.000Z", product="OneToOneChat"),
            conv("19:grp@thread.v2", "chat", "2026-10-26T08:42:00.000Z", topic="TF05 project"),
            conv("19:meeting_x@thread.v2", "meeting", "2026-10-21T09:40:00.000Z", topic="INV lesson"),
            conv("19:chan@thread.tacv2", "topic", None, topic="General"),
            conv("19:team@thread.tacv2", "space", "2026-10-27T02:33:00.000Z", product="TeamsTeam",
                 space_topic="BKALY - TF05 Oct2026"),
            conv("19:oldteam@thread.tacv2", "space", "2026-09-07T03:13:00.000Z", space_topic="FDB - TF05"),
            conv("48:notifications", "streamofnotifications", "2026-10-26T08:42:00.000Z"),
        ]})
        r.get(f"{CHAT}/v1/users/ME/conversations/19:dm@unq.gbl.spaces/messages").respond(json={"messages": [
            m("2", "2026-10-27T02:17:00.000Z", "<p>Are you&nbsp;coming?</p>", "Mr Tan"),
            m("1", "2026-10-25T02:17:00.000Z", "old"),
            m("3", "2026-10-27T02:18:00.000Z", "<p>mine</p>", "Jake", oid=ME),
            m("4", "2026-10-27T02:19:00.000Z", "", mtype="ThreadActivity/AddMember"),
        ]})
        r.get(f"{CHAT}/v1/users/ME/conversations/19:grp@thread.v2/messages").respond(json={"messages": [
            m("g1", "2026-10-26T08:40:00.000Z", "<p>lol</p>", "Ben"),
            m("g2", "2026-10-26T08:42:00.000Z", "<p><span>Jake</span> do slide 3</p>", "Ben",
              mentions=[{"mri": f"8:orgid:{ME}", "displayName": "Jake"}]),
        ]})
        r.get(f"{CHAT}/v1/users/ME/conversations/19:team@thread.tacv2/messages").respond(json={"messages": [
            m("t1", "2026-10-27T02:33:00.000Z", "<p>Quiz 2 is next week</p>", "Ms Lim"),
        ]})
        yield r


def teams():
    async def token(aud):
        return {"graph.microsoft.com": "G", "api.spaces.skype.com": "S"}[aud]
    return Teams(session=None, token_provider=token)


async def test_chats_read_via_chat_service(svc):
    items = {i.stable_id: i for i in await teams().recent(SINCE)}
    assert set(items) == {"teams:19:dm@unq.gbl.spaces:2", "teams:19:grp@thread.v2:g1", "teams:19:grp@thread.v2:g2",
                          "teams:19:team@thread.tacv2:t1"}


async def test_class_team_space_posts(svc):
    items = {i.stable_id: i for i in await teams().recent(SINCE)}
    post = items["teams:19:team@thread.tacv2:t1"]
    assert post.kind == "teams_post" and post.payload["chat_name"] == "BKALY - TF05 Oct2026"
    assert post.payload["body_text"] == "Quiz 2 is next week"


async def test_graph_channels_not_polled(svc):
    await teams().recent(SINCE)
    assert not any("/channels" in str(c.request.url) for c in svc.calls)


async def test_kinds(svc):
    items = {i.stable_id: i for i in await teams().recent(SINCE)}
    assert items["teams:19:dm@unq.gbl.spaces:2"].kind == "teams_dm"
    assert items["teams:19:grp@thread.v2:g1"].kind == "teams_post"
    assert items["teams:19:grp@thread.v2:g2"].kind == "teams_mention"


async def test_text_and_names(svc):
    items = {i.stable_id: i for i in await teams().recent(SINCE)}
    dm = items["teams:19:dm@unq.gbl.spaces:2"]
    assert dm.payload["body_text"] == "Are you coming?" and dm.payload["sender"] == "Mr Tan"
    assert dm.payload["chat_name"] == "Direct message"
    assert items["teams:19:grp@thread.v2:g1"].payload["chat_name"] == "TF05 project"


async def test_skype_token_cached(svc):
    t = teams()
    await t.recent(SINCE)
    await t.recent(SINCE)
    assert svc.routes[0].call_count == 1


async def test_chat_service_401_refreshes_then_auth_required(svc):
    svc.get(f"{CHAT}/v1/users/ME/conversations").respond(401)
    with pytest.raises(AuthRequired):
        await teams().recent(SINCE)


async def test_read_conversation(svc):
    msgs = await teams().read("19:dm@unq.gbl.spaces", limit=10)
    assert [x["text"] for x in msgs][:1] == ["Are you coming?"]


async def test_send_via_chat_service(svc):
    route = svc.post(f"{CHAT}/v1/users/ME/conversations/19:dm@unq.gbl.spaces/messages").respond(
        201, json={"OriginalArrivalTime": 1})
    await teams().send("19:dm@unq.gbl.spaces", "On my way")
    body = json.loads(route.calls.last.request.content)
    assert body["content"] == "On my way" and body["messagetype"] == "Text"
    assert route.calls.last.request.headers["Authentication"] == "skypetoken=SKY"


async def test_describe_from_conversation_list(svc):
    t = teams()
    assert await t.describe("19:grp@thread.v2") == "group chat “TF05 project”"
    assert await t.describe("19:dm@unq.gbl.spaces") == "direct chat with Mr Tan"
