from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from core.deadlines import Deadline, DeadlineStore
from core.gcal import CalendarAuthRequired, EventGone
from ops.calsync import sync

T0 = datetime(2026, 10, 27, 2, 0, tzinfo=timezone.utc)
SGT = ZoneInfo("Asia/Singapore")
NAMES = {"26S2-731IV_013686": "INVESTMENTS(731IV_013686)"}


def dl(key="bs:assignment:1:5", title="Tutorial 5", **kw):
    base = dict(key=key, title=title, course_code="26S2-731IV_013686", kind="assignment",
                start_utc="2026-10-30T15:29:00+00:00", end_utc="2026-10-30T15:59:00+00:00", all_day=False,
                source="brightspace", source_ref="", confidence="confirmed", quote="")
    base.update(kw)
    return Deadline(**base)


class FakeCal:
    def __init__(self):
        self.events = {}
        self.writes = 0
        self.gone = set()
        self.auth_fail = False

    async def insert(self, body):
        if self.auth_fail:
            raise CalendarAuthRequired("x")
        self.writes += 1
        eid = body.get("id") or f"ev{len(self.events) + 1}"
        if eid in self.events or eid in self.gone:
            from core.gcal import EventExists
            raise EventExists(eid)
        if getattr(self, "fail_titles", None) and body["summary"].endswith(tuple(self.fail_titles)):
            import httpx
            raise httpx.HTTPStatusError("400", request=httpx.Request("POST", "http://x"),
                                        response=httpx.Response(400))
        self.events[eid] = body
        return eid

    async def get(self, eid):
        self.reads = getattr(self, "reads", 0) + 1
        if self.auth_fail:
            raise CalendarAuthRequired("x")
        if eid in self.gone or eid not in self.events:
            raise EventGone(eid)
        return self.events[eid]

    async def patch(self, eid, body):
        self.writes += 1
        if eid in self.gone:
            raise EventGone(eid)
        self.events[eid] = body

    async def delete(self, eid):
        self.writes += 1
        if eid in self.gone:
            raise EventGone(eid)
        self.events.pop(eid, None)


async def test_inserts_then_idempotent(db):
    store, cal = DeadlineStore(db), FakeCal()
    store.upsert(dl(), T0)
    r = await sync(store, cal, NAMES, SGT)
    [eid] = cal.events
    assert r.inserted == 1 and cal.events[eid]["summary"] == "\U0001F4CC Investments: Tutorial 5"
    writes = cal.writes
    r2 = await sync(store, cal, NAMES, SGT)
    assert cal.writes == writes and (r2.inserted, r2.patched, r2.deleted) == (0, 0, 0)


async def test_changed_deadline_is_patched(db):
    store, cal = DeadlineStore(db), FakeCal()
    store.upsert(dl(), T0)
    await sync(store, cal, NAMES, SGT)
    store.upsert(dl(end_utc="2026-11-02T15:59:00+00:00", start_utc="2026-11-02T15:29:00+00:00"), T0)
    r = await sync(store, cal, NAMES, SGT)
    [eid] = cal.events
    assert r.patched == 1 and cal.events[eid]["end"]["dateTime"].startswith("2026-11-02T23:59")


async def test_gone_deadline_deletes_event(db):
    store, cal = DeadlineStore(db), FakeCal()
    store.upsert(dl(), T0)
    await sync(store, cal, NAMES, SGT)
    store.mark_gone_missing("brightspace", set())
    r = await sync(store, cal, NAMES, SGT)
    assert r.deleted == 1 and cal.events == {} and store.get("bs:assignment:1:5").gcal_event_id is None


async def test_user_deleted_event_never_comes_back(db):
    store, cal = DeadlineStore(db), FakeCal()
    store.upsert(dl(key="x:1", confidence="tentative", source="announcement"), T0)
    await sync(store, cal, NAMES, SGT)
    cal.gone.add(store.get("x:1").gcal_event_id)
    store.upsert(dl(key="x:1", title="Tutorial 5 (updated)", confidence="tentative", source="announcement"), T0)
    r = await sync(store, cal, NAMES, SGT)
    assert r.user_deleted == 1 and store.is_user_deleted("x:1")
    store.upsert(dl(key="x:1", confidence="tentative", source="announcement"), T0)
    r = await sync(store, cal, NAMES, SGT)
    assert r.inserted == 0


async def test_auth_failure_propagates(db):
    store, cal = DeadlineStore(db), FakeCal()
    cal.auth_fail = True
    store.upsert(dl(), T0)
    with pytest.raises(CalendarAuthRequired):
        await sync(store, cal, NAMES, SGT)



def test_event_id_is_deterministic_base32hex():
    from ops.calsync import event_id_for
    a = event_id_for("bs:assignment:1:5")
    assert a == event_id_for("bs:assignment:1:5") and a != event_id_for("bs:assignment:1:6")
    assert set(a) <= set("0123456789abcdefghijklmnopqrstuv") and 5 <= len(a) <= 1024


async def test_concurrent_syncs_do_not_duplicate(db):
    import asyncio
    store, cal = DeadlineStore(db), FakeCal()
    store.upsert(dl(), T0)
    await asyncio.gather(sync(store, cal, NAMES, SGT), sync(store, cal, NAMES, SGT))
    assert len(cal.events) == 1 and store.get("bs:assignment:1:5").gcal_event_id in cal.events


