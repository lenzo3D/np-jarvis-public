# Deploying JARVIS on a Singapore VPS

What runs where:

| Unit | User | What |
|---|---|---|
| `jarvis-ops` | `jarvis-ops` | The one Telegram bot (J.A.R.V.I.S.), watcher jobs, local API on `/run/jarvis/ops.sock`. Owns secrets, sessions, DB. Runs plain-language messages through `claude -p` as the `jarvis` user via sudo (`CLAUDE_RUN_AS`). |
| `jarvis-telethon` | `jarvis-ops` | Reads the Telegram class group as you (read-only). |

There is no separate chat service or Telegram channel plugin: the `school` MCP server is passed to Claude inline by
`ops/chat_agent.py`, so no `claude mcp add` is needed either. `jarvis` cannot read secrets or sessions.

## 1. Create the server
- Any provider with a **Singapore** region (DigitalOcean SGP1, Vultr Singapore, AWS ap-southeast-1).
  Ubuntu 24.04, 2 vCPU / 4 GB RAM. Add your SSH key; disable password login.

## 2. Bootstrap
```bash
ssh root@<server>
git clone <your private repo url> /opt/politemalljarvis   # or scp the folder
bash /opt/politemalljarvis/deploy/bootstrap.sh /opt/politemalljarvis
```

## 3. Claude Code login (your subscription)
Log in interactively as `jarvis` so the credentials are saved in `/home/jarvis/.claude` and are used by the
chat session, the briefings and Do-it runs (`claude setup-token` only *prints* a token; it doesn't save one):
```bash
sudo -u jarvis -H /home/jarvis/.local/bin/claude
/login        # choose your Claude subscription, open the link on your phone/laptop, paste the code
/exit
sudo -u jarvis -H /home/jarvis/.local/bin/claude -p "say ok" --tools ""   # should print ok
```

## 4. Telegram bot (BotFather)
- Create one bot (**JARVIS**). Its token goes into `secrets.env` as `OPS_BOT_TOKEN`.
- Get your numeric Telegram user id (message @userinfobot) → `ALLOWED_TELEGRAM_ID`.

Fill secrets (as root): `nano /opt/politemalljarvis/secrets.env`, including `ACTIONS_ENABLED=true` (server only) and
`SEMESTER_START=YYYY-MM-DD` (Monday of week 1).
Get `TELETHON_API_ID` / `TELETHON_API_HASH` at https://my.telegram.org → API development tools.

## 5. Skills (as jarvis)
Copy your tutorial skills from the Mac:
```bash
scp -r ~/.claude/skills/tutorial-* root@<server>:/home/jarvis/.claude/skills/
ssh root@<server> chown -R jarvis:jarvis /home/jarvis/.claude/skills
```

## 6. First logins (you approve MFA on your phone)
Either log in on the VPS as below, or log in on the Mac and copy `state/` (sessions, `google_token.json`, DB) to
`/opt/politemalljarvis/state/` securely, then `chown -R jarvis-ops:jarvis-ops` it and keep mode 700.
```bash
cd /opt/politemalljarvis
sudo -u jarvis-ops .venv/bin/python -m core.auth login brightspace
sudo -u jarvis-ops .venv/bin/python -m core.auth login teams
sudo -u jarvis-ops .venv/bin/python -m ops.telethon_main login   # phone + Telegram code
sudo -u jarvis-ops .venv/bin/python -m ops.telethon_main list    # copy the group id into GROUP_CHAT_IDS
sudo -u jarvis-ops .venv/bin/python scripts/probe_brightspace.py
sudo -u jarvis-ops .venv/bin/python scripts/probe_teams.py
```

## 7. Start
```bash
systemctl enable --now jarvis-ops jarvis-telethon
journalctl -u jarvis-ops -f
```
Send the bot a message on Telegram; `/status` should answer.

## 8. Isolation check (must all fail)
```bash
sudo -u jarvis cat /opt/politemalljarvis/secrets.env          # Permission denied
sudo -u jarvis ls /opt/politemalljarvis/state                 # Permission denied
sudo -u jarvis curl -s --unix-socket /run/jarvis/ops.sock -X POST http://x/actions/1/approve -o /dev/null -w '%{http_code}\n'   # 404
```

## 9. End-to-end checks
- [ ] (a) A new assignment posted on POLITEMall reaches JARVIS Ops within 20 minutes (daytime).
- [ ] (b) `rm -rf /opt/politemalljarvis/state/sessions/brightspace` → one 🔐 prompt → tap Log in → approve number → recovers.
- [ ] (c) Message JARVIS "do my investments tutorial" → a .docx arrives in Telegram.
- [ ] (d) Ask JARVIS to submit it → Ops shows an approval card; ❌ cancels. Ask again, ✅ → receipt shown.
- [ ] (e) The 07:00 briefing arrives.
- [ ] (f) `/quiet 23-7` in Ops → no 🟡 batches overnight; 🔴 still arrive.

## Running on the Mac (development)
```bash
cp secrets.env.example secrets.env   # fill in; leave ACTIONS_ENABLED=false
.venv/bin/python -m playwright install chromium
.venv/bin/python -m core.auth login brightspace --show
.venv/bin/python scripts/probe_brightspace.py
.venv/bin/python -m ui               # control panel at http://127.0.0.1:8765: start/stop JARVIS, chat, logs
scripts/install_launchagent.sh       # once: start the panel automatically at login
```
(`.venv/bin/python -m ops.main` still runs JARVIS directly, but not at the same time as the panel's Start button.)

## Google Calendar (deadlines)
One-time, on the machine that runs `jarvis-ops` (the Mac during testing):
1. https://console.cloud.google.com → create a project (e.g. "JARVIS").
2. APIs & Services → Library → enable **Google Calendar API**.
3. APIs & Services → OAuth consent screen → External → app name "JARVIS", your email → add your Gmail under
   **Test users**.
4. APIs & Services → Credentials → Create credentials → OAuth client ID → **Desktop app** → Download JSON →
   save it as `state/google_client.json`.
5. Back on the OAuth consent screen, click **Publish app** (status "In production"). Apps left in "Testing"
   get Google sign-ins that expire every 7 days. Google will show an "unverified app" warning when you sign in —
   that's expected for your own personal app: Advanced → Go to JARVIS.
6. `.venv/bin/python -m core.gcal login` → approve in the browser → "Google Calendar connected."
   (On the VPS: run it on the Mac, then copy `state/google_token.json` to `/opt/politemalljarvis/state/` as jarvis-ops.)

JARVIS writes only to your primary calendar, only events it created (📝 confirmed POLITEMall due dates,
❓ tentative dates it read from text). Delete an event and it won't be re-added.
Optional: set `SEMESTER_START=YYYY-MM-DD` (Monday of week 1) in secrets.env so "Week 9" becomes a date.
