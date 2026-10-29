# Deadlines + Google Calendar sync — Design Spec

Date: 2026-10-27
Status: decisions agreed in chat (one question round); the user asked to skip section-by-section review.

## Goal

JARVIS reads all of the student's course material and messages, works out the key dates (assignment and homework
deadlines, tests, exams, presentations, group project meetings), and keeps them in the user's **main Google
Calendar**, updating or removing events when things change.

## Decisions

| Topic | Decision |
|---|---|
| Calendar access | Google Calendar API, OAuth "Desktop app" client owned by the user, scope `calendar.events` only |
| Target | User's primary calendar. JARVIS only ever edits/deletes events it created (tagged via `extendedProperties.private.jarvis_key`) |
| Brightspace due dates | Added as normal ("confirmed") events |
| Dates Claude extracts from text | Added automatically, marked tentative: title prefixed `❓`, source quoted in the description, Telegram note |
| Scope | Assignment/homework deadlines; tests, exams, presentations; group project meetings. Not the class timetable |

## Sources

Confirmed (structured, no AI):
1. Brightspace assignment folders (`items` kind `assignment`, due or end date).
2. Brightspace calendar events (`myEvents` for current courses), e.g. "Homework 1", quizzes. Events that are plain
   lessons are skipped (title matches lesson/lecture/tutorial session/class without a due/quiz/test word).

Tentative (Claude extraction, `claude -p --model sonnet`, no tools):
3. Announcements: new or changed ones after each sweep, plus all current-semester ones on the first scan.
4. Teaching plans / course schedules: content files whose title matches
   `teaching plan|lesson plan|schedule|course outline|assessment|weekly plan|syllabus` (PDF, DOCX, PPTX, XLSX skipped).
   Text extracted locally (pdfplumber, python-docx, python-pptx). Re-read when the file's `last_modified` changes.
5. Teams posts (class teams, chats) and Telegram class-group messages: batched, at most hourly.

Week numbers: teaching plans often say "Week 9". If `SEMESTER_START` (Monday of week 1, `YYYY-MM-DD`) is set, it is
given to Claude to convert weeks to dates. Without it, only explicit dates are used.

## Extraction contract

Claude returns JSON only: `{"deadlines": [{"title", "kind", "course", "date", "time", "end_time", "quote"}]}`
- `kind` ∈ assignment, homework, quiz, test, exam, presentation, meeting, other
- `date` = `YYYY-MM-DD` (SGT), `time`/`end_time` = `HH:MM` or null (null → all-day)
- `quote` = the exact source sentence (≤ 200 chars)
Validation in code: drop entries with unparsable dates, dates more than 1 day in the past or more than 240 days
ahead, unknown kinds become `other`, missing course → the source's course.

## Data model

Table `deadlines`:
`key PK, title, course_code, kind, start_utc, end_utc, all_day, source, source_ref, confidence ('confirmed'|'tentative'),
quote, status ('active'|'gone'|'user_deleted'), gcal_event_id, gcal_hash, updated_at`.

Keys:
- assignment: `bs:assignment:<ou>:<folder>`; calendar event: `bs:cal:<CalendarEventId>`
- extracted: `x:<sha1(course|kind|normalised title|date)[:16]>`

Merging: an extracted deadline is dropped if a confirmed deadline exists for the same course on the same date whose
title shares ≥ 50% of words (case-insensitive, stopwords removed). Confirmed sources mark their rows `gone` when the
item disappears or loses its date (→ event deleted). Extracted rows are never auto-removed (the user deletes them in
Google; JARVIS then marks them `user_deleted` and never re-adds that key).

## Calendar events

- Title: `📝 {Course}: {title}` (confirmed) / `❓ {Course}: {title}` (tentative). `{Course}` is a short name derived
  from the Brightspace course name, e.g. "INVESTMENTS(731IV_013686)" → "Investments".
- Timed deadlines: 30-minute block ending at the due time. Meetings/tests with start and end use them; with only a
  start, 1 hour. Date-only → all-day event.
- Reminders: deadlines popup 1 day and 3 hours before; tests/exams/presentations 1 day; meetings 30 min.
- Description: kind, source (and quote for tentative), Brightspace link where known, "Added by JARVIS — edits to this
  event may be overwritten".
- `extendedProperties.private`: `jarvis=1`, `jarvis_key=<key>`.
- Sync is idempotent: insert when no event id; patch when the event hash changes; delete when status `gone`;
  a 404/410 on patch/delete → `user_deleted`.

## Interfaces

- `python -m core.gcal login` — one-time OAuth in the user's browser (the user signs in; token in
  `state/google_token.json`, 0600). Needs `state/google_client.json` from Google Cloud Console.
- Ops jobs: `sync_deadlines` after every Brightspace sweep; `extract_deadlines` hourly; `/deadlines` command (next 14
  days); `/scan` command runs the full first scan. MCP tool `get_deadlines(days)`.
- Telegram: new tentative events are reported in the hourly batch: "❓ Added to calendar: TMBI Quiz 2 — Tue 10 Nov
  (from announcement)". Calendar auth failure → one ⚠️ with instructions.

## Non-goals

Class timetable; two-way edits (changes made in Google are not read back, except deletion); other calendars.

## Risks

- Extraction mistakes → tentative marker + quote; user deletes and it stays deleted.
- Teaching plans as scanned images → no text; skipped with a log line.
- Claude usage: extraction batches text (≤ 40k chars per call) and runs at most hourly plus on demand.

## Addendum (2026-10-27): semester knowledge base + plain-language questions

User request: scrape all existing POLITEMall content, list existing deadlines and teaching plans, and answer
plain-language questions ("when are all of my assignments due").

- **Library table** `materials(key PK, course_code, kind, title, module, source_ref, text, updated_at)` with FTS5
  `materials_fts(title, text)`. Kinds: `document` (content files: PDF/DOCX/PPTX/TXT text), `page` (HTML content topics),
  `announcement`, `assignment` (dropbox instructions). Long text stored whole; search returns ≤ 600-char snippets.
- **Full scan** (`/scan` in Ops, also run once automatically when the library is empty): for every current course,
  index all content topics, announcements and assignment instructions; then run deadline extraction over teaching
  plans and announcements. Re-indexes a document only when its `last_modified` changes. Size cap 25 MB per file.
- **Tools for Claude** (MCP + API): `search_material(query, course?)`, `get_material(key)` (full text, ≤ 20k chars),
  `get_deadlines(days=60, course?)`.
- **Plain-language questions:** any non-command text sent to JARVIS Ops runs
  `claude -p` with only the read-only school tools allowed (`get_*`, `search_*`, `find_course`, `list_courses`,
  `teams_*`, `group_chat_*`), and the answer is sent back. One question at a time; 5-minute cap.

## Change (2026-10-27): one bot
The user chose a single Telegram bot. Non-command messages to JARVIS Ops now go to a full Claude Code agent
(`ops/chat_agent.py`): school MCP with all tools (submit/post/send remain propose-only and need ✅ in Ops), skills,
workspace read/write, conversation memory via `--resume` (reset with `/new` or after 6 h idle). The read-only
question mode (`ops/ask.py`) is removed; the read-only MCP mode remains available. The separate JARVIS chat bot
(Claude Code Telegram channel) is no longer needed.
