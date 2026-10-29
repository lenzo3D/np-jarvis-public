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


def detect(db: DB, source: str, items: list[Item], now: datetime) -> list[Change]:
    baseline = not db.has_source(source) and db.get_setting(f"baseline:{source}") != "1"
    changes: list[Change] = []
    for item in items:
        h = content_hash(item)
        row = db.get_item(source, item.stable_id)
        if not baseline:
            if row is None:
                changes.append(Change("new", item))
            elif row["content_hash"] != h:
                old_payload = json.loads(row["payload_json"])
                changes.append(Change("changed", item, _field_diff(row["title"], old_payload, item)))
        db.upsert_item(item, h, now)
    db.set_setting(f"baseline:{source}", "1")
    return changes
