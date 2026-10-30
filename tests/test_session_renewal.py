import httpx

from core.auth import AuthRequired, SessionManager
from core.config import load_settings
from ops.watcher import Services, run_source
from tests.fakes import Clock, FakeBS, FakeNotifier


async def _noop(*a):
    return None


class FakeCtx:
    def __init__(self, cookies=None):
        self._cookies = cookies or []
        self.added = []

    async def cookies(self):
        return list(self._cookies)

    async def add_cookies(self, cookies):
        self.added.extend(cookies)


class FakeSessions:
    def __init__(self, renews=True):
        self.renews, self.refreshed, self.saved = renews, [], []

    async def refresh(self, site):
        self.refreshed.append(site)
        return self.renews

    async def save_cookies(self, site):
        self.saved.append(site)


def services(db, env_file, sessions):
    return Services(settings=load_settings(env_file), db=db, sessions=sessions, bs=FakeBS(), notifier=FakeNotifier(),
                    clock=Clock())


# ---------- the sweep renews quietly before it alerts ----------
async def test_quiet_renewal_means_no_alert_and_the_sweep_is_retried(db, env_file):
    sessions = FakeSessions(renews=True)
    s = services(db, env_file, sessions)
    calls = []

    async def collect():
        calls.append(1)
        if len(calls) == 1:
            raise AuthRequired("brightspace: redirected to the sign-in page")
        return []

    await run_source(s, "brightspace", collect)
    assert len(calls) == 2 and sessions.refreshed == ["brightspace"]
    assert not [m for m, _ in s.notifier.messages if "session expired" in m]
    assert db.get_setting("auth_prompted:brightspace") == "0" and sessions.saved == ["brightspace"]


async def test_failed_renewal_alerts_once_with_the_reason(db, env_file):
    sessions = FakeSessions(renews=False)
    s = services(db, env_file, sessions)

    async def collect():
        raise AuthRequired("teams: no graph.microsoft.com token in the Teams web session")

    await run_source(s, "teams", collect)
    await run_source(s, "teams", collect)
    msgs = [m for m, _ in s.notifier.messages if "session expired" in m]
    assert len(msgs) == 1 and "Reason: teams: no graph.microsoft.com token" in msgs[0]
    assert sessions.refreshed == ["teams", "teams"]


async def test_a_session_that_expires_again_right_after_renewal_is_reported(db, env_file):
    s = services(db, env_file, FakeSessions(renews=True))

    async def collect():
        raise AuthRequired("outlook: signed out")

    await run_source(s, "outlook", collect)
    assert len([m for m, _ in s.notifier.messages if "session expired" in m]) == 1


async def test_groups_are_not_renewed_through_the_browser(db, env_file):
    sessions = FakeSessions()
    s = services(db, env_file, sessions)

    async def collect():
        raise AuthRequired("x")

    await run_source(s, "group", collect)
    assert sessions.refreshed == []


# ---------- SessionManager ----------
async def test_refresh_is_rate_limited_and_saves_cookies_on_success(env_file):
    sm = SessionManager(load_settings(env_file), _noop)
    attempts = []

    async def live(site):
        attempts.append(site)
        return len(attempts) > 1  # first try fails, later ones would succeed

    sm._refresh_live = live
    assert await sm.refresh("teams") is False
    assert await sm.refresh("teams") is False  # inside the cool-down: no second browser visit
    assert attempts == ["teams"]


async def test_refresh_success_saves_the_cookies(env_file):
    sm = SessionManager(load_settings(env_file), _noop)
    saved = []

    async def live(site):
        return True

    async def save(site, ctx=None):
        saved.append(site)

    sm._refresh_live, sm.save_cookies = live, save
    assert await sm.refresh("outlook") is True and saved == ["outlook"]


async def test_refresh_never_runs_during_a_login_and_survives_browser_errors(env_file):
    sm = SessionManager(load_settings(env_file), _noop)
    sm.login_in_progress.add("teams")
    assert await sm.refresh("teams") is False

    async def boom(site):
        raise RuntimeError("browser died")

    sm._refresh_live = boom
    assert await sm.refresh("outlook") is False


