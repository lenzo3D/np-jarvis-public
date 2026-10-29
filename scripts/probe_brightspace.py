"""Live probe: does each Brightspace route work on POLITEMall with a browser session cookie?

Run (you approve MFA on your phone):
    .venv/bin/python scripts/probe_brightspace.py [--show]

Prints PASS/FAIL per route, writes state/probe-results.md, and saves redacted JSON
samples to state/probe-samples/ (git-ignored: real account data). Read-only: never writes to POLITEMall.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.auth import SessionManager  # noqa: E402
from core.brightspace import latest_semester  # noqa: E402
from core.config import load_settings  # noqa: E402

FIX = ROOT / "state" / "probe-samples"  # real account data: kept out of git
WRITE = "--no-write" not in sys.argv  # the panel passes --no-write: a routine check must not modify tracked files
PII_KEYS = re.compile(r"(?i)^(display|first|last|user)?name$|email|orgdefinedid|uniquename|identifier$|username")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def redact(obj):
    if isinstance(obj, dict):
        return {k: ("REDACTED" if PII_KEYS.search(k) and isinstance(v, str) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(x) for x in obj[:15]]
    if isinstance(obj, str):
        return EMAIL.sub("REDACTED", obj)
    return obj


def latest(versions: list[dict], code: str) -> str:
    for v in versions:
        if v.get("ProductCode") == code:
            return v.get("LatestVersion")
    return "1.0"


async def main() -> None:
    async def notify(site, n):
        print(f"\n>>> Approve sign-in in Microsoft Authenticator: {n}\n", flush=True)

    s = load_settings()
    sm = SessionManager(s, notify, headless="--show" not in sys.argv)
    results: list[tuple[str, str, int | str]] = []
    try:
        if not await sm.is_valid("brightspace"):
            await sm.login("brightspace")
        base = s.politemall_base
        async with httpx.AsyncClient(cookies=await sm.cookies("brightspace"), timeout=30) as c:
            async def hit(name: str, path: str, save: bool = True):
                try:
                    r = await c.get(base + path)
                    ok = r.status_code == 200
                    results.append(("PASS" if ok else "FAIL", name, r.status_code))
                    if WRITE and ok and save and "json" in r.headers.get("content-type", ""):
                        FIX.mkdir(parents=True, exist_ok=True)
                        (FIX / f"{name}.json").write_text(json.dumps(redact(r.json()), indent=2))
                    return r.json() if ok and "json" in r.headers.get("content-type", "") else None
                except Exception as e:  # noqa: BLE001
                    results.append(("FAIL", name, repr(e)))
                    return None

            versions = await hit("versions", "/d2l/api/versions/") or []
            lp, le = latest(versions, "lp"), latest(versions, "le")
            print(f"API versions: lp={lp} le={le}")
            await hit("whoami", f"/d2l/api/lp/{lp}/users/whoami")
            enr = await hit("myenrollments", f"/d2l/api/lp/{lp}/enrollments/myenrollments/?orgUnitTypeId=3") or {}
            courses = [i["OrgUnit"] for i in enr.get("Items", []) if i.get("Access", {}).get("IsActive", True)]
            tag = s.course_prefix or latest_semester([c.get("Code", "") for c in courses])
            if tag and tag != "*":
                courses = [c for c in courses if c.get("Code", "").startswith(tag)] or courses
            if not courses:
                print("No active courses found; stopping.")
            else:
                ou = courses[0]["Id"]
                print(f"Probing course {ou} ({courses[0].get('Code')})")
                toc = await hit("content_toc", f"/d2l/api/le/{le}/{ou}/content/toc")
                await hit("dropbox_folders", f"/d2l/api/le/{le}/{ou}/dropbox/folders/")
                await hit("news", f"/d2l/api/le/{le}/{ou}/news/")
                await hit("grades", f"/d2l/api/le/{le}/{ou}/grades/values/myGradeValues/")
                await hit("discussions", f"/d2l/api/le/{le}/{ou}/discussions/forums/")
                now = datetime.now(timezone.utc)
                ids = ",".join(str(c["Id"]) for c in courses)
                q = (f"?startDateTime={now:%Y-%m-%dT00:00:00.000Z}&endDateTime={now + timedelta(days=14):%Y-%m-%dT00:00:00.000Z}"
                     f"&orgUnitIdsCSV={ids}")
                await hit("calendar", f"/d2l/api/le/{le}/calendar/events/myEvents/{q}")
                await hit("xsrf", "/d2l/lp/auth/xsrf-tokens", save=False)
                topic = _first_file_topic(toc)
                if topic:
                    r = await c.get(f"{base}/d2l/api/le/{le}/{ou}/content/topics/{topic}/file", follow_redirects=True)
                    results.append(("PASS" if r.status_code == 200 else "FAIL", "topic_file", r.status_code))
    finally:
        await sm.close()

    lines = ["# Brightspace probe results", "", f"Run: {datetime.now():%Y-%m-%d %H:%M}", "",
             "| Result | Route | Status |", "|---|---|---|"]
    for res, name, status in results:
        print(f"{res:4}  {name:16} {status}")
        lines.append(f"| {res} | {name} | {status} |")
    if WRITE:
        (ROOT / "state").mkdir(exist_ok=True)
        (ROOT / "state" / "probe-results.md").write_text("\n".join(lines) + "\n")
        print("\nWrote state/probe-results.md and state/probe-samples/*.json (git-ignored; do not share them).")
    else:
        print("\nChecked only: nothing was written to the repository.")


def _first_file_topic(toc) -> int | None:
    if not toc:
        return None
    stack = list(toc.get("Modules", []))
    while stack:
        m = stack.pop()
        for t in m.get("Topics", []):
            if t.get("TypeIdentifier") == "File":
                return t["TopicId"]
        stack.extend(m.get("Modules", []))
    return None


if __name__ == "__main__":
    asyncio.run(main())
