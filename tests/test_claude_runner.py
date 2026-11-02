import sys
from pathlib import Path

import pytest

from ops.claude_runner import _base_cmd


def test_base_cmd_dev_uses_claude_on_path(monkeypatch):
    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    assert Path(_base_cmd(None)[-1]).stem == "claude"


@pytest.mark.skipif(sys.platform == "win32", reason="CLAUDE_RUN_AS (sudo) is POSIX only; see test_windows_support")
def test_base_cmd_run_as_uses_claude_bin(monkeypatch):
    monkeypatch.setenv("CLAUDE_BIN", "/home/jarvis/.local/bin/claude")
    assert _base_cmd("jarvis") == ["sudo", "-n", "-H", "-u", "jarvis", "/home/jarvis/.local/bin/claude"]
