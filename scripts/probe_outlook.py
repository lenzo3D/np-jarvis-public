"""What can the Outlook login read? Prints statuses only (never message content) and remembers the method that works.

    .venv/bin/python scripts/probe_outlook.py [--show]

Run it from the panel (Logins & Tools > Check Outlook email access) after logging in to Outlook.
If nothing works, copy the output and send it on: it is exactly what is needed to add the right method.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone

from core.auth import AuthRequired, SessionManager
from core.config import load_settings
from core.db import DB
from core.outlook import Outlook


async def notify(site, number):
    print(f"[{site}] Approve the sign-in in Microsoft Authenticator: {number}", flush=True)


async def main() -> int:
    settings = load_settings()
    sm = SessionManager(settings, notify, headless="--show" not in sys.argv)
    try:
        try:
            await sm.ensure("outlook")
        except AuthRequired:
            print("Not logged in to Outlook yet (Outlook showed the sign-in page, not a mailbox).")
            print("Use 'Log in to Outlook email' first, approve the sign-in on your phone, then run this again.")
            db = DB(settings.db_path)
            db.init()
            db.set_setting("auth_prompted:outlook", "1")
            return 2
        print("Logged in. Looking at what this login is allowed to read (no message content is shown)...\n", flush=True)
        outlook = Outlook(sm)
        report = await outlook.probe(progress=lambda m: print(m, flush=True))
    finally:
        await sm.close()

    for problem in report.get("errors") or []:
        print(f"PROBLEM: {problem}")
    tc = report.get("token_cache")
    if tc:
        print(f"Token cache in the page: {tc.get('entries')} access-token entries ({tc.get('plain')} plain, {tc.get('encrypted')} encrypted); "
              f"decrypted {tc.get('decrypted')}; decryption key cookie present: {tc.get('has_cookie_key')}; worked with: {tc.get('worked_with')}")
        if tc.get("errors"):
            print(f"  problems: {tc['errors']}")
    print(f"Access tokens found in the Outlook web session: {len(report['tokens'])}")
    for t in report["tokens"]:
        scopes = ", ".join(t["scopes"][:8]) or "(none listed)"
        print(f"  - for {t['aud']}: {scopes}  [{t['minutes_left']} min left]")
    print("\nTried to read the inbox:")
    for a in report["attempts"]:
        print(f"  - {a['endpoint']} using a token for {a['aud']}: HTTP {a['status']} {a.get('error') or ''}".rstrip())
    owa = report.get("owa_service")
    if owa:
        print(f"\nOutlook web service check: HTTP {owa.get('status')} (canary cookie: {owa.get('canary')})")
    st = report.get("storage")
    if st:
        print(f"Page: {st.get('url_host')} (signed in: {st.get('signed_in')}); storage keys: {st.get('local_keys')} local, {st.get('session_keys')} session")
        print(f"  local keys that look like tokens/accounts: {st.get('local_matches')}")
        print(f"  session keys that look like tokens/accounts: {st.get('session_matches')}")
        print(f"  cookie names: {st.get('cookies')}")

    (settings.state_dir).mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "outlook-probe.json").write_text(json.dumps(report, indent=2))
    db = DB(settings.db_path)
    db.init()
    now = datetime.now(timezone.utc)
    if report["strategy"]:
        db.set_setting("outlook_strategy", report["strategy"])
        db.set_setting("auth_prompted:outlook", "0")
        db.record_health("outlook", True, now)  # the red "could not read" state is over
        db.set_json_setting("outlook_status", {"state": "ok", "strategy": report["strategy"], "at": now.isoformat()})
        print(f"\nRESULT: JARVIS can read your school email using '{report['strategy']}'. New mail is checked every 10 minutes.")
        return 0
    db.set_json_setting("outlook_status", {"state": "no_access", "message": "access check found no working method", "at": now.isoformat()})
    print("\nRESULT: none of the known methods could read the mailbox. Copy everything above and send it on;")
    print("it shows exactly which method to add next. Nothing was changed in your mailbox.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
