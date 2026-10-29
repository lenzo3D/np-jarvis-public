"""Update JARVIS from GitHub without a terminal: fast-forward pull, reinstall if dependencies changed."""
from __future__ import annotations

import asyncio
import re
import sys
import time
from pathlib import Path

from ui.supervisor import REPO

GIT_TIMEOUT = 60
PIP_TIMEOUT = 600
BRANCH = "main"
IGNORABLE = {".DS_Store"}


async def _run(*cmd: str, cwd: Path, timeout: float) -> tuple[int, str]:
    """Returns (exit code, text). On success the text is stdout only, so warnings on stderr (which git and pip
    print freely) can never corrupt what we parse; on failure it is the error text."""
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(cwd), stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timed out"
    except OSError as e:
        return 127, str(e)
    out_s, err_s = out.decode(errors="replace").strip(), err.decode(errors="replace").strip()
    return proc.returncode, out_s if proc.returncode == 0 else (err_s or out_s)


class Updater:
    def __init__(self, repo: Path = REPO, remote: str = "origin", python: str = sys.executable):
        self.repo, self.remote, self.python = repo, remote, python
        self._lock = asyncio.Lock()

    async def _git(self, *args: str, timeout: float = GIT_TIMEOUT) -> tuple[int, str]:
        return await _run("git", *args, cwd=self.repo, timeout=timeout)

    async def check(self) -> dict:
        """Fetch and compare with the remote branch. Never changes any file."""
        info = {"ok": False, "branch": None, "behind": 0, "ahead": 0, "dirty": False, "dirty_files": [], "changes": [],
                "notes": [], "message": ""}
        if not (self.repo / ".git").exists():
            info["message"] = "This folder is not a git checkout, so it cannot update itself."
            return info
        code, out = await self._git("rev-parse", "--abbrev-ref", "HEAD")
        info["branch"] = out if code == 0 else None
        code, out = await self._git("fetch", "--quiet", self.remote, BRANCH)
        if code != 0:
            info["message"] = f"Could not reach GitHub: {out[-200:]}"
            return info
        remote = f"{self.remote}/{BRANCH}"
        code, out = await self._git("rev-list", "--left-right", "--count", f"HEAD...{remote}")
        counts = re.search(r"(\d+)\s+(\d+)\s*$", out) if code == 0 else None
        if counts is None:
            info["message"] = f"Could not compare with GitHub: {out[-200:] or 'no answer from git'}"
            return info
        info["ahead"], info["behind"] = int(counts.group(1)), int(counts.group(2))
        info["dirty_files"] = await self._dirty_files()
        info["dirty"] = bool(info["dirty_files"])
        if info["behind"]:
            info["changes"] = (await self._git("log", "--format=%s", f"HEAD..{remote}", "-n", "15"))[1].splitlines()
        info["ok"] = True
        info["message"] = (f"{info['behind']} update(s) available." if info["behind"] else "JARVIS is up to date.")
        if info["behind"]:
            if info["branch"] != BRANCH:
                info["notes"].append(f"This copy is on branch '{info['branch']}'. It will switch to '{BRANCH}'.")
            if info["dirty"]:
                info["notes"].append(f"{len(info['dirty_files'])} locally changed file(s) will be saved aside first "
                                     "(git stash), not deleted.")
            if info["ahead"]:
                info["notes"].append(f"{info['ahead']} local commit(s) will be kept on a backup branch, not deleted.")
        return info

    async def _tracked_changes(self) -> list[str]:
        out = (await self._git("status", "--porcelain", "--untracked-files=no"))[1]
        # _run() strips the output, which can eat the leading space of the first status line, so split on whitespace
        return [parts[1].strip('"') for parts in (line.split(None, 1) for line in out.splitlines()) if len(parts) == 2]

    async def _dirty_files(self) -> list[str]:
        """Changed tracked files that matter. Finder's .DS_Store is rewritten constantly and is never real work."""
        return [f for f in await self._tracked_changes() if Path(f).name not in IGNORABLE]

    async def apply(self) -> dict:
        """Bring this copy to the remote branch without ever destroying local work: changed files are stashed and
        local commits stay on a backup branch."""
        async with self._lock:
            st = await self.check()
            if not st["ok"]:
                return {"ok": False, "message": st["message"], "restart": False}
            if not st["behind"]:
                return {"ok": True, "restart": False, "message": "Already up to date."}
            fail = lambda msg: {"ok": False, "restart": False, "message": msg}  # noqa: E731
            stamp, kept = time.strftime("%Y%m%d-%H%M%S"), []
            code, before = await self._git("rev-parse", "HEAD")
            changed = await self._tracked_changes()
            if st["dirty"]:
                code, out = await self._git("stash", "push", "-q", "-m", f"JARVIS auto-update {stamp}")
                if code != 0:
                    return fail(f"Could not save your local changes aside: {out[-200:]}")
                kept.append(f"{len(st['dirty_files'])} changed file(s) saved with 'git stash' (JARVIS auto-update {stamp})")
            elif changed:  # only Finder junk: drop it so it cannot block the update
                for f in changed:
                    await self._git("checkout", "--", f)
            remote = f"{self.remote}/{BRANCH}"
            if st["branch"] != BRANCH or st["ahead"]:
                backup = f"backup/{(st['branch'] or 'detached').replace('/', '-')}-{stamp}"
                code, out = await self._git("branch", backup, "HEAD")
                if code != 0:
                    return fail(f"Could not create a backup branch: {out[-200:]}")
                kept.append(f"previous commits kept on branch '{backup}'")
                code, out = await self._git("checkout", "-q", "-B", BRANCH, remote)
            else:
                code, out = await self._git("pull", "--ff-only", "--quiet", self.remote, BRANCH)
            if code != 0:
                return fail(f"Update failed: {out[-300:]}")
            _, names = await self._git("diff", "--name-only", before, "HEAD")
            if "pyproject.toml" in names.splitlines():
                code, out = await _run(self.python, "-m", "pip", "install", "-q", "-e", str(self.repo),
                                       cwd=self.repo, timeout=PIP_TIMEOUT)
                if code != 0:
                    return fail(f"Code updated, but installing dependencies failed: {out[-300:]}")
            msg = f"Updated ({st['behind']} change(s)). Restarting."
            if kept:
                msg += " Kept aside: " + "; ".join(kept) + "."
            return {"ok": True, "restart": True, "message": msg}
