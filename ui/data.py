"""Read-only views of JARVIS state for the panel. Opens the SQLite file read-only, so it never competes with Ops."""
from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone

from ops.gcal_events import short_course


def _connect(settings) -> sqlite3.Connection | None:
    if not settings.db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _setting(conn, key: str, default=None):
    try:
        row = conn.execute("select value from settings where key=?", (key,)).fetchone()
    except sqlite3.Error:
        return default
    return row["value"] if row and row["value"] not in (None, "") else default


def checks(settings) -> list[dict]:
    """Setup checklist. Reports presence only; never reads secret values."""
    return [
        {"name": "Claude Code installed", "ok": shutil.which("claude") is not None,
         "hint": "Install Claude Code and sign in once with: claude"},
        {"name": "Telegram bot configured", "ok": bool(settings.ops_bot_token and settings.allowed_telegram_id),
         "hint": "Set OPS_BOT_TOKEN and ALLOWED_TELEGRAM_ID in secrets.env"},
        {"name": "Semester start date set", "ok": bool(settings.semester_start),
         "hint": "Set SEMESTER_START=YYYY-MM-DD (Monday of week 1) in secrets.env, then restart JARVIS"},
    ]


# How old the last successful check may be before the connection no longer counts as live.
# POLITEMall is checked every 15 min from 07:00 to 23:59 and hourly overnight; Teams every 10 min.
FRESH = {"brightspace_day": timedelta(minutes=45), "brightspace_night": timedelta(minutes=135),
         "teams": timedelta(minutes=30)}
SITES = (("brightspace", "POLITEMall", "login_brightspace"), ("teams", "Teams", "login_teams"))
COLORS = {"live": "green", "connected": "green", "stale": "amber", "starting": "amber", "offline": "grey",
          "unconfigured": "grey", "login": "red", "failing": "red", "pick": "amber"}


def _has_session(settings, site: str) -> bool:
    sessions = settings.state_dir / "sessions"
    return any((sessions / f"{site}{ext}").exists() for ext in (".cookies.json", ""))


def _ago(delta: timedelta) -> str:
    s = max(0, int(delta.total_seconds()))
    if s < 90:
        return "just now"
    if s < 5400:
        return f"{round(s / 60)} min ago"
    if s < 129600:
        return f"{round(s / 3600)} h ago"
    return f"{round(s / 86400)} days ago"


def connections(settings, ops_mode: str, now: datetime | None = None) -> list[dict]:
    """Per-service connection state. Green means logged in AND checked recently by a running JARVIS."""
    now = now or datetime.now(timezone.utc)
    running = ops_mode in ("running", "external")
    conn = _connect(settings)
    out = []
    for site, name, job in SITES:
        health = prompted = None
        if conn is not None:
            try:
                health = conn.execute("select * from source_health where source=?", (site,)).fetchone()
            except sqlite3.Error:
                pass
            prompted = _setting(conn, f"auth_prompted:{site}") == "1"
        last_ok = datetime.fromisoformat(health["last_ok"]) if health and health["last_ok"] else None
        failures = health["consecutive_failures"] if health else 0
        if site == "teams":
            limit = FRESH["teams"]
        else:
            hour = now.astimezone(settings.tz).hour
            limit = FRESH["brightspace_day"] if hour >= 7 else FRESH["brightspace_night"]
        age = now - last_ok if last_ok else None
        if prompted:
            state, detail = "login", "The session expired. Log in again to resume alerts."
        elif failures >= 3:
            state, detail = "failing", f"{failures} checks in a row failed. See the Logs tab."
        elif last_ok is None and not _has_session(settings, site):
            state, detail = "login", "No login saved yet."
        elif ops_mode == "starting":
            state, detail = "starting", "JARVIS is starting."
        elif not running:
            state = "offline"
            detail = (f"JARVIS is not running. Last confirmed {_ago(age)}." if age is not None
                      else "JARVIS is not running, so the login cannot be checked.")
        elif age is None:
            state, detail = "stale", "Waiting for the first check."
        elif age > limit:
            state, detail = "stale", f"Last successful check {_ago(age)}, later than expected."
        else:
            state, detail = "live", f"Logged in. Last checked {_ago(age)}."
        out.append({"id": site, "name": name, "state": state, "color": COLORS[state], "detail": detail,
                    "last_ok": last_ok.isoformat() if last_ok else None,
                    "action": job if state in ("login", "failing") else None, "relogin": job})
    # Google Calendar: the token refreshes by itself; it only needs attention when Google rejects it
    token, client = settings.state_dir / "google_token.json", settings.state_dir / "google_client.json"
    warned = conn is not None and _setting(conn, "gcal_warned") == "1"
    if warned:
        state, detail = "login", "Google rejected the saved login. Connect it again."
    elif token.exists():
        state, detail = "connected", "Deadlines sync to your primary calendar."
    elif client.exists():
        state, detail = "login", "Not connected yet."
    else:
        state, detail = "unconfigured", "Needs state/google_client.json first (see deploy/README.md)."
    out.append({"id": "google", "name": "Google Calendar", "state": state, "color": COLORS[state], "detail": detail,
                "last_ok": None, "action": "login_google" if state == "login" else None, "relogin": "login_google"})
    out.append(_telegram_connection(settings, conn, running))
    out.append(_outlook_connection(settings, conn, running, now))
    if conn is not None:
        conn.close()
    return out


