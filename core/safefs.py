"""Symlink-proof file access inside the Claude-writable workspace.

The workspace is writable by the `jarvis` user (Claude). The ops service runs as a different user with
access to secrets, so it must never follow a link Claude planted: every path component is opened with
O_NOFOLLOW relative to its parent directory fd, and files are written by atomic replace (rename never
follows a symlink at the destination).

Windows has no O_NOFOLLOW, O_DIRECTORY or dir_fd, so there the same API is implemented the portable way: every
path component is checked with lstat (symlink, junction or other reparse point = refused), the final path must
resolve inside the workspace, and writes go to a temp file in the same directory followed by os.replace.
That is weaker: between the check and the use another process could swap a directory for a link (a race the
fd-relative POSIX version cannot lose). It is acceptable because a Windows install is a single-user desktop where
Claude runs as the same user as JARVIS anyway, so there is no privilege boundary for the link to cross; the
checks still stop the ordinary mistakes (a link planted in the workspace, a ../ path). Server deployments are
Linux and keep the strong version. `USE_PORTABLE` selects the portable version (tests set it to cover it on any OS).
"""
from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path

from core import hostos

USE_PORTABLE = hostos.is_windows()
DIR_FLAGS = getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | os.O_RDONLY

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
# Reparse tags that redirect somewhere else. Others (OneDrive/cloud placeholders, dedup) are plain files to us.
REDIRECTING_TAGS = {0xA000000C, 0xA0000003}  # IO_REPARSE_TAG_SYMLINK, IO_REPARSE_TAG_MOUNT_POINT (junction)
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


class UnsafePath(Exception):
    pass


def _check_part(part: str) -> None:
    if not part or part in (".", "..") or "/" in part or "\x00" in part:
        raise UnsafePath(f"bad path component: {part!r}")
    if USE_PORTABLE and (any(c in part for c in '\\:*?"<>|') or part != part.rstrip(" .")
                         or part.split(".")[0].upper() in _RESERVED):
        raise UnsafePath(f"bad path component: {part!r}")  # separators, streams (a:b), device names on Windows


def _rel_parts(workspace: Path, path: str | os.PathLike) -> list[str]:
    p = Path(path)
    if p.is_absolute():
        root = os.path.normcase(os.path.normpath(str(Path(workspace).absolute())))
        norm = os.path.normcase(os.path.normpath(str(p)))
        if ".." in p.parts or not (norm + os.sep).startswith(root + os.sep) or norm == root:
            raise UnsafePath("path must be inside the workspace")
        parts = Path(norm[len(root) + 1:]).parts
    else:
        parts = p.parts
    for part in parts:
        _check_part(part)
    if not parts:
        raise UnsafePath("empty path")
    return list(parts)


def _open_dirs(workspace: Path, parts: list[str], create: bool) -> int:
    fd = os.open(str(workspace), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            _check_part(part)
            if create:
                try:
                    os.mkdir(part, 0o2775, dir_fd=fd)
                except FileExistsError:
                    pass
            try:
                nfd = os.open(part, DIR_FLAGS, dir_fd=fd)
            except OSError as e:
                raise UnsafePath(f"{part!r} is not a plain directory") from e
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def is_link_like(path: str | os.PathLike, lstat=os.lstat) -> bool:
    """Symlink, junction or other redirecting reparse point. A missing path is not a link."""
    try:
        st = lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    if getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
        tag = getattr(st, "st_reparse_tag", None)
        return tag is None or tag in REDIRECTING_TAGS
    return False


def _walk_plain_dirs(workspace: Path, parts: list[str], create: bool, lstat=os.lstat) -> Path:
    """Portable counterpart of _open_dirs: every component must be a plain directory (not a link of any kind)."""
    cur = Path(workspace)
    if not cur.is_dir():
        raise UnsafePath("workspace is missing")
    for part in parts:
        _check_part(part)
        cur = cur / part
        if create and not os.path.lexists(cur):
            try:
                os.mkdir(cur)
            except FileExistsError:
                pass
        if is_link_like(cur, lstat) or not cur.is_dir():
            raise UnsafePath(f"{part!r} is not a plain directory")
    return cur


def _inside(workspace: Path, path: Path, allow_root: bool = False) -> None:
    root = os.path.normcase(os.path.realpath(workspace))
    real = os.path.normcase(os.path.realpath(path))
    if os.path.commonpath([root, real]) != root or (real == root and not allow_root):
        raise UnsafePath("path must be inside the workspace")


def _portable_write(workspace: Path, dir_parts: list[str], name: str, data: bytes, lstat=os.lstat) -> Path:
    d = _walk_plain_dirs(workspace, dir_parts, create=True, lstat=lstat)
    target = d / name
    _inside(workspace, d, allow_root=True)
    tmp = d / f".tmp-{uuid.uuid4().hex}"
    try:
        f = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o664)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(f, view):]
        finally:
            os.close(f)
        os.replace(tmp, target)  # replaces a link standing at `target`; it never writes through it
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return Path(workspace).joinpath(*dir_parts, name)


def _portable_read(workspace: Path, parts: list[str], lstat=os.lstat) -> bytes:
    d = _walk_plain_dirs(workspace, parts[:-1], create=False, lstat=lstat)
    target = d / parts[-1]
    if is_link_like(target, lstat):
        raise UnsafePath(f"cannot open {parts[-1]!r} (missing or a link)")
    try:
        if not stat.S_ISREG(os.stat(target).st_mode):
            raise UnsafePath("not a regular file")
    except OSError as e:
        raise UnsafePath(f"cannot open {parts[-1]!r} (missing or a link)") from e
    _inside(workspace, target)
    return target.read_bytes()


def write_file(workspace: Path, dir_parts: list[str], name: str, data: bytes) -> Path:
    _check_part(name)
    if USE_PORTABLE:
        return _portable_write(workspace, dir_parts, name, data)
    fd = _open_dirs(Path(workspace), dir_parts, create=True)
    tmp = f".tmp-{uuid.uuid4().hex}"
    try:
        f = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o664, dir_fd=fd)
        try:
            os.write(f, data)
        finally:
            os.close(f)
        os.replace(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
    finally:
        os.close(fd)
    return Path(workspace).joinpath(*dir_parts, name)


def read_file(workspace: Path, path: str | os.PathLike) -> bytes:
    parts = _rel_parts(Path(workspace), path)
    if USE_PORTABLE:
        return _portable_read(Path(workspace), parts)
    fd = _open_dirs(Path(workspace), parts[:-1], create=False)
    try:
        try:
            f = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        except OSError as e:
            raise UnsafePath(f"cannot open {parts[-1]!r} (missing or a link)") from e
        try:
            if not stat.S_ISREG(os.fstat(f).st_mode):
                raise UnsafePath("not a regular file")
            chunks = []
            while chunk := os.read(f, 1 << 20):
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(f)
    finally:
        os.close(fd)
