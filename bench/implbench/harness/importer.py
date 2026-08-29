"""Descriptor-held, bounded Git bundle importer for the scored boundary."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import selectors
import shutil
import stat
import subprocess
import tempfile
import time
import zlib
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from posixpath import normpath
import tarfile
import sys
from typing import Any, Callable, Mapping, Sequence

from .sandbox import LaunchSpec, spawn_child


class ImporterError(RuntimeError):
    """Raised for an untrusted or unprovable import."""


@dataclass(frozen=True)
class ImportLimits:
    max_file_bytes: int = 64 * 1024 * 1024
    max_total_bytes: int = 512 * 1024 * 1024
    max_files: int = 100_000
    max_depth: int = 16
    max_open_descriptors: int = 32
    max_wall_time_s: float = 30.0
    max_compression_ratio: int = 100
    max_objects: int = 100_000

    def __post_init__(self) -> None:
        for name in ("max_file_bytes", "max_total_bytes", "max_files", "max_depth", "max_open_descriptors", "max_objects"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_wall_time_s <= 0 or self.max_compression_ratio <= 0:
            raise ValueError("import limits must be positive")


@dataclass(frozen=True)
class ImportResult:
    bundle: Path
    bundle_digest: str
    files: int
    bytes: int
    object_ids: tuple[str, ...]


@dataclass(frozen=True)
class ImportGraphAttestation:
    """Controller-owned result of the post-copy graph/fsck boundary."""

    attested: bool
    imported_graph_digest: str
    object_ids: tuple[str, ...]
    materialization: Path
    materialization_digest: str


_LOOSE = re.compile(r"^[0-9a-f]{38}$")
_PACK = re.compile(r"^pack-[0-9a-f]{40}\.(?:pack|idx)$")
_PACK_REV = re.compile(r"^pack-[0-9a-f]{40}\.rev$")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")


def _absolute_source(path: str | Path) -> Path:
    value = Path(path)
    if not value.is_absolute() or value.is_symlink():
        raise ImporterError("source path must be absolute and non-symlinked")
    try:
        value = value.resolve(strict=True)
    except OSError as exc:
        raise ImporterError("source path is unavailable") from exc
    if not value.is_dir():
        raise ImporterError("source path is not a directory")
    return value


def _reject_type(info: os.stat_result, where: str) -> None:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ImporterError(f"unsafe importer entry: {where}")


def _read_fd(fd: int, limits: ImportLimits, started: float, *, declared: int) -> bytes:
    if declared < 0 or declared > limits.max_file_bytes:
        raise ImporterError("file bytes limit exceeded before allocation")
    chunks: list[bytes] = []
    total = 0
    while True:
        if time.monotonic() - started > limits.max_wall_time_s:
            raise ImporterError("import wall-time limit exceeded")
        chunk = os.read(fd, min(131072, limits.max_file_bytes - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > limits.max_file_bytes:
            raise ImporterError("file bytes limit exceeded")
        chunks.append(chunk)
    if total != declared:
        raise ImporterError("file changed size during copy")
    return b"".join(chunks)


def _loose_object_oid(path: str, payload: bytes, limits: ImportLimits) -> str:
    """Validate a loose object without materialising its hostile expanded body.

    The old ``zlib.decompress`` call allocated the complete attacker-controlled
    object before the ratio check.  This reader caps every decompressor call, parses
    the bounded header first, and hashes the body incrementally.  ``max_expanded`` is
    determined before the first output allocation and includes the Git header.
    """

    compressed_size = len(payload)
    if compressed_size == 0:
        raise ImporterError("malformed loose object")
    max_expanded = min(
        limits.max_file_bytes,
        limits.max_total_bytes,
        compressed_size * limits.max_compression_ratio,
    )
    if max_expanded <= 0:
        raise ImporterError("object compression ratio limit exceeded")
    decompressor = zlib.decompressobj()
    header = bytearray()
    declared: int | None = None
    body_size = 0
    expanded_size = 0
    digest = hashlib.sha1()

    def consume(raw: bytes) -> None:
        nonlocal declared, body_size
        if declared is None:
            separator = raw.find(b"\0")
            if separator < 0:
                if len(header) + len(raw) > 4096:
                    raise ImporterError("loose object header exceeds limit")
                header.extend(raw)
                return
            if len(header) + separator > 4096:
                raise ImporterError("loose object header exceeds limit")
            header.extend(raw[:separator])
            if b" " not in header:
                raise ImporterError("malformed loose object header")
            kind, size_raw = bytes(header).split(b" ", 1)
            if kind not in {b"commit", b"tree", b"blob", b"tag"}:
                raise ImporterError("unknown Git object type")
            try:
                declared = int(size_raw)
            except ValueError as exc:
                raise ImporterError("malformed loose object size") from exc
            if declared < 0 or declared > limits.max_file_bytes:
                raise ImporterError("loose object declared size exceeds limit")
            if len(header) + 1 + declared > max_expanded:
                raise ImporterError("object compression ratio limit exceeded")
            digest.update(header)
            digest.update(b"\0")
            raw = raw[separator + 1:]
        if raw:
            assert declared is not None
            body_size += len(raw)
            if body_size > declared:
                raise ImporterError("loose object size mismatch")
            digest.update(raw)

    try:
        for offset in range(0, compressed_size, 131072):
            remaining = payload[offset:offset + 131072]
            if decompressor.eof:
                raise ImporterError("loose object has trailing compressed data")
            while remaining:
                room = max_expanded - expanded_size
                # ``room + 1`` gives the parser one byte of overflow evidence while
                # still bounding the output allocation of this call.
                raw = decompressor.decompress(remaining, min(room + 1, 131072))
                if len(raw) > room:
                    raise ImporterError("object compression ratio limit exceeded")
                expanded_size += len(raw)
                consume(raw)
                remaining = decompressor.unconsumed_tail
                if remaining and expanded_size >= max_expanded:
                    raise ImporterError("object compression ratio limit exceeded")
        if not decompressor.eof or decompressor.unused_data:
            raise ImporterError("malformed loose object")
    except zlib.error as exc:
        raise ImporterError("malformed loose object") from exc
    if declared is None or body_size != declared:
        raise ImporterError("loose object size mismatch")
    oid = digest.hexdigest()
    expected = path[:2] + path[2:]
    if oid != expected:
        raise ImporterError("loose object hash mismatch")
    return oid


def _relative_entry_kind(relative: str, *, candidate_ref: str | None) -> str:
    parts = relative.split("/")
    if parts == ["config"] or parts == ["HEAD"] or parts[:1] == ["logs"] or parts[:1] == ["hooks"]:
        raise ImporterError("config or mutable Git metadata is forbidden")
    if parts[:2] == ["objects", "pack"] and len(parts) == 3 and _PACK.fullmatch(parts[2]):
        return "pack"
    if parts[:2] == ["objects", "pack"] and len(parts) == 3 and _PACK_REV.fullmatch(parts[2]):
        # Reverse indexes are a derived Git cache. Bound and stability-check the
        # bytes, but rebuild or omit them rather than importing attacker metadata.
        return "derived"
    if len(parts) == 3 and parts[0] == "objects" and len(parts[1]) == 2 and re.fullmatch(r"[0-9a-f]{2}", parts[1]) and _LOOSE.fullmatch(parts[2]):
        return "loose"
    if parts[:2] == ["refs", "implbench"] and candidate_ref and relative in {candidate_ref, candidate_ref.removeprefix("refs/")}:
        return "ref"
    raise ImporterError(f"unallowlisted Git metadata: {relative}")


def _validate_pack(path: str, data: bytes, limits: ImportLimits) -> None:
    if path.endswith(".pack"):
        if len(data) < 32 or data[:4] != b"PACK" or int.from_bytes(data[4:8], "big") not in {2, 3}:
            raise ImporterError("malformed pack header")
        count = int.from_bytes(data[8:12], "big")
        if count > limits.max_objects:
            raise ImporterError("pack object count limit exceeded")
        if hashlib.sha1(data[:-20]).digest() != data[-20:]:
            raise ImporterError("malformed pack checksum")
    else:
        if data.startswith(b"\xfftOc"):
            if len(data) < 8 or int.from_bytes(data[4:8], "big") not in {2, 3}:
                raise ImporterError("malformed pack index version")
            if len(data) < 8 + 256 * 4 + 40:
                raise ImporterError("malformed pack index size")
        elif len(data) < 256 * 4 + 40:
            raise ImporterError("malformed pack index")


def _walk_files(root_fd: int, *, candidate_ref: str | None, limits: ImportLimits):
    stack: list[tuple[int, str, int]] = [(os.dup(root_fd), "", 0)]
    opened = 1
    try:
        while stack:
            directory_fd, prefix, depth = stack.pop()
            try:
                if depth > limits.max_depth:
                    raise ImporterError("import traversal depth limit exceeded")
                try:
                    names = sorted(os.listdir(directory_fd), key=lambda value: os.fsencode(value))
                except OSError as exc:
                    raise ImporterError("cannot enumerate source descriptor") from exc
                for name in names:
                    relative = f"{prefix}/{name}" if prefix else name
                    info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        if opened >= limits.max_open_descriptors:
                            raise ImporterError("open descriptor limit exceeded")
                        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                        opened += 1
                        stack.append((child, relative, depth + 1))
                        continue
                    _reject_type(info, relative)
                    yield directory_fd, relative, info
            finally:
                try:
                    os.close(directory_fd)
                except OSError:
                    pass
                opened -= 1
    finally:
        for directory_fd, _, _ in stack:
            try:
                os.close(directory_fd)
            except OSError:
                pass


def _import_from_descriptor(source_fd: int, destination: str | Path, *, limits: ImportLimits, candidate_ref: str | None) -> ImportResult:
    """Copy from an already-captured directory descriptor without resolving its pathname."""

    limits = limits or ImportLimits()
    target = Path(destination)
    if target.exists():
        raise ImporterError("import destination already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    spool = Path(tempfile.mkdtemp(prefix=".implbench-import-", dir=target.parent))
    started = time.monotonic()
    total_bytes = 0
    file_count = 0
    object_ids: list[str] = []
    entries: list[tuple[str, str, int]] = []
    root_fd: int | None = None
    try:
        root_fd = os.dup(source_fd)
        root_info = os.fstat(root_fd)
    except OSError as exc:
        if root_fd is not None:
            os.close(root_fd)
        raise ImporterError("source descriptor is unavailable") from exc
    if not stat.S_ISDIR(root_info.st_mode):
        os.close(root_fd)
        root_fd = None
        raise ImporterError("source descriptor is not a directory")
    try:
        # A worktree source has a directory .git; a bare source has objects directly.
        names = os.listdir(root_fd)
        if ".git" in names:
            info = os.stat(".git", dir_fd=root_fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                raise ImporterError("source .git pointer or unsafe type")
            git_fd = os.open(".git", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        else:
            git_fd = os.dup(root_fd)
        try:
            for directory_fd, relative, info in _walk_files(git_fd, candidate_ref=candidate_ref, limits=limits):
                kind = _relative_entry_kind(relative, candidate_ref=candidate_ref)
                file_count += 1
                if file_count > limits.max_files:
                    raise ImporterError("file count limit exceeded")
                fd = os.open(os.path.basename(relative), os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
                try:
                    before = os.fstat(fd)
                    if (before.st_ino, before.st_dev, before.st_mode, before.st_nlink, before.st_size) != (info.st_ino, info.st_dev, info.st_mode, info.st_nlink, info.st_size):
                        raise ImporterError("source inode/type changed before copy")
                    data = _read_fd(fd, limits, started, declared=before.st_size)
                    os.lseek(fd, 0, os.SEEK_SET)
                    second = _read_fd(fd, limits, started, declared=before.st_size)
                    after = os.fstat(fd)
                finally:
                    os.close(fd)
                if (before.st_ino, before.st_dev, before.st_mode, before.st_nlink, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_dev, after.st_mode, after.st_nlink, after.st_size, after.st_mtime_ns):
                    raise ImporterError("source changed during copy")
                total_bytes += len(data)
                if total_bytes > limits.max_total_bytes:
                    raise ImporterError("aggregate byte limit exceeded")
                if kind == "derived":
                    if hashlib.sha256(second).digest() != hashlib.sha256(data).digest():
                        raise ImporterError("source content changed during copy")
                    continue
                if kind == "loose":
                    object_ids.append(_loose_object_oid(relative.split("/")[1] + relative.rsplit("/", 1)[-1], data, limits))
                    if len(object_ids) > limits.max_objects:
                        raise ImporterError("object count limit exceeded")
                elif kind == "ref":
                    if not _HEX40.fullmatch(data.decode("ascii", "strict").strip()):
                        raise ImporterError("candidate ref does not contain one canonical OID")
                elif kind == "pack":
                    _validate_pack(relative, data, limits)
                out = spool / relative
                out.parent.mkdir(parents=True, exist_ok=True)
                fd_out = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    offset = 0
                    while offset < len(data):
                        written = os.write(fd_out, data[offset:offset + 131072])
                        if not written:
                            raise ImporterError("spool write failed")
                        offset += written
                    os.fsync(fd_out)
                finally:
                    os.close(fd_out)
                # The second descriptor-held pass above is deliberately compared after the
                # spool write: a same-size replacement or content race must not be hidden by the
                # first read's digest.
                if hashlib.sha256(second).digest() != hashlib.sha256(data).digest():
                    raise ImporterError("source content changed during copy")
                entries.append((relative, hashlib.sha256(data).hexdigest(), len(data)))
        finally:
            os.close(git_fd)
    except Exception:
        os.close(root_fd)
        root_fd = None
        shutil.rmtree(spool, ignore_errors=True)
        raise
    else:
        os.close(root_fd)
        root_fd = None
    digest_input = "".join(f"{name}\0{digest}\0{size}\n" for name, digest, size in sorted(entries)).encode("ascii")
    bundle_digest = hashlib.sha256(b"implbench-import-bundle-v1\0" + digest_input).hexdigest()
    try:
        # Git's plumbing requires these controller-derived files to recognise the sealed
        # object directory.  They are never copied from the untrusted descriptor.
        (spool / "config").write_text("[core]\n\trepositoryformatversion = 0\n\tbare = true\n", encoding="ascii")
        if candidate_ref is not None:
            candidate_oid = (spool / candidate_ref).read_text(encoding="ascii").strip()
            (spool / "HEAD").write_text(candidate_oid + "\n", encoding="ascii")
        (spool / "bundle.digest").write_text(bundle_digest + "\n")
        os.replace(spool, target)
    except Exception:
        shutil.rmtree(spool, ignore_errors=True)
        raise ImporterError("cannot seal imported bundle")
    return ImportResult(target, bundle_digest, file_count, total_bytes, tuple(sorted(set(object_ids))))


def import_repository(source: str | Path, destination: str | Path, *, limits: ImportLimits | None = None, candidate_ref: str | None = None) -> ImportResult:
    """Capture the source once, then perform the complete import through its descriptor."""

    limits = limits or ImportLimits()
    source_path = _absolute_source(source)
    try:
        source_fd = os.open(source_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ImporterError("source path is unavailable") from exc
    try:
        return _import_from_descriptor(source_fd, destination, limits=limits, candidate_ref=candidate_ref)
    finally:
        os.close(source_fd)


def import_from_descriptor(source_fd: int, destination: str | Path, *, limits: ImportLimits | None = None, candidate_ref: str | None = None) -> ImportResult:
    """Descriptor entry point; the supplied descriptor is the only source identity."""

    return _import_from_descriptor(source_fd, destination, limits=limits or ImportLimits(), candidate_ref=candidate_ref)


def import_from_descriptor_child(
    source_fd: int,
    destination: str | Path,
    *,
    launch_spec: LaunchSpec,
    limits: ImportLimits | None = None,
    candidate_ref: str | None = None,
    allow_unprofiled_test: bool = False,
    structural_identity: bool = False,
    child_spawner: Callable[[LaunchSpec, tuple[int, ...]], subprocess.Popen[bytes]] | None = None,
) -> tuple[ImportResult, dict[str, Any]]:
    """Run the hostile parser in a fresh importer child and verify its evidence.

    The descriptor is inherited directly; no source pathname, controller key, receipt
    log, or ambient environment is handed to the child.  The caller owns all
    classification and receipt authority after the bounded response returns.
    """

    active_limits = limits or ImportLimits()
    if launch_spec.plane != "importer":
        raise ImporterError("import child requires the importer launch profile")
    request_read, request_write = os.pipe()
    destination_path = Path(destination)
    destination_preexisting = destination_path.exists()
    process: subprocess.Popen[bytes] | None = None
    completed = False
    try:
        request = {
            "version": "implbench-import-child-v1",
            "source_fd": source_fd,
            "destination": str(destination),
            "candidate_ref": candidate_ref,
            "limits": active_limits.__dict__,
            "profile_digest": launch_spec.profile_digest,
            "template_digest": launch_spec.template_digest,
            "expected_uid": launch_spec.uid,
            "test_unprofiled": allow_unprofiled_test,
            "structural_identity": structural_identity,
            "effective_uid": os.getuid() if structural_identity else launch_spec.uid,
        }
        encoded = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) > 65536:
            raise ImporterError("import child request exceeds its bound")
        child_spec = LaunchSpec(**{**launch_spec.__dict__, "argv": (
            sys.executable, "-u", "-m", "implbench.harness.importer_child", "--request-fd", str(request_read),
        ), "inherited_fds": tuple(sorted(set((*launch_spec.inherited_fds, source_fd, request_read))) )})
        if child_spawner is None:
            process = spawn_child(
                child_spec,
                pass_fds=child_spec.inherited_fds,
                allow_unprofiled_test=allow_unprofiled_test,
            )
        else:
            if allow_unprofiled_test:
                raise ImporterError("production child spawner cannot use the test seam")
            process = child_spawner(child_spec, child_spec.inherited_fds)
        os.close(request_read)
        request_read = -1
        try:
            os.write(request_write, encoded)
        except OSError as exc:
            raise ImporterError("import child rejected its request") from exc
        os.close(request_write)
        request_write = -1
        stdout, stderr = _bounded_child_output(process, limit=65536, timeout=active_limits.max_wall_time_s + 2)
        if process.returncode != 0:
            raise ImporterError("import child failed")
        try:
            response = json.loads(stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ImporterError("import child returned malformed evidence") from exc
        required = {"version", "ok", "pid", "uid", "profile_digest", "template_digest", "limits", "result"}
        if not isinstance(response, Mapping) or set(response) != required or response.get("version") != "implbench-import-child-v1" or response.get("ok") is not True:
            raise ImporterError("import child response is not closed")
        expected_uid = os.getuid() if structural_identity else launch_spec.uid
        if response.get("pid") != process.pid or response.get("uid") != expected_uid:
            raise ImporterError("import child PID or UID evidence mismatch")
        if response.get("profile_digest") != launch_spec.profile_digest or response.get("template_digest") != launch_spec.template_digest:
            raise ImporterError("import child profile evidence mismatch")
        if response.get("limits") != active_limits.__dict__:
            raise ImporterError("import child limit evidence mismatch")
        result = response.get("result")
        if not isinstance(result, Mapping) or set(result) != {"bundle", "bundle_digest", "files", "bytes", "object_ids"}:
            raise ImporterError("import child result is not closed")
        object_ids = result["object_ids"]
        if (not isinstance(result["bundle"], str) or not isinstance(result["bundle_digest"], str)
                or not isinstance(result["files"], int) or not isinstance(result["bytes"], int)
                or not isinstance(object_ids, list) or not all(isinstance(oid, str) and _HEX40.fullmatch(oid) for oid in object_ids)):
            raise ImporterError("import child result has invalid fields")
        expected_bundle = Path(destination)
        if Path(result["bundle"]) != expected_bundle or result["files"] < 0 or result["bytes"] < 0:
            raise ImporterError("import child result escapes the bound destination")
        completed = True
        return ImportResult(Path(result["bundle"]), result["bundle_digest"], result["files"], result["bytes"], tuple(object_ids)), dict(response)
    finally:
        if request_read >= 0:
            os.close(request_read)
        if request_write >= 0:
            os.close(request_write)
        # The request write can fail after the child has been created (for example,
        # if it exits or is signalled before accepting its request).  That path must
        # reap the exact child just as bounded-output failures do.
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        if process is not None:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        if not completed:
            _cleanup_failed_import(destination_path, destination_preexisting)


def _cleanup_failed_import(destination: Path, destination_preexisting: bool) -> None:
    """Remove only this child boundary's untrusted result/spool area after failure."""

    if not destination_preexisting and destination.exists() and not destination.is_symlink():
        shutil.rmtree(destination, ignore_errors=True)
    parent = destination.parent
    if not parent.is_dir() or parent.is_symlink():
        return
    for spool in parent.glob(".implbench-import-*"):
        if spool.is_dir() and not spool.is_symlink():
            shutil.rmtree(spool, ignore_errors=True)


