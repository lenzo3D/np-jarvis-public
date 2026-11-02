"""Usage cap warnings (Telegram) and the /usage command. The arithmetic lives in core/usagecap.py."""
from __future__ import annotations

import asyncio
import logging

from core import usagecap
from ops import persona

log = logging.getLogger(__name__)
RESUME_CALLBACK = "usage:resume"
_lock: asyncio.Lock | None = None


async def check(ctx) -> list[str]:
    """Send the 80% and 100% warnings, once per period each. Returns what was sent, e.g. ["daily:80"]."""
    global _lock
    _lock = _lock or asyncio.Lock()
    sent: list[str] = []
    async with _lock:
        now, tz, db = ctx.clock(), ctx.settings.tz, ctx.db
        cfg = usagecap.get_config(db)
        if not any(cfg[k] for k in usagecap.PERIODS):
            return sent
        snap = usagecap.snapshot(db, tz, now)
        state = db.get_json_setting("usage_warned", {}) or {}
        for kind in usagecap.PERIODS:
            p = snap["periods"][kind]
            if not p["limit"]:
                continue
            st = state.get(kind) or {}
            if st.get("period") != p["start"] or st.get("limit") != p["limit"]:
                st = {"period": p["start"], "limit": p["limit"], "80": False, "100": False}  # new day/week or new cap
            frac = p["fraction"]
            level = 100 if frac >= 1.0 else 80 if frac >= usagecap.WARN_AT else 0
            if not level or st[str(level)]:
                state[kind] = st
                continue
            st["80"], st["100"] = True, st["100"] or level == 100
            state[kind] = st
            db.set_json_setting("usage_warned", state)  # recorded before sending, so a crash can never double-send
            buttons = [[(f"Resume for the rest of {usagecap.PERIOD_WORD[kind]}", RESUME_CALLBACK)]] if level == 100 else None
            await ctx.notifier.send(persona.usage_warning(ctx.settings, kind, level, p["used"], p["limit"]), buttons)
            sent.append(f"{kind}:{level}")
        db.set_json_setting("usage_warned", state)
    return sent


def schedule_check(ctx) -> None:
    """Called after every recorded Claude call: look at the cap without blocking the call itself."""
    try:
        asyncio.get_running_loop().create_task(_safe_check(ctx))
    except RuntimeError:
        pass  # no event loop (e.g. a thread); the 5-minute job will catch it


async def _safe_check(ctx) -> None:
    try:
        await check(ctx)
    except Exception:  # noqa: BLE001
        log.debug("usage check failed", exc_info=True)


async def command(ctx, *args: str) -> str:
    """/usage [resume]"""
    s, now = ctx.settings, ctx.clock()
    if args and args[0] == "resume":
        snap = usagecap.snapshot(ctx.db, s.tz, now)
        if not snap["paused"]:
            return persona.usage_not_paused(s)
        usagecap.resume(ctx.db, s.tz, now)
        return persona.usage_resumed(s)
    return usagecap.describe(usagecap.snapshot(ctx.db, s.tz, now))


async def on_resume_button(ctx, _arg: str = "") -> str:
    return await command(ctx, "resume")
