# JARVIS

A personal school assistant for Ngee Ann Polytechnic students. It runs on your own computer, watches POLITEMall and
Microsoft Teams, and messages you on Telegram about deadlines, announcements and new material. You can also talk to
it in plain language, from Telegram or from a local control panel.

> Not affiliated with Ngee Ann Polytechnic. It signs in as you, the same way you do in a browser. Use it on your own
> account only, and follow your school's rules on automated access.

## What it does

- **Deadlines and reminders.** Reads assignments from POLITEMall, reminds you before they are due, and keeps them on
  your Google Calendar (optional). Dates it only guesses are marked tentative. Ask in chat ("add the meeting with
  Mr Yap on Tuesday at 4pm to my calendar") and it puts the event on your own calendar; it never invites anyone, and
  you get a Telegram note for every event it adds or removes. For an assignment you have not
  handed in, reminders come 3 days, 1 day, 6 hours, 3 hours, 1 hour and 30 minutes before it is due, and stop as
  soon as POLITEMall shows your submission. The last 6 hours ignore quiet hours and mutes (only `/pause` stops
  them). Buttons: Do it, Snooze 1h, and "I submitted elsewhere". If a deadline passes unsubmitted you get one
  "Missed" message. Quizzes and tests get a reminder 1 day and 3 hours before.
- **Morning briefing.** One short message each morning: classes, what is due, what changed.
- **Announcements and Teams.** Tells you when a lecturer posts something new.
- **Class and project group chats (optional).** Reads the Telegram groups you choose and summarises them. It only
  reads. By default it never replies in a group, and nobody in the group can talk to it.
- **Project tasks (optional).** In the Chats tab, set your project group's type to **Project** (press Change groups,
  pick the type, Save). JARVIS then keeps a list of who owes what from that chat: "I'll do the survey" goes to
  whoever said it, "@Ben do the slides" to Ben, and "done" or "sent the slides" ticks the task off. The Tasks tab shows
  it per person (you first), with the message each item came from; you can mark items done or dropped, change the owner
  or date, and list nicknames (for example "bubu is Ben Tan"). Only your own tasks with a date, and group meetings
  and demos, go on your Google Calendar, as tentative events. Every Sunday at 19:00 you get one Telegram message per project
  group (this week, overdue, decided), and `/tasks` shows it any time. JARVIS never closes your own tasks or moves their dates because of something written in
  the chat: when someone says one is done, or gives it a new date, it appears in the Tasks tab (and the digest) as a
  suggestion with Close/Accept and Keep buttons, and nothing changes until you answer. A task someone else gave you
  goes on your calendar only after you confirm it in the Tasks tab; tasks you volunteered for go on automatically. Ask JARVIS "what do I owe the group?" in chat.
  It only reads the group; it never posts there. It uses a little Claude time (haiku by default, paused by the usage cap).
- **Auto-reply (optional, off by default).** Mark a group as **Reply** in the Chats tab and JARVIS answers there
  automatically, from your own Telegram account, using only that group's chat, your upcoming deadlines and course
  material. It never sees your email or your other chats, and it is limited to 20 replies an hour per group.
  `/pause` in Telegram stops it at once.
- **School email (optional).** Reads Outlook mail and alerts you to important messages.
- **Ask it anything.** "What is due this week?", "Find the week 5 notes on cash flow", "Do my tutorial" (it shows
  you the result first).
- **Control panel.** A dashboard at `http://127.0.0.1:8765` to start and stop JARVIS, see connections, your
  calendar, deadlines and usage.

**Safety rule:** JARVIS never submits, sends or posts anything on its own. Every action that changes something
outside your computer needs a tap on an Approve button in Telegram.

## What you need

| You need | Why |
|---|---|
| A Mac, a Windows 10/11 PC (or Linux) that is on while you want alerts | JARVIS runs on your computer. While it runs, the panel keeps it from sleeping (closing the lid still stops it). |
| Python 3.12 or newer | The installer checks this. On a Mac: `brew install python@3.12`. On Windows: install it from https://www.python.org/downloads/windows/ and tick **Add python.exe to PATH** |
| The Telegram app | JARVIS messages you there |
| A Claude subscription and the Claude Code command | Chat, briefings and summaries use it. Install from https://claude.com/claude-code, then run `claude` once and sign in |
| Your POLITEMall login | Stored only on your computer, in `secrets.env` |

Each person runs their own copy with their own Telegram bot and their own logins. Nothing is shared between users.

## Install

```bash
git clone https://github.com/lenzo3D/np-jarvis-public.git
cd np-jarvis-public
./install.sh
```

The installer creates a private Python environment, installs the dependencies and the sign-in browser, checks your
setup, and opens the control panel. On a Mac you can also double-click **Open JARVIS.command**.

**On Windows 10/11** (in PowerShell, from the folder you cloned):

