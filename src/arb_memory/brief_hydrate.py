"""Worker-side pinned-brief hydration via the local-reader DSN.

CLI: ``arb-memory-brief-hydrate --artefact-id ID --version N --output PATH --receipt PATH``

The helper alone opens ``ARB_MEMORY_LOCAL_DSN`` through ``local_read_policy.local_read_dsn``.
It never uses the gate-reader role. Domain-separated ``artefact_hash`` is compared to the
fetched row's ``content_hash``. Atomic O_EXCL temp+fsync+replace for body and receipt;
mode 0600 files. Prints only receipt metadata (never body).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Mapping

from arb_memory.hash import artefact_hash
from arb_memory.local_read_policy import local_read_dsn
from arb_memory.store import fetch_artefact

BRIEF_HYDRATION_MISSING = "brief_hydration_missing"
BRIEF_HYDRATION_HASH_MISMATCH = "brief_hydration_hash_mismatch"
BRIEF_HYDRATION_POLICY = "brief_hydration_policy"
BRIEF_HYDRATION_STORE = "brief_hydration_store"
BRIEF_HYDRATION_INVALID_ID = "brief_hydration_invalid_id"
BRIEF_HYDRATION_WRITE = "brief_hydration_write"
BRIEF_HYDRATION_INVALID_PATH = "brief_hydration_invalid_path"
BRIEF_HYDRATION_UNAVAILABLE = "brief_hydration_unavailable"


class BriefHydrationError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message or code
        super().__init__(self.message)


def validate_artefact_id(artefact_id: str) -> str:
    if not isinstance(artefact_id, str) or not artefact_id.strip():
        raise BriefHydrationError(BRIEF_HYDRATION_INVALID_ID, "blank artefact_id")
    if "\x00" in artefact_id:
        raise BriefHydrationError(BRIEF_HYDRATION_INVALID_ID, "NUL in artefact_id")
    if any(sep in artefact_id for sep in ("/", "\\")):
        raise BriefHydrationError(BRIEF_HYDRATION_INVALID_ID, "path separator in artefact_id")
    if ".." in artefact_id:
        raise BriefHydrationError(BRIEF_HYDRATION_INVALID_ID, "traversal in artefact_id")
    if artefact_id.startswith(("-",)):
        raise BriefHydrationError(BRIEF_HYDRATION_INVALID_ID, "option-shaped artefact_id")
    return artefact_id.strip()


def _refuse_symlink(path: Path) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink():
            raise BriefHydrationError(
                BRIEF_HYDRATION_INVALID_PATH, f"refusing symlink destination: {path}"
            )


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte of data to fd; short writes loop until complete."""
    view = memoryview(data)
    offset = 0
    total = len(data)
    while offset < total:
        n = os.write(fd, view[offset:])
        if n <= 0:
            raise OSError("os.write returned zero before all bytes were written")
        offset += n


