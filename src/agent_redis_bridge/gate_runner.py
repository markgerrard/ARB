"""Digest-pinned, contained execution for gate-first validator scripts."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence, TextIO

try:
    import resource
except ImportError:  # pragma: no cover - the project supports Unix hosts
    resource = None  # type: ignore[assignment]


EXIT_GREEN = 0
EXIT_RED = 1
EXIT_DIGEST_REFUSAL = 3
EXIT_INVALID_BASELINE = 4
EXIT_GATE_ERROR = 5
EXIT_TIMEOUT = 6
EXIT_VACUOUS_BASELINE = 7

_CHECK_ID = r"[A-Za-z0-9_.:-]+"
_HEADER_RE = re.compile(
    rf"^CHECK\[(?P<id>{_CHECK_ID})\]: class=(?P<class>delta|invariant)"
    r"(?: baseline-exempt=(?P<reason>\S.*))?$"
)
_RESULT_RE = re.compile(
    rf"^(?P<status>PASS|FAIL)\[(?P<id>{_CHECK_ID})\]: (?P<message>.*)$"
)
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BASE_ENV = ("PATH", "HOME", "TMPDIR", "LANG")


@dataclass
class GateCheck:
    id: str
    check_class: str
    status: str
    message: str
    baseline_exempt: str | None = None


@dataclass(frozen=True)
class DirtSnapshot:
    fingerprint: str
    status: str


@dataclass
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool


class GateRunnerError(RuntimeError):
    """An attributed gate-runner failure."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run-gate",
        description="Run a digest-pinned Python acceptance gate through uv.",
        epilog=(
            "exit codes: 0 GREEN/valid baseline; 1 normal RED; "
            "2 CLI usage; 3 digest refusal; 4 RED-INVALID baseline; "
            "5 gate/runner error; 6 timeout; 7 VACUOUS baseline"
        ),
    )
    parser.add_argument("--gate", required=True, type=Path, help="validator gate script")
    parser.add_argument("--project", required=True, type=Path, help="git project root")
    parser.add_argument(
        "--baseline", action="store_true", help="enforce per-check baseline semantics"
    )
    pinning = parser.add_mutually_exclusive_group()
    pinning.add_argument("--pin", help="expected lowercase sha256 digest")
    pinning.add_argument(
        "--repin",
        action="store_true",
        help="append the current digest to <gate>.sha256 before running",
    )
    parser.add_argument("--timeout", type=float, default=60.0, help="wall timeout in seconds")
    parser.add_argument("--json", dest="json_path", type=Path, help="write JSON summary")
    parser.add_argument(
        "--pass-env",
        action="append",
        default=[],
        metavar="NAME",
        help="explicitly pass one additional parent environment variable (repeatable)",
    )
    return parser


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sidecar_path(gate: Path) -> Path:
    return Path(f"{gate}.sha256")


def _append_pin(sidecar: Path, digest: str) -> None:
    timestamp = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    with sidecar.open("a", encoding="utf-8") as handle:
        handle.write(f"{timestamp} {digest}\n")


def _latest_pin(sidecar: Path) -> str:
    try:
        lines = [line.strip() for line in sidecar.read_text(encoding="utf-8").splitlines()]
    except FileNotFoundError as exc:
        raise GateRunnerError(
            f"digest sidecar missing: {sidecar}; use --repin or --pin"
        ) from exc
    records = [line for line in lines if line]
    if not records:
        raise GateRunnerError(f"digest sidecar has no history: {sidecar}")
    digest = records[-1].split()[-1]
    if not _SHA256_RE.fullmatch(digest):
        raise GateRunnerError(f"digest sidecar has an invalid latest record: {sidecar}")
    return digest


def _expected_digest(gate: Path, explicit_pin: str | None, repin: bool) -> tuple[str, str]:
    current = sha256_file(gate)
    if repin:
        _append_pin(_sidecar_path(gate), current)
        return current, current
    if explicit_pin is not None:
        normalized = explicit_pin.lower()
        if explicit_pin != normalized or not _SHA256_RE.fullmatch(normalized):
            raise GateRunnerError("--pin must be exactly 64 lowercase hexadecimal characters")
        return normalized, current
    return _latest_pin(_sidecar_path(gate)), current


