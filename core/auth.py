"""Browser sessions for POLITEMall and Teams, with Microsoft SSO + number-match MFA relay.

Never retries a login on its own: callers get AuthRequired and the user decides
when to try again (avoids account lockout).
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import sys
from pathlib import Path
from typing import Awaitable, Callable, Literal

import httpx

from core.config import Settings, load_settings

Site = Literal["brightspace", "teams", "outlook"]
MfaNotifier = Callable[[str, str], Awaitable[None]]

# Every selector in one place so they are easy to fix after the live probe.
SELECTORS = {
    "username": 'input[name="loginfmt"]',
    "password": 'input[name="passwd"]',
    "submit": "#idSIButton9",
    "mfa_number": "#idRichContext_DisplaySign",
    "kmsi_yes": "#idSIButton9",
    "kmsi_marker": "#KmsiCheckboxField, #kmsiTitle",
    "kmsi_text": "text=Stay signed in?",
    "teams_ready": '[data-tid="app-bar-wrapper"], [data-tid="titlebar-search"], #app-bar',
}
MFA_RE = re.compile(r'id="idRichContext_DisplaySign"[^>]*>\s*(\d{1,3})\s*<')
TEAMS_URL = "https://teams.microsoft.com/v2/"
OUTLOOK_URL = "https://outlook.office.com/mail/"
OUTLOOK_URL_RE = re.compile(r"https://outlook\.(office|office365|live)\.com/")
MFA_TIMEOUT = 300.0


class AuthRequired(Exception):
    """The session for a site is not valid and the user needs to log in."""


def extract_mfa_number(html: str) -> str | None:
    m = MFA_RE.search(html)
    return m.group(1) if m else None


# Signs that Outlook on the web is really signed in. The page loads before it redirects a signed-out visitor to
# Microsoft's login, so "the URL looks like Outlook" proves nothing; these do.
OUTLOOK_READY = ('[data-app-section="MailModule"], [aria-label="Message list"], #O365_MainLink_Me, '
                 '[role="tree"][aria-label*="older"]')


async def outlook_signed_in(page, wait_s: int = 25) -> bool:
    """True once Outlook shows a signed-in mailbox (canary cookie or the mail UI); False on the login page or timeout."""
    for _ in range(max(1, wait_s)):
        if is_login_page(page.url):
            return False
        try:
            if any(c.get("name") == "X-OWA-CANARY" for c in await page.context.cookies()):
                return True
            if await page.locator(OUTLOOK_READY).count():
                return True
        except Exception:  # noqa: BLE001 - page navigating; look again
            pass
        await page.wait_for_timeout(1000)
    return False


def is_login_page(url: str) -> bool:
    return "login.microsoftonline.com" in url or "/d2l/login" in url or "login.live.com" in url


async def fill_reliably(page, selector: str, value: str, attempts: int = 4) -> None:
    """Fill an input on Microsoft's sign-in page and make sure the value actually stuck.
    The page's scripts can wipe text typed before they finish loading."""
    await page.wait_for_selector(selector, state="visible", timeout=60000)
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:  # noqa: BLE001 - some pages never go idle; carry on
        pass
    for _ in range(attempts):
        await page.fill(selector, value)
        await page.wait_for_timeout(400)
        if await page.input_value(selector) == value:
            return
        await page.wait_for_timeout(800)
    raise AuthRequired(f"could not type into {selector} on the sign-in page")


