"""One-shot, descriptor-only importer child entry point."""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
from pathlib import Path

from .importer import ImportLimits, ImporterError, _import_from_descriptor


def _read_request(fd: int) -> dict:
    raw = bytearray()
    while len(raw) <= 65536:
        chunk = os.read(fd, 65536 - len(raw) + 1)
        if not chunk:
            break
        raw.extend(chunk)
    if len(raw) > 65536:
        raise ImporterError("import child request exceeds its bound")
    value = json.loads(bytes(raw))
    required = {"version", "source_fd", "destination", "candidate_ref", "limits", "profile_digest", "template_digest", "expected_uid", "test_unprofiled", "structural_identity", "effective_uid"}
    if not isinstance(value, dict) or set(value) != required or value["version"] != "implbench-import-child-v1":
        raise ImporterError("import child request is not closed")
    return value


def _limits(value: dict) -> ImportLimits:
    if not isinstance(value, dict) or set(value) != set(ImportLimits().__dict__):
        raise ImporterError("import child limits are not closed")
    return ImportLimits(**value)


def _apply_limits(limits: ImportLimits, *, allow_darwin_address_space_gap: bool) -> None:
    # These are child-only limits, set before any hostile source traversal or parser
    # allocation.  The file/descriptor/object limits are enforced again by the parser.
    def cap(which: int, value: int) -> None:
        soft, hard = resource.getrlimit(which)
        effective = min(value, hard) if hard != resource.RLIM_INFINITY else value
        # A non-root child cannot lower a hard limit below an inherited soft
        # limit on every supported Darwin build.  Lower the effective soft limit
        # before parsing; the controller never depends on a child hard-limit claim.
        effective_soft = min(effective, soft) if soft != resource.RLIM_INFINITY else effective
        try:
            resource.setrlimit(which, (effective_soft, hard))
        except ValueError as exc:
            raise ImporterError(f"cannot set child resource limit {which}") from exc

    cap(resource.RLIMIT_CPU, max(1, int(limits.max_wall_time_s)))
    cap(resource.RLIMIT_FSIZE, limits.max_total_bytes)
    cap(resource.RLIMIT_NOFILE, limits.max_open_descriptors)
    if hasattr(resource, "RLIMIT_AS"):
        address = max(limits.max_file_bytes * 2, min(limits.max_total_bytes, 256 * 1024 * 1024))
        try:
            cap(resource.RLIMIT_AS, address)
        except ImporterError:
            # Darwin cannot lower RLIMIT_AS from an unlimited non-root parent.  This
            # is permitted only for the explicit hermetic seam or repository-owned
            # structural helper. Task 14's privileged helper must establish the
            # address-space limit before exec.
            if not allow_darwin_address_space_gap:
                raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request-fd", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        request = _read_request(args.request_fd)
        if not isinstance(request["structural_identity"], bool):
            raise ImporterError("import child structural identity mode is malformed")
        expected_uid = request["effective_uid"] if request["structural_identity"] else request["expected_uid"]
        if expected_uid != os.getuid():
            raise ImporterError("import child UID does not match the launch binding")
        limits = _limits(request["limits"])
        _apply_limits(
            limits,
            allow_darwin_address_space_gap=(
                request["test_unprofiled"] is True or request["structural_identity"] is True
            ),
        )
        result = _import_from_descriptor(
            int(request["source_fd"]), Path(request["destination"]), limits=limits,
            candidate_ref=request["candidate_ref"],
        )
        response = {
            "version": "implbench-import-child-v1", "ok": True, "pid": os.getpid(), "uid": os.getuid(),
            "profile_digest": request["profile_digest"], "template_digest": request["template_digest"],
            "limits": limits.__dict__,
            "result": {"bundle": str(result.bundle), "bundle_digest": result.bundle_digest, "files": result.files,
                       "bytes": result.bytes, "object_ids": list(result.object_ids)},
        }
        encoded = json.dumps(response, sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) > 65536:
            raise ImporterError("import child response exceeds its bound")
        sys.stdout.buffer.write(encoded)
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
