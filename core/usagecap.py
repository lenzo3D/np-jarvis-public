"""Usage cap, "paused to save usage" state and the per-job model choice.

The unit is estimated cost in US dollars. Claude Code reports a real cost for every agent run (chat, Do it); for the
one-shot text jobs the log holds estimated token counts, so their cost is worked out from a small price table. The user
is on a subscription, so the figure is only a yardstick for "how much have I used", never a bill.

Periods follow Singapore time: a day runs midnight to midnight, a week from Monday 00:00. Everything here is plain
functions over the DB so the panel (a separate process) and Ops read the same state.
"""
from __future__ import annotations

import logging
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import hostos

log = logging.getLogger(__name__)

MODELS = ("haiku", "sonnet", "opus")
# job key -> (label shown in the panel, default model; "" means Claude Code's own default)
JOBS = {
    "group_summary": ("Class chat summaries", "haiku"),
    "extract_chats": ("Dates from chats", "haiku"),
    "extract_mail": ("Dates from email", "haiku"),
    "extract_docs": ("Dates from course material", "sonnet"),
    "briefing": ("Morning briefing", "sonnet"),
    "group_reply": ("Group replies", "haiku"),
    "project_tasks": ("Project group tasks", "haiku"),
    "chat": ("Chat", ""),
}
# $ per million tokens (input, output); a rough yardstick, deliberately not exact
PRICES = {"haiku": (1.0, 5.0), "sonnet": (3.0, 15.0), "opus": (15.0, 75.0)}
PERIODS = ("daily", "weekly")
PERIOD_WORD = {"daily": "today", "weekly": "this week"}
WARN_AT = 0.8
MAX_LIMIT = 10_000.0  # a typo guard, far above any real subscription use
_UNSET = object()


# ---------- settings ----------
def get_config(db) -> dict:
    raw = db.get_json_setting("usage_cap", {}) or {}
    return {"daily": _limit(raw.get("daily")), "weekly": _limit(raw.get("weekly")),
            "keep_group_replies": bool(raw.get("keep_group_replies"))}


def _limit(v) -> float | None:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v) or v <= 0:
        return None
    return round(min(v, MAX_LIMIT), 2)


def set_config(db, daily=_UNSET, weekly=_UNSET, keep_group_replies=_UNSET) -> dict:
    """Fields left out keep their saved value; pass "" or None to clear a limit."""
    cur = get_config(db)
    cfg = {"daily": cur["daily"] if daily is _UNSET else _limit(daily),
           "weekly": cur["weekly"] if weekly is _UNSET else _limit(weekly),
           "keep_group_replies": cur["keep_group_replies"] if keep_group_replies is _UNSET else bool(keep_group_replies)}
    db.set_json_setting("usage_cap", cfg)
    return cfg


def get_models(db) -> dict[str, str]:
    saved = db.get_json_setting("job_models", {}) or {}
    return {k: (saved[k] if saved.get(k) in MODELS or (k == "chat" and saved.get(k) == "") else d)
            for k, (_, d) in JOBS.items()}


def set_models(db, models: dict) -> dict[str, str]:
    saved = dict(db.get_json_setting("job_models", {}) or {})  # merge: a partial update keeps the other jobs
    saved.update({k: v for k, v in (models or {}).items() if k in JOBS and (v in MODELS or (k == "chat" and v == ""))})
    db.set_json_setting("job_models", saved)
    return get_models(db)


def model_for(db, job: str) -> str:
    return get_models(db).get(job) or JOBS[job][1] or ""


def chat_model(settings) -> str:
    """The chat model ('' = Claude Code's default), read straight from the DB file because the chat runner only has
    the settings object. Never raises: a missing or locked DB just means the default."""
    try:
        if not Path(settings.db_path).exists():
            return ""
        conn = sqlite3.connect(hostos.sqlite_ro_uri(settings.db_path), uri=True, timeout=2)
        try:
            row = conn.execute("select value from settings where key='job_models'").fetchone()
        finally:
            conn.close()
        import json

        m = (json.loads(row[0]) if row and row[0] else {}).get("chat", "")
        return m if m in MODELS else ""
    except (sqlite3.Error, ValueError, OSError):
        return ""


# ---------- periods and spend ----------
def period_start(now: datetime, tz, kind: str) -> datetime:
    local = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return local - timedelta(days=local.weekday()) if kind == "weekly" else local


