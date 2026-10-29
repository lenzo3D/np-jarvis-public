"""Shared test doubles."""
from __future__ import annotations

from datetime import datetime, timezone

from core.auth import AuthRequired
from core.models import Course, Item


class FakeNotifier:
    def __init__(self):
        self.messages: list[tuple[str, object]] = []
        self.files: list = []
        self.edits: list = []

    async def send(self, text, buttons=None):
        self.messages.append((text, buttons))
        return type("M", (), {"message_id": len(self.messages)})()

    async def edit(self, message_id, text):
        self.edits.append((message_id, text))

    async def send_file(self, path, caption=""):
        self.files.append((path, caption))

    async def send_bytes(self, name, data, caption=""):
        if len(data) > 50 * 1024 * 1024:
            self.messages.append((f"File too large for Telegram: {name}", None))
            return False
        self.files.append((name, caption))
        return True

    async def mfa_prompt(self, site, number):
        self.messages.append((f"mfa {site} {number}", None))


class FakeBS:
    def __init__(self):
        self.course_list = [Course(1001, "INV2101", "Investments")]
        self.assign = {1001: []}
        self.news = {1001: []}
        self.content = {1001: []}
        self.grade = {1001: []}
        self.fail: Exception | None = None
        self.submitted_folders: set[int] = set()
        self.submit_calls: list = []

    async def courses(self):
        if self.fail:
            raise self.fail
        return self.course_list

    async def assignment_items(self, ou, code):
        return list(self.assign.get(ou, []))

    async def announcements(self, ou, code):
        return list(self.news.get(ou, []))

    async def content_items(self, ou, code):
        return list(self.content.get(ou, []))

    async def grades(self, ou, code):
        return list(self.grade.get(ou, []))

    async def submitted(self, ou, folder_id):
        return folder_id in self.submitted_folders

    async def submit(self, ou, folder_id, file, comment=""):
        self.submit_calls.append((ou, folder_id, file, file.read_bytes()))
        return {"status": 200, "file": file.name}

    async def fetch_file(self, ou, topic_id):
        return (f"topic-{topic_id}.docx", b"PK-course-file")

    async def folder_instructions(self, ou):
        return dict(getattr(self, "instructions", {}).get(ou, {}))

    async def calendar(self, start, end, ou_ids=None):
        return list(getattr(self, "cal_events", []))

    async def calendar_range_days(self, days, now=None):
        return []


def assignment(i, due="2026-10-30T15:59:00+00:00", ou=1001):
    return Item("brightspace", f"bs:assignment:{ou}:{i}", "INV2101", "assignment", f"Tutorial {i}",
                {"ou": ou, "folder_id": i, "due_at": due, "end_at": None})


class Clock:
    def __init__(self, now=datetime(2026, 10, 26, 2, 0, tzinfo=timezone.utc)):
        self.now = now

    def __call__(self):
        return self.now


__all__ = ["FakeNotifier", "FakeBS", "assignment", "Clock", "AuthRequired"]
