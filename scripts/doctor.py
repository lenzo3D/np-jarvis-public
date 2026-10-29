"""Check that this JARVIS install is healthy:  .venv/bin/python scripts/doctor.py

Prints one line per check and what to do about anything that fails. Never prints secret values.
"""
from __future__ import annotations

import importlib
import shutil
import socket
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import env_path, load_settings  # noqa: E402


def check_python():
    ok = sys.version_info >= (3, 12)
    return ok, f"Python {sys.version_info.major}.{sys.version_info.minor}", "Install Python 3.12 or newer and run ./install.sh again."


def check_deps():
    missing = []
    for mod in ("playwright", "httpx", "telegram", "apscheduler", "telethon", "mcp", "aiohttp", "docx", "pptx",
                "pdfplumber", "google.oauth2"):
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(mod)
    return not missing, "Python packages" + (f" (missing: {', '.join(missing)})" if missing else ""), \
        "Run ./install.sh again."


def check_chromium():
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            ok = Path(p.chromium.executable_path).exists()
    except Exception:  # noqa: BLE001 - any failure means the browser is not usable
        ok = False
    return ok, "Browser for sign-ins (Chromium)", "Run: .venv/bin/python -m playwright install chromium"


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
    if not path.exists():
        return True, "secrets.env is private", ""
    mode = stat.S_IMODE(path.stat().st_mode)
    return not (mode & 0o077), "secrets.env is private to you", f"Run: chmod 600 {path}"


def check_port():
    with socket.socket() as s:
        s.settimeout(0.5)
        busy = s.connect_ex(("127.0.0.1", 8765)) == 0
    return True, "Panel port 8765 " + ("(JARVIS panel already running)" if busy else "(free)"), ""


CHECKS = [check_python, check_deps, check_chromium, check_claude, check_config, check_permissions, check_port]


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
