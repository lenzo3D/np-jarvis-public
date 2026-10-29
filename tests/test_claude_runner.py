from ops.claude_runner import _base_cmd


def test_base_cmd_dev_uses_claude_on_path(monkeypatch):
    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    assert _base_cmd(None)[-1].endswith("claude")


def test_base_cmd_run_as_uses_claude_bin(monkeypatch):
    monkeypatch.setenv("CLAUDE_BIN", "/home/jarvis/.local/bin/claude")
    assert _base_cmd("jarvis") == ["sudo", "-n", "-H", "-u", "jarvis", "/home/jarvis/.local/bin/claude"]
