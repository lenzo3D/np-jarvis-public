"""Connect everything in one go: log in to POLITEMall, Teams and Outlook (and Google Calendar if it is set up), then check
that each one really works.  Run from the panel (Logins & Tools > Connect everything).

    .venv/bin/python scripts/connect_all.py

Sites that are already connected are only checked. A problem with one site never stops the others. Microsoft's
sign-in number is printed here and also sent to you on Telegram. Nothing is posted or changed anywhere.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from core.auth import AuthRequired, SessionManager  # noqa: E402
from core.brightspace import Brightspace  # noqa: E402
from core.config import load_settings  # noqa: E402
from core.db import DB  # noqa: E402
from core.outlook import Outlook, remember_probe  # noqa: E402
from core.teams import Teams  # noqa: E402

SITES = (("brightspace", "POLITEMall"), ("teams", "Teams"), ("outlook", "Outlook email"))
STEP_LIMIT = 180  # seconds for any one check


def say(text: str = "") -> None:
    print(text, flush=True)


async def tell_telegram(settings, text: str) -> None:
    """Best effort: the sign-in number reaches your phone even when you are not looking at this window."""
    if not (settings.ops_bot_token and settings.allowed_telegram_id):
        return
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"https://api.telegram.org/bot{settings.ops_bot_token}/sendMessage",
                         json={"chat_id": settings.allowed_telegram_id, "text": text})
    except httpx.HTTPError:
        pass


async def check_brightspace(sm, settings, db) -> str:
    courses = await asyncio.wait_for(Brightspace(sm, settings.politemall_base, settings.course_prefix).courses(), STEP_LIMIT)
    return f"connected, {len(courses)} course(s) found"


async def check_teams(sm, settings, db) -> str:
    teams = Teams(sm)
    mode = await asyncio.wait_for(teams.mode(), STEP_LIMIT)
    items = await asyncio.wait_for(teams.recent(datetime.now(timezone.utc) - timedelta(days=3)), STEP_LIMIT)
    return f"connected (reading through {mode}), {len(items)} recent message(s)"


async def check_outlook(sm, settings, db) -> str:
    report = await Outlook(sm).probe(progress=None, step_timeout=STEP_LIMIT)
    if not remember_probe(db, report, datetime.now(timezone.utc)):
        problems = "; ".join(report.get("errors") or []) or "no known method could read the mailbox"
        raise RuntimeError(f"signed in, but the mailbox could not be read ({problems}). "
                           "Run 'Check Outlook email access' and send the output on")
    return f"connected, mail can be read ({report['strategy']})"


CHECKS = {"brightspace": check_brightspace, "teams": check_teams, "outlook": check_outlook}


async def connect(sm, settings, db, site: str, name: str) -> str:
    """Make sure the site is logged in. Returns what was done: 'already', 'renewed' or 'signed in'."""
    if await sm.is_valid(site):
        return "already"
    if await sm.refresh(site):  # Microsoft's stay-signed-in cookies often bring it back without any action
        return "renewed"
    say(f"    {name} needs a sign-in. A browser window is opening: if it asks, approve the sign-in on your phone.")
    await sm.login(site)
    return "signed in"


async def main() -> int:
    settings = load_settings()
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    db = DB(settings.db_path)
    db.init()

    async def notify(site, number):
        say(f"    >>> Approve the {site} sign-in in Microsoft Authenticator. The number is: {number}")
        await tell_telegram(settings, f"Approve the {site} sign-in in Microsoft Authenticator. The number is {number}.")

    sm = SessionManager(settings, notify, headless=False)
    results: list[tuple[str, bool, str]] = []
    try:
        for i, (site, name) in enumerate(SITES, 1):
            say(f"[{i}/{len(SITES) + 1}] {name}")
            try:
                how = await connect(sm, settings, db, site, name)
                db.set_setting(f"auth_prompted:{site}", "0")
                say(f"    login: {how if how != 'already' else 'already signed in'}")
                say("    checking access...")
                detail = await CHECKS[site](sm, settings, db)
                results.append((name, True, detail))
                say(f"    OK: {detail}")
            except AuthRequired as e:
                results.append((name, False, f"sign-in did not finish ({e}). Press Connect everything to try again"))
                say(f"    NOT CONNECTED: {results[-1][2]}")
            except asyncio.TimeoutError:
                results.append((name, False, "the check took too long. Try again in a minute"))
                say(f"    NOT CONNECTED: {results[-1][2]}")
            except Exception as e:  # noqa: BLE001 - one site failing must not stop the others
                results.append((name, False, f"{type(e).__name__}: {str(e)[:200]}"))
                say(f"    NOT CONNECTED: {results[-1][2]}")
    finally:
        await sm.close()

    say(f"[{len(SITES) + 1}/{len(SITES) + 1}] Google Calendar (optional)")
    client, token = settings.state_dir / "google_client.json", settings.state_dir / "google_token.json"
    if token.exists():
        say("    already connected")
        results.append(("Google Calendar", True, "already connected"))
    elif client.exists():
        say("    opening Google's sign-in page in your browser...")
        ok = subprocess.run([sys.executable, "-m", "core.gcal", "login"], cwd=str(ROOT)).returncode == 0
        if ok:
            db.set_setting("gcal_warned", "0")
        results.append(("Google Calendar", ok, "connected" if ok else "sign-in did not finish"))
    else:
        say("    skipped: not set up (see docs/GOOGLE_CALENDAR.md). Only needed if you want deadlines on your calendar")

    say("\nSUMMARY")
    for name, ok, detail in results:
        say(f"  {'OK  ' if ok else 'FAIL'}  {name}: {detail}")
    failed = [n for n, ok, _ in results if not ok]
    say("\nAll set. JARVIS restarts now and keeps these connected." if not failed
        else f"\nNeeds attention: {', '.join(failed)}. Fix the line above, then press Connect everything again.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
