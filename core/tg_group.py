"""Storage and search for the Telegram class group (read-only; there is no send path)."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from core.db import DB
from core.models import GroupMessage


def _row(r) -> GroupMessage:
    ts = datetime.fromisoformat(r["ts"])
    return GroupMessage(r["chat_id"], r["msg_id"], r["sender"] or "", r["text"] or "",
                        ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc))


def store_message(db: DB, m: GroupMessage) -> None:
    db.conn.execute(
        "insert or ignore into group_messages(chat_id, msg_id, sender, text, ts) values (?,?,?,?,?)",
        (m.chat_id, m.msg_id, m.sender, m.text, m.ts.astimezone(timezone.utc).isoformat()),
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
