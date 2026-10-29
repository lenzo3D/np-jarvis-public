import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from core.config import load_settings
from core.db import DB
from ops import scheduler
from ui import data
from ui.jobs import Jobs
from ui.server import make_app
from ui.supervisor import OpsSupervisor, Prefs

H = {"Host": "127.0.0.1:8765", "X-JARVIS-UI": "1"}
ROOT = Path(__file__).resolve().parent.parent
OK = [sys.executable, "-c", "print('logged in')"]
BAD = [sys.executable, "-c", "import sys; print('login failed'); sys.exit(1)"]


@pytest.fixture
def settings(env_file):
    s = load_settings(env_file)
    s.state_dir.mkdir(parents=True, exist_ok=True)
    return s


async def run_job(settings, cmd):
    db = DB(settings.db_path)
    db.init()
    db.set_setting("auth_prompted:brightspace", "1")
    jobs = Jobs({"login": {"label": "Login", "cmd": cmd, "clears": "auth_prompted:brightspace"}})
    sup = OpsSupervisor(settings, Prefs(settings.state_dir / "ui.json"))
    c = TestClient(TestServer(make_app(settings, sup, jobs=jobs, port=8765, db=db)))
    await c.start_server()
    try:
        assert (await c.post("/api/jobs/start", headers=H, data=json.dumps({"id": "login"}))).status == 200
        await jobs._task
    finally:
        await c.close()
    return DB(settings.db_path)


async def test_successful_login_clears_the_session_expired_flag_at_once(settings):
    db = await run_job(settings, OK)
    assert db.get_setting("auth_prompted:brightspace") == "0"
    now = datetime.now(timezone.utc)
    db.conn.execute("insert or replace into source_health values ('brightspace', ?, 0)", ((now - timedelta(minutes=2)).isoformat(),))
    row = {c["id"]: c for c in data.connections(settings, "running", now)}["brightspace"]
    assert row["state"] == "live" and row["color"] == "green"


async def test_failed_login_leaves_the_flag_alone(settings):
    db = await run_job(settings, BAD)
    assert db.get_setting("auth_prompted:brightspace") == "1"


def test_boot_checks_are_scheduled_shortly_after_start(env_file):
    added = []

    class FakeSched:
        def add_job(self, fn, trigger, **kw):
            added.append((fn.__name__, kw["id"], trigger.run_date, kw["replace_existing"]))

    scheduler.register_boot(FakeSched(), object(), delay_s=20)
    assert [(n, i) for n, i, _, _ in added] == [("sweep_brightspace", "boot_bs"), ("sweep_teams", "boot_teams"), ("sweep_outlook", "boot_mail")]
    assert all(r for *_, r in added)
    soonest, latest = added[0][2], added[-1][2]
    assert 0 < (soonest - datetime.now(soonest.tzinfo)).total_seconds() <= 25 and latest > soonest


def test_panel_probe_job_does_not_write_into_the_repo():
    from ui.jobs import JOBS
    assert "--no-write" in JOBS["probe_brightspace"]["cmd"]
    src = (ROOT / "scripts" / "probe_brightspace.py").read_text()
    assert 'WRITE = "--no-write" not in sys.argv' in src and "if WRITE:" in src
