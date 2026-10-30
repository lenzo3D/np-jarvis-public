# Security

JARVIS runs on your own computer and holds your school login, so treat the folder like a password manager.

## What is stored, and where

| What | Where | Protection |
|---|---|---|
| Telegram bot token, POLITEMall login | `secrets.env` | File mode 600, ignored by git |
| Browser sessions, Google token, database, logs | `state/` | Folder mode 700, ignored by git |
| Downloaded course files | `workspace/` | Ignored by git |

Never commit, share or paste `secrets.env` or anything in `state/`. If you think they leaked: revoke the bot with
@BotFather (`/revoke`), change your POLITEMall password, and remove JARVIS from your Google and Microsoft account
permissions.

## How it is locked down

- The control panel only listens on `127.0.0.1`, checks the Host header, and requires a custom header on every change,
  so other websites cannot drive it.
- Only your Telegram account (found during setup) can use the bot. Messages from anyone else are ignored, and the bot
  leaves any group that someone else adds it to.
- In group chats the bot only reads. It does not reply and does not accept commands.
- The one exception is a group you mark **Reply**. JARVIS then posts replies there from your own Telegram account, with no
  approval step. Because anyone in the group can write text that reaches it, the model runs with all tools off and sees
  only that group's recent chat, your upcoming deadlines and matching course material, never email, other chats or
  secrets. It posts at most 20 replies an hour and 100 a day per group, never replies to its own account or to bots, and
  `/pause` stops it. Leave groups on Class, Project or Other and JARVIS never posts.
- Anything that submits, sends or posts needs your tap on an Approve button, one action at a time.
- Text from course pages, group chats and email is passed to the AI as data and never obeyed as instructions.
- You sign in yourself. JARVIS never asks for your phone approvals or one-time codes except to show them to you.

## Reporting a problem

Please open a private security advisory on the GitHub repository (Security tab), or contact the maintainer directly.
Do not post secrets, logs from `state/`, or screenshots showing tokens in a public issue.
