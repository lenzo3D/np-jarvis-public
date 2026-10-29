import json
import stat
import sys
from datetime import datetime, timedelta, timezone

import pytest

from core.config import load_settings
from core.db import DB
from ops import claude_runner
from ops.chat_agent import stream_claude
from ui import data

NOW = datetime(2026, 10, 27, 4, 0, tzinfo=timezone.utc)


@pytest.fixture
def recorded():
    rows = []
    claude_runner.set_usage_sink(lambda **kw: rows.append(kw))
    yield rows
    claude_runner.set_usage_sink(None)


def fake_claude(tmp_path, monkeypatch, body: str):
    exe = tmp_path / "claude"
    exe.write_text(f"#!{sys.executable}\n{body}\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("CLAUDE_BIN", str(exe))


STREAM = ('import json\n'
          'print(json.dumps({"type": "system", "session_id": "s1"}))\n'
          'print(json.dumps({"type": "result", "session_id": "s1", "result": "ok", "num_turns": 3, '
          '"total_cost_usd": 0.12, "usage": {"input_tokens": 1500, "output_tokens": 300, '
          '"cache_read_input_tokens": 9000, "cache_creation_input_tokens": 100}}))')


async def test_run_text_records_estimated_usage_under_its_label(tmp_path, monkeypatch, recorded):
    fake_claude(tmp_path, monkeypatch, 'import sys\nsys.stdin.read()\nprint("x" * 400)')
    with claude_runner.labelled("briefing"):
        out = await claude_runner.run_text("p" * 100, "i" * 300)
    assert out == "x" * 400
    (r,) = recorded
    assert r["job"] == "briefing" and r["kind"] == "text" and r["ok"] and r["estimated"]
    assert r["in_tokens"] == 100 and r["out_tokens"] == 100 and r["model"] == "sonnet"


async def test_failed_text_call_is_recorded_as_failed(tmp_path, monkeypatch, recorded):
    fake_claude(tmp_path, monkeypatch, 'import sys\nsys.stderr.write("usage limit")\nsys.exit(1)')
    with pytest.raises(claude_runner.ClaudeError):
        await claude_runner.run_text("p", "i")
    assert recorded[0]["ok"] is False and recorded[0]["job"] == "other"


async def test_run_agent_records_real_tokens(tmp_path, monkeypatch, recorded):
    fake_claude(tmp_path, monkeypatch, STREAM)
    with claude_runner.labelled("do_it"):
        _ = [t async for t in claude_runner.run_agent("go", tmp_path)]
    (r,) = recorded
    assert r["job"] == "do_it" and r["kind"] == "agent" and r["ok"]
    assert (r["in_tokens"], r["out_tokens"], r["cache_tokens"], r["turns"], r["cost_usd"]) == (1500, 300, 9100, 3, 0.12)


async def test_chat_stream_records_real_tokens(tmp_path, monkeypatch, recorded, env_file):
    fake_claude(tmp_path, monkeypatch, STREAM)
    s = load_settings(env_file)
    s.workspace_dir.mkdir(parents=True, exist_ok=True)
    with claude_runner.labelled("chat_web"):
        events = [e async for e in stream_claude(s, "hello", None)]
    assert ("result", "ok") in events
    (r,) = recorded
    assert r["job"] == "chat_web" and r["in_tokens"] == 1500 and r["ok"] and not r.get("estimated")


async def test_sink_errors_never_break_the_call(tmp_path, monkeypatch):
    def boom(**kw):
        raise RuntimeError("disk full")

    claude_runner.set_usage_sink(boom)
    try:
        fake_claude(tmp_path, monkeypatch, 'import sys\nsys.stdin.read()\nprint("fine")')
        assert await claude_runner.run_text("p", "i") == "fine"
    finally:
        claude_runner.set_usage_sink(None)


def test_attach_db_logs_and_prunes(db):
    old = NOW - timedelta(days=120)
    db.record_usage(old, "briefing", "text", "sonnet", True, 3.0)
    claude_runner.attach_db(db, clock=lambda: NOW)
    try:
        claude_runner.record_usage(kind="text", model="haiku", ok=True, seconds=2.0, in_tokens=10, out_tokens=5,
                                   estimated=True)
    finally:
        claude_runner.set_usage_sink(None)
    rows = db.conn.execute("select job, model from claude_usage").fetchall()
    assert [(r["job"], r["model"]) for r in rows] == [("other", "haiku")]


def test_usage_summary(env_file):
    s = load_settings(env_file)
    db = DB(s.db_path)
    db.init()
    t = NOW - timedelta(hours=2)
    db.record_usage(t, "briefing", "text", "sonnet", True, 4.0, 800, 300, estimated=True)
    db.record_usage(t, "briefing", "text", "sonnet", False, 2.0, 800, 0, estimated=True)
    db.record_usage(t, "chat_telegram", "agent", None, True, 30.0, 2000, 500, 8000, False, 4, 0.2)
    db.record_usage(NOW - timedelta(days=3), "chat_telegram", "agent", None, True, 10.0, 100, 50)
    db.record_usage(NOW - timedelta(days=40), "do_it", "agent", None, True, 99.0, 1, 1)
    u = data.usage(s, 7, now=NOW)
    jobs = {j["job"]: j for j in u["by_job"]}
    assert set(jobs) == {"briefing", "chat_telegram"} and u["by_job"][0]["calls"] == 2
    assert jobs["briefing"]["failed"] == 1 and jobs["briefing"]["estimated"] is True
    assert jobs["chat_telegram"]["in_tokens"] == 2100 and jobs["chat_telegram"]["estimated"] is False
    assert u["total"]["calls"] == 4 and u["total"]["failed"] == 1
    assert [d["calls"] for d in u["by_day"]] == [1, 3]
    assert data.usage(s, 90, now=NOW)["total"]["calls"] == 5


def test_usage_without_database_or_table_is_empty(env_file):
    s = load_settings(env_file)
    assert data.usage(s)["by_job"] == []
