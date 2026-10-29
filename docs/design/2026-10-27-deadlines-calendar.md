# Deadlines + Google Calendar Implementation Plan

> **For agentic workers:** executed inline (superpowers:executing-plans), TDD per task.

**Goal:** Extract key dates from Brightspace, course documents, announcements, Teams and the class group, and keep them
in the user's primary Google Calendar.

**Architecture:** `core/deadlines.py` stores and merges deadlines (SQLite). `ops/deadline_sources.py` builds confirmed
deadlines from Brightspace and tentative ones via `claude -p` extraction. `core/gcal.py` is a minimal Calendar REST
client (google-auth credentials). `ops/calsync.py` reconciles deadlines ↔ events. Wiring in scheduler/bot/API/MCP.

**Tech:** existing stack + google-auth-oauthlib, pdfplumber, python-docx, python-pptx.

**Spec:** `docs/superpowers/specs/2026-10-27-deadlines-calendar-design.md`

## Global Constraints
- All times stored UTC ISO; displayed and extracted in Asia/Singapore.
- Only events with `extendedProperties.private.jarvis_key` are ever patched/deleted, and only by stored event id.
- `user_deleted` keys are never re-created.
- Extraction calls: no tools, sonnet, ≤ 40,000 input chars per call.

## Review Focus
1. A date-only extracted deadline must become an all-day event on the right SGT date (not shifted by UTC).
2. Re-running a sync with no changes must make zero Calendar API writes.
3. Brightspace assignment with neither due nor end date → no event (and an existing one is deleted).
4. Claude output that is not JSON, or has bad dates → nothing written, no crash.
5. A deadline whose event the user deleted in Google must not come back.

### Task D1: Deadline store and merge — `core/deadlines.py`, tests `tests/test_deadlines.py`
Produces: `Deadline` dataclass (fields per spec table minus gcal columns), `DeadlineStore(db)` with
`upsert(d) -> bool changed`, `active() -> list`, `mark_gone_missing(source, keys_seen)`, `set_event(key, id, hash)`,
`mark_user_deleted(key)`, `is_user_deleted(key)`, `upcoming(now, days)`; `extracted_key(course, kind, title, date)`;
`duplicates_confirmed(d, confirmed) -> bool` (same course, same SGT date, ≥50% word overlap).

### Task D2: Event rendering — `ops/gcal_events.py`, tests `tests/test_gcal_events.py`
Produces: `short_course(name, code)`, `event_body(d, course_name, tz) -> dict` (title/prefix, timed vs all-day, 30-min
block ending at due, reminders per kind, description, extendedProperties), `event_hash(body) -> str`.

### Task D3: Calendar client — `core/gcal.py`, tests `tests/test_gcal.py`
Produces: `GCal(token_path, client_path)` with async `insert(body) -> id`, `patch(id, body)`, `delete(id)`;
404/410 → `EventGone`; 401 after refresh → `CalendarAuthRequired`; `python -m core.gcal login` CLI (InstalledAppFlow,
scope `https://www.googleapis.com/auth/calendar.events`, token file 0600).

### Task D4: Sync engine — `ops/calsync.py`, tests `tests/test_calsync.py`
Produces: `async sync(store, gcal, course_names, tz) -> SyncReport(inserted, patched, deleted, user_deleted)`.

### Task D5: Confirmed sources — `ops/deadline_sources.py` (part 1), tests `tests/test_deadline_sources.py`
Produces: `confirmed_from_db(db)` (assignments), `confirmed_from_calendar(events, course_by_ou)` (skips lessons),
`async refresh_confirmed(ctx)` (upserts, marks missing as gone).

### Task D6: Document text — `core/doctext.py`, tests `tests/test_doctext.py`
Produces: `extract_text(name, data) -> str` for .pdf/.docx/.pptx/.txt; others → "".

### Task D7: Extraction — `ops/deadline_sources.py` (part 2) + `ops/prompts/extract_deadlines.md`, tests
Produces: `parse_extraction(raw, source_course, now, tz) -> list[Deadline]`, `async extract(ctx, texts)`,
`async extract_recent(ctx)` (changed announcements, Teams/group since last run, changed teaching plans),
`async full_scan(ctx)`. New tentative deadlines → batch notes.

### Task D8: Wiring
`ops/main.py`/`scheduler.py` (sync after Brightspace sweep, hourly extract), `/deadlines`, `/scan`, API
`GET /deadlines`, MCP `get_deadlines`, config `SEMESTER_START`, `GOOGLE_*` paths, README "Google Calendar" section.

### Task D9: Library — `core/library.py`, tests `tests/test_library.py`
Produces: `Library(db)` with `upsert(key, course_code, kind, title, module, source_ref, text, now) -> bool`,
`search(query, course=None, limit=10) -> list[dict]` (snippets), `get(key)`, `needs_update(key, version) -> bool`,
`count() -> int`. Scan: `async index_course(ctx, course)` in `ops/scan.py`, `async full_scan(ctx)`.

### Task D10: Tools — API `GET /material/search`, `GET /material/{key}`, `GET /deadlines`; MCP `search_material`,
`get_material`, `get_deadlines`.

### Task D11: Ask — `ops/ask.py`: non-command Ops text → `claude -p` with read-only school tools; tests with fake runner.
