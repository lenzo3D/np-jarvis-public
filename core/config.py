"""Settings loaded from secrets.env plus environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from core import hostos

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    state_dir: Path
    workspace_dir: Path
    db_path: Path
    ops_socket: Path
    allowed_telegram_id: int
    ops_bot_token: str
    politemall_base: str
    politemall_user: str
    politemall_pass: str
    telethon_api_id: int | None = None
    telethon_api_hash: str | None = None
    group_chat_ids: list[int] = field(default_factory=list)
    group_labels: dict[int, str] = field(default_factory=dict)
    actions_enabled: bool = False
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo("Asia/Singapore"))
    claude_run_as: str | None = None
    course_prefix: str | None = None
    semester_start: str | None = None
    recess_week: int | None = None  # the teaching week after which the one-week recess falls; None = no break
    school_mail_domains: tuple[str, ...] = ()
    user_name: str = ""
    address_as: str = "Sir"
    ops_endpoint: Path | None = None  # where Ops writes how to reach its API (default state/ops.api.json)
    ops_api_transport: str | None = None  # "unix" or "tcp"; None = unix on macOS/Linux, tcp on Windows
    ops_port: int = 0  # tcp only; 0 = let the OS pick a free port


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _parse_groups(raw: str) -> tuple[list[int], dict[int, str]]:
    """GROUP_CHAT_IDS="-100123,-100456:Project team": ids, with an optional label after a colon."""
    ids, labels = [], {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        ident, _, label = part.partition(":")
        ids.append(int(ident))
        if label.strip():
            labels[int(ident)] = label.strip()
    return ids, labels


def env_path() -> Path:
    return Path(os.environ.get("SECRETS_FILE", REPO_ROOT / "secrets.env"))


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """Set keys in a secrets.env, keeping comments and other lines. Creates the file (mode 600) if needed."""
    for k, v in updates.items():
        if "\n" in v or "\r" in v or "=" in k or not k.isidentifier():
            raise ValueError(f"bad value for {k}")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    done: set[str] = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key in updates and key not in done:
            lines[i] = f"{key}={updates[key]}"
            done.add(key)
    for k, v in updates.items():
        if k not in done:
            lines.append(f"{k}={v}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    hostos.restrict_to_user(tmp)
    os.replace(tmp, path)


def load_settings(env_file: Path | None = None) -> Settings:
    env_file = env_file or env_path()
    values = _parse_env_file(env_file) if env_file.exists() else {}
    values.update({k: v for k, v in os.environ.items() if k in _KEYS})

    def get(k: str, default: str | None = None) -> str | None:
        v = values.get(k)
        return v if v not in (None, "") else default

    state_dir = Path(get("STATE_DIR", str(REPO_ROOT / "state")))
    api_id = get("TELETHON_API_ID")
    groups, labels = _parse_groups(get("GROUP_CHAT_IDS", "") or "")
    transport = (get("OPS_API_TRANSPORT") or "").lower() or None
    hostos.check_api_transport(transport)
    return Settings(
        state_dir=state_dir,
        workspace_dir=Path(get("WORKSPACE_DIR", str(REPO_ROOT / "workspace"))),
        db_path=Path(get("DB_PATH", str(state_dir / "jarvis.db"))),
        ops_socket=Path(get("OPS_SOCKET", str(state_dir / "ops.sock"))),
        ops_endpoint=Path(get("OPS_ENDPOINT", str(state_dir / "ops.api.json"))),
        ops_api_transport=transport,
        ops_port=int(get("OPS_PORT", "0")),
        allowed_telegram_id=int(get("ALLOWED_TELEGRAM_ID", "0")),
        ops_bot_token=get("OPS_BOT_TOKEN", "") or "",
        politemall_base=(get("POLITEMALL_BASE", "") or "").rstrip("/"),
        politemall_user=get("POLITEMALL_USER", "") or "",
        politemall_pass=get("POLITEMALL_PASS", "") or "",
        telethon_api_id=int(api_id) if api_id else None,
        telethon_api_hash=get("TELETHON_API_HASH"),
        group_chat_ids=groups,
        group_labels=labels,
        actions_enabled=(get("ACTIONS_ENABLED", "false") or "").lower() == "true",
        tz=ZoneInfo(get("TZ_NAME", "Asia/Singapore")),
        claude_run_as=get("CLAUDE_RUN_AS"),
        course_prefix=get("COURSE_PREFIX"),
        semester_start=get("SEMESTER_START"),
        recess_week=_recess_week(get("RECESS_WEEK")),
        user_name=(get("USER_NAME", "") or "").strip()[:40],
        address_as=((get("ADDRESS_AS", "Sir") or "Sir").strip()[:20] or "Sir"),
        school_mail_domains=tuple(x.strip().lower() for x in (get("SCHOOL_MAIL_DOMAINS", "") or "").split(",") if x.strip()),
    )


def _recess_week(raw) -> int | None:
    """RECESS_WEEK=N: a one-week break falls after teaching week N. Blank, non-numeric or out of range = no break."""
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 30 else None


_KEYS = {
    "STATE_DIR", "WORKSPACE_DIR", "DB_PATH", "OPS_SOCKET", "OPS_ENDPOINT", "OPS_API_TRANSPORT", "OPS_PORT", "ALLOWED_TELEGRAM_ID", "OPS_BOT_TOKEN",
    "POLITEMALL_BASE", "POLITEMALL_USER", "POLITEMALL_PASS", "TELETHON_API_ID", "TELETHON_API_HASH",
    "GROUP_CHAT_IDS", "ACTIONS_ENABLED", "TZ_NAME", "CLAUDE_RUN_AS", "COURSE_PREFIX", "SEMESTER_START", "RECESS_WEEK", "SCHOOL_MAIL_DOMAINS", "USER_NAME", "ADDRESS_AS",
}
