"""Email sweep: new Outlook mail -> local store, alerts for what matters, dates found inside messages."""
from __future__ import annotations

import logging
from datetime import timedelta

from core import mail as mailcore
from core.mail import Mail, MailStore
from core.models import Item
from core.outlook import MailUnavailable
from ops.watcher import Services, run_source

log = logging.getLogger(__name__)
SWEEP_LIMIT = 40
FIRST_LIMIT = 100
LOOKBACK = timedelta(days=3)
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


async def sweep_outlook(ctx: Services) -> list:
    reader = ctx.extra.get("outlook")
    if reader is None or not configured(ctx):
        return []
    store, now, new_mails = MailStore(ctx.db), ctx.clock(), []

    async def collect() -> list[Item]:
        # The first successful sweep is a silent baseline (no alerts for mail that was already there).
        first = ctx.db.get_setting("baseline:outlook") != "1" and store.stats()["count"] == 0
        try:
            mails = await reader.recent(None if first else now - LOOKBACK, FIRST_LIMIT if first else SWEEP_LIMIT)
        except MailUnavailable as e:
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
    if new_mails:
        await extract_emails(ctx, new_mails)
    return changes


async def extract_emails(ctx: Services, mails: list[Mail]) -> int:
    """Look for deadlines and dates inside new messages (tentative until confirmed, same as chats and notices)."""
    from ops.deadline_jobs import calendar_sync
    from ops.deadline_sources import _course_names, _run_blocks, _worth_extracting

    blocks = []
    for m in mails[:EXTRACT_PER_SWEEP * 3]:
        text = m.body or m.preview
        if _worth_extracting({"kind": "email", "title": m.subject, "text": text}):
            head = f"From: {m.sender_name} <{m.sender_addr}>\nSubject: {m.subject}\n\n{text}"
            blocks.append((f"email:{m.id}"[:80], "email", None, head))
        if len(blocks) >= EXTRACT_PER_SWEEP:
            break
    if not blocks:
        return 0
    new, _ok = await _run_blocks(ctx, blocks, await _course_names(ctx))
    if new:
        await calendar_sync(ctx)
    return new
