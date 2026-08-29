"""Bridge-seat lifecycle support plus the cross-mode local artefact lock."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parents[3]
DISPATCH = REPO / "scripts" / "dispatch-dev"
LEDGER = REPO / ".claude" / "faba-bridge-leases.json"
FORENSICS = REPO / ".claude" / "faba-forensics"
LOCKS = REPO / ".claude" / "faba-locks"
MAX_BOUNCES = 2
# Contract examples and checked-in FABA_EXIT occurrences are below 7 KiB; 1 MiB
# is deliberately far above a legitimate pointer or ordinary accidental body,
# while still bounding decoded text before it reaches a parent orchestrator.
REPLY_CHAR_CAP = 1_048_576
POST_REPLY_MARGIN = 300

# FALLBACK table only: seats on current bridge code advertise their actual
# capability in the arm reply (`thread_resume`), which overrides this map.
# Pinned from AgentEngine.supports_thread_resume; target ids are fleet names,
# so the engine family is the first matching prefix, never a guessed fallback.
# agent-sdk falls back to "stateless" because resume against a ONESHOT asdk
# seat is refused bridge-side as thread-affinity-worktree-incompatible (Chain
# B r2c, 2026-07-22) and the driver cannot see oneshot-ness without the
# advertisement — stateless is the safe degradation for the resumable
# minority (e.g. asdk-bridge-dev-haiku45 runs non-oneshot with
# supports_thread_resume=True; blanket-stateless silently cost it
# transcript-resume bounces — panel-pisdk-rebaseline finding, 2026-07-23).
BOUNCE_MODES = {
    "codex": "resume",
    "agent-sdk": "stateless",
    "pi-sdk": "stateless",
    "pi": "stateless",
    "grok": "stateless",
    "agy": "stateless",
    "devin": "stateless",
}
TERMINAL_ERRORS = (
    "worktree_escape",
    "worktree-lease-expired",
    "worktree-lease-unavailable",
    "worktree-lease-busy",
)

# A run_id is an AUDIT identity; the worktree name is a FILESYSTEM identity.
# They are deliberately decoupled: the audited run id travels verbatim into
# every dispatch (--run-id is never truncated), while the worktree name is
# bounded here so derived child run ids with compounded prefixes cannot be
# refused for length (observed live: panel-arb-role-polisher-v5-remediation
# synthesis refused pre-dispatch, 2026-07-22).
MAX_WORKTREE_RUN_ID = 60

# Mirrors agent_redis_bridge.engines._stdio.ENV_SCRUB_CAPABILITY (this module is
# deliberately decoupled from that package); a parity test keeps them in lockstep.
# An agent-sdk seat may run author rounds only when its registry entry advertises
# this capability, which the daemon sets once every SDK child-spawn path blanks
# bus credentials and asserts the blank at spawn time.
AGENT_SDK_ENV_SCRUB_CAPABILITY = "bus-and-gate-daemon-creds-v2"


class BridgeRoundError(RuntimeError):
    pass


@dataclass
class BridgeRoundResult:
    passed: bool
    reason: str
    workspace: Path | None
    attempts: int
    engine_family: str
    bounce_mode: str
    lease_id: str | None = None
    thread_id: str | None = None
    base_oid: str | None = None
    forensics_loss: bool = False
    events: list[dict] = field(default_factory=list)


def worktree_name_for_run(run_id: str) -> str:
    """Bounded, deterministic, collision-resistant worktree name for a run."""
    if len(run_id) <= MAX_WORKTREE_RUN_ID:
        return f"faba-{run_id}"
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:12]
    return f"faba-{run_id[:MAX_WORKTREE_RUN_ID - 13]}-{digest}"


def _parse_env_kv(env_file: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not env_file.is_file():
        return values
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip().strip('"')
    return values


def _registry_env_scrub(target_id: str, env_file: Path) -> str | None:
    """Read the seat's advertised env_scrub capability from the bus registry."""
    import redis  # deferred: only agent-sdk targets pay for it

    cfg = _parse_env_kv(env_file)
    client = redis.Redis(
        host=cfg.get("AGENT_REDIS_HOST") or "127.0.0.1",
        port=int(cfg.get("AGENT_REDIS_PORT") or 6379),
        db=int(cfg.get("AGENT_REDIS_DB") or 0),
        username=cfg.get("AGENT_REDIS_USER") or None,
        password=cfg.get("AGENT_REDIS_PASSWORD") or None,
        ssl=cfg.get("AGENT_REDIS_TLS") == "1",
        decode_responses=True,
        socket_timeout=10,
    )
    prefix = cfg.get("AGENT_REDIS_PREFIX") or "agent_scratch:"
    return client.hget(f"{prefix}registry:{target_id}", "env_scrub")


