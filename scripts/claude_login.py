"""Sign Claude Code in again (the panel's "Sign in to Claude" button). Opens the browser; you do the sign-in yourself.

Runs `claude auth login`, then `claude auth status` to confirm. stdin is a pipe that stays open, because the login
command may wait for a pasted code and would give up at once on an empty stdin.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import hostos  # noqa: E402


def main() -> int:
    try:
        claude = hostos.claude_command(None)
    except hostos.LauncherError as e:
        print(f"Problem: {e}")
        return 1
    print("Opening your browser to sign in to Claude. Approve it there; this window updates when it is done.", flush=True)
    with subprocess.Popen(claude + ["auth", "login"], stdin=subprocess.PIPE) as proc:
        try:
            proc.wait(timeout=10 * 60)
        except subprocess.TimeoutExpired:
            proc.kill()
            print("Gave up after 10 minutes. Press Sign in again to retry.")
            return 1
    out = subprocess.run(claude + ["auth", "status"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60).stdout
    try:
        signed_in = bool(json.loads(out).get("loggedIn"))
    except (ValueError, AttributeError):
        signed_in = False
    if not signed_in:
        print("Claude is still signed out. Press Sign in again, or run in a terminal: claude auth login")
        return 1
    print("Signed in to Claude. The Claude light turns green within 10 minutes (or after the next chat).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
