"""Multi-repository push-less exempt Git credential (Slice 1d-iii).

Resolves the target repository from a worker checkout's actual ``origin``,
configures worktree-local origin + ``core.sshCommand`` for the one
``arb-exempt-bot`` machine-user SSH identity, and proves
fetch-positive / classified push-permission-denied before arm.

Does **not** consult ``BRIDGE_EXEMPT_GIT_REMOTE_URL``, an ARB hardcode, operator
credentials, a PAT, or per-repository deploy keys. Does **not** claim OS-level
isolation (§9.3 residual: same-UID ambient credentials can still push).
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shlex
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Recorded machine-user identity (provisioned 2026-07-27; runbook is authority).
EXEMPT_BOT_ACCOUNT = "arb-exempt-bot"
EXEMPT_BOT_FINGERPRINT = "SHA256:<fingerprint>"

# Default client-side bound for every git probe. Makes EXEMPT_NETWORK_TIMEOUT
# (exit 124) reachable and keeps a black-holed ls-remote from hanging the
# single-threaded inbox loop while it holds the lease lock.
DEFAULT_GIT_TIMEOUT_SEC = 60.0

# --- Rejection / classification codes (closed catalog) ----------------------

EXEMPT_SSH_CONFIG_MISSING = "exempt-ssh-config-missing"
EXEMPT_ORIGIN_MISSING = "exempt-origin-missing"
EXEMPT_ORIGIN_AMBIGUOUS = "exempt-origin-ambiguous"
EXEMPT_ORIGIN_INVALID = "exempt-origin-invalid"
EXEMPT_ORIGIN_NOT_SSH = "exempt-origin-not-ssh"
EXEMPT_KEY_FINGERPRINT_MISMATCH = "exempt-key-fingerprint-mismatch"
EXEMPT_KEY_PERMISSIONS = "exempt-key-permissions"
EXEMPT_IDENTITY_MISMATCH = "exempt-identity-mismatch"
EXEMPT_LEDGER_ENTRY_MISSING = "exempt-ledger-entry-missing"
EXEMPT_REMOTE_READ_UNAVAILABLE = "exempt-remote-read-unavailable"
EXEMPT_PUSH_PERMISSION_DENIED = "exempt-push-permission-denied"
EXEMPT_PUSH_CREDENTIAL_WRITABLE = "exempt-push-credential-writable"
EXEMPT_PUSH_DENIAL_UNPROVEN = "exempt-push-denial-unproven"
EXEMPT_AUTH_PUBLICKEY = "exempt-auth-publickey"
EXEMPT_REMOTE_NOT_FOUND = "exempt-remote-not-found"
EXEMPT_REMOTE_ARCHIVED = "exempt-remote-archived"
EXEMPT_PUSH_HOOK_REJECTED = "exempt-push-hook-rejected"
EXEMPT_NETWORK_DNS = "exempt-network-dns"
EXEMPT_NETWORK_TIMEOUT = "exempt-network-timeout"
EXEMPT_NETWORK_REFUSED = "exempt-network-refused"
EXEMPT_NETWORK_RESET = "exempt-network-reset"
EXEMPT_CONFIG_WRITE_FAILED = "exempt-config-write-failed"
EXEMPT_PREP_INTERNAL_ERROR = "exempt-prep-internal-error"

REJECTION_CODES: frozenset[str] = frozenset(
    {
        EXEMPT_SSH_CONFIG_MISSING,
        EXEMPT_ORIGIN_MISSING,
        EXEMPT_ORIGIN_AMBIGUOUS,
        EXEMPT_ORIGIN_INVALID,
        EXEMPT_ORIGIN_NOT_SSH,
        EXEMPT_KEY_FINGERPRINT_MISMATCH,
        EXEMPT_KEY_PERMISSIONS,
        EXEMPT_IDENTITY_MISMATCH,
        EXEMPT_LEDGER_ENTRY_MISSING,
        EXEMPT_REMOTE_READ_UNAVAILABLE,
        EXEMPT_PUSH_PERMISSION_DENIED,
        EXEMPT_PUSH_CREDENTIAL_WRITABLE,
        EXEMPT_PUSH_DENIAL_UNPROVEN,
        EXEMPT_AUTH_PUBLICKEY,
        EXEMPT_REMOTE_NOT_FOUND,
        EXEMPT_REMOTE_ARCHIVED,
        EXEMPT_PUSH_HOOK_REJECTED,
        EXEMPT_NETWORK_DNS,
        EXEMPT_NETWORK_TIMEOUT,
        EXEMPT_NETWORK_REFUSED,
        EXEMPT_NETWORK_RESET,
        EXEMPT_CONFIG_WRITE_FAILED,
        EXEMPT_PREP_INTERNAL_ERROR,
    }
)

# Non-repo-scoping vars the lane must still control (not all appear in
# `git rev-parse --local-env-vars`). Repo-scoping names are derived at runtime
# from git itself and unioned with a static floor so a PATH-poisoned or older
# `git` that returns a plausible short list cannot shrink coverage below the
# names git 2.50.1 declares (which let GIT_COMMON_DIR retarget probes).
_EXPLICIT_PROBE_SCRUB_KEYS: frozenset[str] = frozenset(
    {
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "GIT_PROXY_COMMAND",
        "GIT_EXEC_PATH",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
    }
)
# Static floor: the 15 repo-scoping names declared by git 2.50.1. Unioned into
# every scrub set so a short derived list cannot drop GIT_COMMON_DIR et al.
_REPO_SCOPING_FLOOR: frozenset[str] = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_OBJECT_DIRECTORY",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_GRAFT_FILE",
        "GIT_INDEX_FILE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_REPLACE_REF_BASE",
        "GIT_PREFIX",
        "GIT_SHALLOW_FILE",
        "GIT_COMMON_DIR",
    }
)
# GIT_CONFIG_KEY_n / GIT_CONFIG_VALUE_n scrubbed by prefix in build_probe_env.

_local_env_vars_cache: frozenset[str] | None = None


def reset_local_env_vars_cache() -> None:
    """Clear the process-level --local-env-vars cache (tests / re-probe)."""
    global _local_env_vars_cache
    _local_env_vars_cache = None


def _git_local_env_vars() -> frozenset[str]:
    """Ask git for its authoritative repo-scoping env var names. Fail closed."""
    global _local_env_vars_cache
    if _local_env_vars_cache is not None:
        return _local_env_vars_cache
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--local-env-vars"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExemptGitError(
            EXEMPT_PREP_INTERNAL_ERROR,
            f"git rev-parse --local-env-vars failed: {exc}",
        ) from exc
    if result.returncode != 0 or not (result.stdout or "").strip():
        raise ExemptGitError(
            EXEMPT_PREP_INTERNAL_ERROR,
            f"git rev-parse --local-env-vars empty/failed "
            f"(exit={result.returncode}); refuse rather than a short static list",
        )
    names = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    if not names:
        raise ExemptGitError(
            EXEMPT_PREP_INTERNAL_ERROR,
            "git rev-parse --local-env-vars returned no names; fail closed",
        )
    _local_env_vars_cache = frozenset(names)
    return _local_env_vars_cache


def probe_scrub_keys() -> frozenset[str]:
    """Runtime scrub set: git's --local-env-vars ∪ floor ∪ explicit controls.

    The floor alone already makes a short derived list ineffective for the known
    repo-scoping redirect names; runtime derivation still picks up names a
    future git adds.
    """
    return _git_local_env_vars() | _REPO_SCOPING_FLOOR | _EXPLICIT_PROBE_SCRUB_KEYS

# Live-captured push-permission-denied fixture (read-only machine-user role,
# dry-run against a private repo). Complete lines + exit.
# Classification is bound to a concrete target_url (must be a normalized SSH
# form); the proof object then carries that URL so repo A cannot cover repo B.
PUSH_CLASS_FIXTURES: dict[str, dict] = {
    EXEMPT_PUSH_PERMISSION_DENIED: {
        "exit": 128,
        "lines": (
            "ERROR: Write access to repository not granted.",
            "fatal: Could not read from remote repository.",
        ),
    },
    EXEMPT_AUTH_PUBLICKEY: {
        "exit": 128,
        "lines": ("Permission denied (publickey).",),
    },
    EXEMPT_REMOTE_NOT_FOUND: {
        "exit": 128,
        "lines": ("ERROR: Repository not found.",),
    },
    EXEMPT_REMOTE_ARCHIVED: {
        "exit": None,  # exit varies
        # Phrase token (not a full line) — any_lines, never the accepting class.
        "any_lines": ("This repository has been archived",),
    },
    EXEMPT_PUSH_HOOK_REJECTED: {
        "exit": None,
        "any_lines": (
            "Protected branch update failed",
            "hook declined",
            "pre-receive hook declined",
        ),
    },
    EXEMPT_NETWORK_DNS: {
        "exit": None,
        "any_lines": (
            "Could not resolve hostname",
            "nodename nor servname provided",
            "Name or service not known",
        ),
    },
    EXEMPT_NETWORK_TIMEOUT: {
        "exit": 124,
        "any_lines": ("timed out", "Timeout", "timeout"),
    },
    EXEMPT_NETWORK_REFUSED: {
        "exit": None,
        "any_lines": ("Connection refused",),
    },
    EXEMPT_NETWORK_RESET: {
        "exit": None,
        "any_lines": ("Connection reset", "Connection reset by peer"),
    },
}

# Classification order for push (first match wins). Permission-denied is the
# only accepted proof class; all others are terminal failures.
_PUSH_CLASSIFY_ORDER: tuple[str, ...] = (
    EXEMPT_AUTH_PUBLICKEY,
    EXEMPT_REMOTE_NOT_FOUND,
    EXEMPT_REMOTE_ARCHIVED,
    EXEMPT_PUSH_HOOK_REJECTED,
    EXEMPT_NETWORK_DNS,
    EXEMPT_NETWORK_TIMEOUT,
    EXEMPT_NETWORK_REFUSED,
    EXEMPT_NETWORK_RESET,
    EXEMPT_PUSH_PERMISSION_DENIED,
)

_GITHUB_SSH_RE = re.compile(
    r"^git@github\.com:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?$"
)
_GITHUB_SSH_SLASH_RE = re.compile(
    r"^ssh://git@github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?$"
)
# Owner/repo character class — reject option-shaped, path traversal, NUL, etc.
_OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TARGET_SSH_URL_RE = re.compile(
    r"^git@github\.com:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git$"
)

RunGit = Callable[..., subprocess.CompletedProcess]


class ExemptGitError(Exception):
    """Terminal exempt-credential failure. ``code`` is the closed catalog name."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in REJECTION_CODES:
            raise ValueError(f"non-catalog exempt code: {code!r}")
        self.code = code
        self.detail = detail
        message = code if not detail else f"{code}: {detail}"
        super().__init__(message)


