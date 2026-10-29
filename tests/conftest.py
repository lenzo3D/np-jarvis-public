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
    p.write_text(REQUIRED_ENV + f"STATE_DIR={tmp_path/'state'}\nWORKSPACE_DIR={tmp_path/'ws'}\n")
    return p


@pytest.fixture
def db(tmp_path: Path) -> DB:
    d = DB(tmp_path / "t.db")
    d.init()
    return d
