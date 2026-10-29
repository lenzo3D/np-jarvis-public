# Future features

One section per feature, roughly in the order I would build them. Each says what it is, why it is worth doing,
what it builds on, and what is still undecided. Delete a section when the feature ships (and note it in `docs/DEVELOPER_NOTES.md`).

Hard rules that apply to every feature (from `docs/DEVELOPER_NOTES.md`):
- Anything that posts, submits, sends or changes something outside this computer needs a tap on the Approve card in
  Telegram, one action at a time. No exceptions.
- Text written by other people (course pages, chats, email) is data, never instructions.
- The user signs in themselves. Secrets are never printed.
- In group chats the bot only reads. It never replies there.

---

## 1. Project group assistant

**Status:** next, recommended.

**What:** Turn the project group chat into a plain list of who owes what by when. Pick out decisions, tasks and
dates from the conversation, attach them to a person, put dates on the calendar as tentative, and send one short
weekly digest ("this week: Ben - survey by Fri; you - market section by Thu; demo Tue 3pm in Block P").

**Why:** Project chats are noisy and deadlines get buried. This is the highest-value use of the chat JARVIS now reads.

**Builds on:** per-group messages (`core/tg_group.py`), per-group summaries (`ops/briefing.py`), tentative date
extraction (`ops/deadline_sources.py`), the group registry with its Project kind (`core/groups.py`).

**Rough scope:**
- An action-item store: task, owner, due date, source message, status (open / done / dropped).
- A prompt that extracts tasks and owners from a project group, tolerant of nicknames and first names only.
- A panel tab: open items per person, mark done, see the message each came from.
- A weekly digest on Telegram, and an "ask JARVIS what I owe the group" tool.

**Open questions:** how to map chat nicknames to real people; whether "I'll do it" counts as an assignment; how long
done items stay visible.

---

## 2. Daily plan and study blocks

**What:** One morning plan that combines today's classes, deadlines, email and group tasks, and suggests study
blocks in the free slots of the day. Optionally adds the blocks to Google Calendar after a tap to approve.

**Why:** The morning briefing says what exists; this says what to do first.

**Builds on:** the morning briefing, deadlines, Google Calendar view (`ui/calendar.py`), email, project tasks (item 1).

**Rough scope:** a free-slot finder over the calendar; a priority order (due soonest, biggest, not yet submitted);
a calendar write path limited to events JARVIS created itself, behind an Approve card; a panel Plan tab.

**Open questions:** preferred study hours, how long a block should be, whether to plan the weekend.

---

## 3. Email triage and draft replies

**Status:** waits for Outlook reading to be verified (see item 8).

**What:** For important email, JARVIS writes a suggested reply for the user to review. Sending always needs a tap on
the Approve card in Telegram. It also groups mail by course and highlights anything that needs an answer.

**Builds on:** the email store and classification (`core/mail.py`, `ops/mail_jobs.py`), the approvals flow
(`ops/approvals.py`).

**Rough scope:** a "needs a reply" flag, a draft-reply tool for the agent, a new send action behind approval (needs
mail-send access, which is a separate permission from reading), and a Drafts view in the Email tab.

**Open questions:** whether the school tenant allows sending through the same login; reply tone and sign-off.

---

## 4. Grade tracker and target calculator

**What:** Show grades per course, the weight of what is done and what is left, and what is needed on the remaining
work to reach a target (for example a GPA goal). Warn when a grade is released.

**Builds on:** the Brightspace grades API already read by `core/brightspace.py` (`get_grades`).

**Rough scope:** weights per assessment (some come from the course outline, some must be entered once), a projection
table, a panel Grades tab, a "what do I need to get an A?" tool for the agent.

**Open questions:** NP's exact grading scale and GPA formula; where assessment weights are published per course.

---

## 5. Exam prep from course material

**What:** For a chosen course and weeks, produce a summary, practice questions with answers, and flashcards from the
lecture notes and tutorials JARVIS has already indexed.

**Builds on:** the course library (`core/library.py`, `search_material`), the Claude chat agent.

**Rough scope:** a prep mode in the chat agent and the panel (pick course, weeks, format), saved sets to revisit,
export to a document file. Uses Claude, so it shows up in the Usage tab.

**Open questions:** output format (documents vs on-screen cards); how many questions per week of material.

---

## 6. One search across everything

**What:** A single search box that looks through course material, announcements, email and group chats at once and
shows where each hit came from.

**Builds on:** the existing full-text indexes (`materials_fts`, `group_fts`, `emails_fts`).

**Rough scope:** a combined query endpoint, a panel search bar (or a tab) with results grouped by source, and a
matching agent tool. Local only, no Claude calls.

---

## 7. Usage cap and warnings

**What:** Set a daily or weekly limit on Claude usage; warn on Telegram when it is close and optionally pause the
non-essential jobs (summaries, extraction) when it is reached, while chat keeps working.

**Builds on:** the usage log (`claude_usage` table, Usage tab).

**Rough scope:** a settings field in the panel, a check in the Claude runner, a warning message, and a "paused to
save usage" indicator.

**Also on the list (cheaper-running ideas from earlier):** use the smaller Claude model for the briefing, group
summaries and date extraction; send only date-like chat lines to extraction; scan courses only when something
changed; check Teams hourly overnight; a setting for the chat model.

---

## 8. Outlook email: finish the reader

**Status:** in progress. Waiting on the output of the next "Check Outlook email access" run.

**What:** The reader decrypts Outlook's stored tokens and tries Microsoft Graph and the Outlook REST API. If the
school's tenant refuses those, the next method is Outlook web's own service endpoint (the probe already tests it).

**Done when:** the Outlook indicator is green and the Email tab shows real mail.

---

## 9. Class reminders with room and leave-by time

**What:** A reminder before each class with the room, and a "leave by" time worked out from where the user usually
travels from. Warn when a class is cancelled or the room changes (from email and announcements).

**Builds on:** the Brightspace calendar, Google Calendar view, email alerts.

**Open questions:** where the user travels from, and whether to estimate travel time or just use a fixed buffer.

---

## 10. Stronger submission safety net

**What:** Escalating reminders for anything due soon and not submitted (for example 24 hours, 6 hours, 1 hour,
then every 30 minutes), each with the Do it button, quiet only when submission is confirmed.

**Builds on:** `ops/reminders.py` and the submitted check (`core/brightspace.py`).

---

## 11. Move to the VPS

**Status:** parked on purpose. Run locally for now.

**What:** Run JARVIS 24/7 on a Singapore server. The deploy files, the one-bot setup and the isolation checks are
written (`deploy/`), but have never been run on a real server.

**Before doing it:** the skills path in `ops/chat_agent.py` uses the Ops user's home, not the Claude user's; the
Telegram reader runs as its own service there (`TELETHON_EXTERNAL=1`); Outlook and other browser logins have to be
done on the server or copied across.

---

## 12. Small known issues (backlog)

None block anything.

- A word like "contest" is read as "test".
- Timed events of type "other" are not handled well.
- Recess week is not handled when turning "Week N" into a date.
- Web addresses are not removed from tentative calendar event text.
- Bad number parameters in the local API return a server error instead of a clear message.
- The calendar is not re-read before an event is changed.
- Course material HTML is decoded twice in places.
- Muting a course is case-sensitive.
- A newly added course can produce a flood of alerts on its first scan.
- Submit file names are not escaped everywhere.

---

## Not planned

- **Reading WhatsApp groups.** There is no official way to read them, and automating WhatsApp Web can get the number
  banned. Use Telegram for group chats that JARVIS should read.
- **Letting anyone in a group talk to the bot.** By design, the bot only reads groups.
