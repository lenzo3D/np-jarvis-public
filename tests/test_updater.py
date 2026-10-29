import json
import subprocess
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from core.config import load_settings
from ui.server import make_app
from ui.supervisor import OpsSupervisor, Prefs
from ui.updater import Updater

GIT_ENV = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *GIT_ENV, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    origin, mac, dev = tmp_path / "origin.git", tmp_path / "mac", tmp_path / "dev"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(dev)], check=True, capture_output=True)
    git(dev, "checkout", "-q", "-b", "main")
    (dev / "pyproject.toml").write_text("v1\n")
    (dev / "app.py").write_text("one\n")
    git(dev, "add", "-A"); git(dev, "commit", "-q", "-m", "first"); git(dev, "push", "-q", "-u", "origin", "main")
    subprocess.run(["git", "clone", "-q", str(origin), str(mac)], check=True, capture_output=True)
    return mac, dev


def publish(dev: Path, name: str, text: str, msg: str):
    (dev / name).write_text(text)
    git(dev, "add", "-A"); git(dev, "commit", "-q", "-m", msg); git(dev, "push", "-q", "origin", "main")


def fake_pip(tmp_path) -> tuple[str, Path]:
    log = tmp_path / "pip.log"
    exe = tmp_path / "py"
    exe.write_text(f"#!{sys.executable}\nimport sys\nopen({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n")
    exe.chmod(0o755)
    return str(exe), log


async def test_up_to_date_then_behind(repos):
    mac, dev = repos
    up = Updater(mac)
    st = await up.check()
    assert st["ok"] and st["behind"] == 0 and st["branch"] == "main" and not st["dirty"]
    publish(dev, "app.py", "two\n", "Make alerts readable")
    st = await up.check()
    assert st["behind"] == 1 and st["changes"] == ["Make alerts readable"] and "1 update" in st["message"]
    assert (mac / "app.py").read_text() == "one\n"  # checking never changes files


async def test_apply_pulls_and_requests_restart_without_pip_when_deps_unchanged(repos, tmp_path):
    mac, dev = repos
    py, log = fake_pip(tmp_path)
    publish(dev, "app.py", "two\n", "change")
    res = await Updater(mac, python=py).apply()
    assert res["ok"] and res["restart"] and (mac / "app.py").read_text() == "two\n"
    assert not log.exists()
    again = await Updater(mac, python=py).apply()
    assert again["ok"] and not again["restart"] and "up to date" in again["message"]


async def test_apply_reinstalls_when_pyproject_changed(repos, tmp_path):
    mac, dev = repos
    py, log = fake_pip(tmp_path)
    publish(dev, "pyproject.toml", "v2\n", "new dependency")
    res = await Updater(mac, python=py).apply()
    assert res["ok"] and res["restart"]
    assert log.read_text().split() == ["-m", "pip", "install", "-q", "-e", str(mac)]


async def test_local_edits_are_stashed_not_destroyed(repos):
    mac, dev = repos
    publish(dev, "app.py", "two\n", "remote change")
    (mac / "app.py").write_text("my edit\n")
    st = await Updater(mac).check()
    assert st["dirty"] and st["dirty_files"] == ["app.py"] and any("saved aside" in n for n in st["notes"])
    res = await Updater(mac, python=sys.executable).apply()
    assert res["ok"] and res["restart"] and "git stash" in res["message"]
    assert (mac / "app.py").read_text() == "two\n"
    assert "my edit" in git(mac, "stash", "show", "-p", "stash@{0}")


async def test_local_commits_are_kept_on_a_backup_branch(repos):
    mac, dev = repos
    (mac / "local.txt").write_text("mine\n")
    git(mac, "add", "-A"); git(mac, "commit", "-q", "-m", "local commit")
    publish(dev, "app.py", "two\n", "remote change")
    st = await Updater(mac).check()
    assert st["ahead"] == 1 and st["behind"] == 1 and any("backup branch" in n for n in st["notes"])
    res = await Updater(mac, python=sys.executable).apply()
    assert res["ok"] and "backup/main-" in res["message"]
    assert (mac / "app.py").read_text() == "two\n" and not (mac / "local.txt").exists()
    backup = [b.strip() for b in git(mac, "branch", "--list", "backup/*").splitlines()][0]
    assert "local.txt" in git(mac, "show", "--stat", backup)


