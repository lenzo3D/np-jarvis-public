import importlib.util
from pathlib import Path

import pytest

from core.auth import AuthRequired
from core.config import load_settings
from core.db import DB
from ui.jobs import JOBS, Jobs

SPEC = importlib.util.spec_from_file_location("connect_all", Path(__file__).resolve().parent.parent / "scripts" / "connect_all.py")
connect_all = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(connect_all)


class FakeSM:
    valid: dict = {}
    renewable: dict = {}
    login_fails: set = set()
    logins: list = []

    def __init__(self, settings, notify, headless=True):
        self.headless = headless

    async def is_valid(self, site):
        return self.valid.get(site, False)

    async def refresh(self, site):
        return self.renewable.get(site, False)

    async def login(self, site):
        FakeSM.logins.append(site)
        if site in self.login_fails:
            raise AuthRequired(f"{site}: MFA not approved in time")

    async def close(self):
        pass


@pytest.fixture
def fake(monkeypatch, env_file):
    monkeypatch.setenv("SECRETS_FILE", str(env_file))
    FakeSM.valid, FakeSM.renewable, FakeSM.login_fails, FakeSM.logins = {}, {}, set(), []

    async def ok(sm, settings, db):
        return "connected"

    monkeypatch.setattr(connect_all, "SessionManager", FakeSM)
    monkeypatch.setattr(connect_all, "CHECKS", {"brightspace": ok, "teams": ok, "outlook": ok})
    return env_file


def test_one_consolidated_job_runs_the_script_and_is_exclusive():
    job = JOBS["connect_all"]
    assert job["label"] == "Connect everything" and job["exclusive"] is True and job["cmd"][-1].endswith("connect_all.py")
    assert job["timeout"] >= 20 * 60 and Jobs().catalog()[0]["id"] == "connect_all"  # first in the list


async def test_connect_uses_the_least_disruptive_route(fake, env_file):
    s = load_settings(env_file)
    sm = FakeSM(s, None)
    FakeSM.valid["brightspace"] = True
    FakeSM.renewable["teams"] = True
    assert await connect_all.connect(sm, s, None, "brightspace", "POLITEMall") == "already"
    assert await connect_all.connect(sm, s, None, "teams", "Teams") == "renewed"
    assert await connect_all.connect(sm, s, None, "outlook", "Outlook email") == "signed in"
    assert FakeSM.logins == ["outlook"]  # only the one that really needed a sign-in


async def test_everything_connects_and_the_flags_are_cleared(fake, env_file, capsys):
    db = DB(load_settings(env_file).db_path)
    db.init()
    for site in ("brightspace", "teams", "outlook"):
        db.set_setting(f"auth_prompted:{site}", "1")
    FakeSM.valid = {"brightspace": True, "teams": True, "outlook": True}
    assert await connect_all.main() == 0
    out = capsys.readouterr().out
    assert "SUMMARY" in out and out.count("OK  ") >= 3 and "All set" in out
    db2 = DB(load_settings(env_file).db_path)
    assert all(db2.get_setting(f"auth_prompted:{s}") == "0" for s in ("brightspace", "teams", "outlook"))


async def test_one_failing_site_does_not_stop_the_others(fake, env_file, capsys):
    FakeSM.login_fails = {"teams"}
    FakeSM.valid = {"brightspace": True, "outlook": True}
    assert await connect_all.main() == 1
    out = capsys.readouterr().out
    assert "FAIL  Teams" in out and "OK    POLITEMall" in out and "OK    Outlook email" in out
    assert "Needs attention: Teams" in out


async def test_a_failing_check_is_reported_with_its_reason(fake, env_file, capsys, monkeypatch):
    async def bad(sm, settings, db):
        raise RuntimeError("signed in, but the mailbox could not be read")

    monkeypatch.setattr(connect_all, "CHECKS", {**connect_all.CHECKS, "outlook": bad})
    FakeSM.valid = {"brightspace": True, "teams": True, "outlook": True}
    assert await connect_all.main() == 1
    assert "FAIL  Outlook email: RuntimeError: signed in, but the mailbox could not be read" in capsys.readouterr().out
