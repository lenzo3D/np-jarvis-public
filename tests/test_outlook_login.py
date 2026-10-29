import pytest

from core.auth import AuthRequired, outlook_signed_in
from core.outlook import Outlook


class Locator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    async def count(self):
        return 1 if self.page.ready_after is not None and self.page.ticks >= self.page.ready_after else 0


class Ctx:
    def __init__(self, page):
        self.page = page

    async def cookies(self):
        return [{"name": n} for n in self.page.cookie_names()]

    async def new_page(self):
        return self.page


class FakePage:
    """A page that behaves like Outlook on the web. ticks advance once per wait_for_timeout (one second)."""

    def __init__(self, url="https://outlook.office.com/mail/", canary_after=None, ready_after=None, login_after=None):
        self.url, self.ticks = url, 0
        self.canary_after, self.ready_after, self.login_after = canary_after, ready_after, login_after
        self.context = Ctx(self)
        self.closed = False

    def cookie_names(self):
        names = ["ClientId"]
        if self.canary_after is not None and self.ticks >= self.canary_after:
            names.append("X-OWA-CANARY")
        return names

    def locator(self, selector):
        return Locator(self, selector)

    async def wait_for_timeout(self, ms):
        self.ticks += 1
        if self.login_after is not None and self.ticks >= self.login_after:
            self.url = "https://login.microsoftonline.com/common/oauth2/authorize"

    async def goto(self, *a, **k):
        return None

    async def evaluate(self, *a, **k):
        return {"tokens": [], "diag": {}}

    async def close(self):
        self.closed = True


async def test_outlook_page_that_never_signs_in_is_not_logged_in():
    # the bug: the page stays on outlook.office.com for the whole time (no redirect yet, no mailbox either)
    assert await outlook_signed_in(FakePage(), wait_s=6) is False


async def test_redirect_to_the_microsoft_login_page_means_signed_out_immediately():
    p = FakePage(login_after=3)
    assert await outlook_signed_in(p, wait_s=25) is False and p.ticks == 3  # gave up as soon as the redirect happened


async def test_canary_cookie_or_mail_ui_proves_a_signed_in_mailbox():
    assert await outlook_signed_in(FakePage(canary_after=3), wait_s=10) is True
    assert await outlook_signed_in(FakePage(ready_after=2), wait_s=10) is True
    assert await outlook_signed_in(FakePage(canary_after=0), wait_s=10) is True


class Session:
    def __init__(self, page):
        self.page = page

    async def context(self, site):
        assert site == "outlook"
        return Ctx(self.page)


async def test_reader_reports_a_login_wall_instead_of_an_empty_mailbox(monkeypatch):
    monkeypatch.setattr("core.outlook.outlook_signed_in", lambda page, wait_s=25: _false())
    page = FakePage()
    with pytest.raises(AuthRequired):
        await Outlook(Session(page)).recent()
    assert page.closed  # the browser tab is always closed


async def _false():
    return False


async def test_reader_gets_tokens_when_signed_in(monkeypatch):
    async def yes(page, wait_s=25):
        return True

    monkeypatch.setattr("core.outlook.outlook_signed_in", yes)
    page = FakePage()
    toks = await Outlook(Session(page))._browser_tokens()
    assert toks == [] and page.closed
