"""Minimal Google Calendar client for the user's primary calendar (scope: calendar.events only).

One-time setup (the user does this; JARVIS never sees the Google password):
    1. Google Cloud Console → new project → enable "Google Calendar API"
    2. OAuth consent screen (External, add yourself as a test user)
    3. Credentials → Create OAuth client ID → Desktop app → download JSON → save as state/google_client.json
    4. .venv/bin/python -m core.gcal login     (opens your browser to approve)
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Awaitable, Callable

import httpx

API = "https://www.googleapis.com/calendar/v3"
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


class CalendarAuthRequired(Exception):
    pass


class EventGone(Exception):
    """The event was deleted (by the user) or no longer exists."""


class EventExists(Exception):
    """An event with this id already exists (insert with a deterministic id)."""


class GCal:
    def __init__(self, token_path: Path | None = None, client_path: Path | None = None,
                 token_getter: Callable[..., Awaitable[str]] | None = None, calendar_id: str = "primary"):
        self.token_path = Path(token_path) if token_path else None
        self.client_path = Path(client_path) if client_path else None
        self._getter = token_getter or self._token_from_file
        self.calendar_id = calendar_id
        self._creds = None

    # ---------- auth ----------
    async def _token_from_file(self, force_refresh: bool = False) -> str:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        if self._creds is None:
            if not self.token_path or not self.token_path.exists():
                raise CalendarAuthRequired("Google Calendar not connected: run `python -m core.gcal login`")
            try:
                self._creds = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)
            except (ValueError, OSError) as e:
                raise CalendarAuthRequired(f"Google token file unreadable: run `python -m core.gcal login` ({e})") from e
        if force_refresh or not self._creds.valid:
            try:
                await asyncio.to_thread(self._creds.refresh, Request())
            except RefreshError as e:
                raise CalendarAuthRequired(f"Google sign-in expired: run `python -m core.gcal login` ({e})") from e
            _write_private(self.token_path, self._creds.to_json())
        return self._creds.token

    async def _request(self, method: str, path: str, **kw) -> httpx.Response:
        for attempt in (0, 1):
            token = await self._getter(force_refresh=attempt == 1)
            async with httpx.AsyncClient(base_url=API, timeout=30,
                                         headers={"Authorization": f"Bearer {token}"}) as c:
                r = await c.request(method, path, **kw)
            if r.status_code != 401:
                break
        if r.status_code == 401:
            raise CalendarAuthRequired("Google Calendar rejected the token")
        if r.status_code in (404, 410):
            raise EventGone(path)
        if r.status_code == 409:
            raise EventExists(path)
        r.raise_for_status()
        return r

    # ---------- events ----------
    def _events(self) -> str:
        return f"/calendars/{self.calendar_id}/events"

    async def insert(self, body: dict) -> str:
        return (await self._request("POST", self._events(), json=body)).json()["id"]

    async def patch(self, event_id: str, body: dict) -> None:
        data = (await self._request("PATCH", f"{self._events()}/{event_id}", json=body)).json()
        if data.get("status") == "cancelled":
            raise EventGone(event_id)

    async def delete(self, event_id: str) -> None:
        await self._request("DELETE", f"{self._events()}/{event_id}")

    async def list_events(self, time_min: str, time_max: str, limit: int = 500) -> list[dict]:
        """Read-only: events between two RFC 3339 timestamps (recurring events expanded, soonest first)."""
        items: list[dict] = []
        params = {"timeMin": time_min, "timeMax": time_max, "singleEvents": "true", "orderBy": "startTime",
                  "maxResults": 250, "showDeleted": "false"}
        while len(items) < limit:
            data = (await self._request("GET", self._events(), params=params)).json()
            items += data.get("items", [])
            token = data.get("nextPageToken")
            if not token:
                break
            params = {**params, "pageToken": token}
        return items[:limit]


def _write_private(path: Path, text: str) -> None:
    """Atomic 0600 write: a crash mid-write never leaves a corrupt token."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def login() -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow

    from core.config import load_settings

    s = load_settings()
    client = s.state_dir / "google_client.json"
    if not client.exists():
        raise SystemExit(f"Save your OAuth client JSON (Desktop app) as {client} first. See core/gcal.py docstring.")
    flow = InstalledAppFlow.from_client_secrets_file(str(client), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    _write_private(s.state_dir / "google_token.json", creds.to_json())
    print("Google Calendar connected.")


if __name__ == "__main__":
    if sys.argv[1:] != ["login"]:
        raise SystemExit("usage: python -m core.gcal login")
    login()