def _atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """O_EXCL temp in the same directory, full write, fsync, size-check, replace.

    Guarantees no partial final file: loops until all bytes are written and
    verifies staged size == len(data) before os.replace.
    """
    _refuse_symlink(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    tmp_name = None
    try:
        fd = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        tmp_fd, tmp_name = fd
        # O_EXCL already applied by mkstemp.
        _write_all(tmp_fd, data)
        os.fsync(tmp_fd)
        staged_size = os.fstat(tmp_fd).st_size
        if staged_size != len(data):
            raise OSError(
                f"staged size {staged_size} != expected {len(data)} before replace"
            )
        os.close(tmp_fd)
        fd = None
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
        tmp_name = None
        # Best-effort dir fsync.
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except BriefHydrationError:
        raise
    except OSError as exc:
        raise BriefHydrationError(BRIEF_HYDRATION_WRITE, str(exc)) from exc
    finally:
        if fd is not None:
            try:
                os.close(fd[0])
            except OSError:
                pass
        if tmp_name is not None and os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def hydrate(
    *,
    artefact_id: str,
    version: int,
    output_path: str | Path,
    receipt_path: str | Path,
    env: Mapping[str, str] | None = None,
) -> dict:
    """Fetch exact version as local reader; stage body + metadata-only receipt."""
    art_id = validate_artefact_id(artefact_id)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise BriefHydrationError(BRIEF_HYDRATION_INVALID_ID, "version must be positive int")

    out = Path(output_path)
    receipt = Path(receipt_path)
    environ = env if env is not None else os.environ

    try:
        dsn = local_read_dsn(environ)  # type: ignore[arg-type]
    except RuntimeError as exc:
        raise BriefHydrationError(BRIEF_HYDRATION_POLICY, str(exc)) from exc

    try:
        import psycopg
        from pgvector.psycopg import register_vector
    except ImportError as exc:
        raise BriefHydrationError(BRIEF_HYDRATION_STORE, str(exc)) from exc

    try:
        conn = psycopg.connect(dsn)
    except Exception as exc:  # noqa: BLE001 - map all connect failures
        raise BriefHydrationError(BRIEF_HYDRATION_STORE, str(exc)) from exc

    try:
        conn.autocommit = True
        try:
            register_vector(conn)
        except Exception:
            pass
        try:
            row = fetch_artefact(conn, art_id, version)
        except Exception as exc:  # noqa: BLE001 - privilege / query failures
            raise BriefHydrationError(BRIEF_HYDRATION_STORE, str(exc)) from exc
    finally:
        conn.close()

    if row is None:
        raise BriefHydrationError(
            BRIEF_HYDRATION_MISSING,
            f"artefact {art_id!r} version {version} not found",
        )

    content = row.get("content")
    content_bytes = row.get("content_bytes")
    content_mime = row.get("content_mime") or "text/plain"
    stored_hash = row.get("content_hash")
    try:
        computed = artefact_hash(content, content_bytes, content_mime)
    except ValueError as exc:
        raise BriefHydrationError(BRIEF_HYDRATION_HASH_MISMATCH, str(exc)) from exc

    if computed != stored_hash:
        raise BriefHydrationError(
            BRIEF_HYDRATION_HASH_MISMATCH,
            "computed artefact_hash does not match stored content_hash",
        )

    if content_bytes is not None:
        body = bytes(content_bytes)
    else:
        body = (content or "").encode("utf-8")

    # Validate before publishing either final file.
    try:
        _atomic_write(out, body, mode=0o600)
        receipt_obj = {
            "artefact_id": art_id,
            "version": version,
            "content_hash": computed,
            "path": str(out),
        }
        _atomic_write(
            receipt,
            (json.dumps(receipt_obj, indent=2) + "\n").encode("utf-8"),
            mode=0o600,
        )
    except BriefHydrationError:
        # Leave no final file/receipt on failure.
        for p in (out, receipt):
            try:
                if p.exists() and not p.is_symlink():
                    p.unlink()
            except OSError:
                pass
        raise

    return dict(receipt_obj)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arb-memory-brief-hydrate")
    parser.add_argument(
        "--readiness-probe",
        action="store_true",
        help="Exit 0 if this console script is executable (no store I/O).",
    )
    parser.add_argument("--artefact-id")
    parser.add_argument("--version", type=int)
    parser.add_argument("--output")
    parser.add_argument("--receipt")
    args = parser.parse_args(argv)
    if args.readiness_probe:
        # Seat readiness: prove the bare console script runs, not just import.
        print(json.dumps({"ok": True, "readiness_probe": True}, separators=(",", ":")))
        return 0
    missing = [
        name
        for name, val in (
            ("--artefact-id", args.artefact_id),
            ("--version", args.version),
            ("--output", args.output),
            ("--receipt", args.receipt),
        )
        if val is None
    ]
    if missing:
        parser.error("the following arguments are required: " + ", ".join(missing))
    try:
        meta = hydrate(
            artefact_id=args.artefact_id,
            version=args.version,
            output_path=args.output,
            receipt_path=args.receipt,
            env=os.environ,
        )
    except BriefHydrationError as exc:
        print(json.dumps({"ok": False, "error": exc.code, "message": exc.message}), file=sys.stderr)
        return 1
    # Print only receipt metadata — never body.
    print(json.dumps(meta, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
