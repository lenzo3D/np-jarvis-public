"""Everything that differs between macOS/Linux and Windows lives here, so the rest of the code has no
`if sys.platform` checks.

Every helper takes an optional `windows=` argument (and the OS calls it makes can be injected), so both
branches are covered by tests on any machine. The Windows branches have not been run on a real Windows PC
by the people who wrote them; see "Windows" in docs/DEVELOPER_NOTES.md.
"""
from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, Iterator

log = logging.getLogger(__name__)

CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)


def is_windows() -> bool:
    return sys.platform == "win32"


def _win(windows: bool | None) -> bool:
    return is_windows() if windows is None else windows


# ---------- text shown to the user ----------
def venv_python(windows: bool | None = None) -> str:
    """The venv Python as you would type it in a terminal opened in the JARVIS folder."""
    return r".\.venv\Scripts\python.exe" if _win(windows) else ".venv/bin/python"


def python_hint(args: str, windows: bool | None = None) -> str:
    """'Run: <venv python> <args>' command text, right for this OS."""
    return f"{venv_python(windows)} {args}"


def installer_hint(windows: bool | None = None) -> str:
    return r".\install.ps1" if _win(windows) else "./install.sh"


# ---------- files ----------
def restrict_to_user(path: Path, windows: bool | None = None, run: Callable = subprocess.run) -> None:
    """Make a secret file readable by this user only. POSIX: chmod 600. Windows: drop inherited permissions and
    grant only the current user (icacls). Best effort; never raises."""
    try:
        if not _win(windows):
            os.chmod(path, 0o700 if Path(path).is_dir() else 0o600)
            return
        sid = _current_sid(run)
        if not sid:
            log.warning("could not find your Windows account id, so %s keeps its inherited permissions", path)
            return
        grant = f"*{sid}:(OI)(CI)F" if Path(path).is_dir() else f"*{sid}:(R,W,D)"
        res = run(["icacls", str(path), "/inheritance:r", "/grant:r", grant], capture_output=True, timeout=10)
        if getattr(res, "returncode", 0) != 0:
            log.warning("could not restrict %s to your user (icacls exit %s)", path, res.returncode)
    except Exception:  # noqa: BLE001 - tightening permissions is a hardening step, not a requirement
        log.warning("could not restrict %s", path, exc_info=True)


def _current_sid(run: Callable = subprocess.run) -> str | None:
    """SID of the current Windows user, from `whoami /user` (robust against DOMAIN\\USER naming quirks)."""
    out = run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, encoding="utf-8",
              timeout=10)
    m = re.search(r'"(S-1-[0-9-]+)"', getattr(out, "stdout", "") or "")
    return m.group(1) if m else None


def sqlite_ro_uri(path: str | os.PathLike) -> str:
    """URI that opens a SQLite file read-only. A plain `file:C:\\...` string is not a valid URI on Windows."""
    return Path(path).absolute().as_uri() + "?mode=ro"


def rule_path(path: str | os.PathLike, windows: bool | None = None) -> str:
    """Path as it goes inside a Claude Code permission rule. An absolute path needs a leading "//": a single "/" means
    relative to the settings file, so such a rule silently never matches (allow rules do nothing, deny rules block
    nothing). Windows paths become //c/Users/..., the POSIX form Claude Code matches against."""
    if _win(windows):
        w = PureWindowsPath(path)
        if w.drive and w.root:
            return "//" + w.drive.rstrip(":").lower() + "/" + "/".join(w.parts[1:])
        return w.as_posix()
    posix = PurePosixPath(os.fspath(path)).as_posix()
    return "/" + posix if posix.startswith("/") and not posix.startswith("//") else posix


def secrets_mode_is_private(path: Path, windows: bool | None = None) -> bool:
    """POSIX: no group/other bits. Windows has no mode bits, so there is nothing to check (always True)."""
    if _win(windows):
        return True
    import stat

    return not (stat.S_IMODE(path.stat().st_mode) & 0o077)


# ---------- file lock ----------
def _lock_posix(fh) -> None:
    import fcntl

    fcntl.flock(fh, fcntl.LOCK_EX)


