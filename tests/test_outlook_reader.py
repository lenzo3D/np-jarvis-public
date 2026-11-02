"""Outlook reader robustness: strategy fallback, token renewal, transient errors, paging, parsing, read-only."""
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx

from core.mail import MailStore
from core.outlook import BODY_LIMIT, ENDPOINTS, MailUnavailable, Outlook, mail_from_json
from ops import mail_jobs
from tests.test_mail import FakeReader, NOW, jwt, mail, svc

GRAPH, REST365 = ENDPOINTS[0], ENDPOINTS[2]
AUD = "https://outlook.office.com"

# Shapes recorded from the two APIs (content replaced with made-up text).
GRAPH_PAGE_1 = {
    "@odata.context": "https://graph.microsoft.com/v1.0/$metadata#users('x')/mailFolders('inbox')/messages(...)",
    "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?$top=1&$skiptoken=abc",
    "value": [{"@odata.etag": 'W/"CQ"', "id": "AAMk1", "subject": "Week 9 quiz", "bodyPreview": "Quiz on 3 Nov",
               "receivedDateTime": "2026-10-27T01:00:00Z", "importance": "high", "conversationId": "C1", "isRead": False,
               "hasAttachments": True, "inferenceClassification": "focused", "webLink": "https://outlook.office.com/owa/?ItemID=1",
               "from": {"emailAddress": {"name": "Mr Tan", "address": "tan@np.edu.sg"}},
               "body": {"contentType": "text", "content": "Quiz on 3 Nov\r\n\r\n\r\n\r\nBlock L"}}]}
GRAPH_PAGE_2 = {"value": [{"id": "AAMk2", "subject": "Lunch?", "receivedDateTime": "2026-10-26T05:00:00Z",
                           "from": {"emailAddress": {"name": "Ben", "address": "ben@student.np.edu.sg"}},
                           "body": {"contentType": "text", "content": "free at 1?"}}]}
REST_PAGE = {
    "@odata.context": "https://outlook.office365.com/api/v2.0/$metadata#Me/MailFolders('inbox')/Messages",
    "value": [{"@odata.id": "https://outlook.office365.com/api/v2.0/Users('u')/Messages('R1')", "@odata.etag": 'W/"CQ"',
               "Id": "R1", "ReceivedDateTime": "2026-10-27T01:00:00.0000000Z", "HasAttachments": False, "Subject": "Venue",
               "BodyPreview": "Moved", "Importance": "Normal", "ConversationId": "C2", "IsRead": True,
               "InferenceClassification": "Other", "WebLink": "https://outlook.office365.com/owa/?ItemID=R1",
               "From": {"EmailAddress": {"Name": "Admin", "Address": "admin@np.edu.sg"}},
               "Body": {"ContentType": "HTML", "Content": "<html><head><style>p{color:red}</style></head><body><p>Now in<br>Block&nbsp;L</p></body></html>"}}]}


def tokens(*pairs):
    """token_provider returning the given (audience, ttl seconds) tokens; counts calls."""
    calls = {"n": 0}

    async def get():
        calls["n"] += 1
        return [{"secret": jwt(a, exp=time.time() + ttl), "target": a, "expiresOn": ""} for a, ttl in pairs]
    get.calls = calls
    return get


@pytest.fixture
def api():
    with respx.mock(assert_all_called=False) as m:
        yield m


# ---------- strategy fallback and token renewal ----------
async def test_saved_strategy_that_starts_failing_falls_back_to_the_others_and_saves_the_new_one(api):
    for code in (401, 403, 404, 410):
        api.get(REST365["url"]).respond(code)
        api.get(ENDPOINTS[0]["url"]).respond(404)
        api.get(ENDPOINTS[1]["url"]).respond(200, json={"value": REST_PAGE["value"]})
        o = Outlook(None, token_provider=tokens((AUD, 3600)), strategy="outlook365-rest")
        got = await o.recent()
        assert [m.id for m in got] == ["R1"] and o.strategy == "outlook-rest", code


async def test_a_rejected_token_triggers_one_re_read_of_the_browser_tokens(api):
    # first read: only a token for the wrong audience (everything says 401); the second read has the right one
    reads = [[("https://other.example", 3600)], [(AUD, 3600)]]
    calls = {"n": 0}

    async def provider():
        calls["n"] += 1
        return [{"secret": jwt(a, exp=time.time() + t), "target": "", "expiresOn": ""} for a, t in reads[calls["n"] - 1]]
    api.get(ENDPOINTS[0]["url"]).respond(401)
    api.get(ENDPOINTS[1]["url"]).respond(401)
    api.get(REST365["url"]).side_effect = lambda req: httpx.Response(200 if calls["n"] == 2 else 401, json={"value": REST_PAGE["value"]})
    o = Outlook(None, token_provider=provider, strategy="outlook365-rest")
    got = await o.recent()
    assert [m.id for m in got] == ["R1"] and calls["n"] == 2 and o.strategy == "outlook365-rest"


