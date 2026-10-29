"""Plain data types shared by core, ops and the API."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Course:
    ou: int
    code: str
    name: str


@dataclass
class Item:
    source: str
    stable_id: str
    course_code: str | None
    kind: str
    title: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class Assignment:
    ou: int
    folder_id: int
    course_code: str
    title: str
    due_at: datetime | None
    end_at: datetime | None
    submitted: bool = False


@dataclass
class GroupMessage:
    chat_id: int
    msg_id: int
    sender: str
    text: str
    ts: datetime


@dataclass
class PendingAction:
    id: int
    kind: str
    params: dict[str, Any]
    summary: str
    status: str
    created_at: datetime
    expires_at: datetime
    result: dict[str, Any] | None = None
