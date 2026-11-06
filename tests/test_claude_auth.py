"""The Claude light: sign-in state recorded from real calls, the 10-minute check, the Telegram alert and the panel tile."""
from datetime import datetime, timedelta, timezone

import pytest

from core.config import load_settings
from core.db import DB
from ops import claude_runner, claude_watch
from ops.chat_agent import ChatAgent
from ops.claude_runner import ClaudeAuthError, ClaudeError
from ops.watcher import Services
from tests.fakes import Clock, FakeBS, FakeNotifier
from ui import data

NOW = datetime(2026, 11, 6, 6, 0, tzinfo=timezone.utc)
EXPIRED = "Failed to authenticate: OAuth session expired and could not be refreshed"


@pytest.fixture
def svc(db, env_file):
    s = Services(settings=load_settings(env_file), db=db, sessions=None, bs=FakeBS(), notifier=FakeNotifier(),
                 clock=Clock(NOW))
    clock = {"now": NOW}
    claude_runner.set_auth_sink(lambda ok, detail: claude_runner.store_auth(db, clock["now"], ok, detail))
    s.extra["auth_clock"] = clock
    yield s
    claude_runner.set_auth_sink(None)


def test_login_errors_become_auth_errors_and_are_recorded(svc):
    e = claude_runner.claude_error(EXPIRED)
    assert isinstance(e, ClaudeAuthError) and "signed out" in str(e) and "OAuth" not in str(e)
    assert claude_runner.stored_auth(svc.db)["ok"] is False
    other = claude_runner.claude_error("Overloaded")
    assert type(other) is ClaudeError and str(other) == "Overloaded"


def test_a_successful_call_marks_signed_in(svc):
    claude_runner.claude_error(EXPIRED)
    claude_runner.record_usage(kind="text", model="haiku", ok=True, seconds=1.0)
    assert claude_runner.stored_auth(svc.db)["ok"] is True
    claude_runner.record_usage(kind="text", model="haiku", ok=False, seconds=1.0)  # an ordinary failure says nothing
    assert claude_runner.stored_auth(svc.db)["ok"] is True


def test_same_state_is_rewritten_only_every_10_minutes(svc):
    claude_runner.record_auth(True)
    first = claude_runner.stored_auth(svc.db)["at"]
    svc.extra["auth_clock"]["now"] = NOW + timedelta(minutes=5)
    claude_runner.record_auth(True)
    assert claude_runner.stored_auth(svc.db)["at"] == first
    svc.extra["auth_clock"]["now"] = NOW + timedelta(minutes=11)
    claude_runner.record_auth(True)
    assert claude_runner.stored_auth(svc.db)["at"] != first
    claude_runner.record_auth(False, EXPIRED)  # a change is written at once
    assert claude_runner.stored_auth(svc.db)["ok"] is False


def test_reset_value_from_the_sign_in_button_reads_as_unknown(svc):
    svc.db.set_setting("claude_auth", "0")
    assert claude_runner.stored_auth(svc.db) == {}


async def test_signed_out_alerts_once_then_announces_the_return(svc, monkeypatch):
    status = {"v": False}

    async def fake_status(run_as=None, timeout=30):
        return status["v"]

    calls = []

    async def run_text(prompt, input_text, model="sonnet", timeout=180, run_as=None):
        calls.append(model)
        claude_runner.record_usage(kind="text", model=model, ok=True, seconds=0.1)
        return "OK"

    monkeypatch.setattr(claude_runner, "auth_status", fake_status)
    svc.extra["run_text"] = run_text
    assert await claude_watch.check(svc) == "signed_out"
    assert await claude_watch.check(svc) is None  # not again
    assert len(svc.notifier.messages) == 1 and "sign in" in svc.notifier.messages[0][0].lower()
    status["v"] = True
    assert await claude_watch.check(svc) == "signed_in"
    assert calls == ["haiku"]  # one tiny call confirms it really works
    assert await claude_watch.check(svc) is None and calls == ["haiku"]  # no more calls once it is green


async def test_status_says_signed_in_but_calls_still_fail_keeps_it_red(svc, monkeypatch):
    async def fake_status(run_as=None, timeout=30):
        return True

    async def run_text(prompt, input_text, model="sonnet", timeout=180, run_as=None):
        raise claude_runner.claude_error(EXPIRED)

    monkeypatch.setattr(claude_runner, "auth_status", fake_status)
    svc.extra["run_text"] = run_text
    claude_runner.record_auth(False, EXPIRED)
    assert await claude_watch.check(svc) == "signed_out"
    assert claude_runner.stored_auth(svc.db)["ok"] is False


async def test_unknown_status_changes_nothing(svc, monkeypatch):
    async def fake_status(run_as=None, timeout=30):
        return None

    monkeypatch.setattr(claude_runner, "auth_status", fake_status)
    assert await claude_watch.check(svc) is None
    assert claude_runner.stored_auth(svc.db) == {} and svc.notifier.messages == []


async def test_auth_status_reads_the_cli_json(monkeypatch, tmp_path):
    fake = tmp_path / "claude"
    fake.write_text('#!/bin/sh\necho \'{"loggedIn": false, "authMethod": "none"}\'\n', encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("CLAUDE_BIN", str(fake))
    if not claude_runner.hostos.is_windows():
        assert await claude_runner.auth_status() is False


async def test_telegram_chat_does_not_retry_and_says_what_to_do(svc):
    svc.db.set_json_setting("chat_session", {"id": "old", "at": NOW.isoformat()})
    tries = []

    async def runner(prompt, session_id):
        tries.append(session_id)
        raise claude_runner.claude_error(EXPIRED)
        yield  # pragma: no cover

    agent = ChatAgent(svc, runner=runner)
    await agent.handle("what's due?")
    await agent.wait()
    assert tries == ["old"]  # no pointless fresh retry
    last = svc.notifier.messages[-1][0]
    assert "signed out" in last and "ClaudeAuthError" not in last and "OAuth" not in last


def _tile(settings):
    return {c["id"]: c for c in data.connections(settings, "running", NOW)}["claude"]


def test_panel_tile_follows_the_stored_state(env_file):
    settings = load_settings(env_file)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    db = DB(settings.db_path)
    db.init()
    assert _tile(settings)["state"] == "stale"
    claude_runner.store_auth(db, NOW, False, EXPIRED)
    t = _tile(settings)
    assert t["color"] == "red" and t["action"] == "login_claude" and t["action_label"] == "Sign in"
    claude_runner.store_auth(db, NOW, True)
    t = _tile(settings)
    assert t["color"] == "green" and t["action"] is None
    db.set_setting("claude_auth", "0")  # the Sign in button finished
    assert _tile(settings)["state"] == "stale"
    assert {c["id"]: c for c in data.connections(settings, "stopped", NOW)}["claude"]["state"] == "offline"