async def test_a_token_close_to_expiry_is_not_reused(monkeypatch, api):
    api.get(REST365["url"]).respond(200, json={"value": REST_PAGE["value"]})
    api.get(ENDPOINTS[0]["url"]).respond(401)
    api.get(ENDPOINTS[1]["url"]).respond(401)
    prov = tokens((AUD, 300))
    o = Outlook(None, token_provider=prov, strategy="outlook365-rest")
    t0 = time.time()
    await o.recent()
    await o.recent()
    assert prov.calls["n"] == 1  # still comfortably valid: reused
    monkeypatch.setattr("core.outlook.time.time", lambda: t0 + 200)  # inside the 120 s margin of the 300 s token
    await o.recent()
    assert prov.calls["n"] == 2  # re-read from the browser instead of sending a token about to expire


async def test_transient_errors_are_marked_transient_and_do_not_re_read_tokens(api):
    for ep in ENDPOINTS:
        api.get(ep["url"]).respond(503)
    prov = tokens((AUD, 3600))
    with pytest.raises(MailUnavailable) as e:
        await Outlook(None, token_provider=prov).recent()
    assert e.value.transient is True and prov.calls["n"] == 1

    for ep in ENDPOINTS:
        api.get(ep["url"]).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(MailUnavailable) as e:
        await Outlook(None, token_provider=tokens((AUD, 3600))).recent()
    assert e.value.transient is True and "ConnectError" in str(e.value)

    for ep in ENDPOINTS:
        api.get(ep["url"]).respond(403)
    with pytest.raises(MailUnavailable) as e:
        await Outlook(None, token_provider=tokens((AUD, 3600))).recent()
    assert e.value.transient is False


# ---------- the sweep: health accounting ----------
def _failures(db):
    return db.conn.execute("select consecutive_failures from source_health where source='outlook'").fetchone()[0]


async def test_one_transient_failure_does_not_turn_the_indicator_red_and_a_good_sweep_clears_red(env_file, tmp_path):
    from core.config import load_settings
    from core.db import DB
    from ui import data

    db = DB(load_settings(env_file).db_path)  # the panel reads settings.db_path, so use the same file
    db.init()

    transient = MailUnavailable("Could not read the mailbox (graph: HTTP 503).", True)
    reader = FakeReader([[mail("a")], transient, transient, transient, [mail("a")]])
    s = svc(db, env_file, tmp_path, reader)
    (s.settings.state_dir / "sessions").mkdir(parents=True, exist_ok=True)
    (s.settings.state_dir / "sessions" / "outlook.cookies.json").write_text("[]", encoding="utf-8")
    state = lambda: {c["id"]: c for c in data.connections(s.settings, "running", NOW)}["outlook"]["state"]  # noqa: E731
    await mail_jobs.sweep_outlook(s)  # baseline
    await mail_jobs.sweep_outlook(s)
    assert _failures(db) == 1 and state() != "failing"
    await mail_jobs.sweep_outlook(s)
    assert _failures(db) == 2 and state() != "failing"
    await mail_jobs.sweep_outlook(s)
    assert _failures(db) == 3 and state() == "failing"
    assert "checks failing (3 in a row)" in s.notifier.messages[-1][0]
    await mail_jobs.sweep_outlook(s)  # a successful sweep clears it
    assert _failures(db) == 0 and state() == "live"
    assert db.get_json_setting("outlook_status", {})["state"] == "ok"
    assert "back to normal" in s.notifier.messages[-1][0]


def test_sweeps_start_from_the_newest_stored_mail_so_an_outage_leaves_no_gap(db):
    store = MailStore(db)
    assert mail_jobs._since(store, NOW) == NOW - mail_jobs.LOOKBACK  # nothing stored
    store.upsert(mail("a", received=NOW - timedelta(hours=1)), "normal", NOW)
    assert mail_jobs._since(store, NOW) == NOW - mail_jobs.LOOKBACK  # recent mail: the normal overlap
    store.upsert(mail("b", received=NOW - timedelta(days=1)), "normal", NOW)  # older than newest: unchanged
    db.conn.execute("delete from emails")
    store.upsert(mail("old", received=NOW - timedelta(days=6)), "normal", NOW)
    assert mail_jobs._since(store, NOW) == NOW - timedelta(days=6, hours=6)  # long outage: reach back to it
    db.conn.execute("delete from emails")
    store.upsert(mail("ancient", received=NOW - timedelta(days=60)), "normal", NOW)
    assert mail_jobs._since(store, NOW) == NOW - mail_jobs.MAX_LOOKBACK  # but not without limit


