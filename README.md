# JARVIS

A personal school assistant for Ngee Ann Polytechnic students. It runs on your own computer, watches POLITEMall and
Microsoft Teams, and messages you on Telegram about deadlines, announcements and new material. You can also talk to
it in plain language, from Telegram or from a local control panel.

> Not affiliated with Ngee Ann Polytechnic. It signs in as you, the same way you do in a browser. Use it on your own
> account only, and follow your school's rules on automated access.

## What it does

- **Deadlines and reminders.** Reads assignments from POLITEMall, reminds you before they are due, and keeps them on
  your Google Calendar (optional). Dates it only guesses are marked tentative.
- **Morning briefing.** One short message each morning: classes, what is due, what changed.
- **Announcements and Teams.** Tells you when a lecturer posts something new.
- **Class and project group chats (optional).** Reads the Telegram groups you choose and summarises them. It only
  reads. It never replies in a group, and nobody in the group can talk to it.
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
| A Mac (or Linux) that is on while you want alerts | JARVIS runs on your computer. While it runs, the panel keeps the Mac from sleeping (closing the lid still stops it). |
| Python 3.12 or newer | The installer checks this. On a Mac: `brew install python@3.12` |
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

## First-run setup (about five minutes)

The control panel opens a setup wizard the first time:

1. **Create your bot.** In Telegram, message @BotFather, send `/newbot`, and paste the token it gives you.
2. **Connect your Telegram.** Press Start on your new bot and send it a message. JARVIS finds your account and will
   only ever answer you.
3. **POLITEMall login.** Your student ID and password.
4. **About you.** Your name and how JARVIS should address you.

Then, in the **Logins & Tools** tab, sign in to POLITEMall and Teams once (you do the sign-in and any phone approval
yourself), and press **Start** at the top. The indicators next to the clock turn green when everything is live.

Optional extras, each from the panel:

- **Google Calendar:** see [docs/GOOGLE_CALENDAR.md](docs/GOOGLE_CALENDAR.md).
- **Group chats:** add your bot to the group (turn off its privacy mode in @BotFather: `/setprivacy` then Disable),
  then tick the group in the **Chats** tab.
- **School email:** sign in to Outlook from **Logins & Tools**, then run "Check Outlook email access".
- **Start at login (Mac):** `scripts/install_launchagent.sh`

Set `SEMESTER_START` in `secrets.env` to the Monday of week 1 so "Week 9" in teaching plans becomes a real date.

## Everyday use

- Open the panel with **Open JARVIS.command**, or bookmark `http://127.0.0.1:8765`.
- Talk to the bot in Telegram. Useful commands: `/status`, `/deadlines`, `/scan`, `/quiet 23-7`, `/pause`, `/resume`.
- **Updating:** the Updates card in the panel shows new versions and has an **Update and restart** button.
- **Something wrong?** Run `.venv/bin/python scripts/doctor.py`. It checks the install and says how to fix each problem.

## Cost and privacy

- JARVIS itself is free. Chat, briefings and summaries use your Claude subscription. The **Usage** tab shows how
  much.
- Your passwords, cookies and messages stay on your computer (`secrets.env` and `state/`, both private to your user
  and ignored by git). The only outside services are Telegram, Claude, POLITEMall, Teams, and Google or Outlook if
  you connect them.
- Text written by other people (course pages, group chats, email) is treated as data, never as instructions.

More in [SECURITY.md](SECURITY.md).

## Uninstall

```bash
scripts/install_launchagent.sh --remove   # Mac only: stop starting at login
cd .. && rm -rf np-jarvis-public          # deletes JARVIS and everything it stored
```

Revoke the bot in @BotFather (`/revoke`) and change your POLITEMall password if you want a clean break.

## For developers

Architecture, conventions and design notes are in [docs/DEVELOPER_NOTES.md](docs/DEVELOPER_NOTES.md); planned work is
in [FUTURE_FEATURES.md](FUTURE_FEATURES.md). Run the tests with `pip install -e ".[dev]" && pytest -q`.

## Licence

MIT. See [LICENSE](LICENSE).
