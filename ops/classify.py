"""Alert level rules (spec section 4.8)."""
from __future__ import annotations

import re
from typing import Literal

from ops.diff import Change

Level = Literal["red", "yellow", "white"]

URGENT_RE = re.compile(
    r"(?i)\b(test|quiz|exam|deadline|due|submission|submit|cancel+ed|postponed|venue|room change)\b"
)
ALWAYS_RED = {"teams_mention", "teams_dm", "group_action", "grade"}
YELLOW = {"announcement", "content", "teams_post", "assignment"}


def classify(change: Change) -> Level:
    kind = change.item.kind
    if kind in ALWAYS_RED:
        return "red"
    if kind == "email":  # core/mail.py decided important / normal / bulk when the message arrived
        return {"important": "red", "bulk": "white"}.get(change.item.payload.get("category"), "yellow")
    if kind == "assignment":
        if change.kind == "new" or {"due_at", "end_at"} & change.changed_fields.keys():
            return "red"
        return "yellow"
    if kind == "announcement":
        text = change.item.title + " " + str(change.item.payload.get("body_text", ""))
        return "red" if URGENT_RE.search(text) else "yellow"
    if kind in YELLOW:
        return "yellow"
    return "white"
