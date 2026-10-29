import os

import pytest

from core.safefs import UnsafePath, read_file, write_file


def test_write_creates_nested_dirs(tmp_path):
    p = write_file(tmp_path, ["INV2101", "downloads"], "T7.docx", b"PK")
    assert p == tmp_path / "INV2101" / "downloads" / "T7.docx" and p.read_bytes() == b"PK"


def test_write_refuses_symlinked_dir(tmp_path):
    target = tmp_path / "outside"
    target.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "INV2101").symlink_to(target)
    with pytest.raises(UnsafePath):
        write_file(ws, ["INV2101", "downloads"], "x.docx", b"x")
    assert list(target.iterdir()) == []


def test_write_replaces_symlink_file_instead_of_following(tmp_path):
    victim = tmp_path / "victim.py"
    victim.write_text("original")
    ws = tmp_path / "ws"
    (ws / "c").mkdir(parents=True)
    (ws / "c" / "f.docx").symlink_to(victim)
    p = write_file(ws, ["c"], "f.docx", b"new")
    assert victim.read_text() == "original"
    assert not p.is_symlink() and p.read_bytes() == b"new"


def test_write_rejects_bad_names(tmp_path):
    for bad in ["..", "a/b", ""]:
        with pytest.raises(UnsafePath):
            write_file(tmp_path, ["c"], bad, b"x")
    with pytest.raises(UnsafePath):
        write_file(tmp_path, ["..", "x"], "f", b"x")


def test_read_refuses_symlink(tmp_path):
    secret = tmp_path / "secrets.env"
    secret.write_text("PASS=x")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "answers.docx").symlink_to(secret)
    with pytest.raises(UnsafePath):
        read_file(ws, ws / "answers.docx")


def test_read_refuses_outside_and_reads_inside(tmp_path):
    ws = tmp_path / "ws"
    (ws / "c").mkdir(parents=True)
    (ws / "c" / "a.docx").write_bytes(b"PK")
    assert read_file(ws, ws / "c" / "a.docx") == b"PK"
    assert read_file(ws, "c/a.docx") == b"PK"
    with pytest.raises(UnsafePath):
        read_file(ws, tmp_path / "other")
    with pytest.raises(UnsafePath):
        read_file(ws, ws / "c" / ".." / ".." / "x")