def period_end(now: datetime, tz, kind: str) -> datetime:
    start = period_start(now, tz, kind)
    # calendar arithmetic on naive local fields, so a DST zone would still land on midnight
    nxt = start.replace(tzinfo=None) + timedelta(days=7 if kind == "weekly" else 1)
    return nxt.replace(tzinfo=tz)


def row_cost(r) -> float:
    """Reported cost when Claude Code gave one, otherwise estimated from the token counts."""
    if r["cost_usd"] is not None:
        return float(r["cost_usd"])
    price = PRICES.get((r["model"] or "sonnet"), PRICES["sonnet"])
    return ((r["in_tokens"] or 0) * price[0] + (r["out_tokens"] or 0) * price[1]) / 1_000_000


def spent(db, since: datetime, until: datetime | None = None) -> dict:
    out = {"cost": 0.0, "calls": 0, "in_tokens": 0, "out_tokens": 0}
    try:
        # timestamps are stored as UTC text; a day of slack makes the text comparison safe whatever offset `since` has
        floor = (since.astimezone(timezone.utc) - timedelta(days=1)).isoformat()
        rows = db.conn.execute("select * from claude_usage where ts >= ? order by ts", (floor,)).fetchall()
    except sqlite3.Error:
        return out
    for r in rows:
        when = datetime.fromisoformat(r["ts"])
        if when < since or (until is not None and when >= until):
            continue
        out["calls"] += 1
        out["cost"] += row_cost(r)
        out["in_tokens"] += r["in_tokens"] or 0
        out["out_tokens"] += r["out_tokens"] or 0
    out["cost"] = round(out["cost"], 4)
    return out


def snapshot(db, tz, now: datetime) -> dict:
    """Everything the panel, /status and the warnings need, in one place."""
    cfg, resumed = get_config(db), db.get_json_setting("usage_resume", {}) or {}
    periods, paused = {}, False
    for kind in PERIODS:
        start, end, limit = period_start(now, tz, kind), period_end(now, tz, kind), cfg[kind]
        use = spent(db, start, end)
        frac = use["cost"] / limit if limit else 0.0
        over = bool(limit) and frac >= 1.0
        overridden = over and resumed.get(kind) == start.isoformat()
        paused = paused or (over and not overridden)
        periods[kind] = {"limit": limit, "used": round(use["cost"], 2), "calls": use["calls"],
                         "in_tokens": use["in_tokens"], "out_tokens": use["out_tokens"],
                         "fraction": frac, "percent": int(frac * 100) if limit else None, "over": over, "resumed": overridden,
                         "start": start.isoformat(), "resets": end.isoformat()}
    return {"enabled": any(cfg[k] for k in PERIODS), "paused": paused, "periods": periods, **cfg}


def is_paused(db, tz, now: datetime) -> bool:
    if not any(get_config(db)[k] for k in PERIODS):
        return False
    return snapshot(db, tz, now)["paused"]


def paused(ctx) -> bool:
    """True when non-essential Claude jobs should not run right now (cap reached and not resumed)."""
    try:
        return is_paused(ctx.db, ctx.settings.tz, ctx.clock())
    except Exception:  # noqa: BLE001 - a bookkeeping problem must never stop real work
        log.debug("usage cap check failed", exc_info=True)
        return False


def group_replies_paused(ctx) -> bool:
    return paused(ctx) and not get_config(ctx.db)["keep_group_replies"]


def resume(db, tz, now: datetime) -> str:
    """Lift the pause for the rest of the current day/week (whichever caps are over)."""
    snap = snapshot(db, tz, now)
    resumed = db.get_json_setting("usage_resume", {}) or {}
    for kind, p in snap["periods"].items():
        if p["over"]:
            resumed[kind] = p["start"]
    db.set_json_setting("usage_resume", resumed)
    return "resumed"


def describe(snap: dict, address: str = "Sir") -> str:
    """Short factual lines for /status and /usage."""
    if not snap["enabled"]:
        return "Claude usage cap: off"
    parts = []
    for kind in PERIODS:
        p = snap["periods"][kind]
        if p["limit"]:
            parts.append(f"{PERIOD_WORD[kind]} est. ${p['used']:.2f} of ${p['limit']:.2f} ({p['percent']}%)")
    line = "Claude usage: " + "; ".join(parts)
    if snap["paused"]:
        line += ". PAUSED to save usage: class chat summaries, date extraction, project group tasks and group replies (unless kept) are on hold (chat, Do it and the briefing still work). /usage resume to carry on"
    elif any(p["resumed"] for p in snap["periods"].values()):
        line += ". Over the cap, but resumed for the rest of the period"
    return line
