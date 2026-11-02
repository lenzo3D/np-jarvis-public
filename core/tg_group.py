"""Storage and search for the Telegram class group (read-only; there is no send path)."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from core.db import DB
from core.models import GroupMessage


def _row(r) -> GroupMessage:
    ts = datetime.fromisoformat(r["ts"])
    sid = r["sender_id"] if "sender_id" in r.keys() else None
    return GroupMessage(r["chat_id"], r["msg_id"], r["sender"] or "", r["text"] or "",
                        ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc), sid)


def store_message(db: DB, m: GroupMessage) -> None:
    db.conn.execute(
        "insert or ignore into group_messages(chat_id, msg_id, sender, text, ts, sender_id) values (?,?,?,?,?,?)",
        (m.chat_id, m.msg_id, m.sender, m.text, m.ts.astimezone(timezone.utc).isoformat(), m.sender_id),
    )


def messages_since(db: DB, chat_id: int | None, since: datetime) -> list[GroupMessage]:
    sql, args = "select * from group_messages where ts>=?", [since.astimezone(timezone.utc).isoformat()]
    if chat_id is not None:
        sql += " and chat_id=?"
        args.append(chat_id)
    return [_row(r) for r in db.conn.execute(sql + " order by ts, msg_id", args)]


def count_since(db: DB, since: datetime, chat_id: int | None = None) -> int:
    sql, args = "select count(*) from group_messages where ts>=?", [since.astimezone(timezone.utc).isoformat()]
    if chat_id is not None:
        sql += " and chat_id=?"
        args.append(chat_id)
    return db.conn.execute(sql, args).fetchone()[0]


def search(db: DB, q: str, limit: int = 20, chat_id: int | None = None) -> list[GroupMessage]:
    tokens = re.findall(r"\w+", q)
    if not tokens:
        return []
    fts = " ".join('"' + t.replace('"', "") + '"' for t in tokens)
    sql = """select g.* from group_fts f join group_messages g on g.rowid = f.rowid where group_fts match ?"""
    args: list = [fts]
    if chat_id is not None:
        sql += " and g.chat_id=?"
        args.append(chat_id)
    rows = db.conn.execute(sql + " order by g.ts desc limit ?", [*args, limit])
    return [_row(r) for r in rows]


def latest(db: DB, chat_id: int, limit: int = 100) -> list[GroupMessage]:
    """The newest `limit` messages of one group, oldest first (chat order)."""
    rows = db.conn.execute("select * from group_messages where chat_id=? order by ts desc, msg_id desc limit ?",
                           (chat_id, limit)).fetchall()
    return [_row(r) for r in reversed(rows)]


def stats(db: DB) -> dict[int, dict]:
    """chat_id -> {count, last} for every group we hold messages for."""
    return {r["chat_id"]: {"count": r["n"], "last": r["last"]} for r in db.conn.execute(
        "select chat_id, count(*) n, max(ts) last from group_messages group by chat_id")}


def unprocessed(db: DB, chat_id: int, since: datetime, limit: int = 150) -> list[GroupMessage]:
    """The oldest messages of a group that a job has not marked as handled (see mark_processed), newer than `since`, in
    time order. Which messages were handled is tracked per (chat, message id), not by a position, because a backfill
    stores the newest messages first: rowid order is not time order, so a single watermark would skip or repeat some.
    """
    rows = db.conn.execute(
        "select * from group_messages g where chat_id=? and ts>=? and not exists "
        "(select 1 from group_task_seen s where s.chat_id=g.chat_id and s.msg_id=g.msg_id) "
        "order by ts, msg_id limit ?", (chat_id, since.astimezone(timezone.utc).isoformat(), limit))
    return [_row(r) for r in rows]


def mark_processed(db: DB, chat_id: int, msg_ids: list[int]) -> None:
    db.conn.executemany("insert or ignore into group_task_seen(chat_id, msg_id) values (?,?)",
                        [(chat_id, i) for i in msg_ids])


def prune_processed(db: DB, before: datetime) -> int:
    """Forget markers of messages older than `before` (far outside any lookback window, so they are never read again)."""
    return db.conn.execute("delete from group_task_seen where (chat_id, msg_id) in "
                           "(select chat_id, msg_id from group_messages where ts<?)",
                           (before.astimezone(timezone.utc).isoformat(),)).rowcount


def processed_count(db: DB, chat_id: int) -> int:
    return db.conn.execute("select count(*) from group_task_seen where chat_id=?", (chat_id,)).fetchone()[0]


def context_before(db: DB, chat_id: int, ts: datetime, limit: int = 6) -> list[GroupMessage]:
    """The few messages sent just before a moment, oldest first, so a reply like "done" can be read in context."""
    rows = db.conn.execute("select * from group_messages where chat_id=? and ts<? order by ts desc, msg_id desc limit ?",
                           (chat_id, ts.astimezone(timezone.utc).isoformat(), limit)).fetchall()
    return [_row(r) for r in reversed(rows)]


def sender_names(db: DB, chat_id: int, sender_id: int | None = None) -> list[str]:
    """Distinct sender display names seen in a group (optionally only those of one Telegram user id)."""
    sql, args = "select distinct sender from group_messages where chat_id=? and sender<>''", [chat_id]
    if sender_id is not None:
        sql += " and sender_id=?"
        args.append(sender_id)
    return [r[0] for r in db.conn.execute(sql, args)]
