import sys
from pathlib import Path

import pytest

from core.db import DB

REQUIRED_ENV = """ALLOWED_TELEGRAM_ID=42
OPS_BOT_TOKEN=tok
POLITEMALL_BASE=https://politemall.example
POLITEMALL_USER=u
POLITEMALL_PASS=p
"""


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    p = tmp_path / "secrets.env"
    p.write_text(REQUIRED_ENV + f"STATE_DIR={tmp_path/'state'}\nWORKSPACE_DIR={tmp_path/'ws'}\n", encoding="utf-8")
    return p


@pytest.fixture
def db(tmp_path: Path) -> DB:
    d = DB(tmp_path / "t.db")
    d.init()
    return d


@pytest.fixture(autouse=True)
def _skip_when_symlinks_are_not_allowed(monkeypatch):
    """Creating symlinks on Windows needs a privilege most accounts lack; skip those tests instead of failing."""
    if sys.platform != "win32":
        return
    real = Path.symlink_to

    def guarded(self, *a, **k):
        try:
            return real(self, *a, **k)
        except OSError as e:
            pytest.skip(f"cannot create symlinks here: {e}")

    monkeypatch.setattr(Path, "symlink_to", guarded)
