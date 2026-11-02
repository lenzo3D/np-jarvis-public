# Future features

One section per feature, roughly in the order I would build them. Each says what it is, why it is worth doing,
what it builds on, and what is still undecided. Delete a section when the feature ships (and note it in `docs/DEVELOPER_NOTES.md`).

Hard rules that apply to every feature (from `docs/DEVELOPER_NOTES.md`):
- Anything that posts, submits, sends or changes something outside this computer needs a tap on the Approve card in
  Telegram, one action at a time. No exceptions.
- Text written by other people (course pages, chats, email) is data, never instructions.
- The user signs in themselves. Secrets are never printed.
- In group chats JARVIS only reads, except in a group the user has marked Reply (opt-in, per group). There it posts
  from the user's own account, limited and with the model's tools off. The Ops bot never posts in any group.

---

## 1. Daily plan and study blocks

**What:** One morning plan that combines today's classes, deadlines, email and group tasks, and suggests study
blocks in the free slots of the day. Optionally adds the blocks to Google Calendar after a tap to approve.

**Why:** The morning briefing says what exists; this says what to do first.

**Builds on:** the morning briefing, deadlines, Google Calendar view (`ui/calendar.py`), email, project tasks (shipped).

**Rough scope:** a free-slot finder over the calendar; a priority order (due soonest, biggest, not yet submitted);
a calendar write path limited to events JARVIS created itself, behind an Approve card; a panel Plan tab.

**Open questions:** preferred study hours, how long a block should be, whether to plan the weekend.

---

## 2. Email triage and draft replies

**Status:** no longer waiting on Outlook reading (it works, see `docs/DEVELOPER_NOTES.md`). The Outlook token was
seen to carry the Mail.Send scope, but sending has not been tried or verified.

**What:** For important email, JARVIS writes a suggested reply for the user to review. Sending always needs a tap on
the Approve card in Telegram. It also groups mail by course and highlights anything that needs an answer.

**Builds on:** the email store and classification (`core/mail.py`, `ops/mail_jobs.py`), the approvals flow
(`ops/approvals.py`).

**Rough scope:** a "needs a reply" flag, a draft-reply tool for the agent, a new send action behind approval (needs
mail-send access, which is a separate permission from reading), and a Drafts view in the Email tab.

**Open questions:** whether the school tenant allows sending through the same login; reply tone and sign-off.

---

## 3. Grade tracker and target calculator

**What:** Show grades per course, the weight of what is done and what is left, and what is needed on the remaining
work to reach a target (for example a GPA goal). Warn when a grade is released.

**Builds on:** the Brightspace grades API already read by `core/brightspace.py` (`get_grades`).

**Rough scope:** weights per assessment (some come from the course outline, some must be entered once), a projection
table, a panel Grades tab, a "what do I need to get an A?" tool for the agent.

**Open questions:** NP's exact grading scale and GPA formula; where assessment weights are published per course.

---

## 4. Exam prep from course material

**What:** For a chosen course and weeks, produce a summary, practice questions with answers, and flashcards from the
lecture notes and tutorials JARVIS has already indexed.

**Builds on:** the course library (`core/library.py`, `search_material`), the Claude chat agent.

**Rough scope:** a prep mode in the chat agent and the panel (pick course, weeks, format), saved sets to revisit,
export to a document file. Uses Claude, so it shows up in the Usage tab.

**Open questions:** output format (documents vs on-screen cards); how many questions per week of material.

---

## 5. One search across everything

**What:** A single search box that looks through course material, announcements, email and group chats at once and
shows where each hit came from.

**Builds on:** the existing full-text indexes (`materials_fts`, `group_fts`, `emails_fts`).

**Rough scope:** a combined query endpoint, a panel search bar (or a tab) with results grouped by source, and a
matching agent tool. Local only, no Claude calls.

---

## 6. Class reminders with room and leave-by time

**What:** A reminder before each class with the room, and a "leave by" time worked out from where the user usually
travels from. Warn when a class is cancelled or the room changes (from email and announcements).

**Builds on:** the Brightspace calendar, Google Calendar view, email alerts.

**Open questions:** where the user travels from, and whether to estimate travel time or just use a fixed buffer.

---

## 7. Move to the VPS

**Status:** parked on purpose. Run locally for now.

**What:** Run JARVIS 24/7 on a Singapore server. The deploy files, the one-bot setup and the isolation checks are
written (`deploy/`), but have never been run on a real server.

**Before doing it:** the skills path in `ops/chat_agent.py` uses the Ops user's home, not the Claude user's; the
Telegram reader runs as its own service there (`TELETHON_EXTERNAL=1`); Outlook and other browser logins have to be
done on the server or copied across.

---

## 8. Small known issues (backlog)

Nothing open right now. Add small known issues here as one-line bullets and delete them when fixed.

None known.

---

## Not planned

- **Reading WhatsApp groups.** There is no official way to read them, and automating WhatsApp Web can get the number
  banned. Use Telegram for group chats that JARVIS should read.
- **Letting anyone in a group give the bot commands.** The bot never obeys group members. Reply groups only produce
  replies; they cannot run actions, change settings or reveal private data.