```powershell
git clone https://github.com/lenzo3D/np-jarvis-public.git
cd np-jarvis-public
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

It does the same steps, then asks whether to start JARVIS when you log in (a Task Scheduler entry). Afterwards,
double-click **Open JARVIS.bat** to open the panel. Notes for Windows: install Claude Code with its Windows
installer, so that `claude.exe` is found (a `claude.cmd` from npm also works when the real program sits beside it);
keep the JARVIS folder somewhere only you can read, such as inside your user folder (Windows has no per-file
`chmod 600`); and the `CLAUDE_RUN_AS` option from the server setup does not exist on Windows.

## First-run setup (about five minutes)

The control panel opens a setup wizard the first time:

1. **Create your bot.** In Telegram, message @BotFather, send `/newbot`, and paste the token it gives you.
2. **Connect your Telegram.** Press Start on your new bot and send it a message. JARVIS finds your account and will
   only ever answer you.
3. **POLITEMall login.** Your student ID and password.
4. **About you.** Your name and how JARVIS should address you.

Then open the **Logins & Tools** tab and press **Connect everything**. It signs in to POLITEMall, Teams and Outlook
one after the other (you do the sign-in and any phone approval yourself; Microsoft's number is also sent to your
Telegram), then checks that each one really works and prints a summary. JARVIS restarts by itself afterwards. The
indicators next to the clock turn green when everything is live. Press the same button again any time something
turns red. The **Claude** light shows whether Claude Code is still signed in; if it turns red (Telegram tells you too),
press its **Sign in** button and approve in the browser.

Optional extras, each from the panel:

- **Google Calendar:** see [docs/GOOGLE_CALENDAR.md](docs/GOOGLE_CALENDAR.md).
- **Group chats:** add your bot to the group (turn off its privacy mode in @BotFather: `/setprivacy` then Disable),
  then tick the group in the **Chats** tab.
- **School email:** included in **Connect everything**.
- **Start at login:** `scripts/install_launchagent.sh` (Mac) or `scripts\install_startup.ps1` (Windows; the installer offers it)

Set `SEMESTER_START` in `secrets.env` to the Monday of week 1 so "Week 9" in teaching plans becomes a real date. If your semester has a recess week, also set `RECESS_WEEK` to the teaching week it follows (for example `7`), so later weeks land a week further on.

## Everyday use

- Open the panel with **Open JARVIS.command** (Mac) or **Open JARVIS.bat** (Windows), or bookmark `http://127.0.0.1:8765`.
- Talk to the bot in Telegram. Useful commands: `/status`, `/deadlines`, `/scan`, `/usage`, `/quiet 23-7`, `/pause`, `/resume`.
- **Updating:** the Updates card in the panel shows new versions and has an **Update and restart** button.
- **Something wrong?** Run `.venv/bin/python scripts/doctor.py` (Windows: `.venv\Scripts\python.exe scripts\doctor.py`). It checks the install and says how to fix each problem.

## Cost and privacy

- JARVIS itself is free. Chat, briefings and summaries use your Claude subscription. The **Usage** tab shows how
  much.
- **Usage cap (optional, off by default).** In the Usage tab you can set a daily and/or weekly limit in estimated usage
  (a yardstick worked out from the cost Claude Code reports, not a bill). JARVIS warns you on Telegram at 80% and 100%.
  At 100% it pauses class chat summaries, date extraction and group replies to save usage; chat, "Do it" and the
  morning briefing (as a plain list) keep working, and the paused work picks up where it left off when the day or week
  resets. Tap Resume (panel, the Telegram warning, or `/usage resume`) to carry on for the rest of the period. The Usage tab
  also lets you pick a cheaper model per job; summaries and chat dates use the small one by default.
- Your passwords, cookies and messages stay on your computer (`secrets.env` and `state/`, both private to your user
  and ignored by git). The only outside services are Telegram, Claude, POLITEMall, Teams, and Google or Outlook if
  you connect them.
- Text written by other people (course pages, group chats, email) is treated as data, never as instructions.

**What the chat agent can reach.** On a normal desktop install (Mac or Windows) the Claude agent that answers your
Telegram messages runs as you, and it is allowed to run Python. So, despite the "do not read `state/` or
`secrets.env`" rules it is given, it is not walled off from those files: treat it as having your own access. The
guard for anything that leaves your computer is the Approve card in Telegram, which only you can tap. The isolated
setup (Claude as a separate user that cannot read your secrets) is the server deployment in `deploy/`.

More in [SECURITY.md](SECURITY.md).

## Uninstall

```bash
scripts/install_launchagent.sh --remove   # Mac only: stop starting at login
# Windows: powershell -ExecutionPolicy Bypass -File scripts\install_startup.ps1 -Remove
cd .. && rm -rf np-jarvis-public          # deletes JARVIS and everything it stored
```

Revoke the bot in @BotFather (`/revoke`) and change your POLITEMall password if you want a clean break.

## For developers

Architecture, conventions and design notes are in [docs/DEVELOPER_NOTES.md](docs/DEVELOPER_NOTES.md); planned work is
in [FUTURE_FEATURES.md](FUTURE_FEATURES.md). Run the tests with `pip install -e ".[dev]" && pytest -q`.

## Licence

MIT. See [LICENSE](LICENSE).
