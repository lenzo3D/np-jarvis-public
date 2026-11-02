"""Behaviour that exists for Windows but is plain Python, plus guards that keep Windows from breaking."""
import ast
import asyncio
import sys
from pathlib import Path

import pytest

from core import hostos
from core.config import load_settings
from ops import claude_runner
from ui.supervisor import OpsSupervisor, Prefs

ROOT = Path(__file__).resolve().parent.parent
SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]


def test_every_text_file_access_names_its_encoding():
    """Windows defaults to cp1252, which cannot hold emoji or the prompts. Binary opens and os.open are exempt."""
    bad = []
    for path in list(ROOT.glob("**/*.py")):
        rel = path.relative_to(ROOT)
        if rel.parts[0] in {".venv", "state", "workspace", "tests"} or "site-packages" in rel.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            kw = {k.arg for k in node.keywords}
            owner = ast.unparse(f.value) if isinstance(f, ast.Attribute) else ""
            if name in ("read_text", "write_text") and "encoding" not in kw:
                bad.append(f"{rel}:{node.lineno} {name}")
            elif name in ("open", "fdopen") and owner in ("", "os", "io", "Path") or name == "fdopen":
                mode = next((a.value for a in node.args[1:2] if isinstance(a, ast.Constant)), None)
                mode = next((k.value.value for k in node.keywords if k.arg == "mode"), mode)
                if name == "open" and owner == "os":
                    continue
                if "b" not in str(mode) and "encoding" not in kw:
                    bad.append(f"{rel}:{node.lineno} {name}({mode})")
            elif name == "run" and owner == "subprocess" and "text" in kw and "encoding" not in kw:
                bad.append(f"{rel}:{node.lineno} subprocess.run(text=True)")
    assert bad == []


def test_no_posix_only_calls_outside_the_helper():
    """fcntl, killpg, add_signal_handler and AF_UNIX must stay behind core/hostos.py (or a guard)."""
    allowed = {"core/hostos.py", "core/localapi.py", "ui/supervisor.py", "ops/api.py"}
    needles = ("import fcntl", "os.killpg", "add_signal_handler", "start_new_session")
    hits = []
    for path in ROOT.glob("**/*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel.split("/")[0] in {".venv", "state", "workspace", "tests"} or rel in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        hits += [f"{rel}: {n}" for n in needles if n in text]
    assert hits == []


def test_claude_runner_refuses_run_as_on_windows(monkeypatch):
    monkeypatch.setattr(hostos, "is_windows", lambda: True)
    with pytest.raises(claude_runner.ClaudeError, match="CLAUDE_RUN_AS"):
        claude_runner._base_cmd("jarvis")


async def test_run_text_reports_a_missing_cmd_shim_as_claude_error(monkeypatch, tmp_path):
    shim = tmp_path / "claude.cmd"
    shim.write_text("echo hi", encoding="utf-8")
    monkeypatch.setattr(hostos, "is_windows", lambda: True)
    monkeypatch.setenv("CLAUDE_BIN", str(shim))
    with pytest.raises(claude_runner.ClaudeError, match="claude.exe"):
        await claude_runner.run_text("p", "x")


@pytest.fixture
def settings(env_file):
    s = load_settings(env_file)
    s.state_dir.mkdir(parents=True, exist_ok=True)
    return s


async def test_stop_skips_the_grace_period_when_a_polite_stop_is_impossible(settings, monkeypatch):
    """Windows: `taskkill /T` (no /F) fails for a process without a window; the supervisor must then force at once."""
    sup = OpsSupervisor(settings, Prefs(settings.state_dir / "ui.json"), command=SLEEP)
    calls = []
    real = hostos.terminate_tree

    def fake(pid, force=False, **kw):
        calls.append(force)
        return real(pid, force=True) if force else False  # polite request "fails"

    monkeypatch.setattr(hostos, "terminate_tree", fake)
    monkeypatch.setattr("ui.supervisor.STOP_GRACE", 60.0)
    await sup.start()
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    assert (await sup.stop())["ok"]
    assert calls == [False, True] and loop.time() - t0 < 10 and not sup.managed_running


def test_doctor_skips_mode_check_and_flags_run_as_on_windows(monkeypatch, tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    import doctor

    env = tmp_path / "secrets.env"
    env.write_text("CLAUDE_RUN_AS=jarvis\nALLOWED_TELEGRAM_ID=1\n", encoding="utf-8")
    env.chmod(0o666)
    monkeypatch.setenv("SECRETS_FILE", str(env))
    monkeypatch.setattr(hostos, "is_windows", lambda: True)
    ok, label, _ = doctor.check_permissions()
    assert ok and "Windows" in label
    ok, label, hint = doctor.check_run_as()
    assert not ok and "Remove CLAUDE_RUN_AS" in hint
    monkeypatch.setattr(hostos, "is_windows", lambda: False)
    assert doctor.check_run_as()[0] and not doctor.check_permissions()[0]