async def test_overlapping_sweeps_never_duplicate_or_re_alert(db, env_file, tmp_path):
    a = mail("n1", "Quiz 2 venue changed", addr="tan@np.edu.sg")
    s = svc(db, env_file, tmp_path, FakeReader([[], [a], [a, mail("n2", "Lunch?")], [a, mail("n2", "Lunch?")]]))
    for _ in range(4):
        await mail_jobs.sweep_outlook(s)
    assert MailStore(db).stats()["count"] == 2
    assert len([t for t, _ in s.notifier.messages if "Quiz 2 venue" in t]) == 1


# ---------- paging ----------
async def test_paging_follows_next_links_on_the_same_host_only(api):
    r1 = api.get(GRAPH["url"], params={"$top": "2"}).respond(200, json=GRAPH_PAGE_1)
    api.get(GRAPH_PAGE_1["@odata.nextLink"]).respond(200, json=GRAPH_PAGE_2)
    o = Outlook(None, token_provider=tokens(("https://graph.microsoft.com", 3600)), strategy="graph")
    got = await o.recent(None, 2)
    assert [m.id for m in got] == ["AAMk1", "AAMk2"] and r1.called
    assert got[0].has_attachments is True and got[0].body == "Quiz on 3 Nov\n\nBlock L" and got[0].importance == "high"

    evil = dict(GRAPH_PAGE_1, **{"@odata.nextLink": "https://evil.example/steal"})
    api.get(GRAPH["url"]).respond(200, json=evil)
    leak = api.get("https://evil.example/steal").respond(200, json=GRAPH_PAGE_2)
    got = await o.recent(None, 50)
    assert [m.id for m in got] == ["AAMk1"] and not leak.called  # the bearer token is never sent off-host


async def test_paging_stops_at_the_limit_dedupes_and_a_failed_page_raises(api):
    page_a = {"value": [{"id": "x", "subject": "s"}, {"id": "x", "subject": "s"}], "@odata.nextLink": GRAPH["url"] + "?$skiptoken=2"}
    api.get(GRAPH["url"], params={"$top": "100"}).respond(200, json=page_a)
    p2 = api.get(GRAPH["url"] + "?$skiptoken=2").respond(200, json=GRAPH_PAGE_2)
    o = Outlook(None, token_provider=tokens(("https://graph.microsoft.com", 3600)), strategy="graph")
    assert [m.id for m in await o.recent(None, 100)] == ["x", "AAMk2"]  # duplicate id collapsed, page 2 followed
    p2.respond(500)
    with pytest.raises(MailUnavailable) as e:
        await o.recent(None, 100)  # never silently skip the rest of the inbox
    assert e.value.transient is True and "page 2" in str(e.value)


async def test_since_becomes_a_received_time_filter_in_both_styles(api):
    g = api.get(GRAPH["url"]).respond(200, json={"value": []})
    r = api.get(REST365["url"]).respond(200, json={"value": []})
    since = datetime(2026, 10, 24, 2, 0, tzinfo=timezone.utc)
    await Outlook(None, token_provider=tokens(("https://graph.microsoft.com", 3600)), strategy="graph").recent(since, 10)
    await Outlook(None, token_provider=tokens((AUD, 3600)), strategy="outlook365-rest").recent(since, 10)
    assert g.calls.last.request.url.params["$filter"] == "receivedDateTime ge 2026-10-24T02:00:00Z"
    assert r.calls.last.request.url.params["$filter"] == "ReceivedDateTime ge 2026-10-24T02:00:00Z"
    assert g.calls.last.request.url.params["$orderby"] == "receivedDateTime desc"


# ---------- parsing ----------
def test_recorded_rest_message_with_html_body_and_seven_digit_seconds():
    m = mail_from_json(REST_PAGE["value"][0])
    assert m.id == "R1" and m.body == "Now in Block L" and m.received == datetime(2026, 10, 27, 1, 0, tzinfo=timezone.utc)
    assert m.has_attachments is False and m.inbox_tab == "other" and "color:red" not in m.body