class SessionManager:
    def __init__(
        self,
        settings: Settings,
        mfa_notifier: MfaNotifier,
        checker: Callable[[str], Awaitable[bool]] | None = None,
        flow: Callable[[str, MfaNotifier], Awaitable[None]] | None = None,
        timeout: float = MFA_TIMEOUT,
        headless: bool = True,
    ):
        self.settings = settings
        self.mfa_notifier = mfa_notifier
        self._checker = checker or self._check_live
        self._flow = flow or self._flow_live
        self.timeout = timeout
        self.headless = headless
        self._locks: dict[str, asyncio.Lock] = {}
        self._pw = None
        self._contexts: dict[str, object] = {}
        self.login_in_progress: set[str] = set()

    # ---------- browser ----------
    def session_dir(self, site: str) -> Path:
        d = self.settings.state_dir / "sessions" / site
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def context(self, site: Site):
        if site not in self._contexts:
            if self._pw is None:
                from playwright.async_api import async_playwright

                self._pw = await async_playwright().start()
            ctx = await self._pw.chromium.launch_persistent_context(
                str(self.session_dir(site)), headless=self.headless, locale="en-SG",
                timezone_id="Asia/Singapore",
            )
            await self.restore_cookies(site, ctx)
            self._contexts[site] = ctx
        return self._contexts[site]

    # Brightspace/Teams session cookies have no expiry, so Chromium drops them when it closes.
    # Keep our own copy (0600, inside the 0700 state dir) and put them back on start.
    def _cookie_file(self, site: str) -> Path:
        return self.settings.state_dir / "sessions" / f"{site}.cookies.json"

    async def save_cookies(self, site: str, ctx=None) -> None:
        """Fetch first, then atomically replace the file — a dead browser must never wipe a good copy."""
        ctx = ctx or self._contexts.get(site)
        if ctx is None:
            return
        try:
            cookies = await ctx.cookies()
        except Exception:  # noqa: BLE001 - browser already closed (e.g. Ctrl+C)
            return
        if not cookies:
            return
        path = self._cookie_file(site)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(cookies, fh)
        os.replace(tmp, path)

    async def restore_cookies(self, site: str, ctx) -> None:
        path = self._cookie_file(site)
        if not path.exists():
            return
        try:
            cookies = json.loads(path.read_text())
            if cookies:
                await ctx.add_cookies(cookies)
        except (OSError, ValueError):
            return

    async def close(self) -> None:
        for site, ctx in self._contexts.items():
            try:
                await self.save_cookies(site, ctx)
            except Exception:  # noqa: BLE001
                pass
            await ctx.close()
        self._contexts.clear()
        if self._pw:
            await self._pw.stop()
            self._pw = None

    async def cookies(self, site: Site) -> httpx.Cookies:
        ctx = await self.context(site)
        jar = httpx.Cookies()
        for c in await ctx.cookies():
            jar.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"))
        return jar

    # ---------- validity ----------
    async def is_valid(self, site: Site) -> bool:
        try:
            return await self._checker(site)
        except Exception:
            return False

    async def ensure(self, site: Site) -> None:
        if site in self.login_in_progress:
            async with self._lock(site):
                pass
        if not await self.is_valid(site):
            raise AuthRequired(site)

    async def _check_live(self, site: str) -> bool:
        if site == "brightspace":
            base = self.settings.politemall_base
            async with httpx.AsyncClient(cookies=await self.cookies("brightspace"), follow_redirects=False,
                                         timeout=20) as c:
                r = await c.get(f"{base}/d2l/api/lp/1.0/users/whoami")
                return r.status_code == 200
        ctx = await self.context(site)  # type: ignore[arg-type]
        page = await ctx.new_page()
        try:
            await page.goto(OUTLOOK_URL if site == "outlook" else TEAMS_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(5000)
            if site == "outlook":
                return await outlook_signed_in(page)
            return not is_login_page(page.url)
        finally:
            await page.close()

    # ---------- login ----------
    def _lock(self, site: str) -> asyncio.Lock:
        return self._locks.setdefault(site, asyncio.Lock())

    async def login(self, site: Site) -> None:
        async with self._lock(site):
            if await self.is_valid(site):
                return
            lock_path = self.settings.state_dir / f"login-{site}.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open(lock_path, "w") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                try:
                    self.login_in_progress.add(site)
                    try:
                        await asyncio.wait_for(self._flow(site, self.mfa_notifier), timeout=self.timeout)
                    except asyncio.TimeoutError as e:
                        raise AuthRequired(f"{site}: login timed out waiting for MFA approval") from e
                    except AuthRequired:
                        raise
                    except Exception as e:  # noqa: BLE001 - e.g. the browser window was closed
                        raise AuthRequired(f"{site}: login stopped ({type(e).__name__}: browser closed or "
                                           f"page failed: {str(e)[:150]})") from e
                    if not await self.is_valid(site):
                        raise AuthRequired(f"{site}: login finished but session is not valid")
                    await self.save_cookies(site)
                finally:
                    self.login_in_progress.discard(site)
                    fcntl.flock(fh, fcntl.LOCK_UN)

    async def _flow_live(self, site: str, notify: MfaNotifier) -> None:
        ctx = await self.context(site)  # type: ignore[arg-type]
        # The saved profile reopens old tabs: reuse one and close the rest.
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        for extra in ctx.pages[1:]:
            await extra.close()
        try:
            start = (f"{self.settings.politemall_base}/d2l/home" if site == "brightspace"
                     else OUTLOOK_URL if site == "outlook" else TEAMS_URL)
            await page.goto(start, wait_until="domcontentloaded", timeout=60000)
            await self._ms_login(page, site, notify)
            if site == "brightspace":
                await page.wait_for_url(re.compile(r".*/d2l/(home|le|lp).*"), timeout=120000)
            elif site == "outlook":
                await page.wait_for_url(re.compile(r"https://outlook\.(office|office365|live)\.com/(mail|owa).*"), timeout=120000)
                if not await outlook_signed_in(page, 60):
                    raise AuthRequired("outlook: the mailbox did not load after signing in")
            else:
                await page.wait_for_selector(SELECTORS["teams_ready"], timeout=120000)
        finally:
            await page.close()

    async def _ms_login(self, page, site: str, notify: MfaNotifier) -> None:
        s = SELECTORS
        if not is_login_page(page.url):
            # POLITEMall may show its own landing page with an SSO button first.
            for label in ("Login", "Log In", "Sign in", "Student"):
                btn = page.get_by_role("link", name=re.compile(label, re.I))
                if await btn.count():
                    await btn.first.click()
                    break
            await page.wait_for_load_state("domcontentloaded")
        # Microsoft's "stay signed in" cookie often logs us straight back in: no form, no MFA.
        for _ in range(60):
            if site == "brightspace" and "/d2l/" in page.url and not is_login_page(page.url):
                return
            if await page.locator(f'{s["username"]}, {s["password"]}').first.is_visible():
                break
            await page.wait_for_timeout(1000)
        else:
            raise AuthRequired(f"{site}: sign-in page did not appear")
        if await page.locator(s["username"]).is_visible():
            await fill_reliably(page, s["username"], self.settings.politemall_user)
            await page.click(s["submit"])
        await fill_reliably(page, s["password"], self.settings.politemall_pass)
        await page.click(s["submit"])
        notified = False
        for _ in range(int(self.timeout)):
            await page.wait_for_timeout(1000)
            if not notified:
                number = extract_mfa_number(await page.content())
                if number:
                    await notify(site, number)
                    notified = True
            if await page.locator(s["kmsi_marker"]).count() or await page.locator(s["kmsi_text"]).count():
                await page.click(s["kmsi_yes"])
                return
            if not is_login_page(page.url):
                return
        raise AuthRequired(f"{site}: MFA not approved in time")


async def _cli(site: str) -> None:
    async def print_number(s, n):
        print(f"[{s}] Approve sign-in in Microsoft Authenticator: {n}", flush=True)

    sm = SessionManager(load_settings(), print_number, headless="--show" not in sys.argv)
    try:
        await sm.login(site)  # type: ignore[arg-type]
        print(f"{site}: logged in")
    finally:
        await sm.close()


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] != "login" or sys.argv[2] not in ("brightspace", "teams", "outlook"):
        print("usage: python -m core.auth login brightspace|teams|outlook [--show]")
        sys.exit(2)
    asyncio.run(_cli(sys.argv[2]))