@dataclass(frozen=True)
class ResolvedTarget:
    owner_repo: str  # owner/repo as derived from the checkout origin
    ssh_url: str  # git@github.com:owner/repo.git
    raw_origin: str


@dataclass(frozen=True)
class ExemptProof:
    classification: str
    target: ResolvedTarget
    target_url: str
    ledger_repo: str | None
    read_exit: int
    push_exit: int
    push_stderr_lines: tuple[str, ...]


def proof_covers_target(proof: ExemptProof, target: ResolvedTarget) -> bool:
    """A proof is valid only for the exact resolved target URL it was taken against."""
    return (
        proof.classification == EXEMPT_PUSH_PERMISSION_DENIED
        and proof.target_url == target.ssh_url
        and proof.target.owner_repo == target.owner_repo
    )


def normalize_lines(text: str) -> tuple[str, ...]:
    lines: list[str] = []
    for raw in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = raw.strip()
        if stripped:
            lines.append(stripped)
    return tuple(lines)


def build_probe_env(ssh_command: str | None = None) -> dict[str, str]:
    """Sanitized env for every git/ssh probe: scrub overrides, pin identity.

    Scrub set is ``git rev-parse --local-env-vars`` ∪ the static repo-scoping
    floor ∪ explicit non-repo controls (SSH, proxy, file-based config). Ambient
    values are dropped; global/system config is neutralized so operator-scoped
    insteadOf rewrites cannot retarget resolution or probes.

    Trust boundary (honest strength): this hermetic env neutralizes
    **global/system** config only. **Repo-local** ``url.*.insteadOf`` rewrites
    in the checkout's own ``.git/config`` remain visible to ``git remote
    get-url`` and to probes that dial a remote name. Resolution and push/read
    legs that dial an explicit verified SSH URL are not expanded by
    remote-name rewrites, but operators must not treat a plain
    ``git remote get-url`` (without GIT_CONFIG_GLOBAL=/dev/null) as the
    config-file truth. Empty/failed local-env-vars → refuse.
    """
    scrub = probe_scrub_keys()
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in scrub:
            continue
        if key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_"):
            continue
        if key.startswith("GIT_CONFIG_COUNT"):
            continue
        env[key] = value
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Neutralize file-based config (ambient GIT_CONFIG_GLOBAL was scrubbed).
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    if ssh_command is not None:
        env["GIT_SSH_COMMAND"] = ssh_command
    return env