def _unlock_posix(fh) -> None:
    import fcntl

    fcntl.flock(fh, fcntl.LOCK_UN)


def _lock_windows(fh, msvcrt, sleep: Callable[[float], None] = time.sleep) -> None:
    """Blocking exclusive lock on byte 0. msvcrt.LK_LOCK gives up after ~10 s, so poll the non-blocking form."""
    fh.seek(0)
    while True:
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            sleep(0.2)


def _unlock_windows(fh, msvcrt) -> None:
    fh.seek(0)
    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)


@contextlib.contextmanager
def file_lock(path: Path, windows: bool | None = None, msvcrt=None) -> Iterator[None]:
    """Blocking exclusive cross-process lock held on `path` for the length of the with block."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        if _win(windows):
            if msvcrt is None:
                import msvcrt  # type: ignore[no-redef]
            _lock_windows(fh, msvcrt)
            try:
                yield
            finally:
                _unlock_windows(fh, msvcrt)
        else:
            _lock_posix(fh)
            try:
                yield
            finally:
                _unlock_posix(fh)


# ---------- processes ----------
def new_group_kwargs(windows: bool | None = None) -> dict:
    """Popen/create_subprocess_exec kwargs that put the child in its own process group (so we can stop its tree)."""
    if _win(windows):
        return {"creationflags": CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def child_env(windows: bool | None = None) -> dict | None:
    """Environment for a Python child. On Windows force UTF-8 (the default is cp1252, which breaks on emoji).
    None means 'inherit', which is what macOS and Linux have always done."""
    if not _win(windows):
        return None
    return {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def pid_alive(pid: int, windows: bool | None = None) -> bool:
    if not _win(windows):
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    # os.kill(pid, 0) on Windows would send Ctrl+C, so ask the OS about the process instead.
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def terminate_tree(pid: int, force: bool = False, windows: bool | None = None, run: Callable = subprocess.run) -> bool:
    """Stop a process and everything it started. `force=False` asks politely, `force=True` kills.
    Returns True if the request was delivered (always True on POSIX); on Windows a polite request to a process
    without a window fails, and the caller should then skip its grace period and force."""
    if not _win(windows):
        try:
            os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            pass
        return True
    cmd = ["taskkill", "/T", "/PID", str(pid)] + (["/F"] if force else [])
    try:
        return run(cmd, capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def kill_process_tree(proc, windows: bool | None = None, run: Callable = subprocess.run) -> None:
    """Stop a child we did not put in its own group (login jobs, which start a browser). POSIX: terminate."""
    if _win(windows):
        terminate_tree(proc.pid, force=True, windows=True, run=run)
    else:
        proc.terminate()


def install_stop_signals(loop, callback: Callable[[], None], windows: bool | None = None, signal_module=signal) -> None:
    """Call `callback` on Ctrl+C / terminate. asyncio's add_signal_handler does not exist on Windows."""
    names = ["SIGINT", "SIGTERM"] + (["SIGBREAK"] if _win(windows) else [])
    sigs = [getattr(signal_module, n) for n in names if hasattr(signal_module, n)]
    try:
        for sig in sigs:
            loop.add_signal_handler(sig, callback)
    except NotImplementedError:
        for sig in sigs:
            signal_module.signal(sig, lambda *_: loop.call_soon_threadsafe(callback))


def restart_process(argv: list[str], windows: bool | None = None, execv=os.execv, popen=subprocess.Popen,
                    exit_fn: Callable[[int], None] = sys.exit) -> None:
    """Replace this process with a fresh one running `argv`. Windows has no real exec: if a wrapper loop
    (scripts/run_panel.ps1, which sets JARVIS_RESTART_BY_PARENT) will start the next one, just exit; otherwise start
    it ourselves and exit."""
    if _win(windows):
        if os.environ.get("JARVIS_RESTART_BY_PARENT") != "1":
            popen(argv)
        exit_fn(0)
    else:
        execv(argv[0], argv)