class _ReadOnlySettings:
    """Just enough of DB for core.groups and ui.chats, on top of a read-only connection."""

    def __init__(self, conn):
        self.conn = conn

    def get_json_setting(self, key: str, default):
        raw = _setting(self.conn, key) if self.conn is not None else None
        try:
            return json.loads(raw) if raw else default
        except ValueError:
            return default


def _outlook_connection(settings, conn, running: bool, now: datetime) -> dict:
    """Optional feature: grey until the user logs in to Outlook; then the same live/overdue rules as the other sources."""
    saved = (settings.state_dir / "sessions" / "outlook.cookies.json").exists()
    health = prompted = status = None
    count = 0
    if conn is not None:
        try:
            health = conn.execute("select * from source_health where source='outlook'").fetchone()
            count = conn.execute("select count(*) from emails").fetchone()[0]
        except sqlite3.Error:
            pass
        prompted = _setting(conn, "auth_prompted:outlook") == "1"
        try:
            status = json.loads(_setting(conn, "outlook_status") or "{}")
        except ValueError:
            status = {}
    last_ok = datetime.fromisoformat(health["last_ok"]) if health and health["last_ok"] else None
    failures = health["consecutive_failures"] if health else 0
    hour = now.astimezone(settings.tz).hour
    limit = FRESH["brightspace_day"] if hour >= 7 else FRESH["brightspace_night"]
    action, label = None, None
    if not saved:
        state, detail, action, label = "unconfigured", "Optional. Log in to let JARVIS read your school email.", "login_outlook", "Connect"
    elif prompted:
        state, detail, action = "login", "The Outlook session expired. Log in again to resume.", "login_outlook"
    elif (status or {}).get("state") == "no_access" or failures >= 3:
        state, detail, action, label = "failing", "Logged in, but the mailbox could not be read yet. Run the access check.", "probe_outlook", "Check access"
    elif not running:
        state, detail = "offline", "JARVIS is not running, so email cannot be confirmed." + (f" Last confirmed {_ago(now - last_ok)}." if last_ok else "")
    elif last_ok is None:
        state, detail = "stale", "Waiting for the first check."
    elif now - last_ok > limit:
        state, detail = "stale", f"Last successful check {_ago(now - last_ok)}, later than expected."
    else:
        state, detail = "live", f"Reading your inbox ({count} messages stored). Last checked {_ago(now - last_ok)}."
    return {"id": "outlook", "name": "Outlook email", "state": state, "color": COLORS[state], "detail": detail,
            "last_ok": last_ok.isoformat() if last_ok else None, "action": action, "action_label": label, "relogin": "login_outlook"}


def _telegram_connection(settings, conn, running: bool) -> dict:
    from ui import chats

    t = chats.telegram_state(settings, _ReadOnlySettings(conn), running)
    n = t["groups"]
    table = {
        "offline": ("offline", f"JARVIS is not running, so the {n} group(s) cannot be confirmed."),
        "login": ("login", "Telegram needs you to log in again. Use the Chats tab."),
        "failing": ("failing", "The Telegram reader keeps failing. See the Logs tab."),
        "stale": ("stale", "Not collecting messages yet. Check that the bot's group privacy is off (see the Chats tab)."),
        "live": ("live", f"Reading {n} group(s). The bot only listens: it never posts or replies."),
    }
    if t["stage"] == "setup":
        state, detail = "unconfigured", "Add the JARVIS bot to your class or project group (Chats tab), or log in with your own account."
    elif t["stage"] == "pick":
        state, detail = "pick", "Groups found. Choose which ones JARVIS should read in the Chats tab."
    else:
        state, detail = table.get(t["health"], ("stale", "Checking."))
    return {"id": "telegram", "name": "Telegram groups", "state": state, "color": COLORS[state], "detail": detail,
            "last_ok": None, "action": None, "goto": "chats" if state in ("unconfigured", "login", "pick") else None,
            "relogin": None}