async def test_delete_of_user_deleted_event_tombstones(db):
    store, cal = DeadlineStore(db), FakeCal()
    store.upsert(dl(), T0)
    await sync(store, cal, NAMES, SGT)
    eid = store.get("bs:assignment:1:5").gcal_event_id
    cal.gone.add(eid)
    del cal.events[eid]
    store.mark_gone_missing("brightspace", set())
    await sync(store, cal, NAMES, SGT)
    assert store.is_user_deleted("bs:assignment:1:5")
    store.upsert(dl(), T0)  # comes back from Brightspace: stays deleted
    r = await sync(store, cal, NAMES, SGT)
    assert r.inserted == 0


async def test_one_bad_event_does_not_block_others(db):
    store, cal = DeadlineStore(db), FakeCal()
    cal.fail_titles = ["Broken"]
    store.upsert(dl(key="a", title="Broken", start_utc="2026-10-29T15:29:00+00:00"), T0)
    store.upsert(dl(key="b", title="Fine"), T0)
    r = await sync(store, cal, NAMES, SGT)
    assert r.inserted == 1 and r.errors == 1 and store.get("b").gcal_event_id


async def test_event_is_re_read_before_patch_and_unchanged_sync_reads_nothing(db):
    store, cal = DeadlineStore(db), FakeCal()
    store.upsert(dl(), T0)
    await sync(store, cal, NAMES, SGT)
    await sync(store, cal, NAMES, SGT)
    assert getattr(cal, "reads", 0) == 0 and cal.writes == 1  # no change: zero reads, zero writes
    store.upsert(dl(end_utc="2026-11-02T15:59:00+00:00", start_utc="2026-11-02T15:29:00+00:00"), T0)
    r = await sync(store, cal, NAMES, SGT)
    assert cal.reads == 1 and r.patched == 1


async def test_event_deleted_in_google_is_tombstoned_before_patch(db):
    store, cal = DeadlineStore(db), FakeCal()
    store.upsert(dl(), T0)
    await sync(store, cal, NAMES, SGT)
    eid = store.get("bs:assignment:1:5").gcal_event_id
    del cal.events[eid]  # gone from the calendar, but the patch would not have failed
    writes = cal.writes
    store.upsert(dl(title="Tutorial 5 (moved)"), T0)
    r = await sync(store, cal, NAMES, SGT)
    assert cal.writes == writes and r.user_deleted == 1 and store.is_user_deleted("bs:assignment:1:5")


async def test_event_without_our_key_is_never_patched_or_deleted(db):
    store, cal = DeadlineStore(db), FakeCal()
    store.upsert(dl(), T0)
    await sync(store, cal, NAMES, SGT)
    eid = store.get("bs:assignment:1:5").gcal_event_id
    cal.events[eid] = {"summary": "Dentist"}  # someone else's event now
    writes = cal.writes
    store.upsert(dl(title="Tutorial 5 (moved)"), T0)
    await sync(store, cal, NAMES, SGT)
    assert cal.writes == writes and cal.events[eid] == {"summary": "Dentist"}
    store.upsert(dl(key="k2", title="Other"), T0)
    await sync(store, cal, NAMES, SGT)
    eid2 = store.get("k2").gcal_event_id
    cal.events[eid2] = {"summary": "Not ours"}
    store.mark_gone_missing("brightspace", set())
    writes = cal.writes
    await sync(store, cal, NAMES, SGT)
    assert cal.writes == writes and eid2 in cal.events


async def test_upgrade_with_existing_other_rows_makes_no_calendar_writes_and_keeps_the_due_moment(db):
    from datetime import timedelta
    from core.deadlines import local_date
    from ops.gcal_events import event_body, event_hash
    store, cal = DeadlineStore(db), FakeCal()
    old = dl(key="p:1", title="CA2 Reflection", kind="other", start_utc="2026-11-10T15:29:00+00:00",
             end_utc="2026-11-10T15:59:00+00:00")
    store.upsert(old, T0)
    # what the calendar held before the upgrade: the body of a "due block" (old behaviour for every "other")
    body = event_body(old, NAMES[old.course_code], SGT)
    store.set_event("p:1", "evold", event_hash(body))
    cal.events["evold"] = body
    r = await sync(store, cal, NAMES, SGT)
    assert cal.writes == 0 and getattr(cal, "reads", 0) == 0 and (r.patched, r.inserted) == (0, 0)
    d = store.get("p:1")
    assert not d.as_event and local_date(d, SGT).isoformat() == "2026-11-10"
    assert body["end"]["dateTime"].startswith("2026-11-10T23:59") and body["start"]["dateTime"].startswith("2026-11-10T23:29")
    assert [o["minutes"] for o in body["reminders"]["overrides"]] == [1440, 180]


async def test_transient_failure_of_the_re_read_is_an_error_not_a_tombstone(db):
    import httpx
    store, cal = DeadlineStore(db), FakeCal()
    store.upsert(dl(), T0)
    await sync(store, cal, NAMES, SGT)
    eid = store.get("bs:assignment:1:5").gcal_event_id

    async def boom(_eid):
        raise httpx.HTTPStatusError("503", request=httpx.Request("GET", "http://x"), response=httpx.Response(503))

    cal.get = boom
    writes = cal.writes
    store.upsert(dl(title="Tutorial 5 (moved)"), T0)
    r = await sync(store, cal, NAMES, SGT)
    d = store.get("bs:assignment:1:5")
    assert r.errors == 1 and r.user_deleted == 0 and cal.writes == writes
    assert d.status == "active" and d.gcal_event_id == eid and not store.is_user_deleted(d.key)