# ---------- keep the computer awake while JARVIS runs ----------
def set_windows_awake(on: bool, kernel32=None) -> bool:
    """Ask Windows not to sleep (SetThreadExecutionState). It applies to the calling thread, so call it from the
    panel's main thread. Returns False if it could not."""
    try:
        if kernel32 is None:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        flags = 0x80000000 | (0x00000001 if on else 0)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        return bool(kernel32.SetThreadExecutionState(flags))
    except Exception:  # noqa: BLE001 - staying awake is a nicety
        log.debug("could not change the sleep setting", exc_info=True)
        return False


# ---------- Claude Code launcher ----------
class LauncherError(RuntimeError):
    pass


_SHIM_TARGET = re.compile(r'%~?dp0%?\\([^"%\r\n]+)', re.IGNORECASE)


def _unwrap_cmd_shim(shim: Path, which: Callable) -> list[str]:
    """npm installs `claude.cmd`, a batch shim around the real program. Starting a .cmd means cmd.exe re-parses
    the arguments, and our arguments contain text from the school (& | ^ %), which would run as commands. So find
    the real program behind the shim and start that directly."""
    try:
        text = shim.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    # The shim's first line of logic is `IF EXIST "%dp0%\node.exe" ( SET "_prog=%dp0%\node.exe"`; that node.exe is the
    # interpreter, never the program. Only a target named after the "%_prog%" token (a script or claude.exe) counts.
    after_prog = text.split('"%_prog%"', 1)[1] if '"%_prog%"' in text else text
    for rel in _SHIM_TARGET.findall(after_prog):
        target = shim.parent / rel.replace("\\", os.sep)
        if not target.exists() or target.name.lower() == "node.exe":
            continue
        if target.suffix.lower() == ".exe":
            return [str(target)]
        if target.suffix.lower() in (".js", ".mjs", ".cjs"):
            local = shim.parent / "node.exe"
            node = str(local) if local.exists() else which("node")
            if node:
                return [node, str(target)]
    raise LauncherError(
        f"Found {shim.name} but not the program behind it. Install Claude Code with its Windows installer "
        "(it provides claude.exe), or set CLAUDE_BIN to the full path of claude.exe.")


def claude_command(run_as: str | None = None, windows: bool | None = None, which: Callable = shutil.which,
                   environ: dict | None = None) -> list[str]:
    """Command prefix that starts Claude Code. Raises LauncherError if it cannot be started safely."""
    environ = os.environ if environ is None else environ
    windows = _win(windows)
    configured = environ.get("CLAUDE_BIN")
    if run_as:
        if windows:
            raise LauncherError("CLAUDE_RUN_AS runs Claude as another user with sudo, which Windows does not have. "
                                "Remove CLAUDE_RUN_AS from secrets.env.")
        return ["sudo", "-n", "-H", "-u", run_as, configured or which("claude") or "claude"]
    if not windows:
        return [configured or which("claude") or "claude"]
    exe = configured or which("claude.exe") or which("claude") or "claude"
    if Path(exe).suffix.lower() in (".cmd", ".bat"):
        return _unwrap_cmd_shim(Path(exe), which)
    return [exe]


def api_transport(configured: str | None = None, windows: bool | None = None) -> str:
    """The transport in use: the OPS_API_TRANSPORT setting if given, else the platform default."""
    return configured or default_api_transport(windows)


def check_api_transport(configured: str | None, windows: bool | None = None) -> None:
    """Raise ValueError for a setting that cannot work (config load calls this)."""
    if configured is None:
        return
    if configured not in ("tcp", "unix"):
        raise ValueError(f"OPS_API_TRANSPORT must be tcp or unix, not {configured!r}")
    if configured == "unix" and _win(windows):
        raise ValueError("OPS_API_TRANSPORT=unix is not possible on Windows (no Unix sockets); use tcp or leave it unset")


def default_api_transport(windows: bool | None = None) -> str:
    """How the local Ops API is served: a Unix socket where we can (it is what the server's user isolation relies
    on), localhost TCP with a bearer token on Windows."""
    return "tcp" if _win(windows) else "unix"
