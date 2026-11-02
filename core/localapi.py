"""How the Ops local API is reached: the "endpoint file" Ops writes when it starts (state/ops.api.json).

Two transports:
- unix: a Unix socket (macOS/Linux; the server's user isolation relies on file permissions of the socket).
- tcp:  127.0.0.1 with an OS-assigned (or configured) port and a random bearer token, regenerated on every start
        (Windows has no usable Unix sockets in Python). The file is restricted to the current user.

Clients read the file on every request, so a restarted Ops (new port, new token) is picked up. school_mcp has its
own small reader in school_mcp/client.py because it must not import from core/.
"""
from __future__ import annotations

import http.client
import json
import os
import secrets
import socket
import time
from pathlib import Path

from core import hostos

ENDPOINT_NAME = "ops.api.json"


def endpoint_path(settings) -> Path:
    return settings.ops_endpoint or settings.state_dir / ENDPOINT_NAME


def new_token() -> str:
    return secrets.token_urlsafe(32)


def write_endpoint(path: Path, data: dict) -> None:
    """Atomic write; the file holds a secret (tcp token), so it is created private from the start."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data))
    hostos.restrict_to_user(tmp)
    _replace_with_retry(tmp, path)


def _replace_with_retry(src: Path, dst: Path, seconds: float = 2.0, sleep=time.sleep) -> None:
    """On Windows os.replace fails with PermissionError while a client has the target open; try again briefly."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            sleep(0.05)


def read_endpoint(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("transport") in ("unix", "tcp") else None


def clear_endpoint(path: Path) -> None:
    """Best effort (a client may hold the file open on Windows); a leftover file is harmless, see endpoint_alive."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def endpoint_alive(ep: dict, timeout: float = 0.5) -> bool:
    """Is something answering at this endpoint? tcp: an authenticated GET /health (so a stale file whose port was
    reused by another program does not count)."""
    try:
        if ep["transport"] == "unix":
            if not hasattr(socket, "AF_UNIX"):
                return False
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.settimeout(timeout)
                s.connect(str(ep["path"]))
                return True
            finally:
                s.close()
        conn = http.client.HTTPConnection(ep.get("host", "127.0.0.1"), int(ep["port"]), timeout=timeout)
        try:
            conn.request("GET", "/health", headers={"Authorization": f"Bearer {ep.get('token', '')}"})
            return conn.getresponse().status == 200
        finally:
            conn.close()
    except (OSError, KeyError, ValueError, http.client.HTTPException):
        return False
