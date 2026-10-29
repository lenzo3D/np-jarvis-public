import asyncio
from pathlib import Path

import pytest

from core.auth import AuthRequired, SessionManager, extract_mfa_number, is_login_page
from core.config import load_settings

FIX = Path(__file__).parent / "fixtures"


def test_extract_mfa_number():
    assert extract_mfa_number((FIX / "mfa_number_match.html").read_text()) == "47"


def test_extract_mfa_number_none():
    assert extract_mfa_number((FIX / "login_redirect.html").read_text()) is None


def test_is_login_page():
    assert is_login_page("https://login.microsoftonline.com/abc/oauth2/authorize?x=1")
    assert is_login_page("https://politemall.example/d2l/login?target=%2fd2l%2fhome")
    assert not is_login_page("https://politemall.example/d2l/home")


class FakeAuth:
    def __init__(self):
        self.valid = False
        self.flow_runs = 0

    async def check(self, site):
        return self.valid

    async def flow(self, site, notify):
        self.flow_runs += 1
        await notify(site, "47")
        await asyncio.sleep(0.05)
        self.valid = True


async def _noop(site, n):
    pass


async def test_login_lock_serialises(env_file):
    fake = FakeAuth()
    sm = SessionManager(load_settings(env_file), _noop, checker=fake.check, flow=fake.flow)
    await asyncio.gather(sm.login("brightspace"), sm.login("brightspace"))
    assert fake.flow_runs == 1


async def test_ensure_raises_when_invalid(env_file):
    fake = FakeAuth()
    sm = SessionManager(load_settings(env_file), _noop, checker=fake.check, flow=fake.flow)
    with pytest.raises(AuthRequired):
        await sm.ensure("brightspace")
    assert fake.flow_runs == 0  # never logs in automatically


async def test_login_relays_mfa_number(env_file):
    fake = FakeAuth()
    seen = []

    async def notify(site, n):
        seen.append((site, n))

    sm = SessionManager(load_settings(env_file), notify, checker=fake.check, flow=fake.flow)
    await sm.login("brightspace")
    assert seen == [("brightspace", "47")]


async def test_login_timeout_raises(env_file):
    async def slow_flow(site, notify):
        await asyncio.sleep(10)

    async def never(site):
        return False

    sm = SessionManager(load_settings(env_file), _noop, checker=never, flow=slow_flow, timeout=0.1)
    with pytest.raises(AuthRequired):
        await sm.login("teams")


def test_selectors_do_not_mix_text_engine_into_css_lists():
    from core.auth import SELECTORS
    for name, sel in SELECTORS.items():
        parts = [p.strip() for p in sel.split(",")] if not sel.startswith("text=") else [sel]
        assert not any(p.startswith("text=") for p in parts if len(parts) > 1), name


class LossyPage:
    """Mimics Microsoft's sign-in page clearing input typed before its scripts are ready."""

    def __init__(self, lose_first=1):
        self.value = ""
        self.lose = lose_first
        self.fills = 0

    async def wait_for_selector(self, sel, state="visible", timeout=0):
        return True

    async def wait_for_load_state(self, state, timeout=0):
        return None

    async def wait_for_timeout(self, ms):
        return None

    async def fill(self, sel, value):
        self.fills += 1
        if self.lose:
            self.lose -= 1
            self.value = ""
        else:
            self.value = value

    async def input_value(self, sel):
        return self.value


async def test_fill_reliably_retries_until_value_sticks():
    from core.auth import fill_reliably
    page = LossyPage(lose_first=2)
    await fill_reliably(page, "input", "s1@connect.np.edu.sg")
    assert page.value == "s1@connect.np.edu.sg" and page.fills == 3


async def test_fill_reliably_gives_up():
    from core.auth import fill_reliably
    page = LossyPage(lose_first=10)
    with pytest.raises(AuthRequired):
        await fill_reliably(page, "input", "x", attempts=3)


async def test_browser_closed_during_login_is_auth_required(env_file):
    class TargetClosedError(Exception):
        pass

    async def closed_flow(site, notify):
        raise TargetClosedError("Target page, context or browser has been closed")

    async def never(site):
        return False

    sm = SessionManager(load_settings(env_file), _noop, checker=never, flow=closed_flow)
    with pytest.raises(AuthRequired, match="browser"):
        await sm.login("brightspace")


class FakeCtx:
    def __init__(self, cookies=None):
        self._cookies = cookies or []
        self.added = []

    async def cookies(self):
        return list(self._cookies)

    async def add_cookies(self, cookies):
        self.added.extend(cookies)


async def test_session_cookies_saved_and_restored(env_file):
    s = load_settings(env_file)
    sm = SessionManager(s, _noop)
    live = [{"name": "d2lSessionVal", "value": "abc", "domain": "nplms.polite.edu.sg", "path": "/",
             "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Lax"}]
    await sm.save_cookies("brightspace", FakeCtx(live))
    path = s.state_dir / "sessions" / "brightspace.cookies.json"
    assert path.exists() and oct(path.stat().st_mode & 0o777) == "0o600"
    fresh = FakeCtx()
    await sm.restore_cookies("brightspace", fresh)
    assert fresh.added == live


async def test_restore_without_file_is_noop(env_file):
    sm = SessionManager(load_settings(env_file), _noop)
    ctx = FakeCtx()
    await sm.restore_cookies("teams", ctx)
    assert ctx.added == []


async def test_failed_cookie_fetch_keeps_previous_file(env_file):
    s = load_settings(env_file)
    sm = SessionManager(s, _noop)
    good = [{"name": "d2lSessionVal", "value": "abc", "domain": "nplms.polite.edu.sg", "path": "/"}]
    await sm.save_cookies("brightspace", FakeCtx(good))

    class Dead:
        async def cookies(self):
            raise RuntimeError("Target page, context or browser has been closed")

    await sm.save_cookies("brightspace", Dead())
    import json
    assert json.loads((s.state_dir / "sessions" / "brightspace.cookies.json").read_text()) == good


async def test_empty_cookie_list_does_not_overwrite(env_file):
    s = load_settings(env_file)
    sm = SessionManager(s, _noop)
    good = [{"name": "a", "value": "1", "domain": "x", "path": "/"}]
    await sm.save_cookies("teams", FakeCtx(good))
    await sm.save_cookies("teams", FakeCtx([]))
    import json
    assert json.loads((s.state_dir / "sessions" / "teams.cookies.json").read_text()) == good