def require_agent_sdk_scrub(
    target_id: str, *, env_file: Path,
    registry_lookup: Callable[[str, Path], str | None] = _registry_env_scrub,
) -> None:
    advertised = registry_lookup(target_id, env_file)
    if advertised != AGENT_SDK_ENV_SCRUB_CAPABILITY:
        raise BridgeRoundError(
            f"agent-sdk seat {target_id!r} does not advertise "
            f"env_scrub={AGENT_SDK_ENV_SCRUB_CAPABILITY!r} (registry shows {advertised!r}); "
            "restart the seat onto bridge code with the SDK child bus-credential scrub"
        )


def engine_mode(target_id: str) -> tuple[str, str]:
    if target_id.startswith("seat:"):
        # Audit rosters label actors as seat:<target-id>; the bridge routes on the
        # bare fleet target. Observed live: a synthesis launch was refused for
        # passing the roster label form (polisher v5-remediation, 2026-07-22).
        raise BridgeRoundError(
            f"target {target_id!r} is an audit roster label, not a fleet target id; "
            f"drop the 'seat:' prefix and dispatch to {target_id[5:]!r}"
        )
    if target_id == "asdk" or target_id.startswith("asdk-"):
        return "agent-sdk", BOUNCE_MODES["agent-sdk"]
    for family, mode in BOUNCE_MODES.items():
        if target_id == family or target_id.startswith(family + "-"):
            return family, mode
    raise BridgeRoundError(
        f"target {target_id!r} is absent from the pinned supports_thread_resume table"
    )


def lease_ttl_lower_bound(turn_timeout: int) -> int:
    """Arm + stage + three turns + driver-side post-reply work."""
    return 60 + max(1, turn_timeout) * (MAX_BOUNCES + 1) + POST_REPLY_MARGIN


