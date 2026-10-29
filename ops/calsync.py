"""Reconcile the deadlines table with Google Calendar. Only touches events JARVIS created (by stored id).

Event ids are derived from the deadline key, so a retried or concurrent insert can never create a second copy.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from core.deadlines import DeadlineStore
from core.gcal import CalendarAuthRequired, EventExists, EventGone
from ops.gcal_events import event_body, event_hash

log = logging.getLogger(__name__)


@dataclass
class SyncReport:
    inserted: int = 0
    patched: int = 0
    deleted: int = 0
    user_deleted: int = 0
    errors: int = 0


def event_id_for(key: str) -> str:
    """Google event ids use base32hex (0-9, a-v), 5-1024 chars. Hex digits are a subset."""
    return "jv" + hashlib.sha1(key.encode()).hexdigest()


async def _sync_one(store: DeadlineStore, gcal, d, course_names, tz, rep: SyncReport) -> None:
    if d.status == "gone":
        try:
            await gcal.delete(d.gcal_event_id)
            store.set_event(d.key, None, None)
        except EventGone:  # the user already deleted it: remember, never re-add
            store.mark_user_deleted(d.key)
        rep.deleted += 1
        return
    body = event_body(d, course_names.get(d.course_code or "", ""), tz)
    h = event_hash(body)
    eid = d.gcal_event_id or event_id_for(d.key)
    try:
        if not d.gcal_event_id:
            try:
                await gcal.insert({**body, "id": eid})
                rep.inserted += 1
            except EventExists:
                await gcal.patch(eid, body)
                rep.patched += 1
            store.set_event(d.key, eid, h)
        elif h != d.gcal_hash:
            await gcal.patch(eid, body)
            store.set_event(d.key, eid, h)
            rep.patched += 1
    except EventGone:
        store.mark_user_deleted(d.key)
        rep.user_deleted += 1


async def sync(store: DeadlineStore, gcal, course_names: dict[str, str], tz) -> SyncReport:
    rep = SyncReport()
    for d in store.needing_sync():
        try:
            await _sync_one(store, gcal, d, course_names, tz, rep)
        except CalendarAuthRequired:
            raise
        except Exception as e:  # noqa: BLE001 - one bad event must not block the rest
            log.warning("calendar sync failed for %s: %s", d.key, e)
            rep.errors += 1
    return rep
