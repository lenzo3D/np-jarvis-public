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
- Anything that submits, sends or posts needs your tap on an Approve button, one action at a time.
- Text from course pages, group chats and email is passed to the AI as data and never obeyed as instructions.
- You sign in yourself. JARVIS never asks for your phone approvals or one-time codes except to show them to you.

## Reporting a problem

Please open a private security advisory on the GitHub repository (Security tab), or contact the maintainer directly.
Do not post secrets, logs from `state/`, or screenshots showing tokens in a public issue.