def acquire_local_lock(artefact_id: str):
    LOCKS.mkdir(parents=True, exist_ok=True)
    path = LOCKS / (hashlib.sha256(artefact_id.encode()).hexdigest() + ".lock")
    fh = path.open("a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        fh.close()
        raise BridgeRoundError(f"another local round holds artefact lock {artefact_id!r}") from exc
    return fh


def release_local_lock(fh) -> None:
    fcntl.flock(fh, fcntl.LOCK_UN)
    fh.close()


def _dispatch(
    *, target_id: str, env_file: Path, run_id: str, operation: str,
    timeout: int, task: str | None = None, worktree: str | None = None,
    base_oid: str | None = None, lease_id: str | None = None,
    lease_ttl: int | None = None, thread_id: str | None = None,
    turn_timeout: int | None = None,
) -> dict:
    cmd = [str(DISPATCH), "--target-id", target_id, "--run-id", run_id,
           "--timeout", str(timeout), "--operation", operation]
    if worktree:
        cmd += ["--worktree", worktree, "--worktree-base", base_oid or "HEAD",
                "--worktree-cleanup", "keep"]
    if lease_id:
        cmd += ["--worktree-lease", lease_id]
    if lease_ttl is not None:
        cmd += ["--lease-ttl", str(lease_ttl)]
    if thread_id:
        cmd += ["--thread-id", thread_id]
    if turn_timeout is not None:
        cmd += ["--turn-timeout", str(turn_timeout)]
    if task is not None:
        cmd.append(task)
    from agent_redis_bridge.dispatch_authority import pop_publish_env

    env = dict(os.environ)
    env["AGENT_ENV_FILE"] = str(env_file)
    pop_publish_env(env)
    proc = subprocess.run(cmd, cwd=REPO, env=env, capture_output=True, text=True,
                          timeout=timeout + 30)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise BridgeRoundError(
            f"bridge {operation} returned non-JSON output (exit {proc.returncode}): "
            f"{proc.stderr.strip()}"
        ) from exc
    payload["_returncode"] = proc.returncode
    payload["_stderr"] = proc.stderr
    match = re.search(r"^task-id:\s*(\S+)", proc.stderr, re.MULTILINE)
    if match:
        payload["_task_id"] = match.group(1)
    return payload


def _load_ledger(path: Path) -> list[dict]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _write_ledger(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


@contextmanager
def _locked_ledger(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock").open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _add_ledger_row(path: Path, row: dict) -> None:
    with _locked_ledger(path):
        rows = _load_ledger(path)
        rows.append(row)
        _write_ledger(path, rows)


def _remove_ledger_lease(path: Path, lease_id: str) -> None:
    with _locked_ledger(path):
        rows = [row for row in _load_ledger(path) if row.get("lease_id") != lease_id]
        _write_ledger(path, rows)


def _process_identity(pid: int) -> str | None:
    """Return an OS-reported process start token to disambiguate PID reuse."""
    try:
        proc = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return None
    token = proc.stdout.strip()
    return token if proc.returncode == 0 and token else None


def sweep_ledger(*, ledger_path: Path = LEDGER, dispatch: Callable = _dispatch) -> list[str]:
    """Release only leases previously armed by these FABA drivers."""
    with _locked_ledger(ledger_path):
        rows = _load_ledger(ledger_path)
        remaining, released = [], []
        for row in rows:
            owner_pid = row.get("pid")
            if isinstance(owner_pid, int) and owner_pid != os.getpid():
                try:
                    os.kill(owner_pid, 0)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    remaining.append(row)
                    continue
                else:
                    recorded_identity = row.get("pid_start")
                    current_identity = _process_identity(owner_pid)
                    if not recorded_identity or not current_identity or current_identity == recorded_identity:
                        # Another live driver owns this row; legacy rows without
                        # a start token retain their conservative behaviour.
                        remaining.append(row)
                        continue
            try:
                reply = dispatch(
                    target_id=row["target_id"], env_file=Path(row["env_file"]),
                    run_id=row["run_id"], operation="worktree_release", timeout=120,
                    lease_id=row["lease_id"],
                )
                if reply.get("ok") or "unavailable" in str(reply.get("error", "")):
                    released.append(row["lease_id"])
                else:
                    remaining.append(row)
            except Exception:
                remaining.append(row)
        _write_ledger(ledger_path, remaining)
    return released


def _open_beneath(root: Path, relative: str, *, directory: bool) -> int:
    """Open relative to a trusted root fd; no component may be a symlink."""
    parts = Path(relative).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise BridgeRoundError(f"unsafe relative path {relative!r}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    try:
        current = os.open(root, flags | directory_flag)
        for index, part in enumerate(parts):
            want_directory = index < len(parts) - 1 or directory
            next_fd = os.open(
                part,
                flags | (directory_flag if want_directory else nonblock),
                dir_fd=current,
            )
            os.close(current)
            current = next_fd
        mode = os.fstat(current).st_mode
        if directory and not stat.S_ISDIR(mode):
            raise BridgeRoundError(f"seat path is not a directory: {relative}")
        if not directory and not stat.S_ISREG(mode):
            raise BridgeRoundError(f"seat path is not a regular file: {relative}")
        return current
    except (OSError, BridgeRoundError) as exc:
        if "current" in locals():
            try:
                os.close(current)
            except OSError:
                pass
        if isinstance(exc, BridgeRoundError):
            raise
        raise BridgeRoundError(f"symlink/escape refused in seat-owned path {relative!r}: {exc}") from exc


def safe_read(root: Path, relative: str) -> str:
    fd = _open_beneath(root, relative, directory=False)
    try:
        with os.fdopen(fd, "r", encoding="utf-8", newline="", closefd=False) as fh:
            return fh.read()
    finally:
        os.close(fd)


def safe_read_bytes(root: Path, relative: str) -> bytes:
    fd = _open_beneath(root, relative, directory=False)
    try:
        with os.fdopen(fd, "rb", closefd=False) as fh:
            return fh.read()
    finally:
        os.close(fd)


def safe_write_bytes(root: Path, relative: str, content: bytes) -> None:
    """Write beneath a trusted root without following seat-planted symlinks."""
    parts = Path(relative).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise BridgeRoundError(f"unsafe relative path {relative!r}")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    current = os.open(root, os.O_RDONLY | nofollow | directory_flag)
    try:
        for part in parts[:-1]:
            try:
                next_fd = os.open(part, os.O_RDONLY | nofollow | directory_flag, dir_fd=current)
            except FileNotFoundError:
                os.mkdir(part, dir_fd=current)
                next_fd = os.open(part, os.O_RDONLY | nofollow | directory_flag, dir_fd=current)
            os.close(current)
            current = next_fd
        fd = os.open(
            parts[-1], os.O_WRONLY | os.O_CREAT | os.O_TRUNC | nofollow | nonblock,
            0o600, dir_fd=current,
        )
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise BridgeRoundError(f"seat path is not a regular file: {relative}")
            with os.fdopen(fd, "wb", closefd=False) as fh:
                fh.write(content)
        finally:
            os.close(fd)
    except (OSError, BridgeRoundError) as exc:
        if isinstance(exc, BridgeRoundError):
            raise
        raise BridgeRoundError(f"symlink/escape refused in seat-owned path {relative!r}: {exc}") from exc
    finally:
        os.close(current)


def _copy_dir_fd(source_fd: int, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    for name in os.listdir(source_fd):
        info = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise BridgeRoundError(f"symlink refused in seat-owned forensics tree: {name}")
        if stat.S_ISDIR(info.st_mode):
            child_fd = os.open(name, os.O_RDONLY | nofollow | directory_flag, dir_fd=source_fd)
            try:
                _copy_dir_fd(child_fd, destination / name)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(info.st_mode):
            child_fd = os.open(name, os.O_RDONLY | nofollow, dir_fd=source_fd)
            try:
                with os.fdopen(os.dup(child_fd), "rb") as source_fh:
                    with (destination / name).open("wb") as target_fh:
                        shutil.copyfileobj(source_fh, target_fh)
            finally:
                os.close(child_fd)
        else:
            raise BridgeRoundError(f"non-regular entry refused in seat-owned tree: {name}")


def safe_copy_tree(root: Path, relative: str, destination: Path) -> None:
    """Descriptor-anchored forensics copy; symlinks and swap escapes fail."""
    source_fd = _open_beneath(root, relative, directory=True)
    try:
        _copy_dir_fd(source_fd, destination)
    finally:
        os.close(source_fd)


def _hash_files(root: Path, workspace_relative: str, files: dict[str, bytes]) -> dict[str, str]:
    return {
        name: hashlib.sha256(safe_read_bytes(root, str(Path(workspace_relative) / name))).hexdigest()
        for name in files
    }


def _stage(root: Path, workspace_relative: str, files: dict[str, bytes]) -> dict[str, str]:
    for name, content in files.items():
        safe_write_bytes(root, str(Path(workspace_relative) / name), content)
    manifest = _hash_files(root, workspace_relative, files)
    safe_write_bytes(
        root, str(Path(workspace_relative) / "staged-input-manifest.json"),
        (json.dumps(manifest, indent=2) + "\n").encode(),
    )
    return manifest


def _verify(root: Path, workspace_relative: str, manifest: dict[str, str]) -> list[str]:
    changed = []
    for name, digest in manifest.items():
        try:
            now = hashlib.sha256(
                safe_read_bytes(root, str(Path(workspace_relative) / name))
            ).hexdigest()
        except BridgeRoundError:
            now = None
        if now != digest:
            changed.append(name)
    return changed


def _exclude_faba(worktree: Path) -> None:
    proc = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--git-path", "info/exclude"],
        capture_output=True, text=True, check=True,
    )
    exclude = Path(proc.stdout.strip())
    if not exclude.is_absolute():
        exclude = worktree / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    line = "/.faba/"
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if line not in existing.splitlines():
        with exclude.open("a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(line + "\n")


def _reply_shape(reply: str, expected_key: str, expected_id: str) -> list[str]:
    lines = reply.strip().splitlines()
    if len(lines) != 1 or not lines[0].startswith("FABA_EXIT "):
        return [
            "return-channel rule violated: reply must contain ONLY the single FABA_EXIT line; "
            "write the artefact body to the workspace file"
        ]
    try:
        pointer = json.loads(lines[0][len("FABA_EXIT "):])
    except json.JSONDecodeError:
        return ["return-channel rule violated: FABA_EXIT payload is not valid JSON"]
    if pointer.get(expected_key) != expected_id:
        return [f"FABA_EXIT {expected_key} {pointer.get(expected_key)!r} != expected {expected_id!r}"]
    return []


def run_bridge_round(
    *, target_id: str, env_file: Path, run_id: str, artefact_lock_id: str,
    base_oid: str, lease_ttl: int, turn_timeout: int, staged_files: dict[str, bytes],
    output_name: str, expected_exit_key: str, expected_exit_id: str,
    make_brief: Callable[[Path], str], validate: Callable[[str], list[str]],
    publish: Callable[[Path], tuple], ledger_path: Path = LEDGER,
    forensics_root: Path = FORENSICS, dispatch: Callable = _dispatch,
    registry_lookup: Callable[[str, Path], str | None] = _registry_env_scrub,
) -> tuple[BridgeRoundResult, tuple | None]:
    family, mode = engine_mode(target_id)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", run_id):
        raise BridgeRoundError("--run-id charset is not safe (worktree/audit identity)")
    if family == "agent-sdk":
        require_agent_sdk_scrub(target_id, env_file=env_file, registry_lookup=registry_lookup)
    minimum = lease_ttl_lower_bound(turn_timeout)
    if lease_ttl < minimum:
        raise BridgeRoundError(f"lease TTL {lease_ttl}s is below driver lower bound {minimum}s")

    sweep_ledger(ledger_path=ledger_path, dispatch=dispatch)
    lock_fh = acquire_local_lock(artefact_lock_id)

    lease_id = None
    events: list[dict] = []
    forensic = forensics_root / run_id
    publish_result = None
    forensics_loss = False
    try:
        arm = dispatch(
            target_id=target_id, env_file=env_file, run_id=run_id,
            operation="worktree_arm", timeout=120, worktree=worktree_name_for_run(run_id),
            base_oid=base_oid, lease_ttl=lease_ttl,
        )
        if not arm.get("ok"):
            raise BridgeRoundError(f"worktree arm failed: {arm.get('error')}")
        lease_id = arm.get("lease_id")
        # The seat's own capability advertisement overrides the pinned family
        # fallback: seats on current bridge code report supports_thread_resume
        # ground truth here (bool over JSON; tolerate "true"/"false" strings).
        advertised = arm.get("thread_resume")
        if isinstance(advertised, str):
            advertised = {"true": True, "false": False}.get(advertised.lower())
        if isinstance(advertised, bool):
            mode = "resume" if advertised else "stateless"
        # The seat resolves and pins the base ref at arm time; record ITS oid
        # as provenance (the caller may legitimately have passed "HEAD").
        pinned_base_oid = arm.get("base_oid") if isinstance(arm.get("base_oid"), str) else None
        expires_at = arm.get("expires_at")
        workspace_root = Path(str(arm.get("path", "")))
        if not lease_id:
            raise BridgeRoundError("worktree arm reply missing lease_id/expires_at")
        row = {"lease_id": lease_id, "target_id": target_id, "env_file": str(env_file),
               "run_id": run_id, "pid": os.getpid(), "pid_start": _process_identity(os.getpid())}
        _add_ledger_row(ledger_path, row)
        if not isinstance(expires_at, (int, float)):
            raise BridgeRoundError("worktree arm reply missing lease_id/expires_at")
        if not workspace_root.is_dir():
            raise BridgeRoundError(f"armed worktree is not co-located: {workspace_root}")
        _exclude_faba(workspace_root)
        workspace = workspace_root / ".faba" / run_id
        workspace.mkdir(parents=True, exist_ok=True)
        workspace_relative = str(workspace.relative_to(workspace_root))
        first_brief = make_brief(workspace)
        thread_id = None
        final_reason = "bounce limit exhausted"

        for attempt in range(MAX_BOUNCES + 1):
            manifest = _stage(workspace_root, workspace_relative, staged_files)
            if _verify(workspace_root, workspace_relative, manifest):
                raise BridgeRoundError("driver re-stage hash verification failed")
            remaining = expires_at - time.time()
            if remaining < turn_timeout + POST_REPLY_MARGIN:
                events.append({
                    "attempt": attempt, "dispatch_id": None,
                    "payload_shape": {"operation": "worktree_run", "worktree_lease": lease_id},
                    "mode": mode, "reply_shape": "insufficient-headroom",
                    "refusal_reasons": ["insufficient lease headroom before run dispatch"],
                })
                final_reason = "insufficient lease headroom before run dispatch; forensics may be lost"
                forensics_loss = True
                break
            if attempt == 0:
                task = first_brief
            elif mode == "resume":
                task = "FABA gate refused the prior attempt. Fix exactly these problems and return only FABA_EXIT:\n" + "\n".join(problems)
            else:
                task = (
                    f"FABA stateless bounce attempt {attempt + 1}. Re-read the draft at "
                    f"{workspace / output_name}. Fix exactly these validator problems:\n"
                    + "\n".join(problems)
                    + "\nReturn only the contract's FABA_EXIT line."
                )
            reply = dispatch(
                target_id=target_id, env_file=env_file, run_id=run_id,
                operation="worktree_run", timeout=turn_timeout + 60,
                task=task, lease_id=lease_id,
                thread_id=thread_id if mode == "resume" and attempt else None,
                turn_timeout=turn_timeout,
            )
            if expires_at - time.time() < POST_REPLY_MARGIN:
                events.append({
                    "attempt": attempt, "dispatch_id": reply.get("_task_id"),
                    "payload_shape": {"operation": "worktree_run", "worktree_lease": lease_id},
                    "mode": mode, "reply_shape": "insufficient-headroom",
                    "refusal_reasons": ["insufficient lease headroom at reply"],
                })
                final_reason = "insufficient lease headroom at reply; forensics may be lost"
                forensics_loss = True
                break
            error = str(reply.get("error") or "")
            completion = reply.get("completion")
            completion_state = completion.get("state") if isinstance(completion, dict) else None
            event = {"attempt": attempt, "dispatch_id": reply.get("_task_id"),
                     "payload_shape": {"operation": "worktree_run", "worktree_lease": lease_id,
                                       "thread_id": thread_id if mode == "resume" and attempt else None},
                     "mode": mode, "reply_chars": len(str(reply.get("result") or ""))}
            if completion_state == "worktree_escape" or any(marker in error for marker in TERMINAL_ERRORS):
                terminal_reason = "worktree_escape" if completion_state == "worktree_escape" else error
                event.update(reply_shape="terminal", refusal_reasons=[terminal_reason])
                events.append(event)
                final_reason = terminal_reason
                break
            if not reply.get("ok"):
                event.update(reply_shape="bridge-error", refusal_reasons=[error or "bridge run failed"])
                events.append(event)
                final_reason = error or "bridge run failed"
                break
            observed_thread = reply.get("thread_id")
            if attempt == 0:
                thread_id = observed_thread if isinstance(observed_thread, str) and observed_thread else None
                if mode == "resume" and thread_id is None:
                    event.update(reply_shape="missing-thread-id", refusal_reasons=["resume engine returned no thread_id"])
                    events.append(event)
                    final_reason = "resume engine returned no thread_id on attempt 0"
                    break
            reply_text = str(reply.get("result") or "")
            shape_problems = _reply_shape(reply_text, expected_exit_key, expected_exit_id)
            if len(reply_text) > REPLY_CHAR_CAP:
                event.update(reply_shape="oversize", refusal_reasons=["decoded reply exceeds hard size ceiling"])
                events.append(event)
                final_reason = "decoded reply exceeds hard size ceiling"
                break
            mutations = _verify(workspace_root, workspace_relative, manifest)
            problems = shape_problems
            if not problems:
                try:
                    safe_copy_tree(workspace_root, workspace_relative, forensic)
                    # open(newline="") rather than read_text(newline=""): the
                    # kwarg needs Python >= 3.13 and this driver runs on 3.12 venvs.
                    with open(forensic / output_name, encoding="utf-8", newline="") as fh:
                        output_text = fh.read()
                    problems = list(validate(output_text))
                except BridgeRoundError as exc:
                    problems = [str(exc)]
            if mutations:
                problems += ["staged workspace input mutated: " + name for name in mutations]
            event.update(reply_shape="valid" if not shape_problems else "invalid",
                         refusal_reasons=list(problems), input_mutations=mutations)
            events.append(event)
            if not problems:
                (forensic / "bounce-log.json").write_text(
                    json.dumps(events, indent=2) + "\n", encoding="utf-8"
                )
                publish_result = publish(forensic)
                passed = bool(publish_result[0])
                final_reason = str(publish_result[1])
                return BridgeRoundResult(
                    passed, final_reason, forensic, attempt + 1, family, mode,
                    lease_id=lease_id, thread_id=thread_id, base_oid=pinned_base_oid,
                    forensics_loss=forensics_loss, events=events,
                ), publish_result
            final_reason = "; ".join(problems)
        try:
            safe_copy_tree(workspace_root, str(workspace.relative_to(workspace_root)), forensic)
            (forensic / "bounce-log.json").write_text(json.dumps(events, indent=2) + "\n", encoding="utf-8")
            forensics_loss = False
        except BridgeRoundError:
            forensics_loss = True
        return BridgeRoundResult(
            False, final_reason, forensic if forensic.exists() else None, len(events),
            family, mode, lease_id=lease_id, thread_id=thread_id,
            base_oid=pinned_base_oid, forensics_loss=forensics_loss, events=events,
        ), None
    finally:
        if lease_id:
            released = False
            try:
                try:
                    release = dispatch(target_id=target_id, env_file=env_file, run_id=run_id,
                                       operation="worktree_release", timeout=120, lease_id=lease_id)
                    released = bool(release.get("ok")) or "unavailable" in str(release.get("error", ""))
                except Exception:
                    released = False
            finally:
                if released:
                    _remove_ledger_lease(ledger_path, lease_id)
        release_local_lock(lock_fh)