def test_missing_sender_subject_and_body_do_not_break_a_message():
    m = mail_from_json({"id": "d1", "subject": None, "from": None, "body": None, "receivedDateTime": "2026-10-27T01:00:00Z"})
    assert (m.subject, m.sender_name, m.sender_addr, m.body, m.has_attachments) == ("(no subject)", "(unknown sender)", "", "", False)
    m = mail_from_json({"id": "d2", "subject": "   ", "from": {"emailAddress": {"address": "a@b.com"}}})
    assert (m.subject, m.sender_name, m.sender_addr) == ("(no subject)", "a@b.com", "a@b.com")
    m = mail_from_json({"Id": "d3", "Sender": {"EmailAddress": {"Name": "Sys", "Address": "sys@np.edu.sg"}}, "ReceivedDateTime": "garbage"})
    assert m.sender_name == "Sys" and m.received.tzinfo is not None
    assert mail_from_json({"subject": "no id"}).id == ""  # the reader drops id-less rows


def test_very_long_bodies_are_capped_in_both_content_types():
    text = mail_from_json({"id": "t", "body": {"contentType": "text", "content": "word " * 20000}})
    html = mail_from_json({"id": "h", "body": {"contentType": "html", "content": "<p>" + "word " * 20000 + "</p>"}})
    for m in (text, html):
        assert BODY_LIMIT <= len(m.body) <= BODY_LIMIT + 1 and m.body.endswith("…")
    assert mail_from_json({"id": "s", "body": {"contentType": "text", "content": "short"}}).body == "short"


async def test_an_unreadable_message_does_not_hide_the_rest(api):
    page = {"value": [{"id": "ok1", "subject": "fine"}, {"id": "bad", "from": 5, "body": 7}, {"id": "ok2", "subject": "fine too"}]}
    api.get(REST365["url"]).respond(200, json=page)
    got = await Outlook(None, token_provider=tokens((AUD, 3600)), strategy="outlook365-rest").recent()
    assert [m.id for m in got] == ["ok1", "bad", "ok2"]


# ---------- read-only ----------
async def test_the_reader_only_ever_issues_get_requests(api):
    methods = []

    def record(req):
        methods.append(req.method)
        return httpx.Response(200, json=GRAPH_PAGE_1 if "graph" in req.url.host else REST_PAGE)
    api.route().mock(side_effect=record)

    class NoBrowser:
        async def context(self, site):
            raise RuntimeError("no browser in tests")

    o = Outlook(NoBrowser(), token_provider=tokens(("https://graph.microsoft.com", 3600), (AUD, 3600)))
    await o.recent(None, 150)
    await o.probe()
    assert methods and set(methods) == {"GET"}


def test_no_code_path_in_the_reader_can_send_move_delete_or_mark_read():
    src = Path("core/outlook.py").read_text(encoding="utf-8")
    code = re.sub(r"_TOKEN_JS = .*?\n\}\"\"\"", "", src, flags=re.S)  # the in-page JS is checked separately below
    for verb in (r"\.post\(", r"\.put\(", r"\.patch\(", r"\.delete\(", r"\.request\(", r"\.send\(", "sendMail", "createReply",
                 "createForward", r"/move\b", r"/copy\b", "markAsRead"):
        assert not re.search(verb, code), verb
    # the one in-page POST is the probe's read-only GetFolder diagnostic
    assert re.findall(r"action=(\w+)", src) == ["GetFolder"]


# ---------- review follow-ups ----------
async def test_a_blip_on_the_saved_strategy_is_transient_even_though_the_others_always_refuse(api):
    api.get(REST365["url"]).respond(503)
    api.get(ENDPOINTS[0]["url"]).respond(401)
    api.get(ENDPOINTS[1]["url"]).respond(403)
    prov = tokens((AUD, 3600))
    with pytest.raises(MailUnavailable) as e:
        await Outlook(None, token_provider=prov, strategy="outlook365-rest").recent()
    assert e.value.transient is True and e.value.hard is False and prov.calls["n"] == 1  # no browser token re-read


async def test_saved_strategy_refused_with_fresh_tokens_is_a_hard_failure_but_403_is_not(api):
    for ep in ENDPOINTS:
        api.get(ep["url"]).respond(401)
    prov = tokens((AUD, 3600))
    with pytest.raises(MailUnavailable) as e:
        await Outlook(None, token_provider=prov, strategy="outlook365-rest").recent()
    assert e.value.hard is True and e.value.transient is False and prov.calls["n"] == 2  # re-read once, still refused
    for ep in ENDPOINTS:
        api.get(ep["url"]).respond(403)
    with pytest.raises(MailUnavailable) as e:
        await Outlook(None, token_provider=tokens((AUD, 3600)), strategy="outlook365-rest").recent()
    assert e.value.hard is False  # wrong method, not a login problem


