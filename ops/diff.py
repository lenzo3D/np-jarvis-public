"""Change detection: stable id + content hash per item (spec section 4.8)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from core.db import DB
from core.models import Item


@dataclass
class Change:
    kind: Literal["new", "changed"]
    item: Item
    changed_fields: dict[str, tuple[Any, Any]] = field(default_factory=dict)


def content_hash(item: Item) -> str:
    blob = item.title + "\x00" + json.dumps(item.payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _field_diff(old_title: str, old_payload: dict, item: Item) -> dict[str, tuple[Any, Any]]:
    out: dict[str, tuple[Any, Any]] = {}
    if old_title != item.title:
        out["title"] = (old_title, item.title)
    for k in sorted(set(old_payload) | set(item.payload)):
        if old_payload.get(k) != item.payload.get(k):
            out[k] = (old_payload.get(k), item.payload.get(k))
    return out


def _unseen_courses(db: DB, source: str, items: list[Item]) -> dict[int, list[Item]]:
    """Brightspace courses (by org unit id, not by code) in this sweep that have never been baselined and none of
    whose items JARVIS already stores -> their items. A course whose code changed, or an old course that just had no
    items, is not new while any of its item ids is already known or it was flagged `baseline:<source>:ou:<ou>`."""
    if source != "brightspace":
        return {}
    by_ou: dict[int, list[Item]] = {}
    for it in items:
        ou = it.payload.get("ou")
        if isinstance(ou, int):
            by_ou.setdefault(ou, []).append(it)
    out = {}
    for ou, its in by_ou.items():
        if db.get_setting(f"baseline:{source}:ou:{ou}") == "1":
            continue
        ids = [i.stable_id for i in its]
        known = any(db.conn.execute(
            f"select 1 from items where source=? and stable_id in ({','.join('?' * len(ids[k:k + 500]))}) limit 1",
            (source, *ids[k:k + 500])).fetchone() for k in range(0, len(ids), 500))
        if not known:
            out[ou] = its
    return out


MAX_DEFERRALS = 3


def detect(db: DB, source: str, items: list[Item], now: datetime, new_courses: dict[str, int] | None = None,
           complete: set[int] | None = None) -> list[Change]:
    """Changes since the last sweep. The very first sweep of a source, and the first sweep that shows a course, are
    silent baselines. `new_courses` (a dict) is filled with {course code: item count} for courses baselined now.
    `complete` is the set of course ou ids whose fetches ALL succeeded (None = all): a course not yet seen is only
    baselined from a complete read; a partial one is skipped until a complete read, so its missing half never floods."""
    baseline = not db.has_source(source) and db.get_setting(f"baseline:{source}") != "1"
    unseen = _unseen_courses(db, source, items)
    deferred = {ou for ou in unseen if complete is not None and ou not in complete}
    for ou in sorted(deferred):  # a course that never reads completely is baselined anyway after a few sweeps
        key = f"deferred:{source}:ou:{ou}"
        n = int(db.get_setting(key) or 0) + 1
        db.set_setting(key, str(n))
        if n >= MAX_DEFERRALS:
            deferred.discard(ou)
    fresh = {ou: its for ou, its in unseen.items() if ou not in deferred}
    if new_courses is not None and not baseline:
        for its in fresh.values():
            code = next((i.course_code for i in its if i.course_code), "")
            new_courses[code] = new_courses.get(code, 0) + len(its)
    skip = {i.stable_id for ou in deferred for i in unseen[ou]}
    silent = {i.stable_id for its in fresh.values() for i in its}
    changes: list[Change] = []
    for item in items:
        if item.stable_id in skip:
            continue
        h = content_hash(item)
        row = db.get_item(source, item.stable_id)
        if not baseline and item.stable_id not in silent:
            if row is None:
                changes.append(Change("new", item))
            elif row["content_hash"] != h:
                old_payload = json.loads(row["payload_json"])
                diff = _field_diff(row["title"], old_payload, item)
                if set(diff) != {"ou"} or diff["ou"][0] is not None:  # "ou" newly added to a payload is no change
                    changes.append(Change("changed", item, diff))
        db.upsert_item(item, h, now)
    db.set_setting(f"baseline:{source}", "1")
    if source == "brightspace":
        ous = {o for o in (i.payload.get("ou") for i in items) if isinstance(o, int)}
        for ou in (ous - deferred) if complete is None else (set(complete) | (ous - deferred)):
            db.set_setting(f"baseline:{source}:ou:{ou}", "1")
    return changes