def _git(project: Path, *args: str) -> bytes:
    proc = subprocess.run(
        ["git", "-C", str(project), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise GateRunnerError(f"git {' '.join(args)} failed for {project}: {detail}")
    return proc.stdout


def _untracked_fingerprint(project: Path) -> bytes:
    digest = hashlib.sha256()
    paths = _git(project, "ls-files", "--others", "-z").split(b"\0")
    for raw_path in sorted(path for path in paths if path):
        path = project / os.fsdecode(raw_path)
        digest.update(raw_path)
        digest.update(b"\0")
        mode = path.lstat().st_mode
        digest.update(mode.to_bytes(4, "big"))
        if path.is_symlink():
            digest.update(b"L")
            digest.update(os.fsencode(os.readlink(path)))
        elif path.is_file():
            digest.update(b"F")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            digest.update(b"MISSING")
        digest.update(b"\0")
    return digest.digest()


def _hash_filesystem_path(digest: Any, label: bytes, path: Path) -> None:
    digest.update(label)
    digest.update(b"\0")
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        digest.update(b"MISSING\0")
        return
    digest.update(mode.to_bytes(4, "big"))
    if path.is_symlink():
        digest.update(b"L")
        digest.update(os.fsencode(os.readlink(path)))
    elif path.is_file():
        digest.update(b"F")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    elif path.is_dir():
        digest.update(b"D")
        for child in sorted(path.iterdir(), key=lambda item: os.fsencode(item.name)):
            _hash_filesystem_path(digest, os.fsencode(child.name), child)
    else:
        digest.update(b"OTHER")
    digest.update(b"\0")


def _resolve_git_path(project: Path, *rev_parse_args: str) -> Path:
    raw_path = _git(project, "rev-parse", *rev_parse_args).strip()
    path = Path(os.fsdecode(raw_path))
    if not path.is_absolute():
        path = project / path
    return path.resolve()


def _active_hooks_path(project: Path) -> Path:
    proc = subprocess.run(
        ["git", "-C", str(project), "config", "--path", "--get", "core.hooksPath"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode == 1:
        return _resolve_git_path(project, "--git-path", "hooks")
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise GateRunnerError(
            f"git config --path --get core.hooksPath failed for {project}: {detail}"
        )
    path = Path(os.fsdecode(proc.stdout.strip()))
    if not path.is_absolute():
        path = project / path
    return path.resolve()


def _git_internal_fingerprint(project: Path) -> bytes:
    digest = hashlib.sha256()
    targets = (
        ("git-dir", _resolve_git_path(project, "--git-dir")),
        ("git-common-dir", _resolve_git_path(project, "--git-common-dir")),
        ("active-hooks", _active_hooks_path(project)),
    )
    unique_paths: dict[Path, None] = {}
    for label, path in targets:
        digest.update(label.encode("ascii"))
        digest.update(b"\0")
        digest.update(os.fsencode(path))
        digest.update(b"\0")
        unique_paths[path] = None
    for path in sorted(unique_paths, key=os.fsencode):
        _hash_filesystem_path(digest, os.fsencode(path), path)
    return digest.digest()


def dirt_snapshot(project: Path) -> DirtSnapshot:
    status_raw = _git(
        project,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignored",
    )
    # Unlike `git stash create`, these snapshots do not add objects that the
    # Git-internals fingerprint would then correctly identify as a mutation.
    worktree_diff = _git(
        project, "diff", "--binary", "--no-ext-diff", "--no-textconv", "--"
    )
    index_diff = _git(
        project,
        "diff",
        "--cached",
        "--binary",
        "--no-ext-diff",
        "--no-textconv",
        "--",
    )

    digest = hashlib.sha256()
    for part in (
        status_raw,
        worktree_diff,
        index_diff,
        _untracked_fingerprint(project),
        _git_internal_fingerprint(project),
    ):
        digest.update(part)
        digest.update(b"\0")
    return DirtSnapshot(
        fingerprint=digest.hexdigest(),
        status=status_raw.decode("utf-8", errors="backslashreplace").replace("\0", "\\0"),
    )


def parse_checks(output: str) -> tuple[list[GateCheck], list[str]]:
    headers: dict[str, tuple[str, str | None]] = {}
    results: dict[str, tuple[str, str]] = {}
    errors: list[str] = []

    for line_number, line in enumerate(output.splitlines(), start=1):
        header = _HEADER_RE.fullmatch(line)
        if header:
            check_id = header.group("id")
            if check_id in headers:
                errors.append(f"duplicate CHECK header for {check_id} at line {line_number}")
            else:
                check_class = header.group("class")
                reason = header.group("reason")
                if reason is not None and check_class != "delta":
                    errors.append(f"baseline-exempt is only valid for delta check {check_id}")
                headers[check_id] = (check_class, reason)
            continue

        result = _RESULT_RE.fullmatch(line)
        if result:
            check_id = result.group("id")
            if check_id not in headers:
                errors.append(f"result for {check_id} appears before its CHECK header")
            elif check_id in results:
                errors.append(f"duplicate result for {check_id} at line {line_number}")
            else:
                results[check_id] = (result.group("status"), result.group("message"))
            continue

        if line.startswith(("CHECK[", "PASS[", "FAIL[")):
            errors.append(f"malformed gate protocol line {line_number}: {line}")

    if not headers:
        errors.append("gate declared no checks")
    for check_id in headers:
        if check_id not in results:
            errors.append(f"check {check_id} has no result")
    for check_id in results:
        if check_id not in headers:
            errors.append(f"result {check_id} has no CHECK header")

    checks = [
        GateCheck(
            id=check_id,
            check_class=check_class,
            status=results[check_id][0],
            message=results[check_id][1],
            baseline_exempt=reason,
        )
        for check_id, (check_class, reason) in headers.items()
        if check_id in results
    ]
    return checks, errors


def _sandbox_env(project: Path, passthrough: Sequence[str]) -> dict[str, str]:
    env = {name: os.environ[name] for name in _BASE_ENV if name in os.environ}
    if "PATH" not in env:
        env["PATH"] = os.defpath
    env["GATE_PROJECT"] = str(project)
    for name in passthrough:
        if not _ENV_NAME_RE.fullmatch(name):
            raise GateRunnerError(f"invalid --pass-env name: {name!r}")
        if name == "GATE_PROJECT":
            raise GateRunnerError("GATE_PROJECT is runner-owned and cannot be passed through")
        if name not in os.environ:
            raise GateRunnerError(f"--pass-env variable is absent from the parent: {name}")
        env[name] = os.environ[name]
    return env


def _unshare_probe(prefix: Sequence[str], env: dict[str, str], true_binary: str) -> bool:
    try:
        probe = subprocess.run(
            [*prefix, true_binary],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def _process_prefix(env: dict[str, str]) -> tuple[list[str], str]:
    if sys.platform.startswith("linux"):
        unshare = shutil.which("unshare", path=env["PATH"])
        true_binary = next(
            (path for path in ("/usr/bin/true", "/bin/true") if Path(path).exists()), None
        )
        if unshare and true_binary:
            common = [
                "--pid",
                "--fork",
                "--mount-proc",
                "--kill-child=SIGKILL",
                "--",
            ]
            candidates = (
                [unshare, "--user", "--map-root-user", *common],
                [unshare, *common],
            )
            for prefix in candidates:
                if _unshare_probe(prefix, env, true_binary):
                    return prefix, "linux-pid-namespace"
        return [], "UNAVAILABLE: Linux PID namespace could not be enabled"
    if sys.platform == "darwin":
        return (
            [],
            "UNAVAILABLE: macOS cannot reap double-forked grandchildren; "
            "a detached gate subprocess may outlive the timeout",
        )
    return [], f"UNAVAILABLE: no process isolation implementation for {sys.platform}"


def _network_prefix(
    env: dict[str, str], process_prefix: Sequence[str] = ()
) -> tuple[list[str], str]:
    if sys.platform.startswith("linux"):
        unshare = shutil.which("unshare", path=env["PATH"])
        true_binary = next(
            (path for path in ("/usr/bin/true", "/bin/true") if Path(path).exists()), None
        )
        if unshare and true_binary:
            prefix = [unshare, "-n", "--"]
            if _unshare_probe([*process_prefix, *prefix], env, true_binary):
                return prefix, "linux-unshare"
        return [], "UNAVAILABLE: Linux network namespace could not be enabled"
    if sys.platform == "darwin":
        return [], "UNAVAILABLE: macOS has no unshare network namespace"
    return [], f"UNAVAILABLE: no network isolation implementation for {sys.platform}"


def _resource_limiter(timeout: float):
    if resource is None:
        return None

    def apply_limits() -> None:
        requested = [
            ("RLIMIT_CPU", max(1, math.ceil(timeout) + 2)),
            ("RLIMIT_AS", 2 * 1024 * 1024 * 1024),
            ("RLIMIT_FSIZE", 64 * 1024 * 1024),
            ("RLIMIT_NOFILE", 256),
        ]
        for name, soft_limit in requested:
            limit_id = getattr(resource, name, None)
            if limit_id is None:
                continue
            try:
                _, hard = resource.getrlimit(limit_id)
                effective = soft_limit if hard == resource.RLIM_INFINITY else min(soft_limit, hard)
                resource.setrlimit(limit_id, (effective, hard))
            except (OSError, ValueError):
                pass

    return apply_limits


def _pump(pipe: TextIO, sink: TextIO, chunks: list[str]) -> None:
    try:
        for chunk in iter(lambda: pipe.read(8192), ""):
            chunks.append(chunk)
            sink.write(chunk)
            sink.flush()
    finally:
        pipe.close()


def _terminate_process_group(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=1.0)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    proc.wait()


def run_process(
    command: Sequence[str], cwd: Path, env: dict[str, str], timeout: float
) -> ProcessResult:
    started = time.monotonic()
    proc = subprocess.Popen(
        list(command),
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        bufsize=1,
        start_new_session=True,
        preexec_fn=_resource_limiter(timeout),
    )
    assert proc.stdout is not None
    assert proc.stderr is not None
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    stdout_thread = threading.Thread(
        target=_pump, args=(proc.stdout, sys.stdout, stdout_chunks), daemon=True
    )
    stderr_thread = threading.Thread(
        target=_pump, args=(proc.stderr, sys.stderr, stderr_chunks), daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_group(proc)
    stdout_thread.join()
    stderr_thread.join()
    return ProcessResult(
        returncode=proc.returncode,
        stdout="".join(stdout_chunks),
        stderr="".join(stderr_chunks),
        duration_seconds=time.monotonic() - started,
        timed_out=timed_out,
    )


def _write_summary(path: Path | None, summary: dict[str, object]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _error_summary(
    *, gate: Path, project: Path, digest: str | None, verdict: str, exit_code: int, error: str
) -> dict[str, object]:
    return {
        "gate": str(gate),
        "project": str(project),
        "digest": digest,
        "duration_seconds": 0.0,
        "dirt_check": "not-run",
        "checks": [],
        "gate_exit_code": None,
        "exit_code": exit_code,
        "verdict": verdict,
        "error": error,
    }


def _check_summary(check: GateCheck) -> dict[str, str | None]:
    return {
        "id": check.id,
        "class": check.check_class,
        "status": check.status,
        "message": check.message,
        "baseline_exempt": check.baseline_exempt,
    }


def _print_error(kind: str, gate: Path, message: str) -> None:
    print(f"[run-gate] {kind}: gate={gate}: {message}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    gate = args.gate.expanduser().resolve()
    project = args.project.expanduser().resolve()
    json_path = args.json_path.expanduser().resolve() if args.json_path else None

    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if not gate.is_file():
        message = "gate path is not a regular file"
        _print_error("GATE ERROR", gate, message)
        _write_summary(
            json_path,
            _error_summary(
                gate=gate,
                project=project,
                digest=None,
                verdict="GATE-ERROR",
                exit_code=EXIT_GATE_ERROR,
                error=message,
            ),
        )
        return EXIT_GATE_ERROR

    digest: str | None = None
    try:
        expected, digest = _expected_digest(gate, args.pin, args.repin)
        if digest != expected:
            raise GateRunnerError(f"digest mismatch: expected {expected}, found {digest}")
    except (GateRunnerError, OSError) as exc:
        message = str(exc)
        _print_error("DIGEST REFUSAL", gate, message)
        _write_summary(
            json_path,
            _error_summary(
                gate=gate,
                project=project,
                digest=digest,
                verdict="DIGEST-REFUSAL",
                exit_code=EXIT_DIGEST_REFUSAL,
                error=message,
            ),
        )
        return EXIT_DIGEST_REFUSAL

    try:
        env = _sandbox_env(project, args.pass_env)
        uv = shutil.which("uv", path=env["PATH"])
        if uv is None:
            raise GateRunnerError("required executable 'uv' was not found on sanitized PATH")
        before = dirt_snapshot(project)
        process_prefix, process_isolation = _process_prefix(env)
        network_prefix, network_isolation = _network_prefix(env, process_prefix)
        if process_isolation.startswith("UNAVAILABLE"):
            print(f"[run-gate] PROCESS ISOLATION GAP: {process_isolation}", file=sys.stderr)
        if network_isolation.startswith("UNAVAILABLE"):
            print(f"[run-gate] NETWORK ISOLATION GAP: {network_isolation}", file=sys.stderr)
        with tempfile.TemporaryDirectory(prefix="arb-run-gate-") as temp_dir:
            result = run_process(
                [*process_prefix, *network_prefix, uv, "run", str(gate)],
                Path(temp_dir),
                env,
                args.timeout,
            )
        after = dirt_snapshot(project)
        final_digest = sha256_file(gate)
    except (GateRunnerError, OSError, subprocess.SubprocessError) as exc:
        message = str(exc)
        _print_error("GATE ERROR", gate, message)
        _write_summary(
            json_path,
            _error_summary(
                gate=gate,
                project=project,
                digest=digest,
                verdict="GATE-ERROR",
                exit_code=EXIT_GATE_ERROR,
                error=message,
            ),
        )
        return EXIT_GATE_ERROR

    checks, parse_errors = parse_checks(result.stdout)
    dirt_clean = before.fingerprint == after.fingerprint
    summary: dict[str, object] = {
        "gate": str(gate),
        "project": str(project),
        "digest": digest,
        "duration_seconds": round(result.duration_seconds, 6),
        "process_isolation": process_isolation,
        "network_isolation": network_isolation,
        "dirt_check": "clean" if dirt_clean else "mutated",
        "dirt_before": asdict(before),
        "dirt_after": asdict(after),
        "checks": [_check_summary(check) for check in checks],
        "gate_exit_code": result.returncode,
        "baseline": bool(args.baseline),
    }

    if final_digest != digest:
        exit_code = EXIT_DIGEST_REFUSAL
        verdict = "DIGEST-REFUSAL"
        error = f"gate digest changed during execution: pinned {digest}, found {final_digest}"
        _print_error("DIGEST REFUSAL", gate, error)
    elif not dirt_clean:
        exit_code = EXIT_GATE_ERROR
        verdict = "GATE-ERROR"
        error = "gate mutated the project; before/after dirt fingerprints differ"
        _print_error("PROJECT MUTATION", gate, error)
    elif result.timed_out:
        exit_code = EXIT_TIMEOUT
        verdict = "TIMEOUT"
        error = f"wall timeout exceeded ({args.timeout:g}s); process group terminated"
        _print_error("TIMEOUT", gate, error)
    elif result.returncode < 0:
        exit_code = EXIT_GATE_ERROR
        verdict = "GATE-ERROR"
        error = f"gate terminated by signal {-result.returncode}"
        _print_error("GATE ERROR", gate, error)
    elif parse_errors:
        exit_code = EXIT_GATE_ERROR
        verdict = "GATE-ERROR"
        error = "; ".join(parse_errors)
        _print_error("GATE PROTOCOL ERROR", gate, error)
    else:
        any_failed = any(check.status == "FAIL" for check in checks)
        exit_contract_broken = (any_failed and result.returncode == 0) or (
            not any_failed and result.returncode != 0
        )
        if exit_contract_broken:
            exit_code = EXIT_GATE_ERROR
            verdict = "GATE-ERROR"
            error = (
                "gate exit-code contract violated: exit 0 iff all declared checks pass; "
                f"gate exited {result.returncode}"
            )
            _print_error("GATE PROTOCOL ERROR", gate, error)
        elif args.baseline:
            exemptions = [check for check in checks if check.baseline_exempt is not None]
            non_exempt_deltas = [
                check
                for check in checks
                if check.check_class == "delta" and check.baseline_exempt is None
            ]
            offending = [
                check.id
                for check in checks
                if check.baseline_exempt is None
                and (
                    (check.check_class == "delta" and check.status != "FAIL")
                    or (check.check_class == "invariant" and check.status != "PASS")
                )
            ]
            for check in exemptions:
                print(
                    f"[run-gate] BASELINE-EXEMPT[{check.id}]: {check.baseline_exempt}",
                    file=sys.stderr,
                )
            summary["baseline_exemptions"] = [
                {"id": check.id, "reason": check.baseline_exempt} for check in exemptions
            ]
            summary["baseline_offending_checks"] = offending
            if not non_exempt_deltas:
                exit_code = EXIT_VACUOUS_BASELINE
                verdict = "VACUOUS"
                error = "baseline declared no non-exempt delta checks"
                _print_error("VACUOUS", gate, error)
            elif offending:
                exit_code = EXIT_INVALID_BASELINE
                verdict = "RED-INVALID"
                error = f"baseline class mismatch for checks: {', '.join(offending)}"
                _print_error("RED-INVALID", gate, error)
            else:
                exit_code = EXIT_GREEN
                verdict = "BASELINE-VALID"
                error = None
                print("[run-gate] BASELINE-VALID", file=sys.stderr)
        elif any_failed:
            exit_code = EXIT_RED
            verdict = "RED"
            error = None
        else:
            exit_code = EXIT_GREEN
            verdict = "GREEN"
            error = None

    summary["exit_code"] = exit_code
    summary["verdict"] = verdict
    if error is not None:
        summary["error"] = error
    try:
        _write_summary(json_path, summary)
    except OSError as exc:
        _print_error("GATE ERROR", gate, f"could not write JSON summary: {exc}")
        return EXIT_GATE_ERROR
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
