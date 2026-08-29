import os

import pytest

from arb_files.paths import resolve_local_path


def test_absolute_inside_root_ok(tmp_path):
    root = str(tmp_path)
    path = os.path.join(root, "a.txt")
    assert resolve_local_path(root, path) == os.path.realpath(path)


def test_outside_root_rejected(tmp_path):
    with pytest.raises(ValueError):
        resolve_local_path(str(tmp_path), "/etc/passwd")


def test_relative_rejected(tmp_path):
    with pytest.raises(ValueError):
        resolve_local_path(str(tmp_path), "a.txt")


def test_no_root_rejects_even_absolute(tmp_path):
    with pytest.raises(ValueError):
        resolve_local_path(None, str(tmp_path / "a.txt"))


def test_symlink_escape_rejected(tmp_path):
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "link"
    link.symlink_to(outside)
    with pytest.raises(ValueError):
        resolve_local_path(str(tmp_path), str(link / "x.txt"))
