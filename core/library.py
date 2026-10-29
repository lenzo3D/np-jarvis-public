"""Searchable library of everything JARVIS has read for the semester (spec addendum)."""
from __future__ import annotations

import re
from datetime import datetime

from core.db import DB


def _fts_query(q: str) -> str | None:
    tokens = re.findall(r"\w+", q or "")
    return " ".join('"' + t + '"' for t in tokens) if tokens else None


class Library:
    def __init__(self, db: DB):
        self.db = db

    def upsert(self, key: str, course_code: str | None, kind: str, title: str, module: str, source_ref: str,
               text: str, now: datetime, version: str = "") -> bool:
        cur = self.db.conn.execute("select text, version, title from materials where key=?", (key,)).fetchone()
        if cur and cur["text"] == text and cur["version"] == version and cur["title"] == title:
            return False
        self.db.conn.execute(
            """insert into materials(key, course_code, kind, title, module, source_ref, text, version, updated_at)
               values (?,?,?,?,?,?,?,?,?) on conflict(key) do update set
               course_code=excluded.course_code, kind=excluded.kind, title=excluded.title, module=excluded.module,
               source_ref=excluded.source_ref, text=excluded.text, version=excluded.version,
               updated_at=excluded.updated_at""",
            (key, course_code, kind, title, module, source_ref, text, version, now.isoformat()))
        self.db.conn.execute("delete from materials_fts where key=?", (key,))
        self.db.conn.execute("insert into materials_fts(key, title, text) values (?,?,?)", (key, title, text))
        return True

    def mark_extracted(self, key: str) -> None:
        self.db.conn.execute("update materials set extracted_version=coalesce(version, '') where key=?", (key,))

    def pending_extraction(self) -> list[str]:
        """Items whose current version hasn't had deadlines extracted successfully yet."""
        return [r["key"] for r in self.db.conn.execute(
            "select key from materials where extracted_version is null or extracted_version != coalesce(version, '')"
            " order by updated_at")]

    def needs_update(self, key: str, version: str) -> bool:
        r = self.db.conn.execute("select version from materials where key=?", (key,)).fetchone()
        return r is None or r["version"] != version

    def get(self, key: str) -> dict | None:
        r = self.db.conn.execute("select * from materials where key=?", (key,)).fetchone()
        return dict(r) if r else None

    def count(self) -> int:
        return self.db.conn.execute("select count(*) from materials").fetchone()[0]

    def list(self, kind: str | None = None, course: str | None = None, limit: int = 200) -> list[dict]:
        sql, args = "select key, course_code, kind, title, module, source_ref, updated_at from materials where 1=1", []
        if kind:
            sql += " and kind=?"
            args.append(kind)
        if course:
            sql += " and course_code=?"
            args.append(course)
        return [dict(r) for r in self.db.conn.execute(sql + " order by course_code, module, title limit ?",
                                                      (*args, limit))]

    def search(self, query: str, course: str | None = None, limit: int = 10) -> list[dict]:
        fts = _fts_query(query)
        if not fts:
            return []
        sql = """select m.key, m.course_code, m.kind, m.title, m.module, m.source_ref,
                        snippet(materials_fts, 2, '[', ']', '…', 40) as snippet
                 from materials_fts f join materials m on m.key = f.key
                 where materials_fts match ?"""
        args: list = [fts]
        if course:
            sql += " and m.course_code=?"
            args.append(course)
        rows = self.db.conn.execute(sql + " order by bm25(materials_fts) limit ?", (*args, limit))
        return [dict(r) for r in rows]
