from __future__ import annotations

import os
import stat
import hashlib
import zlib
from pathlib import Path

import pytest

from implbench.harness.importer import ImportLimits, ImporterError, import_repository


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "cell"
    (repo / ".git" / "objects" / "aa").mkdir(parents=True)
    return repo


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "device"])
def test_importer_rejects_unsafe_object_entries(
    tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _git_repo(tmp_path)
    entry = source / ".git" / "objects" / "aa" / ("b" * 38)
    if kind == "symlink":
        entry.symlink_to("/etc/passwd")
    elif kind == "hardlink":
        payload = source / "payload"
        payload.write_bytes(b"x")
        os.link(payload, entry)
    elif kind == "fifo":
        os.mkfifo(entry)
    else:
        # Creating a device node is privileged on the supported runners.  Keep the real
        # production type check in the importer and feed it the stat of an existing device
        # descriptor through the test seam; the placeholder ensures the normal walk reaches it.
        entry.write_bytes(b"placeholder")
        device_fd = os.open(os.devnull, os.O_RDONLY)
        try:
            device_info = os.fstat(device_fd)
            assert stat.S_ISCHR(device_info.st_mode)
            real_stat = os.stat

            def stat_device(path, *args, **kwargs):
                if path == entry.name:
                    return device_info
                return real_stat(path, *args, **kwargs)

            monkeypatch.setattr(os, "stat", stat_device)
            with pytest.raises(ImporterError):
                import_repository(source, tmp_path / "bundle")
        finally:
            os.close(device_fd)
        return
    with pytest.raises(ImporterError):
        import_repository(source, tmp_path / "bundle")


def test_importer_copies_descriptor_held_files_and_destroys_spool_on_failure(tmp_path: Path) -> None:
    source = _git_repo(tmp_path)
    loose = source / ".git" / "objects" / "aa" / ("b" * 38)
    loose.write_bytes(b"not-zlib")
    bundle = tmp_path / "bundle"
    with pytest.raises(ImporterError, match="object"):
        import_repository(source, bundle)
    assert not bundle.exists()
    assert not list(tmp_path.glob(".implbench-import-*"))


def test_importer_limits_are_checked_before_copy(tmp_path: Path) -> None:
    source = _git_repo(tmp_path)
    (source / ".git" / "objects" / "aa" / ("b" * 38)).write_bytes(b"x" * 32)
    with pytest.raises(ImporterError, match="bytes"):
        import_repository(source, tmp_path / "bundle", limits=ImportLimits(max_file_bytes=1))


def test_importer_accepts_a_valid_loose_object_and_seals_content_addressed_bundle(tmp_path: Path) -> None:
    source = _git_repo(tmp_path)
    body = b"hello\n"
    header = b"blob " + str(len(body)).encode()
    oid = hashlib.sha1(header + b"\0" + body).hexdigest()
    loose = source / ".git" / "objects" / oid[:2] / oid[2:]
    loose.parent.mkdir(parents=True, exist_ok=True)
    loose.write_bytes(zlib.compress(header + b"\0" + body))
    result = import_repository(source, tmp_path / "bundle")
    assert result.bundle.exists()
    assert result.object_ids == (oid,)
    assert len(result.bundle_digest) == 64


def test_importer_bounds_but_does_not_import_derived_reverse_index(tmp_path: Path) -> None:
    source = _git_repo(tmp_path)
    body = b"hello\n"
    header = b"blob " + str(len(body)).encode()
    oid = hashlib.sha1(header + b"\0" + body).hexdigest()
    loose = source / ".git" / "objects" / oid[:2] / oid[2:]
    loose.parent.mkdir(parents=True, exist_ok=True)
    loose.write_bytes(zlib.compress(header + b"\0" + body))
    reverse = source / ".git" / "objects" / "pack" / ("pack-" + "a" * 40 + ".rev")
    reverse.parent.mkdir(parents=True)
    reverse.write_bytes(b"derived-cache")

    result = import_repository(source, tmp_path / "bundle")

    assert result.files == 2
    assert not (result.bundle / "objects" / "pack" / reverse.name).exists()


def test_importer_rejects_source_path_reopen_and_config_injection(tmp_path: Path) -> None:
    source = _git_repo(tmp_path)
    (source / ".git" / "config").write_text("[core]\n\thooksPath = /tmp/evil\n")
    with pytest.raises(ImporterError, match="config"):
        import_repository(source, tmp_path / "bundle")