def overview(settings) -> dict:
    conn = _connect(settings)
    out = {"health": [], "pending": [], "controls": {}, "counts": {}, "checks": checks(settings)}
    if conn is None:
        return out
    try:
        for r in conn.execute("select * from source_health order by source"):
            out["health"].append({"source": r["source"], "last_ok": r["last_ok"],
                                  "failures": r["consecutive_failures"]})
        for r in conn.execute("select id, kind, summary, created_at, expires_at from pending_actions "
                              "where status='pending' order by id"):
            out["pending"].append({k: r[k] for k in r.keys()})
        muted = _setting(conn, "muted")
        out["controls"] = {"paused": _setting(conn, "paused") == "1", "quiet": _setting(conn, "quiet"),
                           "muted": json.loads(muted) if muted else [],
                           "briefing_time": _setting(conn, "briefing_time", "07:00")}
        for name, sql in (("deadlines", "select count(*) from deadlines where status='active'"),
                          ("materials", "select count(*) from materials")):
            out["counts"][name] = conn.execute(sql).fetchone()[0]
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return out


def deadlines(settings, days: int = 60, now: datetime | None = None) -> list[dict]:
    conn = _connect(settings)
    if conn is None:
        return []
    now = now or datetime.now(timezone.utc)
    tz = settings.tz
    try:
        raw = _setting(conn, "course_names")
        names = json.loads(raw) if raw else {}
        today = now.astimezone(tz).date()
        horizon = today + timedelta(days=days)
        out = []
        for r in conn.execute("select * from deadlines where status='active' order by start_utc"):
            start = datetime.fromisoformat(r["end_utc"] or r["start_utc"]).astimezone(tz)
            if not (today <= start.date() <= horizon):
                continue
            all_day = bool(r["all_day"])
            out.append({"title": r["title"], "course": short_course(names.get(r["course_code"] or "", ""), r["course_code"]),
                        "kind": r["kind"], "date": start.strftime("%a %d %b"), "time": None if all_day else start.strftime("%H:%M"),
                        "confirmed": r["confidence"] == "confirmed", "source": r["source"], "link": r["source_ref"] or None,
                        "sort": start.isoformat()})
        return sorted(out, key=lambda d: d["sort"])
    except (sqlite3.Error, ValueError):
        return []
    finally:
        conn.close()


def usage(settings, days: int = 7, now: datetime | None = None) -> dict:
    """Claude calls per job and per day. Token counts are real for agent runs and estimates (about 4 characters per
    token) for the text jobs, flagged by `estimated`."""
    out = {"days": days, "total": {"calls": 0, "failed": 0, "seconds": 0.0, "in_tokens": 0, "out_tokens": 0},
           "by_job": [], "by_day": []}
    conn = _connect(settings)
    if conn is None:
        return out
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat()
    try:
        rows = conn.execute("select * from claude_usage where ts >= ? order by ts", (since,)).fetchall()
    except sqlite3.Error:  # Ops has not created the table yet
        return out
    finally:
        conn.close()
    jobs: dict[str, dict] = {}
    per_day: dict[str, int] = {}
    for r in rows:
        j = jobs.setdefault(r["job"], {"job": r["job"], "calls": 0, "failed": 0, "seconds": 0.0, "in_tokens": 0,
                                       "out_tokens": 0, "cache_tokens": 0, "estimated": False, "cost_usd": 0.0,
                                       "turns": 0, "last": None})
        j["calls"] += 1
        j["failed"] += 0 if r["ok"] else 1
        j["seconds"] += r["seconds"] or 0
        j["in_tokens"] += r["in_tokens"] or 0
        j["out_tokens"] += r["out_tokens"] or 0
        j["cache_tokens"] += r["cache_tokens"] or 0
        j["estimated"] = j["estimated"] or bool(r["estimated"])
        j["cost_usd"] += r["cost_usd"] or 0
        j["turns"] += r["turns"] or 0
        j["last"] = r["ts"]
        day = datetime.fromisoformat(r["ts"]).astimezone(settings.tz).strftime("%Y-%m-%d")
        per_day[day] = per_day.get(day, 0) + 1
    out["by_job"] = sorted(jobs.values(), key=lambda j: -j["calls"])
    for j in out["by_job"]:
        j["seconds"] = round(j["seconds"], 1)
        j["cost_usd"] = round(j["cost_usd"], 2)
        for k in ("calls", "failed", "seconds", "in_tokens", "out_tokens"):
            out["total"][k] += j[k]
    out["total"]["seconds"] = round(out["total"]["seconds"], 1)
    out["by_day"] = [{"day": d, "calls": c} for d, c in sorted(per_day.items())]
    return out
