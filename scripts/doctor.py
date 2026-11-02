"""Check that this JARVIS install is healthy:  .venv/bin/python scripts/doctor.py   (Windows: .venv\\Scripts\\python.exe)

Prints one line per check and what to do about anything that fails. Never prints secret values.
"""
from __future__ import annotations

import importlib
import shutil
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import hostos  # noqa: E402
from core.config import env_path, load_settings  # noqa: E402


def check_python():
    ok = sys.version_info >= (3, 12)
    return ok, f"Python {sys.version_info.major}.{sys.version_info.minor}", f"Install Python 3.12 or newer and run {hostos.installer_hint()} again."


def check_deps():
    missing = []
    for mod in ("playwright", "httpx", "telegram", "apscheduler", "telethon", "mcp", "aiohttp", "docx", "pptx",
                "pdfplumber", "google.oauth2"):
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(mod)
    return not missing, "Python packages" + (f" (missing: {', '.join(missing)})" if missing else ""), \
        f"Run {hostos.installer_hint()} again."


def check_chromium():
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            ok = Path(p.chromium.executable_path).exists()
    except Exception:  # noqa: BLE001 - any failure means the browser is not usable
        ok = False
    return ok, "Browser for sign-ins (Chromium)", f"Run: {hostos.python_hint('-m playwright install chromium')}"


def check_claude():
    return shutil.which("claude") is not None, "Claude Code command", \
        "Install Claude Code from https://claude.com/claude-code and sign in once by running: claude"


def check_config():
    path = env_path()
    if not path.exists():
        return False, "secrets.env", "Open the panel and complete the setup wizard."
    s = load_settings(path)
    need = [n for n, v in (("OPS_BOT_TOKEN", s.ops_bot_token), ("ALLOWED_TELEGRAM_ID", s.allowed_telegram_id),
                           ("POLITEMALL_USER", s.politemall_user), ("POLITEMALL_PASS", s.politemall_pass)) if not v]
    return not need, "Settings" + (f" (not set: {', '.join(need)})" if need else ""), \
        "Open the panel and complete the setup wizard."


def check_permissions():
    path = env_path()
    if hostos.is_windows():  # Windows has no 0600 mode; access follows the folder's permissions
        return True, "secrets.env (Windows: keep JARVIS inside your own user folder so others cannot read it)", ""
    if not path.exists():
        return True, "secrets.env is private", ""
    return hostos.secrets_mode_is_private(path), "secrets.env is private to you", f"Run: chmod 600 {path}"


def check_run_as():
    if hostos.is_windows() and path_has_run_as():
        return False, "CLAUDE_RUN_AS is set", "Windows cannot run Claude as another user. Remove CLAUDE_RUN_AS from secrets.env."
    return True, "No run-as user configured", ""


def path_has_run_as() -> bool:
    path = env_path()
    return path.exists() and bool(load_settings(path).claude_run_as)


def check_port():
    with socket.socket() as s:
        s.settimeout(0.5)
        busy = s.connect_ex(("127.0.0.1", 8765)) == 0
    return True, "Panel port 8765 " + ("(JARVIS panel already running)" if busy else "(free)"), ""


CHECKS = [check_python, check_deps, check_chromium, check_claude, check_config, check_permissions, check_run_as, check_port]


def main() -> int:
    bad = 0
    for fn in CHECKS:
        ok, label, hint = fn()
        print(f"  [{'ok' if ok else '!!'}] {label}")
        if not ok:
            bad += 1
            print(f"       -> {hint}")
    print("\nAll good." if not bad else f"\n{bad} thing(s) need attention.")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