def _bind_probe_env(run: RunGit, ssh_command: str | None) -> RunGit:
    """Force every run_git invocation onto the sanitized, pinned env."""

    def bound(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        kwargs["env"] = build_probe_env(ssh_command)
        return run(cmd, **kwargs)

    return bound


def normalize_github_remote(raw: str) -> ResolvedTarget:
    """Accept documented GitHub SSH/HTTPS forms only; reject everything else."""
    if raw is None:
        raise ExemptGitError(EXEMPT_ORIGIN_INVALID, "empty origin")
    value = raw.strip()
    if not value:
        raise ExemptGitError(EXEMPT_ORIGIN_INVALID, "empty origin")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ExemptGitError(EXEMPT_ORIGIN_INVALID, "origin contains NUL or newline")
    if value.startswith("-"):
        raise ExemptGitError(EXEMPT_ORIGIN_INVALID, "option-shaped origin")
    if any(ch.isspace() for ch in value):
        raise ExemptGitError(EXEMPT_ORIGIN_INVALID, "origin contains whitespace")

    owner: str | None = None
    repo: str | None = None

    m = _GITHUB_SSH_RE.match(value)
    if m:
        owner, repo = m.group(1), m.group(2)
    else:
        m = _GITHUB_SSH_SLASH_RE.match(value)
        if m:
            # Reject ssh:// forms that smuggle port/userinfo beyond git@host.
            try:
                parsed_ssh = urlparse(value)
                port = parsed_ssh.port
            except ValueError as exc:
                raise ExemptGitError(
                    EXEMPT_ORIGIN_INVALID, f"ssh URL port invalid: {value!r}"
                ) from exc
            if port is not None:
                raise ExemptGitError(
                    EXEMPT_ORIGIN_INVALID, f"ssh URL must not carry a port: {value!r}"
                )
            if parsed_ssh.query or parsed_ssh.fragment:
                raise ExemptGitError(
                    EXEMPT_ORIGIN_INVALID, "ssh URL must not carry query/fragment"
                )
            owner, repo = m.group(1), m.group(2)
        elif value.startswith("https://") or value.startswith("http://"):
            try:
                parsed = urlparse(value)
                port = parsed.port
            except ValueError as exc:
                # Nonnumeric port raises ValueError from urllib — catalog it.
                raise ExemptGitError(
                    EXEMPT_ORIGIN_INVALID, f"https origin port invalid: {value!r}"
                ) from exc
            host = (parsed.hostname or "").lower()
            # Pin host tightly: github.com only (not github.com.evil.tld).
            if host != "github.com":
                raise ExemptGitError(
                    EXEMPT_ORIGIN_INVALID, f"non-github host {host!r}"
                )
            if parsed.username is not None or parsed.password is not None:
                raise ExemptGitError(
                    EXEMPT_ORIGIN_INVALID, "https origin must not carry userinfo"
                )
            if port is not None:
                raise ExemptGitError(
                    EXEMPT_ORIGIN_INVALID, "https origin must not carry a port"
                )
            if parsed.query or parsed.fragment:
                raise ExemptGitError(
                    EXEMPT_ORIGIN_INVALID, "https origin must not carry query/fragment"
                )
            path = parsed.path or ""
            # Reject trailing slash and doubled separators; do not normalize.
            if path.endswith("/") or "//" in path:
                raise ExemptGitError(
                    EXEMPT_ORIGIN_INVALID,
                    f"https path must not have trailing slash or empty segments: {value!r}",
                )
            parts = [p for p in path.split("/") if p]
            # Exactly owner/repo — reject extra path segments outright.
            if len(parts) != 2:
                raise ExemptGitError(
                    EXEMPT_ORIGIN_INVALID,
                    f"https path must be exactly owner/repo, got {parts!r}",
                )
            owner, repo = parts[0], parts[1]
            if repo.endswith(".git"):
                repo = repo[: -len(".git")]
        else:
            raise ExemptGitError(
                EXEMPT_ORIGIN_INVALID,
                f"unsupported origin form: {value!r}",
            )

    if not owner or not repo:
        raise ExemptGitError(EXEMPT_ORIGIN_INVALID, "missing owner/repo")
    # Reject trailing-.git games only (double strip / owner ends with .git).
    # Real repo names like org/.github must pass — do not substring-match ".git".
    if ".git.git" in value or owner.endswith(".git"):
        raise ExemptGitError(EXEMPT_ORIGIN_INVALID, f"double-.git form: {value!r}")
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    # After one optional strip the repo name must not still end with ".git".
    if repo.endswith(".git") or owner.endswith(".git"):
        raise ExemptGitError(EXEMPT_ORIGIN_INVALID, f"double-.git form: {value!r}")
    owner_repo = f"{owner}/{repo}"
    if not _OWNER_REPO_RE.match(owner_repo):
        raise ExemptGitError(EXEMPT_ORIGIN_INVALID, f"bad owner/repo {owner_repo!r}")
    if owner.startswith("-") or repo.startswith("-"):
        raise ExemptGitError(EXEMPT_ORIGIN_INVALID, "option-shaped owner/repo")
    # Path-traversal-shaped segments (dot-only / parent) fail closed.
    if owner in {".", ".."} or repo in {".", ".."}:
        raise ExemptGitError(EXEMPT_ORIGIN_INVALID, f"path-shaped owner/repo {owner_repo!r}")
    ssh_url = f"git@github.com:{owner_repo}.git"
    return ResolvedTarget(owner_repo=owner_repo, ssh_url=ssh_url, raw_origin=value)


def _default_run_git(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("check", False)
    kwargs.setdefault("timeout", DEFAULT_GIT_TIMEOUT_SEC)
    try:
        return subprocess.run(cmd, **kwargs)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=124,
            stdout="",
            stderr="git operation timed out",
        )


def _url_lines(result: subprocess.CompletedProcess) -> list[str]:
    return [u.strip() for u in (result.stdout or "").splitlines() if u.strip()]


def _is_literal_ssh_origin(value: str) -> bool:
    """True only for the canonical form ``git@github.com:owner/repo.git``.

    Shared by preflight and arm. Non-canonical but genuine SSH spellings
    (no ``.git`` suffix, ``ssh://git@github.com/...``) are not admitted —
    they canonicalise to a different string and open a preflight/arm split
    against the post-config single-URL gate. Operators convert once with
    ``git remote set-url origin git@github.com:owner/repo.git``.
    """
    return bool(_TARGET_SSH_URL_RE.match(value))


def _refuse_non_ssh_origin(raw: str) -> None:
    """Raise EXEMPT_ORIGIN_NOT_SSH with the one-line operator conversion."""
    detail = (
        f"origin is not canonical SSH form ({raw!r}); convert with: "
        f"git remote set-url origin git@github.com:owner/repo.git"
    )
    try:
        guessed = normalize_github_remote(raw)
        detail = (
            f"origin is not canonical SSH form ({raw!r}); convert with: "
            f"git remote set-url origin {guessed.ssh_url}"
        )
    except ExemptGitError:
        pass
    raise ExemptGitError(EXEMPT_ORIGIN_NOT_SSH, detail)


def resolve_target_remote(
    worktree: Path | str,
    *,
    run_git: RunGit | None = None,
) -> ResolvedTarget:
    """Derive owner/repo from a canonical SSH-form origin. No env override.

    The lane requires the remote to *say* what it does: only the canonical
    ``git@github.com:owner/repo.git`` spelling is admitted (same predicate at
    preflight and arm). HTTPS-written origins and non-canonical SSH spellings
    (no ``.git``, ``ssh://…``) are refused with ``EXEMPT_ORIGIN_NOT_SSH`` and
    a one-line conversion. Multi-value origins are ambiguous.

    Hermetic probe env neutralizes global/system ``insteadOf``; repo-local
    rewrites remain inside the checkout trust boundary (see ``build_probe_env``).
    """
    # Explicitly ignore BRIDGE_EXEMPT_GIT_REMOTE_URL — it is not authority.
    _ = os.environ.get("BRIDGE_EXEMPT_GIT_REMOTE_URL")
    # Default path always hermetic so operator insteadOf cannot rewrite get-url
    # into an SSH form the config file does not literally contain.
    run = run_git if run_git is not None else _bind_probe_env(_default_run_git, None)
    wt = Path(worktree)
    # Multi-value read: get-url without --all only prints the first URL, so
    # EXEMPT_ORIGIN_AMBIGUOUS was dead. Require exactly one value.
    result = run(["git", "-C", str(wt), "remote", "get-url", "--all", "origin"])
    if result.returncode != 0 or not (result.stdout or "").strip():
        # Fall back to config --get-all (same multi-value contract).
        result = run(
            ["git", "-C", str(wt), "config", "--get-all", "remote.origin.url"]
        )
    if result.returncode != 0 or not (result.stdout or "").strip():
        listed = run(["git", "-C", str(wt), "remote"])
        remotes = [r for r in (listed.stdout or "").splitlines() if r.strip()]
        if not remotes:
            raise ExemptGitError(EXEMPT_ORIGIN_MISSING, "no remotes configured")
        if "origin" not in remotes:
            raise ExemptGitError(EXEMPT_ORIGIN_MISSING, "no origin remote")
        raise ExemptGitError(EXEMPT_ORIGIN_MISSING, "origin URL unreadable")
    urls = _url_lines(result)
    if len(urls) != 1:
        raise ExemptGitError(
            EXEMPT_ORIGIN_AMBIGUOUS,
            f"expected one origin URL, got {len(urls)}: {urls!r}",
        )
    raw = urls[0]
    if not _is_literal_ssh_origin(raw):
        # Known GitHub forms (HTTPS, non-canonical SSH) → NOT_SSH + conversion.
        # Local paths, non-GitHub hosts, and other forms stay INVALID via
        # normalize (raised before refuse when unparseable).
        try:
            normalize_github_remote(raw)
        except ExemptGitError:
            raise
        _refuse_non_ssh_origin(raw)
    return normalize_github_remote(raw)


def _identities_only_pairs(tokens: list[str]) -> list[str]:
    """Return every IdentitiesOnly value present in ssh argv tokens."""
    values: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "-o" and i + 1 < len(tokens):
            opt = tokens[i + 1]
            if opt.lower().startswith("identitiesonly="):
                values.append(opt.split("=", 1)[1])
            i += 2
            continue
        if tok.startswith("-o") and len(tok) > 2:
            opt = tok[2:]
            if opt.lower().startswith("identitiesonly="):
                values.append(opt.split("=", 1)[1])
            i += 1
            continue
        i += 1
    return values


def check_key_permissions(key_path: Path) -> None:
    """Enforce key mode 0600 (no group/other) and parent dir ≤0700."""
    try:
        key_mode = stat.S_IMODE(key_path.stat().st_mode)
    except OSError as exc:
        raise ExemptGitError(
            EXEMPT_KEY_PERMISSIONS, f"cannot stat key {key_path}: {exc}"
        ) from exc
    if key_mode & 0o077:
        raise ExemptGitError(
            EXEMPT_KEY_PERMISSIONS,
            f"key mode {oct(key_mode)} must be 0600 (no group/other bits)",
        )
    parent = key_path.parent
    try:
        parent_mode = stat.S_IMODE(parent.stat().st_mode)
    except OSError as exc:
        raise ExemptGitError(
            EXEMPT_KEY_PERMISSIONS, f"cannot stat key parent {parent}: {exc}"
        ) from exc
    if parent_mode & 0o077:
        raise ExemptGitError(
            EXEMPT_KEY_PERMISSIONS,
            f"key parent dir mode {oct(parent_mode)} must be ≤0700 (no group/other bits)",
        )


def validate_ssh_command(ssh_command: str | None) -> tuple[str, Path]:
    """Require a single machine-user key path and exactly one IdentitiesOnly=yes."""
    if not ssh_command or not str(ssh_command).strip():
        raise ExemptGitError(EXEMPT_SSH_CONFIG_MISSING, "BRIDGE_EXEMPT_GIT_SSH_COMMAND empty")
    cmd = str(ssh_command).strip()
    if "\x00" in cmd or "\n" in cmd:
        raise ExemptGitError(EXEMPT_SSH_CONFIG_MISSING, "ssh command contains NUL/newline")
    try:
        tokens = shlex.split(cmd)
    except ValueError as exc:
        raise ExemptGitError(EXEMPT_SSH_CONFIG_MISSING, f"unparseable ssh command: {exc}") from exc

    # Reject operator-config / agent-forwarding escapes.
    if "-F" in tokens:
        raise ExemptGitError(
            EXEMPT_SSH_CONFIG_MISSING, "ssh command must not use -F (config file)"
        )
    for i, tok in enumerate(tokens):
        if tok == "-o" and i + 1 < len(tokens):
            opt = tokens[i + 1]
            if opt.lower().startswith("identityagent"):
                raise ExemptGitError(
                    EXEMPT_SSH_CONFIG_MISSING,
                    "ssh command must not set IdentityAgent",
                )
        if tok.startswith("-o") and len(tok) > 2:
            opt = tok[2:]
            if opt.lower().startswith("identityagent"):
                raise ExemptGitError(
                    EXEMPT_SSH_CONFIG_MISSING,
                    "ssh command must not set IdentityAgent",
                )

    id_values = _identities_only_pairs(tokens)
    if len(id_values) != 1 or id_values[0].lower() != "yes":
        raise ExemptGitError(
            EXEMPT_SSH_CONFIG_MISSING,
            "ssh command must include exactly one -o IdentitiesOnly=yes",
        )

    key_path: Path | None = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "-i" and i + 1 < len(tokens):
            key_path = Path(tokens[i + 1]).expanduser()
            i += 2
            continue
        if tok.startswith("-i") and len(tok) > 2:
            key_path = Path(tok[2:]).expanduser()
            i += 1
            continue
        i += 1
    if key_path is None:
        raise ExemptGitError(EXEMPT_SSH_CONFIG_MISSING, "ssh command missing -i key path")
    if not key_path.is_file():
        raise ExemptGitError(
            EXEMPT_SSH_CONFIG_MISSING, f"private key not found: {key_path}"
        )
    check_key_permissions(key_path)
    return cmd, key_path


def key_fingerprint(key_path: Path) -> str:
    """Return ``SHA256:…`` fingerprint of the private/public key file."""
    try:
        result = subprocess.run(
            ["ssh-keygen", "-lf", str(key_path), "-E", "sha256"],
            capture_output=True,
            text=True,
            check=False,
            timeout=DEFAULT_GIT_TIMEOUT_SEC,
            env=build_probe_env(),
        )
    except FileNotFoundError as exc:
        raise ExemptGitError(
            EXEMPT_KEY_FINGERPRINT_MISMATCH,
            f"ssh-keygen not found: {exc}",
        ) from exc
    except OSError as exc:
        raise ExemptGitError(
            EXEMPT_KEY_FINGERPRINT_MISMATCH,
            f"ssh-keygen could not run: {exc}",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ExemptGitError(
            EXEMPT_NETWORK_TIMEOUT,
            "ssh-keygen timed out",
        ) from exc
    if result.returncode != 0:
        raise ExemptGitError(
            EXEMPT_KEY_FINGERPRINT_MISMATCH,
            f"ssh-keygen failed: {(result.stderr or '').strip()}",
        )
    # Format: "256 SHA256:xxxx comment (ED25519)"
    parts = (result.stdout or "").split()
    for part in parts:
        if part.startswith("SHA256:"):
            return part
    raise ExemptGitError(
        EXEMPT_KEY_FINGERPRINT_MISMATCH,
        f"fingerprint not parseable from: {(result.stdout or '').strip()!r}",
    )


def github_ssh_identity(ssh_command: str) -> str:
    """Probe GitHub for the account name bound to this SSH command."""
    try:
        tokens = shlex.split(ssh_command)
    except ValueError as exc:
        raise ExemptGitError(
            EXEMPT_SSH_CONFIG_MISSING, f"unparseable ssh command: {exc}"
        ) from exc
    if not tokens:
        raise ExemptGitError(EXEMPT_SSH_CONFIG_MISSING, "empty ssh command tokens")
    # Prepend enforced options so ssh first-value-wins cannot let an operator
    # StrictHostKeyChecking=no / BatchMode=no override them.
    argv = [
        tokens[0],
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        *tokens[1:],
        "-T",
        "git@github.com",
    ]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            env=build_probe_env(ssh_command),
        )
    except subprocess.TimeoutExpired as exc:
        raise ExemptGitError(
            EXEMPT_NETWORK_TIMEOUT,
            "GitHub SSH identity probe timed out",
        ) from exc
    except FileNotFoundError as exc:
        raise ExemptGitError(
            EXEMPT_SSH_CONFIG_MISSING,
            f"ssh binary not found: {exc}",
        ) from exc
    except OSError as exc:
        raise ExemptGitError(
            EXEMPT_IDENTITY_MISMATCH,
            f"GitHub SSH identity probe failed to start: {exc}",
        ) from exc
    text = f"{result.stdout or ''}\n{result.stderr or ''}"
    m = re.search(r"Hi\s+([A-Za-z0-9_-]+)\s*!", text)
    if not m:
        raise ExemptGitError(
            EXEMPT_IDENTITY_MISMATCH,
            f"GitHub SSH identity not recognized: {text.strip()[:200]!r}",
        )
    return m.group(1)


def load_ledger(ledger_path: Path | str | None) -> dict:
    """Load the provisioning ledger. ``None`` is illegal — no manufactured default."""
    if ledger_path is None:
        raise ExemptGitError(
            EXEMPT_LEDGER_ENTRY_MISSING,
            "BRIDGE_EXEMPT_PROVISIONING_LEDGER is required for exempt lane",
        )
    path = Path(ledger_path)
    if not path.is_file():
        raise ExemptGitError(EXEMPT_LEDGER_ENTRY_MISSING, f"ledger file missing: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExemptGitError(
            EXEMPT_LEDGER_ENTRY_MISSING, f"ledger unreadable: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ExemptGitError(EXEMPT_LEDGER_ENTRY_MISSING, "ledger root must be object")
    return data


def match_ledger_entry(ledger: Mapping, owner_repo: str) -> str | None:
    """Return the canonical ledger repo name if owner_repo or an alias matches."""
    targets = ledger.get("targets") or []
    if not isinstance(targets, list):
        return None
    needle = owner_repo.casefold()
    for entry in targets:
        if not isinstance(entry, dict):
            continue
        repo = str(entry.get("repo") or "")
        if repo.casefold() == needle:
            return repo
        aliases = entry.get("aliases") or []
        if isinstance(aliases, list):
            for alias in aliases:
                if str(alias).casefold() == needle:
                    return repo or str(alias)
    return None


def _fixture_matches(fixture: Mapping, exit_code: int, lines: tuple[str, ...]) -> bool:
    expected_exit = fixture.get("exit")
    if expected_exit is not None and exit_code != expected_exit:
        return False
    required = fixture.get("lines") or ()
    if required:
        # Denial acceptance is normalized COMPLETE lines only — no substring.
        line_set = set(lines)
        return all(req in line_set for req in required)
    any_lines = fixture.get("any_lines") or ()
    if any_lines:
        blob = "\n".join(lines)
        return any(token in blob for token in any_lines)
    return False


def classify_push_result(
    exit_code: int,
    stderr: str,
    *,
    target_url: str,
) -> str:
    """Classify a push --dry-run outcome bound to a concrete target URL.

    ``target_url`` must be the normalized SSH form of the repo under proof;
    an empty or non-SSH URL cannot produce the accepting class (so classification
    is not free-floating text matching). The proof object then carries that URL
    so repo A's denial cannot cover repo B.
    """
    if not target_url or not _TARGET_SSH_URL_RE.match(target_url):
        return EXEMPT_PUSH_DENIAL_UNPROVEN
    if exit_code == 0:
        return EXEMPT_PUSH_CREDENTIAL_WRITABLE
    lines = normalize_lines(stderr)
    for code in _PUSH_CLASSIFY_ORDER:
        fixture = PUSH_CLASS_FIXTURES.get(code)
        if fixture and _fixture_matches(fixture, exit_code, lines):
            return code
    return EXEMPT_PUSH_DENIAL_UNPROVEN


def classify_read_result(exit_code: int, stderr: str) -> str | None:
    """Return a failure code, or None if read succeeded."""
    if exit_code == 0:
        return None
    lines = normalize_lines(stderr)
    for code in (
        EXEMPT_AUTH_PUBLICKEY,
        EXEMPT_REMOTE_NOT_FOUND,
        EXEMPT_REMOTE_ARCHIVED,
        EXEMPT_NETWORK_DNS,
        EXEMPT_NETWORK_TIMEOUT,
        EXEMPT_NETWORK_REFUSED,
        EXEMPT_NETWORK_RESET,
    ):
        fixture = PUSH_CLASS_FIXTURES.get(code)
        if fixture and _fixture_matches(fixture, exit_code, lines):
            return code
    return EXEMPT_REMOTE_READ_UNAVAILABLE


def _read_config_value(
    run: RunGit, wt: Path, key: str, *, worktree: bool = False
) -> str | None:
    cmd = ["git", "-C", str(wt), "config"]
    if worktree:
        cmd.append("--worktree")
    cmd.extend(["--get", key])
    result = run(cmd)
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


def configure_exempt_worktree(
    worktree: Path | str,
    target: ResolvedTarget,
    ssh_command: str,
    *,
    run_git: RunGit | None = None,
) -> None:
    """Enable worktreeConfig; set worktree-local origin + core.sshCommand."""
    run = run_git or _default_run_git
    wt = Path(worktree)
    # Note prior common-config value so failure paths leave a trail (P2-5).
    prior_wtc = _read_config_value(run, wt, "extensions.worktreeConfig")
    if prior_wtc != "true":
        logger.info(
            "[exempt-git] enabling extensions.worktreeConfig on %s (was %r)",
            wt,
            prior_wtc,
        )
    # extensions.worktreeConfig lives in the common config (once). While it is
    # still false, plain `git config` writes the common file — which is what we
    # want — and only then does --worktree become meaningful.
    enable = run(
        ["git", "-C", str(wt), "config", "extensions.worktreeConfig", "true"]
    )
    if enable.returncode != 0:
        raise ExemptGitError(
            EXEMPT_CONFIG_WRITE_FAILED,
            f"failed to enable extensions.worktreeConfig: "
            f"{(enable.stderr or '').strip()}",
        )
    # Unset multi-valued worktree keys before set so we never accumulate
    # pushurls across configure calls; post-config --all checks still catch
    # any residual common-config accumulation.
    for key in (
        "remote.origin.url",
        "remote.origin.pushurl",
        "core.sshCommand",
    ):
        run(["git", "-C", str(wt), "config", "--worktree", "--unset-all", key])
    for key, value in (
        ("remote.origin.url", target.ssh_url),
        ("remote.origin.pushurl", target.ssh_url),
        ("core.sshCommand", ssh_command),
    ):
        result = run(
            ["git", "-C", str(wt), "config", "--worktree", key, value]
        )
        if result.returncode != 0:
            raise ExemptGitError(
                EXEMPT_CONFIG_WRITE_FAILED,
                f"failed to set worktree config {key}: {(result.stderr or '').strip()}",
            )


def _require_single_url(
    run: RunGit, wt: Path, *, push: bool, expected: str
) -> None:
    """Require the unique effective origin URL to equal ``expected`` exactly.

    ``get-url --all`` can list the same URL twice when common config and the
    worktree config both name it — that is fine (deduped). Distinct strings
    (HTTPS alongside SSH for the same repo, or a second repository) fail:
    strict single-string equality, no identity-collapse.
    """
    cmd = ["git", "-C", str(wt), "remote", "get-url", "--all"]
    if push:
        cmd.append("--push")
    cmd.append("origin")
    result = run(cmd)
    urls = _url_lines(result)
    label = "push" if push else "fetch"
    if result.returncode != 0 or not urls:
        raise ExemptGitError(
            EXEMPT_ORIGIN_INVALID,
            f"effective {label} origin unreadable after config",
        )
    unique = list(dict.fromkeys(urls))
    if unique != [expected]:
        raise ExemptGitError(
            EXEMPT_ORIGIN_AMBIGUOUS if len(unique) > 1 else EXEMPT_ORIGIN_INVALID,
            f"effective {label} origin {urls!r} != [{expected!r}]",
        )


def prove_exempt_remote(
    worktree: Path | str,
    target: ResolvedTarget,
    *,
    run_git: RunGit | None = None,
    ledger_repo: str | None = None,
) -> ExemptProof:
    """ls-remote must succeed; push --dry-run must match permission-denied fixture.

    Both fetch and push URL views must be exactly ``[target.ssh_url]`` before
    any network probe. Read and push legs dial that verified SSH URL (not the
    remote name), so a multi-URL origin cannot divert the transport.
    """
    run = run_git or _default_run_git
    wt = Path(worktree)

    _require_single_url(run, wt, push=False, expected=target.ssh_url)
    _require_single_url(run, wt, push=True, expected=target.ssh_url)

    # Bind probes to the verified target URL — never the remote name alone.
    read = run(["git", "-C", str(wt), "ls-remote", target.ssh_url, "HEAD"])
    read_fail = classify_read_result(read.returncode, read.stderr or "")
    if read_fail is not None:
        raise ExemptGitError(
            read_fail,
            f"ls-remote failed for {target.ssh_url}: {(read.stderr or '').strip()[:300]}",
        )

    nonce = secrets.token_hex(8)
    ref = f"refs/heads/arb-exempt-deny-proof-{nonce}"
    push = run(
        [
            "git",
            "-C",
            str(wt),
            "push",
            "--dry-run",
            target.ssh_url,
            f"HEAD:{ref}",
        ]
    )
    classification = classify_push_result(
        push.returncode,
        push.stderr or "",
        target_url=target.ssh_url,
    )
    lines = normalize_lines(push.stderr or "")
    if classification != EXEMPT_PUSH_PERMISSION_DENIED:
        if classification == EXEMPT_PUSH_CREDENTIAL_WRITABLE:
            logger.error(
                "[exempt-git] LOUD BLOCKER %s: dry-run ACCEPTED for %s — "
                "machine-user credential is writable; refusing arm/registration",
                EXEMPT_PUSH_CREDENTIAL_WRITABLE,
                target.ssh_url,
            )
        raise ExemptGitError(
            classification,
            f"push dry-run for {target.ssh_url} classified {classification} "
            f"(exit={push.returncode})",
        )
    return ExemptProof(
        classification=classification,
        target=target,
        target_url=target.ssh_url,
        ledger_repo=ledger_repo,
        read_exit=read.returncode,
        push_exit=push.returncode,
        push_stderr_lines=lines,
    )


def prepare_exempt_worktree(
    worktree: Path | str,
    *,
    lane: str,
    ssh_command: str | None,
    expected_fingerprint: str = EXEMPT_BOT_FINGERPRINT,
    ledger_path: Path | str | None = None,
    run_git: RunGit | None = None,
    skip_identity_probe: bool = False,
) -> ExemptProof | None:
    """Full exempt prep: resolve → identity → config → read+push proof.

    Gated lane returns None with no config changes. Every exempt failure is
    terminal — no operator-credential fallback. Ledger path is mandatory.
    """
    if lane != "exempt":
        return None

    cmd, key_path = validate_ssh_command(ssh_command)
    base_run = run_git or _default_run_git
    run = _bind_probe_env(base_run, cmd)

    target = resolve_target_remote(worktree, run_git=run)

    # Ledger is mandatory at arm and preflight — no manufactured default.
    if ledger_path is None:
        raise ExemptGitError(
            EXEMPT_LEDGER_ENTRY_MISSING,
            "BRIDGE_EXEMPT_PROVISIONING_LEDGER is required for exempt lane",
        )
    ledger = load_ledger(ledger_path)
    ledger_repo = match_ledger_entry(ledger, target.owner_repo)
    if ledger_repo is None:
        raise ExemptGitError(
            EXEMPT_LEDGER_ENTRY_MISSING,
            f"no ledger entry for {target.owner_repo}",
        )
    ledger_fp = str(ledger.get("fingerprint") or "")
    if not ledger_fp or ledger_fp != expected_fingerprint:
        raise ExemptGitError(
            EXEMPT_KEY_FINGERPRINT_MISMATCH,
            "ledger fingerprint does not match recorded arb-exempt-bot fingerprint",
        )
    ledger_user = str(ledger.get("machine_user") or "")
    if ledger_user != EXEMPT_BOT_ACCOUNT:
        raise ExemptGitError(
            EXEMPT_IDENTITY_MISMATCH,
            f"ledger machine_user {ledger_user!r} != {EXEMPT_BOT_ACCOUNT!r}",
        )

    fp = key_fingerprint(key_path)
    if fp != expected_fingerprint:
        raise ExemptGitError(
            EXEMPT_KEY_FINGERPRINT_MISMATCH,
            f"key fingerprint {fp} != recorded {expected_fingerprint}",
        )

    if not skip_identity_probe:
        identity = github_ssh_identity(cmd)
        if identity != EXEMPT_BOT_ACCOUNT:
            raise ExemptGitError(
                EXEMPT_IDENTITY_MISMATCH,
                f"GitHub identity {identity!r} != {EXEMPT_BOT_ACCOUNT!r}",
            )

    configure_exempt_worktree(worktree, target, cmd, run_git=run)
    return prove_exempt_remote(
        worktree,
        target,
        run_git=run,
        ledger_repo=ledger_repo,
    )


def supervisor_exempt_settings() -> tuple[str | None, str, Path | None]:
    """Read supervisor process env for exempt SSH command + ledger path."""
    ssh = os.environ.get("BRIDGE_EXEMPT_GIT_SSH_COMMAND")
    fp = os.environ.get("BRIDGE_EXEMPT_GIT_KEY_FINGERPRINT", EXEMPT_BOT_FINGERPRINT)
    ledger = os.environ.get("BRIDGE_EXEMPT_PROVISIONING_LEDGER")
    ledger_path = Path(ledger).expanduser() if ledger else None
    return ssh, fp, ledger_path
