"""Live probe for Teams: which mode works (Graph token from the web session, or UI fallback)?

    .venv/bin/python scripts/probe_teams.py [--show]
Read-only. Appends results to docs/probe-results.md.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.auth import SessionManager  # noqa: E402
from core.config import load_settings  # noqa: E402
from core.teams import Teams  # noqa: E402


async def main() -> None:
    async def notify(site, n):
        print(f"\n>>> Approve sign-in in Microsoft Authenticator: {n}\n", flush=True)

    sm = SessionManager(load_settings(), notify, headless="--show" not in sys.argv)
    lines = ["", "## Teams probe", "", f"Run: {datetime.now():%Y-%m-%d %H:%M}", ""]
    try:
        if not await sm.is_valid("teams"):
            await sm.login("teams")
        t = Teams(sm)
        mode = await t.mode()
        lines.append(f"- mode: **{mode}**")
        items = await t.recent(datetime.now(timezone.utc) - timedelta(days=3))
        lines.append(f"- recent items (3 days): {len(items)}")
        for i in items[:5]:
            print(f"{i.kind:14} {i.payload.get('chat_name')}: {i.payload.get('body_text', '')[:60]}")
        kinds = sorted({i.kind for i in items})
        lines.append(f"- kinds seen: {', '.join(kinds) or 'none'}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"- FAIL: {type(e).__name__}: {e}")
    finally:
        await sm.close()
    print("\n".join(lines))
    with open(ROOT / "docs" / "probe-results.md", "a") as fh:
        fh.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
