"""The Windows (no O_NOFOLLOW / dir_fd) implementation of safefs. It is plain Python, so these run on every OS by
switching it on; the symlink cases need symlink support (always on macOS/Linux, needs privileges on Windows)."""
import os
import types

import pytest

from core import safefs
from core.safefs import UnsafePath, read_file, write_file


@pytest.fixture(autouse=True)
def portable(monkeypatch):
    monkeypatch.setattr(safefs, "USE_PORTABLE", True)


def can_symlink(tmp_path) -> bool:
    try:
        (tmp_path / "probe-link").symlink_to(tmp_path)
        (tmp_path / "probe-link").unlink()
        return True
    except (OSError, NotImplementedError):
        return False


def test_write_creates_nested_dirs_and_leaves_no_temp_files(tmp_path):
    p = write_file(tmp_path, ["INV2101", "downloads"], "T7.docx", b"PK")
    assert p == tmp_path / "INV2101" / "downloads" / "T7.docx" and p.read_bytes() == b"PK"
    assert [x.name for x in p.parent.iterdir()] == ["T7.docx"]
    assert write_file(tmp_path, [], "root.txt", b"x").read_bytes() == b"x"  # directly in the workspace
    write_file(tmp_path, ["INV2101", "downloads"], "T7.docx", b"PK2")  # overwrite
    assert p.read_bytes() == b"PK2"


def test_write_refuses_symlinked_dir(tmp_path):
    if not can_symlink(tmp_path):
        pytest.skip("cannot create symlinks here")
    target = tmp_path / "outside"
    target.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "INV2101").symlink_to(target, target_is_directory=True)
    with pytest.raises(UnsafePath):
        write_file(ws, ["INV2101", "downloads"], "x.docx", b"x")
    assert list(target.iterdir()) == []


def test_write_replaces_symlink_file_instead_of_following(tmp_path):
    if not can_symlink(tmp_path):
        pytest.skip("cannot create symlinks here")
    victim = tmp_path / "victim.py"
    victim.write_text("original", encoding="utf-8")
    ws = tmp_path / "ws"
    (ws / "c").mkdir(parents=True)
    (ws / "c" / "f.docx").symlink_to(victim)
    p = write_file(ws, ["c"], "f.docx", b"new")
    assert victim.read_text(encoding="utf-8") == "original"
    assert not p.is_symlink() and p.read_bytes() == b"new"


def test_read_refuses_symlink_and_outside_paths(tmp_path):
    ws = tmp_path / "ws"
    (ws / "c").mkdir(parents=True)
    (ws / "c" / "a.docx").write_bytes(b"PK")
    assert read_file(ws, ws / "c" / "a.docx") == b"PK" and read_file(ws, "c/a.docx") == b"PK"
    for bad in (tmp_path / "other", ws / "c" / ".." / ".." / "x", ws):
        with pytest.raises(UnsafePath):
            read_file(ws, bad)
    with pytest.raises(UnsafePath):
        read_file(ws, "c/missing.docx")
    with pytest.raises(UnsafePath):
        read_file(ws, "c")  # a directory
    if can_symlink(tmp_path):
        secret = tmp_path / "secrets.env"
        secret.write_text("PASS=x", encoding="utf-8")
        (ws / "answers.docx").symlink_to(secret)
        with pytest.raises(UnsafePath):
            read_file(ws, ws / "answers.docx")
        (ws / "linkdir").symlink_to(tmp_path, target_is_directory=True)
        with pytest.raises(UnsafePath):
            read_file(ws, "linkdir/secrets.env")


@pytest.mark.parametrize("bad", ["..", "a/b", "", "a\\b", "C:evil", "name.txt:stream", "trailing.", "trailing ",
                                 "CON", "nul.txt", "q?", "pipe|x"])
def test_bad_names_are_rejected(tmp_path, bad):
    with pytest.raises(UnsafePath):
        write_file(tmp_path, ["c"], bad, b"x")


def test_bad_dir_parts_are_rejected(tmp_path):
    with pytest.raises(UnsafePath):
        write_file(tmp_path, ["..", "x"], "f", b"x")
    with pytest.raises(UnsafePath):
        write_file(tmp_path, ["a\\..\\b"], "f", b"x")


def test_failed_write_removes_its_temp_file(tmp_path, monkeypatch):
    (tmp_path / "c").mkdir()

    def boom(*a, **k):
        raise PermissionError("locked")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(PermissionError):
        write_file(tmp_path, ["c"], "f.docx", b"x")
    assert list((tmp_path / "c").iterdir()) == []


def stat_result(mode=0o100644, attrs=0, tag=None):
    d = {"st_mode": mode, "st_file_attributes": attrs}
    if tag is not None:
        d["st_reparse_tag"] = tag
    return types.SimpleNamespace(**d)


def test_reparse_points_are_links_unless_they_are_benign_cloud_files():
    REPARSE = safefs.FILE_ATTRIBUTE_REPARSE_POINT
    link = lambda st: safefs.is_link_like("x", lstat=lambda p: st)  # noqa: E731
    assert not link(stat_result())                                  # ordinary file
    assert link(stat_result(mode=0o120777))                        # symlink mode
    assert link(stat_result(attrs=REPARSE, tag=0xA000000C))        # IO_REPARSE_TAG_SYMLINK
    assert link(stat_result(attrs=REPARSE, tag=0xA0000003))        # junction
    assert link(stat_result(attrs=REPARSE))                        # unknown tag: refuse
    assert not link(stat_result(attrs=REPARSE, tag=0x9000001A))    # OneDrive placeholder: a real file for us
    assert not safefs.is_link_like("does-not-exist-anywhere")


def test_junction_in_the_path_is_refused(tmp_path):
    (tmp_path / "ok").mkdir()
    (tmp_path / "junc").mkdir()
    real_lstat = os.lstat

    def fake(path, *a, **k):
        st = real_lstat(path, *a, **k)
        if os.path.basename(str(path)) == "junc":
            return types.SimpleNamespace(st_mode=st.st_mode, st_file_attributes=safefs.FILE_ATTRIBUTE_REPARSE_POINT,
                                         st_reparse_tag=0xA0000003)
        return st

    with pytest.raises(UnsafePath):
        safefs._walk_plain_dirs(tmp_path, ["junc"], create=False, lstat=fake)
    assert safefs._walk_plain_dirs(tmp_path, ["ok"], create=False, lstat=fake) == tmp_path / "ok"
