"""Symlink-proof file access inside the Claude-writable workspace.

The workspace is writable by the `jarvis` user (Claude). The ops service runs as a different user with
access to secrets, so it must never follow a link Claude planted: every path component is opened with
O_NOFOLLOW relative to its parent directory fd, and files are written by atomic replace (rename never
follows a symlink at the destination).
"""
from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class UnsafePath(Exception):
    pass


def _check_part(part: str) -> None:
    if not part or part in (".", "..") or "/" in part or "\x00" in part:
        raise UnsafePath(f"bad path component: {part!r}")


def _rel_parts(workspace: Path, path: str | os.PathLike) -> list[str]:
    p = Path(path)
    if p.is_absolute():
        root = os.path.normpath(str(Path(workspace).absolute()))
        norm = os.path.normpath(str(p))
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


def write_file(workspace: Path, dir_parts: list[str], name: str, data: bytes) -> Path:
    _check_part(name)
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
