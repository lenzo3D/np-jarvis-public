"""Email sweep: new Outlook mail -> local store, alerts for what matters, dates found inside messages."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from core import mail as mailcore
from core.mail import Mail, MailStore
from core.models import Item
from core.outlook import MailUnavailable
from ops.watcher import Services, run_source

log = logging.getLogger(__name__)
SWEEP_LIMIT = 100  # per sweep; the reader pages through the inbox until it has this many
FIRST_LIMIT = 100
CATCHUP_LIMIT = 500  # five pages of 100, the reader's page cap
LOOKBACK = timedelta(days=3)
MAX_LOOKBACK = timedelta(days=14)  # after a long outage, go back to the newest stored mail but no further than this
RED_AFTER = 3  # consecutive failed sweeps before the indicator turns red (same count run_source uses to alert)
EXTRACT_PER_SWEEP = 8


def school_domains(settings) -> tuple[str, ...]:
    return mailcore.SCHOOL_DOMAINS + tuple(getattr(settings, "school_mail_domains", ()) or ())


def to_item(m: Mail, category: str) -> Item:
    # Read/unread and the body are deliberately not part of the item: opening a mail must not look like a change.
    return Item("outlook", f"outlook:{m.id}", None, "email", m.subject[:120], {
        "sender": m.sender_name, "address": m.sender_addr, "received": m.received.isoformat(), "category": category,
        "preview": m.preview[:300], "link": m.web_link})


def configured(ctx) -> bool:
    """Only start once the user has logged in to Outlook (a saved session exists), so nothing nags beforehand."""
    sessions = ctx.sessions
    return sessions is not None and hasattr(sessions, "_cookie_file") and sessions._cookie_file("outlook").exists()


def _since(store: MailStore, now):
    """Where to start reading: at least the last LOOKBACK, and further back (up to MAX_LOOKBACK) when the newest stored
    mail is older than that, so a long outage cannot leave a gap. Mail is stored by id, so overlap never duplicates."""
    start = now - LOOKBACK
    last = store.stats()["last"]
    if last:
        try:
            start = min(start, datetime.fromisoformat(last) - timedelta(hours=6))
        except ValueError:
            pass
    return max(start, now - MAX_LOOKBACK)


async def sweep_outlook(ctx: Services) -> list:
    reader = ctx.extra.get("outlook")
    if reader is None or not configured(ctx):
        return []
    store, now, new_mails = MailStore(ctx.db), ctx.clock(), []

    async def collect() -> list[Item]:
        # The first successful sweep is a silent baseline (no alerts for mail that was already there).
        first = ctx.db.get_setting("baseline:outlook") != "1" and store.stats()["count"] == 0
        try:
            since = None if first else _since(store, now)
            # Catching up after an outage can mean more than one page of mail: read several, not just the newest 100.
            catching_up = since is not None and since < now - LOOKBACK
            mails = await reader.recent(since, FIRST_LIMIT if first else CATCHUP_LIMIT if catching_up else SWEEP_LIMIT)
        except MailUnavailable as e:
            # One bad sweep (a network blip, a 5xx, an expired token that the next sweep renews) must not turn the
            # indicator red: only the third failure in a row does, like the other sources (run_source counts them). The exception
            # is a hard refusal (the saved method returned 401 even with freshly read tokens): that will not fix itself.
            row = ctx.db.conn.execute("select consecutive_failures from source_health where source='outlook'").fetchone()
            if getattr(e, "hard", False) or (row[0] if row else 0) + 1 >= RED_AFTER:
                ctx.db.set_json_setting("outlook_status", {"state": "no_access", "message": str(e)[:200], "at": now.isoformat()})
            raise
        items = []
        for m in mails:
            category = mailcore.classify(m, school_domains(ctx.settings))
            if store.upsert(m, category, now) and not first:
                new_mails.append(m)
            items.append(to_item(m, category))
        ctx.db.set_json_setting("outlook_status", {"state": "ok", "strategy": reader.strategy, "at": now.isoformat()})
        if reader.strategy:
            ctx.db.set_setting("outlook_strategy", reader.strategy)
        return items

    changes = await run_source(ctx, "outlook", collect)
    if new_mails or ctx.db.get_json_setting(PENDING_KEY, []):
        await extract_emails(ctx, new_mails)
    return changes


PENDING_KEY = "email_extract_pending"
MAX_PENDING = 500


async def extract_emails(ctx: Services, mails: list[Mail]) -> int:
    """Look for deadlines and dates inside new messages (tentative until confirmed, same as chats and notices).
    New mail is first added to a pending list and removed only once handled, so mail that arrives while the usage cap
    has extraction paused (or while a Claude call fails) is picked up on a later sweep instead of being lost."""
    from core import usagecap
    from ops.deadline_jobs import calendar_sync
    from ops.deadline_sources import _course_names, _run_blocks, _worth_extracting

    pending = list(dict.fromkeys(ctx.db.get_json_setting(PENDING_KEY, []) + [m.id for m in mails]))[-MAX_PENDING:]
    ctx.db.set_json_setting(PENDING_KEY, pending)
    if not pending or usagecap.paused(ctx):
        return 0
    store, blocks, done = MailStore(ctx.db), [], set()
    for mid in pending:
        m = store.get(mid)
        if m is None:
            done.add(mid)
            continue
        text = m.get("body") or m.get("preview") or ""
        if _worth_extracting({"kind": "email", "title": m.get("subject") or "", "text": text}):
            head = f"From: {m.get('sender_name')} <{m.get('sender_addr')}>\nSubject: {m.get('subject')}\n\n{text}"
            blocks.append((f"email:{mid}"[:80], "email", None, head))
        else:
            done.add(mid)
        if len(blocks) >= EXTRACT_PER_SWEEP:
            break
    new, ok = await _run_blocks(ctx, blocks, await _course_names(ctx), job="extract_mail") if blocks else (0, set())
    done |= {mid for mid in pending if f"email:{mid}"[:80] in ok}
    ctx.db.set_json_setting(PENDING_KEY, [mid for mid in pending if mid not in done])
    if new:
        await calendar_sync(ctx)
    return new
