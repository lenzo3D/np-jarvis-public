"""Keeps the Claude sign-in state fresh for the panel's Claude light, and tells the user on Telegram when it is lost."""
from __future__ import annotations

import logging

from ops import claude_runner, persona
from ops.claude_runner import ClaudeError

log = logging.getLogger(__name__)
ALERTED = "claude_auth_alerted"


async def check(ctx) -> str | None:
    """Returns "signed_out" or "signed_in" when it sent a Telegram message, else None."""
    db, run_as = ctx.db, ctx.settings.claude_run_as
    status = await claude_runner.auth_status(run_as)
    if status is False:
        claude_runner.record_auth(False, "claude auth status: not signed in")
    elif status is True:
        if claude_runner.stored_auth(db).get("ok") is False:
            # Signed in again (or the last failure was a blip): one tiny call settles it and records the result
            try:
                with claude_runner.labelled("claude_check"):
                    run_text = ctx.extra.get("run_text", claude_runner.run_text)
                    await run_text("Reply with the single word OK.", "", model="haiku", timeout=60, run_as=run_as)
            except ClaudeError:
                log.info("Claude sign-in check call failed", exc_info=True)
        else:
            claude_runner.record_auth(True)
    ok = claude_runner.stored_auth(db).get("ok")
    alerted = db.get_setting(ALERTED) == "1"
    if ok is False and not alerted:
        db.set_setting(ALERTED, "1")  # recorded before sending, so a crash can never double-send
        await ctx.notifier.send(persona.claude_signed_out(ctx.settings))
        return "signed_out"
    if ok is True and alerted:
        db.set_setting(ALERTED, "0")
        await ctx.notifier.send(persona.claude_signed_in(ctx.settings))
        return "signed_in"
    return None
