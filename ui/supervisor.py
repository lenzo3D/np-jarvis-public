"""Start, stop and watch the Ops process (python -m ops.main) so nobody has to use a terminal."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import socket
import sys
import time
from collections import deque
from pathlib import Path

log = logging.getLogger(__name__)
REPO = Path(__file__).resolve().parent.parent
LOG_MAX_BYTES = 5 * 1024 * 1024
STOP_GRACE = 20.0
STARTING_WINDOW = 90.0


def socket_alive(path: Path) -> bool:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        s.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        s.close()


class Prefs:
    """Tiny JSON file for UI preferences (state/ui.json)."""

    def __init__(self, path: Path):
        self.path = path

    def get(self, key: str, default=None):
        try:
            return json.loads(self.path.read_text()).get(key, default)
        except (OSError, ValueError):
            return default

    def set(self, key: str, value) -> None:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        data[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data))


class OpsSupervisor:
    def __init__(self, settings, prefs: Prefs, command: list[str] | None = None, cwd: Path = REPO,
                 log_path: Path | None = None):
        self.settings = settings
        self.prefs = prefs
        self.command = command or [sys.executable, "-m", "ops.main"]
        self.cwd = cwd
        self.log_path = log_path or settings.state_dir / "logs" / "ops.log"
        self.proc: asyncio.subprocess.Process | None = None
        self.pid: int | None = None
        self.pid_path = self.log_path.parent / "ops.pid"
        self.started_at: float | None = None
        self.restarts = 0
        self.desired = False
        self._watch_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # ---------- status ----------
    @property
    def managed_running(self) -> bool:
        if self.proc is not None:
            return self.proc.returncode is None
        if self.pid is None:
            return False
        try:
            os.kill(self.pid, 0)
            return True
        except OSError:
            return False

    def adopt(self) -> None:
        """After a panel restart, take back an Ops this panel started earlier (found via its pid file)."""
        try:
            pid = int(self.pid_path.read_text().strip())
        except (OSError, ValueError):
            return
        self.pid = pid
        if self.managed_running and socket_alive(self.settings.ops_socket):
            self.started_at = time.monotonic()
            self.desired = True
            self._ensure_watch()
        else:
            self.pid = None

    def mode(self) -> str:
        if self.managed_running:
            if socket_alive(self.settings.ops_socket):
                return "running"
            return "starting" if time.monotonic() - (self.started_at or 0) < STARTING_WINDOW else "unresponsive"
        if socket_alive(self.settings.ops_socket):
            return "external"
        return "restarting" if self.desired else "stopped"

    def info(self) -> dict:
        mode = self.mode()
        return {"mode": mode, "pid": self.pid if self.managed_running else None,
                "uptime_s": int(time.monotonic() - self.started_at) if self.managed_running and self.started_at else None,
                "restarts": self.restarts, "autostart": bool(self.prefs.get("autostart", True))}

    # ---------- control ----------
    def _rotate_log(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if self.log_path.exists() and self.log_path.stat().st_size > LOG_MAX_BYTES:
            self.log_path.replace(self.log_path.with_suffix(".log.1"))

    async def start(self) -> dict:
        async with self._lock:
            if self.managed_running:
                return {"ok": True, "message": "JARVIS is already running."}
            if socket_alive(self.settings.ops_socket):
                return {"ok": False, "message": "JARVIS is already running outside this panel (for example in a "
                                                "terminal). Stop that one first, or two copies would fight over Telegram."}
            self._rotate_log()
            logfile = open(self.log_path, "ab")
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    *self.command, cwd=str(self.cwd), stdout=logfile, stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True)
            finally:
                logfile.close()
            self.pid = self.proc.pid
            self.pid_path.write_text(str(self.pid))
            self.started_at = time.monotonic()
            self.desired = True
            self._keep_awake(self.pid)
            self._ensure_watch()
            return {"ok": True, "message": "Starting JARVIS."}

    def _keep_awake(self, pid: int) -> None:
        exe = shutil.which("caffeinate")
        if exe and sys.platform == "darwin":
            try:
                asyncio.get_running_loop().create_task(
                    asyncio.create_subprocess_exec(exe, "-i", "-w", str(pid), stdout=asyncio.subprocess.DEVNULL,
                                                   stderr=asyncio.subprocess.DEVNULL, start_new_session=True))
            except Exception:  # noqa: BLE001 - staying awake is a nicety
                log.debug("caffeinate failed", exc_info=True)

    async def stop(self) -> dict:
        async with self._lock:
            self.desired = False
            if not self.managed_running:
                if socket_alive(self.settings.ops_socket):
                    return {"ok": False, "message": "JARVIS was started outside this panel, so it cannot be stopped here."}
                return {"ok": True, "message": "JARVIS is not running."}
            pid = self.pid
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + STOP_GRACE
            while self.managed_running and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            if self.managed_running:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                for _ in range(50):
                    if not self.managed_running:
                        break
                    await asyncio.sleep(0.1)
            self.pid_path.unlink(missing_ok=True)
            self.proc = self.pid = None
            return {"ok": True, "message": "JARVIS stopped."}

    async def restart(self) -> dict:
        out = await self.stop()
        if not out["ok"]:
            return out
        return await self.start()

    async def autostart_if_wanted(self) -> None:
        if self.prefs.get("autostart", True):
            res = await self.start()
            log.info("autostart: %s", res["message"])

    # ---------- crash recovery ----------
    def _ensure_watch(self) -> None:
        if self._watch_task is None or self._watch_task.done():
            self._watch_task = asyncio.get_running_loop().create_task(self._watch())

    async def _watch(self) -> None:
        backoff = 5.0
        while True:
            await asyncio.sleep(2)
            if not self.desired:
                return
            if self.managed_running:
                if self.started_at and time.monotonic() - self.started_at > 120:
                    backoff = 5.0
                continue
            log.warning("ops exited; restarting in %.0fs", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
            if not self.desired:
                return
            self.restarts += 1
            self.proc = self.pid = None
            await self.start()

    async def shutdown(self) -> None:
        """Called when the panel itself exits: take the managed Ops down with it."""
        if self._watch_task:
            self._watch_task.cancel()
        await self.stop()

    # ---------- logs ----------
    def tail(self, n: int = 200) -> list[str]:
        if not self.log_path.exists():
            return []
        with open(self.log_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 256 * 1024))
            data = fh.read()
        return list(deque(data.decode(errors="replace").splitlines(), maxlen=n))