async def test_other_branch_switches_to_main_and_keeps_the_branch(repos):
    mac, dev = repos
    git(mac, "checkout", "-q", "-b", "feature")
    (mac / "f.txt").write_text("f\n")
    git(mac, "add", "-A"); git(mac, "commit", "-q", "-m", "feature work")
    publish(dev, "app.py", "two\n", "remote change")
    res = await Updater(mac, python=sys.executable).apply()
    assert res["ok"] and res["restart"]
    assert git(mac, "rev-parse", "--abbrev-ref", "HEAD") == "main" and (mac / "app.py").read_text() == "two\n"
    assert "feature" in git(mac, "branch", "--list", "feature") and git(mac, "branch", "--list", "backup/feature-*")


async def test_finder_ds_store_never_counts_as_local_work_and_never_blocks(repos):
    mac, dev = repos
    (dev / ".DS_Store").write_text("v1")
    git(dev, "add", "-A"); git(dev, "commit", "-q", "-m", "oops tracked ds_store"); git(dev, "push", "-q", "origin", "main")
    git(mac, "pull", "-q", "--ff-only")
    (mac / ".DS_Store").write_text("finder rewrote this")  # Finder touches it whenever the folder is opened
    git(dev, "rm", "-q", "--cached", ".DS_Store"); git(dev, "commit", "-q", "-m", "stop tracking .DS_Store")
    git(dev, "push", "-q", "origin", "main")
    st = await Updater(mac).check()
    assert st["ok"] and st["behind"] == 1 and st["dirty"] is False and st["notes"] == []
    res = await Updater(mac, python=sys.executable).apply()
    assert res["ok"] and res["restart"] and "stash" not in res["message"]
    assert git(mac, "stash", "list") == ""


async def test_not_a_git_checkout_and_unreachable_remote(tmp_path, repos):
    assert (await Updater(tmp_path / "nope").check())["ok"] is False
    mac, _ = repos
    git(mac, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    st = await Updater(mac).check()
    assert not st["ok"] and "Could not reach GitHub" in st["message"]


class FakeUpdater:
    def __init__(self, res):
        self.res = res

    async def check(self):
        return {"ok": True, "behind": 1}

    async def apply(self):
        return self.res


async def test_update_endpoint_schedules_restart_and_remembers_running_ops(env_file):
    s = load_settings(env_file)
    s.state_dir.mkdir(parents=True, exist_ok=True)
    sup = OpsSupervisor(s, Prefs(s.state_dir / "ui.json"), command=[sys.executable, "-c", "import time; time.sleep(30)"])
    await sup.start()
    called = []
    app = make_app(s, sup, port=8765, updater=FakeUpdater({"ok": True, "restart": True, "message": "Updated"}),
                   on_restart=lambda: called.append(1))
    c = TestClient(TestServer(app))
    await c.start_server()
    H = {"Host": "127.0.0.1:8765", "X-JARVIS-UI": "1"}
    try:
        r = await c.post("/api/update/apply", headers=H)
        assert r.status == 200 and (await r.json())["restart"]
        import asyncio
        await asyncio.sleep(0.7)
        assert called == [1] and sup.prefs.get("resume_ops") is True
        assert (await (await c.post("/api/update/check", headers=H)).json())["behind"] == 1
        no = TestClient(TestServer(make_app(s, sup, port=8765, updater=FakeUpdater({"ok": False, "message": "no", "restart": False}),
                                            on_restart=lambda: called.append(2))))
        await no.start_server()
        try:
            assert (await no.post("/api/update/apply", headers=H)).status == 409
            await asyncio.sleep(0.7)
            assert called == [1]
        finally:
            await no.close()
    finally:
        await sup.stop()
        await c.close()


async def test_git_warnings_on_stderr_do_not_break_the_check(repos, monkeypatch):
    mac, dev = repos
    publish(dev, "app.py", "two\n", "change")
    monkeypatch.setenv("GIT_TRACE", "1")  # git now writes noise to stderr on every command
    st = await Updater(mac).check()
    assert st["ok"] and st["behind"] == 1 and st["changes"] == ["change"]
    res = await Updater(mac, python=sys.executable).apply()
    assert res["ok"] and res["restart"]


async def test_check_failures_are_reported_as_json_not_a_500(env_file):
    class Boom:
        async def check(self):
            raise RuntimeError("kaput")

    s = load_settings(env_file)
    s.state_dir.mkdir(parents=True, exist_ok=True)
    sup = OpsSupervisor(s, Prefs(s.state_dir / "ui.json"))
    c = TestClient(TestServer(make_app(s, sup, port=8765, updater=Boom())))
    await c.start_server()
    try:
        r = await c.post("/api/update/check", headers={"Host": "127.0.0.1:8765", "X-JARVIS-UI": "1"})
        body = await r.json()
        assert r.status == 200 and body["ok"] is False and "RuntimeError: kaput" in body["message"]
    finally:
        await c.close()
