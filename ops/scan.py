"""Read everything in the current courses into the semester library (and extract deadlines)."""
from __future__ import annotations

import asyncio
import logging
from pathlib import PurePosixPath
from urllib.parse import urlparse

from core.auth import AuthRequired
from core.brightspace import FileTooLarge
from core.doctext import extract_text
from core.library import Library

log = logging.getLogger(__name__)
TEXT_EXTS = {".pdf", ".docx", ".pptx", ".txt", ".html", ".htm", ".md", ".csv"}
MAX_FILE_BYTES = 25 * 1024 * 1024


def _ext(url: str | None) -> str:
    return PurePosixPath(urlparse(url or "").path).suffix.lower()


async def index_course(ctx, course, lib: Library, stats: dict) -> list[str]:
    """Index one course. Returns library keys that are new or changed (for deadline extraction)."""
    now, changed = ctx.clock(), []
    for it in await ctx.bs.content_items(course.ou, course.code):
        p = it.payload
        if p.get("type") != "File" or _ext(p.get("url")) not in TEXT_EXTS:
            continue
        version = str(p.get("last_modified") or "")
        if not lib.needs_update(it.stable_id, version):
            continue
        try:
            name, data = await ctx.bs.fetch_file(course.ou, p["topic_id"])
        except AuthRequired:
            raise
        except FileTooLarge as e:  # recorded with its version so it isn't downloaded again every hour
            lib.upsert(it.stable_id, course.code, "document", it.title, p.get("module", ""), p.get("url", ""),
                       f"(file too large to read: {e})", now, version)
            stats["errors"] += 1
            continue
        except Exception as e:  # noqa: BLE001 - one bad file must not stop the scan
            log.warning("skip %s: %s", it.title, e)
            stats["errors"] += 1
            continue
        text = await asyncio.to_thread(extract_text, name, data) if len(data) <= MAX_FILE_BYTES else ""
        if lib.upsert(it.stable_id, course.code, "document", it.title or name, p.get("module", ""), p.get("url", ""),
                      text or f"(no readable text in {name})", now, version):
            changed.append(it.stable_id)
        stats["documents"] += 1
    for it in await ctx.bs.announcements(course.ou, course.code):
        body = it.payload.get("body_text", "")
        if lib.upsert(it.stable_id, course.code, "announcement", it.title, "", "", body, now,
                      str(it.payload.get("posted_at") or "")):
            changed.append(it.stable_id)
        stats["announcements"] += 1
    for folder_id, (name, text) in (await ctx.bs.folder_instructions(course.ou)).items():
        key = f"bs:assignment:{course.ou}:{folder_id}"
        if text and lib.upsert(key, course.code, "assignment", name, "Assignments", "", text, now, ""):
            changed.append(key)
        stats["assignments"] += 1 if text else 0
    return changed


async def full_scan(ctx, extract: bool = True) -> dict:
    lib = Library(ctx.db)
    stats = {"courses": 0, "documents": 0, "announcements": 0, "assignments": 0, "errors": 0, "deadlines": 0}
    changed: list[str] = []
    for course in await ctx.bs.courses():
        stats["courses"] += 1
        try:
            changed += await index_course(ctx, course, lib, stats)
        except AuthRequired:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("scan failed for %s", course.code)
            stats["errors"] += 1
    if extract:
        from ops.deadline_sources import extract_from_library

        stats["deadlines"] = await extract_from_library(ctx, changed)  # also retries earlier failures
    return stats
