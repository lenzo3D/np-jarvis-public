"""Read-only month view of the user's Google Calendar for the panel. Nothing here ever changes an event."""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

from core.gcal import CalendarAuthRequired, GCal

TTL = 60.0
JARVIS_ICONS = ("\U0001F4CC", "❓", "\U0001F4DD")  # pinned (confirmed), question mark (tentative), old memo icon


def month_range(year: int, month: int) -> tuple[date, date]:
    """The 6 weeks (Monday first) shown for a month: first day, and the day after the last one."""
    first = date(year, month, 1)
    start = first - timedelta(days=first.weekday())
    return start, start + timedelta(days=42)


def _local(value: dict, tz) -> tuple[datetime | None, date | None]:
    if "dateTime" in value:
        return datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00")).astimezone(tz), None
    if "date" in value:
        return None, date.fromisoformat(value["date"])
    return None, None


def normalise(item: dict, tz) -> dict | None:
    """One Google event -> what the panel needs (dates and times already in the user's time zone)."""
    if item.get("status") == "cancelled":
        return None
    s_dt, s_day = _local(item.get("start", {}), tz)
    e_dt, e_day = _local(item.get("end", {}), tz)
    if s_dt is None and s_day is None:
        return None
    all_day = s_dt is None
    first = s_day if all_day else s_dt.date()
    if all_day:
        last = (e_day - timedelta(days=1)) if e_day and e_day > first else first  # Google's end date is exclusive
    else:
        last = e_dt.date() if e_dt else first
        if e_dt and e_dt.time() == datetime.min.time() and last > first:
            last -= timedelta(days=1)  # ends exactly at midnight: belongs to the previous day
    title = (item.get("summary") or "(no title)").strip()
    private = (item.get("extendedProperties") or {}).get("private") or {}
    jarvis = private.get("jarvis") == "1"
    tentative = jarvis and (title.startswith("❓") or "Tentative" in (item.get("description") or "")[:200])
    for icon in JARVIS_ICONS:
        if title.startswith(icon):
            title = title[len(icon):].strip()
    return {"id": item.get("id"), "title": title, "all_day": all_day, "date": first.isoformat(), "end_date": last.isoformat(),
            "start": None if all_day else s_dt.strftime("%H:%M"), "end": None if all_day or not e_dt else e_dt.strftime("%H:%M"),
            "location": item.get("location") or "", "link": item.get("htmlLink") or "", "jarvis": jarvis,
            "tentative": tentative}


class CalendarView:
    def __init__(self, settings, gcal: GCal | None = None, clock=time.monotonic):
        self.settings = settings
        self._gcal = gcal
        self._clock = clock
        self._cache: dict[tuple, tuple[float, dict]] = {}

    def _client(self) -> GCal | None:
        if self._gcal is None:
            token = Path(self.settings.state_dir) / "google_token.json"
            if not token.exists():
                return None
            self._gcal = GCal(token_path=token, client_path=Path(self.settings.state_dir) / "google_client.json")
        return self._gcal

    async def month(self, year: int, month: int, refresh: bool = False) -> dict:
        key = (year, month)
        hit = self._cache.get(key)
        if hit and not refresh and self._clock() - hit[0] < TTL:
            return hit[1]
        tz = self.settings.tz
        client = self._client()
        if client is None:
            state = "setup" if not (Path(self.settings.state_dir) / "google_client.json").exists() else "login"
            return {"ok": False, "state": state, "message": "Google Calendar is not connected yet."}
        start, end = month_range(year, month)
        lo = datetime.combine(start, datetime.min.time(), tzinfo=tz).isoformat()
        hi = datetime.combine(end, datetime.min.time(), tzinfo=tz).isoformat()
        try:
            items = await client.list_events(lo, hi)
        except CalendarAuthRequired:
            self._gcal = None
            return {"ok": False, "state": "login", "message": "Google Calendar needs you to sign in again."}
        except (httpx.HTTPError, OSError) as e:
            return {"ok": False, "state": "error", "message": f"Could not reach Google Calendar: {type(e).__name__}"}
        events = [e for e in (normalise(i, tz) for i in items) if e]
        out = {"ok": True, "year": year, "month": month, "tz": getattr(tz, "key", str(tz)),
               "range": [start.isoformat(), (end - timedelta(days=1)).isoformat()], "events": events}
        self._cache[key] = (self._clock(), out)
        return out
