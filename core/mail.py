"""School email: the message type, a local searchable store, and the rules that decide what matters.

The mailbox itself is read by core/outlook.py. Everything here is local and read-only with respect to the mailbox.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from core.db import DB

SCHOOL_DOMAINS = ("np.edu.sg",)  # Ngee Ann Polytechnic; extend with SCHOOL_MAIL_DOMAINS in secrets.env
# Words that mean "something time-sensitive changed or is coming". Deliberately narrow: a mail merely mentioning an
# assignment, or a polite "reminder", must not be flagged, or "Important" would stop meaning anything.
URGENT_RE = re.compile(r"(?i)\b(test|quiz|exam|deadline|due|submission|submit|assessment|cancel+ed|postponed|"
                       r"rescheduled|venue|room change|urgent|action required)\b")
BULK_RE = re.compile(r"(?i)(no-?reply|do-?not-?reply|newsletter|unsubscribe|notification|digest|noreply|mailer-daemon)")


@dataclass
class Mail:
    id: str
    received: datetime
    sender_name: str
    sender_addr: str
    subject: str
    preview: str = ""
    body: str = ""
    is_read: bool = False
    has_attachments: bool = False
    importance: str = "normal"  # the sender's flag: low / normal / high
    web_link: str = ""
    conv_id: str = ""
    inbox_tab: str = ""  # Outlook's own "focused" / "other" sorting, when it tells us
    extra: dict = field(default_factory=dict)


def classify(mail: Mail, school_domains: tuple[str, ...] = SCHOOL_DOMAINS) -> str:
    """important | normal | bulk. Important = from the school (or flagged high) and about something time-sensitive."""
    addr = (mail.sender_addr or "").lower()
    from_school = any(addr.endswith("@" + d) or addr.endswith("." + d) for d in school_domains)
    text = f"{mail.subject} {mail.preview}"
    if BULK_RE.search(addr) or BULK_RE.search(mail.sender_name or "") or (mail.inbox_tab == "other" and not from_school):
        return "bulk"
    if (from_school or mail.importance == "high") and URGENT_RE.search(text):
        return "important"
    if mail.importance == "high":
        return "important"
    return "normal"


def _row_to_dict(r, with_body: bool = False) -> dict:
    d = {"id": r["id"], "received": r["received"], "sender_name": r["sender_name"], "sender_addr": r["sender_addr"],
         "subject": r["subject"], "preview": r["preview"], "is_read": bool(r["is_read"]),
         "has_attachments": bool(r["has_attachments"]), "importance": r["importance"], "web_link": r["web_link"],
         "category": r["category"]}
    if with_body:
        d["body"] = r["body"]
    return d


class MailStore:
    def __init__(self, db: DB):
        self.db = db

    def upsert(self, m: Mail, category: str, now: datetime) -> bool:
        """Insert or refresh a message. Returns True when it was not stored before."""
        existed = self.db.conn.execute("select 1 from emails where id=?", (m.id,)).fetchone() is not None
        self.db.conn.execute(
            """insert into emails(id, received, sender_name, sender_addr, subject, preview, body, is_read,
                                  has_attachments, importance, web_link, conv_id, category, first_seen)
               values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               on conflict(id) do update set is_read=excluded.is_read, has_attachments=excluded.has_attachments,
                 preview=excluded.preview, importance=excluded.importance, web_link=excluded.web_link,
                 body=case when excluded.body != '' then excluded.body else emails.body end""",
            (m.id, m.received.astimezone(timezone.utc).isoformat(), m.sender_name, m.sender_addr, m.subject, m.preview,
             m.body, int(m.is_read), int(m.has_attachments), m.importance, m.web_link, m.conv_id, category,
             now.isoformat()))
        row = self.db.conn.execute("select subject, sender_name, sender_addr, body from emails where id=?", (m.id,)).fetchone()
        self.db.conn.execute("delete from emails_fts where id=?", (m.id,))
        self.db.conn.execute("insert into emails_fts(id, subject, sender, body) values (?,?,?,?)",
                             (m.id, row["subject"] or "", f"{row['sender_name']} {row['sender_addr']}", row["body"] or ""))
        return not existed

    def get(self, mail_id: str) -> dict | None:
        r = self.db.conn.execute("select * from emails where id=?", (mail_id,)).fetchone()
        return _row_to_dict(r, True) if r else None

    def recent(self, limit: int = 40, since: datetime | None = None, unread: bool = False,
               category: str | None = None) -> list[dict]:
        sql, args = "select * from emails where 1=1", []
        if since:
            sql += " and received>=?"
            args.append(since.astimezone(timezone.utc).isoformat())
        if unread:
            sql += " and is_read=0"
        if category:
            sql += " and category=?"
            args.append(category)
        rows = self.db.conn.execute(sql + " order by received desc limit ?", [*args, max(1, min(limit, 200))])
        return [_row_to_dict(r) for r in rows]

    def search(self, query: str, limit: int = 20) -> list[dict]:
        tokens = re.findall(r"\w+", query)
        if not tokens:
            return []
        fts = " ".join('"' + t.replace('"', "") + '"' for t in tokens)
        rows = self.db.conn.execute(
            """select e.* from emails_fts f join emails e on e.id = f.id where emails_fts match ?
               order by e.received desc limit ?""", (fts, max(1, min(limit, 100))))
        return [_row_to_dict(r) for r in rows]

    def stats(self) -> dict:
        r = self.db.conn.execute("select count(*) n, sum(is_read=0) unread, max(received) last from emails").fetchone()
        return {"count": r["n"] or 0, "unread": r["unread"] or 0, "last": r["last"]}