# ---------- cookies ----------
async def test_restore_only_fills_in_missing_cookies_never_overwrites_fresh_ones(env_file):
    s = load_settings(env_file)
    sm = SessionManager(s, _noop)
    old = [{"name": "d2lSessionVal", "value": "OLD", "domain": ".x.edu", "path": "/"},
           {"name": "session_only", "value": "keep", "domain": ".x.edu", "path": "/"}]
    await sm.save_cookies("brightspace", FakeCtx(old))
    live = FakeCtx([{"name": "d2lSessionVal", "value": "NEW", "domain": "x.edu", "path": "/"}])
    await sm.restore_cookies("brightspace", live)
    assert [c["name"] for c in live.added] == ["session_only"]


async def test_absorb_writes_refreshed_server_cookies_back_to_the_browser(env_file):
    sm = SessionManager(load_settings(env_file), _noop)
    ctx = FakeCtx([{"name": "d2lSessionVal", "value": "OLD", "domain": ".x.edu", "path": "/", "secure": True, "httpOnly": True},
                   {"name": "other", "value": "same", "domain": ".x.edu", "path": "/"}])
    sm._contexts["brightspace"] = ctx
    jar = httpx.Cookies()
    jar.set("d2lSessionVal", "NEW", domain=".x.edu", path="/")
    jar.set("other", "same", domain=".x.edu", path="/")
    await sm.absorb("brightspace", jar)
    assert ctx.added == [{"name": "d2lSessionVal", "value": "NEW", "domain": ".x.edu", "path": "/", "secure": True, "httpOnly": True}]
    await sm.absorb("teams", jar)  # no open browser for that site: nothing happens


# ---------- real Chromium ----------
async def test_quiet_renewal_waits_out_a_slow_redirect_but_stops_at_a_password_page(env_file, tmp_path):
    import dataclasses

    from aiohttp import web
    from aiohttp.test_utils import TestServer

    async def home(request):
        return web.Response(text="<script>location.href='/d2l/login/wait'</script>", content_type="text/html")

    async def wait(request):  # sits on a sign-in URL for a while, then lands back in the app
        return web.Response(text="<script>setTimeout(()=>location.href='/d2l/le/done',3000)</script>", content_type="text/html")

    async def done(request):
        return web.Response(text="<p>home</p>", content_type="text/html")

    async def password(request):
        return web.Response(text='<input name="loginfmt" type="text">', content_type="text/html")

    app = web.Application()
    app.router.add_get("/d2l/home", home)
    app.router.add_get("/d2l/login/wait", wait)
    app.router.add_get("/d2l/le/done", done)
    app.router.add_get("/pw/d2l/home", home)
    server = TestServer(app)
    await server.start_server()
    try:
        settings = dataclasses.replace(load_settings(env_file), politemall_base=f"http://127.0.0.1:{server.port}",
                                       state_dir=tmp_path / "state")
        sm = await _manager_with_local_chromium(settings)
        try:
            assert await sm.refresh("brightspace") is True
        finally:
            await sm.close()
        # a page that asks for credentials is never guessed at
        app2 = web.Application()
        app2.router.add_get("/d2l/home", lambda r: web.HTTPFound("/d2l/login/credentials"))
        app2.router.add_get("/d2l/login/credentials", password)
        server2 = TestServer(app2)
        await server2.start_server()
        try:
            settings2 = dataclasses.replace(settings, politemall_base=f"http://127.0.0.1:{server2.port}",
                                            state_dir=tmp_path / "state2")
            sm2 = await _manager_with_local_chromium(settings2)
            try:
                assert await sm2.refresh("brightspace") is False
            finally:
                await sm2.close()
        finally:
            await server2.close()
    finally:
        await server.close()


async def _true():
    return True


async def _manager_with_local_chromium(settings):
    """SessionManager whose browser launch also works where Chromium is not in Playwright's default place (CI, sandboxes)."""
    import glob

    import pytest
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    real = pw.chromium.launch_persistent_context
    exes = [None, *glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome")]

    async def launch(path, **kw):
        last = None
        for exe in exes:
            try:
                return await real(path, **({"executable_path": exe} if exe else {}), **kw)
            except Exception as e:  # noqa: BLE001
                last = e
        pytest.skip(f"no Chromium available: {type(last).__name__}")

    class Chromium:
        launch_persistent_context = staticmethod(launch)

    class PW:
        chromium = Chromium

        async def stop(self):
            await pw.stop()

    sm = SessionManager(settings, _noop, checker=lambda site: _true())
    sm._pw = PW()
    return sm