def _bounded_child_output(
    process: subprocess.Popen[bytes], *, limit: int, timeout: float
) -> tuple[bytes, bytes]:
    """Drain both child streams without allowing a hostile child to allocate us."""

    if process.stdout is None or process.stderr is None:
        raise ImporterError("import child pipes are unavailable")
    stdout_fd, stderr_fd = process.stdout.fileno(), process.stderr.fileno()
    streams = {stdout_fd: bytearray(), stderr_fd: bytearray()}
    selector = selectors.DefaultSelector()
    try:
        for fd in streams:
            os.set_blocking(fd, False)
            selector.register(fd, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            for key, _ in selector.select(min(remaining, 0.1)):
                chunk = os.read(key.fd, min(131072, limit + 1))
                if not chunk:
                    selector.unregister(key.fd)
                    continue
                output = streams[key.fd]
                if len(output) + len(chunk) > limit:
                    raise ImporterError("import child response exceeds its bound")
                output.extend(chunk)
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError from exc
    except TimeoutError as exc:
        if process.poll() is None:
            process.kill()
        process.wait()
        raise ImporterError("import child timed out") from exc
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.wait()
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    return bytes(streams[stdout_fd]), bytes(streams[stderr_fd])


def attest_imported_graph(
    imported: ImportResult,
    *,
    fixture_root_oid: str,
    receipts: Sequence[Mapping[str, Any]],
    allowed_paths: Sequence[str] = (),
) -> ImportGraphAttestation:
    """Run fsck and independently reconstruct every receipted commit and tree."""

    if not _HEX40.fullmatch(fixture_root_oid):
        raise ImporterError("fixture root OID is invalid")
    git_dir = imported.bundle / ".git" if (imported.bundle / ".git").is_dir() else imported.bundle
    env = {
        "PATH": os.defpath,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    try:
        fsck = subprocess.run(
            ["git", "--git-dir", str(git_dir), "fsck", "--strict", "--full", "--no-reflogs"],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ImporterError("imported graph fsck failed") from exc
    if fsck.returncode != 0:
        detail = fsck.stderr.strip().replace("\n", " ")[:200]
        raise ImporterError(f"imported graph fsck rejected the bundle: {detail}")
    # The importer’s loose-object walk is only a copy accounting aid.  The
    # authoritative graph inventory comes from Git’s complete object database,
    # which includes packed objects.
    object_ids: set[str] = set()
    try:
        objects = subprocess.run(
            ["git", "--git-dir", str(git_dir), "cat-file", "--batch-all-objects", "--batch-check"],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ImporterError("imported object enumeration failed") from exc
    if objects.returncode != 0:
        raise ImporterError("imported object enumeration was rejected")
    for line in objects.stdout.splitlines():
        oid = line.split(" ", 1)[0]
        if not _HEX40.fullmatch(oid):
            raise ImporterError("imported object enumeration returned an invalid OID")
        object_ids.add(oid)
    if len(object_ids) > 100_000:
        raise ImporterError("imported object count limit exceeded")
    if fixture_root_oid not in object_ids:
        raise ImporterError("fixture root is absent from imported graph")

    def git_object(*arguments: str) -> bytes:
        try:
            result = subprocess.run(
                ["git", "--git-dir", str(git_dir), *arguments],
                capture_output=True,
                check=False,
                env=env,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ImporterError("imported Git object query failed") from exc
        if result.returncode != 0:
            raise ImporterError("imported Git object query was rejected")
        return result.stdout

    def commit_shape(commit_oid: str, expected_parent: str) -> tuple[str, list[str]]:
        raw = git_object("cat-file", "commit", commit_oid)
        headers, separator, _message = raw.partition(b"\n\n")
        if not separator:
            raise ImporterError("imported commit is malformed")
        tree_oids: list[str] = []
        parents: list[str] = []
        for line in headers.splitlines():
            key, _, value = line.partition(b" ")
            if key == b"tree":
                tree_oids.append(value.decode("ascii"))
            elif key == b"parent":
                parents.append(value.decode("ascii"))
        if len(tree_oids) != 1 or parents != [expected_parent] or not _HEX40.fullmatch(tree_oids[0]):
            raise ImporterError("imported commit parent or tree invariant failed")
        return tree_oids[0], parents

    def changed_paths(parent: str, commit_oid: str) -> list[str]:
        raw = git_object("diff-tree", "--no-commit-id", "--name-only", "-r", "-z", parent, commit_oid)
        values = raw.decode("utf-8", "strict").split("\0")
        return [path for path in values if path]

    def materialize(commit_oid: str) -> tuple[Path, str]:
        try:
            archive = subprocess.run(
                ["git", "--git-dir", str(git_dir), "archive", "--format=tar", commit_oid],
                capture_output=True,
                check=False,
                env=env,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ImporterError("imported tree materialization failed") from exc
        if archive.returncode != 0:
            raise ImporterError("imported tree materialization was rejected")
        target = Path(tempfile.mkdtemp(prefix=".implbench-materialization-", dir=imported.bundle.parent))
        try:
            with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as stream:
                for member in stream:
                    name = member.name
                    if not name or name.startswith("/") or normpath(name) != name or ".." in name.split("/"):
                        raise ImporterError("imported tree contains an unsafe path")
                    destination = target / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        destination.parent.resolve(strict=False).relative_to(target.resolve(strict=True))
                    except ValueError as exc:
                        raise ImporterError("imported tree path escapes materialization") from exc
                    if member.isdir():
                        destination.mkdir(exist_ok=True)
                        os.chmod(destination, 0o755)
                    elif member.isreg():
                        source = stream.extractfile(member)
                        if source is None:
                            raise ImporterError("imported tree contains an unreadable file")
                        with destination.open("xb") as output:
                            shutil.copyfileobj(source, output)
                        os.chmod(destination, 0o755 if member.mode & 0o111 else 0o644)
                    elif member.issym():
                        target_name = member.linkname
                        if target_name.startswith("/") or ".." in normpath(target_name).split("/"):
                            raise ImporterError("imported tree contains an escaping symlink")
                        os.symlink(target_name, destination)
                    else:
                        raise ImporterError("imported tree contains an unsupported entry")
            from .completion import materialization_digest

            return target, materialization_digest(target)
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise

    expected_parent = fixture_root_oid
    final_materialization: Path | None = None
    receipt_facts: list[dict[str, Any]] = []
    for receipt in receipts:
        if not isinstance(receipt, Mapping):
            raise ImporterError("receipt graph entry is not an object")
        if receipt.get("ordered_parent_oids") != [expected_parent]:
            raise ImporterError("imported graph parent chain mismatch")
        commit_oid = receipt.get("commit_oid")
        if not isinstance(commit_oid, str) or not _HEX40.fullmatch(commit_oid) or commit_oid not in object_ids:
            raise ImporterError("receipted commit is absent from imported graph")
        tree_oid, _parents = commit_shape(commit_oid, expected_parent)
        if receipt.get("tree_oid") != tree_oid:
            raise ImporterError("receipted tree is not the imported commit tree")
        paths = receipt.get("changed_paths")
        if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
            raise ImporterError("receipt changed paths are malformed")
        actual_paths = changed_paths(expected_parent, commit_oid)
        if sorted(paths) != sorted(actual_paths) or len(paths) != len(set(paths)):
            raise ImporterError("receipted changed paths do not match the imported tree diff")
        if allowed_paths and any(not any(fnmatchcase(path, pattern) for pattern in allowed_paths) for path in paths):
            raise ImporterError("imported changed path is outside the task allowlist")
        materialization, tree_digest = materialize(commit_oid)
        if receipt.get("tree_digest") != tree_digest:
            shutil.rmtree(materialization, ignore_errors=True)
            raise ImporterError(f"receipted tree digest does not match reconstructed tree: {receipt.get('tree_digest')} != {tree_digest}")
        if final_materialization is not None:
            shutil.rmtree(final_materialization, ignore_errors=True)
        final_materialization = materialization
        receipt_facts.append({
            "commit_oid": commit_oid,
            "tree_oid": tree_oid,
            "changed_paths": paths,
            "tree_digest": tree_digest,
        })
        expected_parent = commit_oid
    if final_materialization is None:
        raise ImporterError("empty receipt graph cannot be attested")
    graph_digest = hashlib.sha256(
        b"implbench-imported-graph-v2\0"
        + json.dumps({"objects": sorted(object_ids), "receipts": receipt_facts}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    from .completion import materialization_digest

    digest = materialization_digest(final_materialization)
    return ImportGraphAttestation(True, graph_digest, tuple(sorted(object_ids)), final_materialization, digest)


def validate_import(*args, **kwargs) -> ImportResult:
    return import_repository(*args, **kwargs)


import_bundle = import_repository
