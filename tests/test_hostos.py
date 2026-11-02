"""The OS-specific helpers. Windows branches are driven with injected fakes so they run on every OS."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import hostos


def test_hints_follow_the_os():
    assert hostos.venv_python(windows=False) == ".venv/bin/python"
    assert hostos.venv_python(windows=True) == r".\.venv\Scripts\python.exe"
    assert hostos.python_hint("-m core.gcal login", windows=True) == r".\.venv\Scripts\python.exe -m core.gcal login"
    assert hostos.installer_hint(windows=False) == "./install.sh" and hostos.installer_hint(windows=True) == r".\install.ps1"


def test_new_group_and_env_kwargs():
    assert hostos.new_group_kwargs(windows=False) == {"start_new_session": True}
    assert hostos.new_group_kwargs(windows=True) == {"creationflags": 0x200}
    assert hostos.child_env(windows=False) is None
    env = hostos.child_env(windows=True)
    assert env["PYTHONUTF8"] == "1" and env["PYTHONIOENCODING"] == "utf-8" and env["PATH"] == os.environ["PATH"]


def test_default_transport():
    assert hostos.default_api_transport(windows=True) == "tcp"
    assert hostos.default_api_transport(windows=False) == "unix"


def test_sqlite_uri_and_rule_path(tmp_path):
    uri = hostos.sqlite_ro_uri(tmp_path / "a b.db")
    assert uri.startswith("file:///") and uri.endswith("a%20b.db?mode=ro")
    assert hostos.rule_path(Path("a") / "b") == "a/b"


# ---------- locks ----------
class FakeMsvcrt:
    LK_NBLCK, LK_UNLCK = 2, 0

    def __init__(self, fail_times=0):
        self.calls, self.fail_times = [], fail_times

    def locking(self, fd, mode, nbytes):
        self.calls.append((mode, nbytes))
        if mode == self.LK_NBLCK and self.fail_times:
            self.fail_times -= 1
            raise OSError("locked")


def test_windows_lock_retries_until_free_then_unlocks(tmp_path):
    fake = FakeMsvcrt(fail_times=3)
    with hostos.file_lock(tmp_path / "x" / "l.lock", windows=True, msvcrt=fake):
        assert fake.calls.count((FakeMsvcrt.LK_NBLCK, 1)) == 4
    assert fake.calls[-1] == (FakeMsvcrt.LK_UNLCK, 1)


def test_windows_lock_unlocks_after_error(tmp_path):
    fake = FakeMsvcrt()
    with pytest.raises(RuntimeError):
        with hostos.file_lock(tmp_path / "l.lock", windows=True, msvcrt=fake):
            raise RuntimeError("boom")
    assert fake.calls[-1][0] == FakeMsvcrt.LK_UNLCK


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl is POSIX only")
def test_posix_lock_excludes_a_second_holder(tmp_path):
    import fcntl

    path = tmp_path / "l.lock"
    with hostos.file_lock(path, windows=False):
        with open(path, "w", encoding="utf-8") as other:
            with pytest.raises(OSError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with open(path, "w", encoding="utf-8") as other:  # released afterwards
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)


# ---------- processes ----------
def test_terminate_tree_windows_uses_taskkill_tree():
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        return SimpleNamespace(returncode=0)

    assert hostos.terminate_tree(42, windows=True, run=run) is True
    assert hostos.terminate_tree(42, force=True, windows=True, run=run) is True
    assert seen == [["taskkill", "/T", "/PID", "42"], ["taskkill", "/T", "/PID", "42", "/F"]]


def test_terminate_tree_windows_reports_failure():
    assert hostos.terminate_tree(1, windows=True, run=lambda *a, **k: SimpleNamespace(returncode=128)) is False

    def broken(*a, **k):
        raise FileNotFoundError

    assert hostos.terminate_tree(1, windows=True, run=broken) is False


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
def test_terminate_tree_posix_kills_the_group():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        assert hostos.pid_alive(p.pid)
        assert hostos.terminate_tree(p.pid) is True
        assert p.wait(timeout=10) == -signal.SIGTERM
        assert hostos.terminate_tree(p.pid, force=True) is True  # already gone: still fine
    finally:
        p.kill()


def test_kill_process_tree_windows_forces_the_tree_posix_terminates():
    seen = []
    proc = SimpleNamespace(pid=7, terminate=lambda: seen.append("terminate"))
    hostos.kill_process_tree(proc, windows=False)
    hostos.kill_process_tree(proc, windows=True, run=lambda cmd, **k: seen.append(cmd) or SimpleNamespace(returncode=0))
    assert seen == ["terminate", ["taskkill", "/T", "/PID", "7", "/F"]]


class FakeLoop:
    def __init__(self, supported):
        self.supported, self.handlers, self.soon = supported, {}, []

    def add_signal_handler(self, sig, cb):
        if not self.supported:
            raise NotImplementedError
        self.handlers[sig] = cb

    def call_soon_threadsafe(self, cb):
        self.soon.append(cb)


class FakeSignals:
    SIGINT, SIGTERM, SIGBREAK = 2, 15, 21

    def __init__(self):
        self.installed = {}

    def signal(self, sig, handler):
        self.installed[sig] = handler


def test_stop_signals_use_the_loop_when_it_can():
    loop, sigs, hits = FakeLoop(True), FakeSignals(), []
    hostos.install_stop_signals(loop, lambda: hits.append(1), windows=False, signal_module=sigs)
    assert set(loop.handlers) == {2, 15} and not sigs.installed


def test_stop_signals_fall_back_to_signal_signal_on_windows():
    loop, sigs, hits = FakeLoop(False), FakeSignals(), []
    hostos.install_stop_signals(loop, lambda: hits.append(1), windows=True, signal_module=sigs)
    assert set(sigs.installed) == {2, 15, 21}
    sigs.installed[2](2, None)  # Ctrl+C arrives on the main thread; the loop is told to stop
    loop.soon[0]()
    assert hits == [1]


def test_restart_process(monkeypatch):
    calls = []
    hostos.restart_process(["py", "-m", "ui"], windows=False, execv=lambda *a: calls.append(("exec", a)))
    assert calls == [("exec", ("py", ["py", "-m", "ui"]))]
    calls.clear()
    monkeypatch.delenv("JARVIS_RESTART_BY_PARENT", raising=False)
    hostos.restart_process(["py", "-m", "ui"], windows=True, popen=lambda a: calls.append(("popen", a)),
                           exit_fn=lambda c: calls.append(("exit", c)))
    assert calls == [("popen", ["py", "-m", "ui"]), ("exit", 0)]
    calls.clear()
    monkeypatch.setenv("JARVIS_RESTART_BY_PARENT", "1")  # the login-task loop starts the next one
    hostos.restart_process(["py", "-m", "ui"], windows=True, popen=lambda a: calls.append(("popen", a)),
                           exit_fn=lambda c: calls.append(("exit", c)))
    assert calls == [("exit", 0)]


def test_windows_keep_awake_flags():
    seen = []
    k32 = SimpleNamespace(SetThreadExecutionState=lambda f: seen.append(f) or 1)
    assert hostos.set_windows_awake(True, kernel32=k32) and hostos.set_windows_awake(False, kernel32=k32)
    assert seen == [0x80000001, 0x80000000]
    assert hostos.set_windows_awake(True, kernel32=SimpleNamespace()) is False  # never raises


SID = "S-1-5-21-111-222-333-1001"


def fake_run(seen, icacls_code=0):
    def run(cmd, **k):
        seen.append(cmd)
        if cmd[0] == "whoami":
            return SimpleNamespace(returncode=0, stdout=f'"PC\\jake","{SID}"\n')
        return SimpleNamespace(returncode=icacls_code, stdout="")
    return run


def test_restrict_to_user_windows_grants_by_sid(tmp_path):
    seen = []
    hostos.restrict_to_user(tmp_path / "t.json", windows=True, run=fake_run(seen))
    assert seen[1][0] == "icacls" and "/inheritance:r" in seen[1] and f"*{SID}:(R,W,D)" in seen[1]
    seen.clear()
    hostos.restrict_to_user(tmp_path, windows=True, run=fake_run(seen))  # a folder gets inheritable full control
    assert f"*{SID}:(OI)(CI)F" in seen[1]


def test_restrict_to_user_windows_warns_on_failure_and_never_raises(tmp_path, caplog):
    hostos.restrict_to_user(tmp_path / "t.json", windows=True, run=fake_run([], icacls_code=5))
    assert "could not restrict" in caplog.text
    hostos.restrict_to_user(tmp_path / "t.json", windows=True, run=lambda *a, **k: 1 / 0)


def test_api_transport_setting_and_validation():
    assert hostos.api_transport("tcp", windows=False) == "tcp" and hostos.api_transport(None, windows=True) == "tcp"
    assert hostos.api_transport(None, windows=False) == "unix"
    hostos.check_api_transport(None, windows=True)
    hostos.check_api_transport("tcp", windows=True)
    hostos.check_api_transport("unix", windows=False)
    with pytest.raises(ValueError, match="Windows"):
        hostos.check_api_transport("unix", windows=True)
    with pytest.raises(ValueError, match="tcp or unix"):
        hostos.check_api_transport("pipe", windows=False)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_restrict_to_user_posix_is_chmod_600(tmp_path):
    p = tmp_path / "s"
    p.write_text("x", encoding="utf-8")
    p.chmod(0o644)
    hostos.restrict_to_user(p, windows=False)
    assert p.stat().st_mode & 0o777 == 0o600
    assert hostos.secrets_mode_is_private(p, windows=False)
    p.chmod(0o640)
    assert not hostos.secrets_mode_is_private(p, windows=False)
    assert hostos.secrets_mode_is_private(p, windows=True)


# ---------- finding claude ----------
def test_claude_posix_unchanged():
    which = lambda n: "/usr/local/bin/claude"  # noqa: E731
    assert hostos.claude_command(None, windows=False, which=which, environ={}) == ["/usr/local/bin/claude"]
    assert hostos.claude_command("jarvis", windows=False, which=which, environ={"CLAUDE_BIN": "/x/claude"}) == \
        ["sudo", "-n", "-H", "-u", "jarvis", "/x/claude"]


def test_run_as_is_refused_on_windows():
    with pytest.raises(hostos.LauncherError, match="CLAUDE_RUN_AS"):
        hostos.claude_command("jarvis", windows=True, which=lambda n: None, environ={})


def test_windows_prefers_claude_exe():
    found = {"claude.exe": r"C:\Users\a\.local\bin\claude.exe", "claude": r"C:\npm\claude.cmd"}
    assert hostos.claude_command(None, windows=True, which=found.get, environ={}) == [found["claude.exe"]]


def test_windows_cmd_shim_is_unwrapped_to_node_script(tmp_path):
    shim = tmp_path / "claude.cmd"
    cli = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("//", encoding="utf-8")
    shim.write_text('@ECHO off\r\nSET dp0=%~dp0\r\n"%_prog%"  "%dp0%\\node_modules\\@anthropic-ai\\claude-code\\cli.js" %*\r\n',
                    encoding="utf-8")
    which = {"claude": str(shim), "node": r"C:\node\node.exe"}.get
    assert hostos.claude_command(None, windows=True, which=which, environ={}) == [r"C:\node\node.exe", str(cli)]


def test_windows_cmd_shim_to_exe_and_unresolvable(tmp_path):
    shim = tmp_path / "claude.cmd"
    exe = tmp_path / "bin" / "claude.exe"
    exe.parent.mkdir()
    exe.write_text("", encoding="utf-8")
    shim.write_text('"%dp0%\\bin\\claude.exe" %*', encoding="utf-8")
    assert hostos.claude_command(None, windows=True, which={"claude": str(shim)}.get, environ={}) == [str(exe)]
    shim.write_text("echo nothing useful", encoding="utf-8")  # never hand text from the school to cmd.exe
    with pytest.raises(hostos.LauncherError, match="claude.exe"):
        hostos.claude_command(None, windows=True, which={"claude": str(shim)}.get, environ={})


NPM_SHIM = ('''@ECHO off\r
GOTO start\r
:find_dp0\r
SET dp0=%~dp0\r
EXIT /b\r
:start\r
SETLOCAL\r
CALL :find_dp0\r
\r
IF EXIST "%dp0%\\node.exe" (\r
  SET "_prog=%dp0%\\node.exe"\r
) ELSE (\r
  SET "_prog=node"\r
  SET PATHEXT=%PATHEXT:;.JS;=;%\r
)\r
\r
endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\\node_modules\\@anthropic-ai\\claude-code\\cli.js" %*\r
''')


def _npm_install(tmp_path, with_local_node):
    shim = tmp_path / "claude.cmd"
    shim.write_bytes(NPM_SHIM.encode())
    cli = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("//", encoding="utf-8")
    if with_local_node:
        (tmp_path / "node.exe").write_text("", encoding="utf-8")
    return shim, cli


@pytest.mark.parametrize("with_local_node", [True, False])
def test_verbatim_npm_shim_gives_node_plus_script_never_bare_node(tmp_path, with_local_node):
    shim, cli = _npm_install(tmp_path, with_local_node)
    which = {"claude": str(shim), "node": r"C:\node\node.exe"}.get
    cmd = hostos.claude_command(None, windows=True, which=which, environ={})
    assert cmd[-1] == str(cli) and len(cmd) == 2  # `node.exe -p <prompt>` would run the prompt as JavaScript
    assert cmd[0] == (str(tmp_path / "node.exe") if with_local_node else r"C:\node\node.exe")


def test_shim_with_only_node_exe_is_refused(tmp_path):
    shim = tmp_path / "claude.cmd"
    shim.write_text('IF EXIST "%dp0%\\node.exe" (\n  SET "_prog=%dp0%\\node.exe"\n)\n"%_prog%" %*\n', encoding="utf-8")
    (tmp_path / "node.exe").write_text("", encoding="utf-8")
    with pytest.raises(hostos.LauncherError):
        hostos.claude_command(None, windows=True, which={"claude": str(shim), "node": "node"}.get, environ={})


# ---------- real OS calls; these only run on the Windows CI job ----------
winonly = pytest.mark.skipif(sys.platform != "win32", reason="uses the real Windows APIs")


@winonly
def test_real_msvcrt_lock_blocks_a_second_process(tmp_path):
    path = tmp_path / "l.lock"
    code = ("import sys, time; from pathlib import Path; from core import hostos\n"
            "t = time.time()\n"
            "with hostos.file_lock(Path(sys.argv[1])): print(round(time.time() - t, 1), flush=True)\n")
    with hostos.file_lock(path):
        p = subprocess.Popen([sys.executable, "-c", code, str(path)], stdout=subprocess.PIPE, text=True,
                             cwd=Path(__file__).resolve().parent.parent)
        time.sleep(1.5)
        assert p.poll() is None  # still waiting for us
    out, _ = p.communicate(timeout=20)
    assert float(out) >= 1.0


@winonly
def test_real_shim_file_is_unwrapped(tmp_path):
    shim, cli = _npm_install(tmp_path, with_local_node=True)
    assert hostos.claude_command(None, windows=True, which={"claude": str(shim)}.get, environ={}) == \
        [str(tmp_path / "node.exe"), str(cli)]


@winonly
def test_real_terminate_tree_kills_the_children_too(tmp_path):
    child = ("import subprocess, sys, time\n"
             "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
             "print(g.pid, flush=True); time.sleep(60)\n")
    p = subprocess.Popen([sys.executable, "-c", child], stdout=subprocess.PIPE, text=True, **hostos.new_group_kwargs())
    grandchild = int(p.stdout.readline())
    assert hostos.pid_alive(grandchild)
    hostos.terminate_tree(p.pid, force=True)
    p.wait(timeout=15)
    for _ in range(50):
        if not hostos.pid_alive(grandchild):
            break
        time.sleep(0.1)
    assert not hostos.pid_alive(grandchild)