async def test_a_hard_failure_turns_the_indicator_red_at_once_and_does_not_prompt_a_login(db, env_file, tmp_path):
    s = svc(db, env_file, tmp_path, FakeReader([[mail("a")], MailUnavailable("saved method refused (HTTP 401)", hard=True)]))
    await mail_jobs.sweep_outlook(s)
    await mail_jobs.sweep_outlook(s)
    assert db.get_json_setting("outlook_status", {})["state"] == "no_access" and _failures(db) == 1
    assert s.notifier.messages == []  # a login prompt is only for AuthRequired (browser really signed out)


async def test_catching_up_after_an_outage_pages_through_more_than_100_mails(db, env_file, tmp_path, api):
    def page(start, n, nxt=None):
        body = {"value": [{"id": f"m{i}", "subject": f"s{i}", "receivedDateTime": "2026-10-25T01:00:00Z"} for i in range(start, start + n)]}
        return dict(body, **({"@odata.nextLink": nxt} if nxt else {}))
    base = GRAPH["url"]
    api.get(base, params={"$top": "100"}).respond(200, json=page(0, 100, base + "?$skiptoken=1"))
    api.get(base + "?$skiptoken=1").respond(200, json=page(100, 100, base + "?$skiptoken=2"))
    api.get(base + "?$skiptoken=2").respond(200, json=page(200, 50))
    o = Outlook(None, token_provider=tokens(("https://graph.microsoft.com", 3600)), strategy="graph")
    assert len(await o.recent(None, mail_jobs.CATCHUP_LIMIT)) == 250
    assert len(await o.recent(None, mail_jobs.SWEEP_LIMIT)) == 100

    MailStore(db).upsert(mail("old", received=NOW - timedelta(days=6)), "normal", NOW)
    s = svc(db, env_file, tmp_path, FakeReader([[]]))
    db.set_setting("baseline:outlook", "1")
    await mail_jobs.sweep_outlook(s)
    assert s.extra["outlook"].calls[0][1] == mail_jobs.CATCHUP_LIMIT
    db.conn.execute("delete from emails")
    MailStore(db).upsert(mail("recent", received=NOW - timedelta(hours=1)), "normal", NOW)
    await mail_jobs.sweep_outlook(s)
    assert s.extra["outlook"].calls[1][1] == mail_jobs.SWEEP_LIMIT


def test_unchanged_mail_does_not_rewrite_the_search_index(db):
    st = MailStore(db)
    st.upsert(mail("a", "Quiz", body="Covers weeks 5 to 8"), "normal", NOW)
    sql = []
    db.conn.set_trace_callback(sql.append)
    st.upsert(mail("a", "Quiz", body="Covers weeks 5 to 8", read=True), "normal", NOW)
    assert not [q for q in sql if "emails_fts" in q and ("delete" in q.lower() or "insert" in q.lower())]
    st.upsert(mail("a", "Quiz", body="Now weeks 9 to 12"), "normal", NOW)
    db.conn.set_trace_callback(None)
    assert any("insert into emails_fts" in q for q in sql) and [m["id"] for m in st.search("weeks 9")] == ["a"]


async def test_a_200_that_is_not_a_json_object_is_a_clean_failure(api):
    for bad in ([], "<html>portal</html>", {"value": "nope"}):
        api.routes.clear()
        for ep in ENDPOINTS:
            api.get(ep["url"]).respond(200, json=bad)
        with pytest.raises(MailUnavailable) as e:
            await Outlook(None, token_provider=tokens((AUD, 3600))).recent()
        assert e.value.transient is True
    api.routes.clear()
    # a later page that is not an object raises rather than dropping the rest
    api.get(GRAPH["url"], params={"$top": "100"}).respond(200, json={"value": [{"id": "a"}], "@odata.nextLink": GRAPH["url"] + "?$skiptoken=2"})
    api.get(GRAPH["url"] + "?$skiptoken=2").respond(200, json=[1])
    with pytest.raises(MailUnavailable, match="page 2"):
        await Outlook(None, token_provider=tokens(("https://graph.microsoft.com", 3600)), strategy="graph").recent(None, 100)
