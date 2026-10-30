"""One-shot helper commands the panel may run (logins and probes). A fixed whitelist; no user-supplied arguments."""
from __future__ import annotations

import asyncio
import sys
import time

from ui.supervisor import REPO

PY = sys.executable
JOBS = {
    # The one button most people need: logs in to everything, then checks each connection works.
    "connect_all": {"label": "Connect everything", "cmd": [PY, "scripts/connect_all.py"], "exclusive": True, "timeout": 30 * 60},
    # "exclusive": uses a browser profile that JARVIS keeps open, so the panel pauses JARVIS while the job runs
    "login_brightspace": {"label": "Log in to POLITEMall", "cmd": [PY, "-m", "core.auth", "login", "brightspace", "--show"], "exclusive": True, "clears": "auth_prompted:brightspace"},
    "login_teams": {"label": "Log in to Teams", "cmd": [PY, "-m", "core.auth", "login", "teams", "--show"], "exclusive": True, "clears": "auth_prompted:teams"},
    "login_outlook": {"label": "Log in to Outlook email", "cmd": [PY, "-m", "core.auth", "login", "outlook", "--show"], "exclusive": True, "clears": "auth_prompted:outlook"},
    "login_google": {"label": "Connect Google Calendar", "cmd": [PY, "-m", "core.gcal", "login"], "clears": "gcal_warned"},
    "probe_brightspace": {"label": "Check POLITEMall connection", "cmd": [PY, "scripts/probe_brightspace.py", "--no-write"], "exclusive": True},
    "probe_teams": {"label": "Check Teams connection", "cmd": [PY, "scripts/probe_teams.py"], "exclusive": True},
    "probe_outlook": {"label": "Check Outlook email access", "cmd": [PY, "scripts/probe_outlook.py"], "exclusive": True},
}
MAX_OUTPUT = 20000
TIMEOUT = 15 * 60


class Jobs:
    def __init__(self, table: dict | None = None):
        self.table = table or JOBS
        self.current: dict | None = None
        self.on_result = None  # callable(setting_key_to_clear, ok): lets the panel clear "login needed" flags at once
        self._task: asyncio.Task | None = None

    def catalog(self) -> list[dict]:
        return [{"id": k, "label": v["label"]} for k, v in self.table.items()]

    def exclusive(self, job_id: str) -> bool:
        return bool(self.table.get(job_id, {}).get("exclusive"))

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict | None:
        return None if self.current is None else {k: v for k, v in self.current.items() if k != "proc"}

    async def start(self, job_id: str, on_finish=None, note: str = "") -> dict:
        if job_id not in self.table:
            return {"ok": False, "message": "Unknown task."}
        if self.running:
            return {"ok": False, "message": "Another task is still running."}
        spec = self.table[job_id]
        self.current = {"id": job_id, "label": spec["label"], "state": "running", "output": note, "started": time.time(),
                        "clears": spec.get("clears"), "timeout": spec.get("timeout")}
        self._task = asyncio.get_running_loop().create_task(self._run(spec["cmd"], on_finish))
        return {"ok": True, "message": f"{spec['label']} started."}

    async def cancel(self) -> None:
        if self.running and self.current and self.current.get("proc"):
            self.current["proc"].terminate()

    async def _run(self, cmd: list[str], on_finish=None) -> None:
        job = self.current
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(REPO), stdin=asyncio.subprocess.DEVNULL,
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            job["proc"] = proc
            deadline = time.monotonic() + (job.get("timeout") or TIMEOUT)
            while True:
                line = await asyncio.wait_for(proc.stdout.readline(), max(1, deadline - time.monotonic()))
                if not line:
                    break
                job["output"] = (job["output"] + line.decode(errors="replace"))[-MAX_OUTPUT:]
            await proc.wait()
            job["state"] = "done" if proc.returncode == 0 else "failed"
            if job["state"] == "done" and job.get("clears") and self.on_result:
                self.on_result(job["clears"], True)
        except asyncio.TimeoutError:
            job["state"] = "failed"
            job["output"] += "\nTimed out."
            proc.kill()
        except Exception as e:  # noqa: BLE001
            job["state"] = "failed"
            job["output"] += f"\n{type(e).__name__}: {e}"
        finally:
            job.pop("proc", None)
            if on_finish is not None:
                try:
                    msg = await on_finish()
                    job["output"] += f"\n{msg}" if msg else ""
                except Exception as e:  # noqa: BLE001
                    job["output"] += f"\nCould not restart JARVIS: {type(e).__name__}. Press Start."
            job["finished"] = time.time()
