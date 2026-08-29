"""Slice 1d-iii: multi-repository push-less exempt credential.

Uses temporary local repositories and an injectable run_git so every catalog
class, resolver rule, and no-fallback path is exercised without live network.
Live catalog fixtures (permission-denied lines + exit 128) were captured from
the provisioned arb-exempt-bot identity on 2026-07-27.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Callable
from unittest import mock

import pytest

# Module under test — import must exist for GREEN; RED is ImportError / missing API.
from agent_redis_bridge import exempt_git as eg


# ---------------------------------------------------------------------------
# Local-repo helpers
# ---------------------------------------------------------------------------


def _init_repo(path: Path, *, origin: str | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / "README").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    if origin is not None:
        subprocess.run(
            ["git", "remote", "add", "origin", origin],
            cwd=path,
            check=True,
            capture_output=True,
        )
    return path


def _add_worktree(base: Path, name: str) -> Path:
    wt = base / ".claude" / "worktrees" / name
    wt.parent.mkdir(parents=True, exist_ok=True)
    oid = subprocess.run(
        ["git", "-C", str(base), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(base), "worktree", "add", "--detach", str(wt), oid],
        check=True,
        capture_output=True,
    )
    return wt


def _default_ssh_command(key_path: Path) -> str:
    return f"ssh -i {key_path} -o IdentitiesOnly=yes"


def _write_key(path: Path, *, mode: int = 0o600) -> Path:
    path.write_text("dummy-private-key\n", encoding="utf-8")
    path.chmod(mode)
    return path


def _write_ledger(
    path: Path,
    *,
    repos: list[dict] | None = None,
    fingerprint: str | None = None,
) -> Path:
    body = {
        "machine_user": eg.EXEMPT_BOT_ACCOUNT,
        "fingerprint": fingerprint or eg.EXEMPT_BOT_FINGERPRINT,
        "key_label": "arb-exempt-bot",
        "targets": repos
        if repos is not None
        else [
            {
                "repo": "example-org/AgentRedisBridge",
                "aliases": ["markgerrard/ARB"],
                "owner_kind": "organization",
                "grant_path": "example-org/arb-exempt-readonly Read",
            },
            {
                "repo": "example-org/project-b",
                "owner_kind": "organization",
                "grant_path": "example-org/arb-exempt-readonly Read",
            },
            {
                "repo": "example-org/project-c",
                "owner_kind": "organization",
                "grant_path": "example-org/arb-exempt-readonly Read",
            },
        ],
    }
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    return path


class ScriptedGit:
    """Injectable run_git: real git for config/remote ops; scripted for probes."""

    def __init__(
        self,
        *,
        ls_remote: list[tuple[int, str, str]] | None = None,
        push: list[tuple[int, str, str]] | None = None,
        fallback: Callable | None = None,
    ) -> None:
        self.ls_remote = list(ls_remote or [])
        self.push = list(push or [])
        self.calls: list[list[str]] = []
        self.fallback = fallback or self._real

    def _real(self, cmd: list[str], **kwargs):
        return subprocess.run(cmd, capture_output=True, text=True, check=False, **kwargs)

    def __call__(self, cmd: list[str], **kwargs):
        self.calls.append(list(cmd))
        # Probe intercepts
        if "ls-remote" in cmd:
            if not self.ls_remote:
                raise AssertionError(f"unexpected ls-remote: {cmd}")
            code, out, err = self.ls_remote.pop(0)
            return subprocess.CompletedProcess(cmd, code, out, err)
        if "push" in cmd:
            if not self.push:
                raise AssertionError(f"unexpected push: {cmd}")
            code, out, err = self.push.pop(0)
            return subprocess.CompletedProcess(cmd, code, out, err)
        return self.fallback(cmd, **kwargs)


# Live-captured permission-denied fixture (arb-exempt-bot, 2026-07-27).
LIVE_PUSH_DENY_STDERR = (
    "ERROR: Write access to repository not granted.\n"
    "fatal: Could not read from remote repository.\n"
    "\n"
    "Please make sure you have the correct access rights\n"
    "and the repository exists.\n"
)
LIVE_PUSH_DENY_EXIT = 128


def _permission_denied_push() -> tuple[int, str, str]:
    return (LIVE_PUSH_DENY_EXIT, "", LIVE_PUSH_DENY_STDERR)


def _ok_ls_remote() -> tuple[int, str, str]:
    return (0, "abc123\tHEAD\n", "")


# ---------------------------------------------------------------------------
# Catalog fixtures must be exact complete lines, not loose substrings
# ---------------------------------------------------------------------------


def test_catalog_permission_denied_is_complete_lines_not_substring() -> None:
    fixture = eg.PUSH_CLASS_FIXTURES[eg.EXEMPT_PUSH_PERMISSION_DENIED]
    assert fixture["exit"] == 128
    assert "ERROR: Write access to repository not granted." in fixture["lines"]
    assert "fatal: Could not read from remote repository." in fixture["lines"]
    # Must not accept a bare "permission denied" substring as the class key.
    assert not any(line.lower() == "permission denied" for line in fixture["lines"])


def test_catalog_non_matches_are_distinct_named_classes() -> None:
    required = {
        eg.EXEMPT_AUTH_PUBLICKEY,
        eg.EXEMPT_REMOTE_NOT_FOUND,
        eg.EXEMPT_REMOTE_ARCHIVED,
        eg.EXEMPT_PUSH_HOOK_REJECTED,
        eg.EXEMPT_NETWORK_DNS,
        eg.EXEMPT_NETWORK_TIMEOUT,
        eg.EXEMPT_NETWORK_REFUSED,
        eg.EXEMPT_NETWORK_RESET,
        eg.EXEMPT_PUSH_DENIAL_UNPROVEN,
        eg.EXEMPT_PUSH_CREDENTIAL_WRITABLE,
        eg.EXEMPT_REMOTE_READ_UNAVAILABLE,
    }
    assert required <= set(eg.REJECTION_CODES)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def test_resolve_target_from_checkout_ssh_forms(tmp_path: Path) -> None:
    """Literal SSH origins resolve; distinct repos stay distinct."""
    ssh_repo = _init_repo(
        tmp_path / "ssh",
        origin="git@github.com:example-org/project-b.git",
    )
    ssh_repo2 = _init_repo(
        tmp_path / "ssh2",
        origin="git@github.com:example-org/project-c.git",
    )
    t1 = eg.resolve_target_remote(ssh_repo)
    t2 = eg.resolve_target_remote(ssh_repo2)
    assert t1.owner_repo == "example-org/project-b"
    assert t1.ssh_url == "git@github.com:example-org/project-b.git"
    assert t2.owner_repo == "example-org/project-c"
    assert t2.ssh_url == "git@github.com:example-org/project-c.git"
    assert t1.owner_repo != t2.owner_repo


def test_resolve_rejects_missing_ambiguous_and_invalid_origins(tmp_path: Path) -> None:
    bare = _init_repo(tmp_path / "bare")
    with pytest.raises(eg.ExemptGitError) as missing:
        eg.resolve_target_remote(bare)
    assert missing.value.code == eg.EXEMPT_ORIGIN_MISSING

    bad_host = _init_repo(tmp_path / "badhost", origin="git@gitlab.com:acme/repo.git")
    with pytest.raises(eg.ExemptGitError) as host:
        eg.resolve_target_remote(bad_host)
    assert host.value.code == eg.EXEMPT_ORIGIN_INVALID

    local = _init_repo(tmp_path / "local", origin="/tmp/some/path.git")
    with pytest.raises(eg.ExemptGitError) as loc:
        eg.resolve_target_remote(local)
    assert loc.value.code == eg.EXEMPT_ORIGIN_INVALID

    # Option-shaped / NUL / newline cannot be planted via `git remote add`
    # (git rejects them); exercise the normalizer directly.
    with pytest.raises(eg.ExemptGitError) as opt:
        eg.normalize_github_remote("--upload-pack=evil")
    assert opt.value.code == eg.EXEMPT_ORIGIN_INVALID

    with pytest.raises(eg.ExemptGitError) as n:
        eg.normalize_github_remote("git@github.com:acme/repo.git\x00evil")
    assert n.value.code == eg.EXEMPT_ORIGIN_INVALID

    with pytest.raises(eg.ExemptGitError):
        eg.normalize_github_remote("git@github.com:acme/repo.git\n-oProxyCommand=x")


def test_resolve_does_not_use_env_singleton_or_arb_constant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(
        tmp_path / "ph",
        origin="git@github.com:example-org/project-b.git",
    )
    monkeypatch.setenv(
        "BRIDGE_EXEMPT_GIT_REMOTE_URL",
        "git@github.com:markgerrard/ARB.git",
    )
    target = eg.resolve_target_remote(repo)
    assert target.owner_repo == "example-org/project-b"
    assert "AgentRedisBridge" not in target.ssh_url
    assert "markgerrard" not in target.ssh_url


def test_two_checkouts_resolve_own_remotes_same_ssh_identity(tmp_path: Path) -> None:
    a = _init_repo(
        tmp_path / "a",
        origin="git@github.com:example-org/project-b.git",
    )
    b = _init_repo(
        tmp_path / "b",
        origin="git@github.com:example-org/project-c.git",
    )
    ta, tb = eg.resolve_target_remote(a), eg.resolve_target_remote(b)
    assert ta.owner_repo == "example-org/project-b"
    assert tb.owner_repo == "example-org/project-c"
    assert ta.ssh_url != tb.ssh_url
    # Neither resolves to personal ARB unless that is actual origin
    assert ta.owner_repo != "markgerrard/ARB"
    assert tb.owner_repo != "markgerrard/ARB"


def test_redirect_alias_origin_still_resolves_checkout_url(tmp_path: Path) -> None:
    """Checkout may still name markgerrard/ARB; resolver keeps that URL form's owner/repo."""
    repo = _init_repo(
        tmp_path / "arb-old",
        origin="git@github.com:markgerrard/ARB.git",
    )
    target = eg.resolve_target_remote(repo)
    assert target.owner_repo == "markgerrard/ARB"
    assert target.ssh_url == "git@github.com:markgerrard/ARB.git"


# ---------------------------------------------------------------------------
# Worktree config + proof
# ---------------------------------------------------------------------------


def test_gated_lane_makes_no_remote_or_config_change(tmp_path: Path) -> None:
    base = _init_repo(
        tmp_path / "base",
        origin="git@github.com:example-org/project-b.git",
    )
    wt = _add_worktree(base, "gated-wt")
    before_url = subprocess.run(
        ["git", "-C", str(wt), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    result = eg.prepare_exempt_worktree(
        wt,
        lane="gated",
        ssh_command=None,
        expected_fingerprint=eg.EXEMPT_BOT_FINGERPRINT,
        ledger_path=None,
    )
    assert result is None
    after_url = subprocess.run(
        ["git", "-C", str(wt), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert after_url == before_url
    # worktree-local sshCommand must not be set
    ssh = subprocess.run(
        ["git", "-C", str(wt), "config", "--worktree", "--get", "core.sshCommand"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ssh.returncode != 0 or not ssh.stdout.strip()


def test_exempt_refuses_missing_ssh_config(tmp_path: Path) -> None:
    base = _init_repo(
        tmp_path / "base",
        origin="git@github.com:example-org/project-b.git",
    )
    wt = _add_worktree(base, "no-ssh")
    with pytest.raises(eg.ExemptGitError) as exc:
        eg.prepare_exempt_worktree(
            wt,
            lane="exempt",
            ssh_command=None,
            expected_fingerprint=eg.EXEMPT_BOT_FINGERPRINT,
            ledger_path=None,
        )
    assert exc.value.code == eg.EXEMPT_SSH_CONFIG_MISSING


def test_worktree_local_origin_and_sshcommand_leave_common_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _write_key(tmp_path / "key")
    base = _init_repo(
        tmp_path / "base",
        origin="git@github.com:example-org/project-b.git",
    )
    common_url_before = subprocess.run(
        ["git", "-C", str(base), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    wt = _add_worktree(base, "exempt-wt")
    ledger = _write_ledger(tmp_path / "ledger.json")
    ssh_cmd = _default_ssh_command(key)
    scripted = ScriptedGit(
        ls_remote=[_ok_ls_remote()],
        push=[_permission_denied_push()],
    )
    monkeypatch.setattr(eg, "key_fingerprint", lambda _p: eg.EXEMPT_BOT_FINGERPRINT)
    monkeypatch.setattr(eg, "github_ssh_identity", lambda _c: eg.EXEMPT_BOT_ACCOUNT)

    proof = eg.prepare_exempt_worktree(
        wt,
        lane="exempt",
        ssh_command=ssh_cmd,
        expected_fingerprint=eg.EXEMPT_BOT_FINGERPRINT,
        ledger_path=ledger,
        run_git=scripted,
    )
    assert proof is not None
    assert proof.classification == eg.EXEMPT_PUSH_PERMISSION_DENIED
    assert proof.target.owner_repo == "example-org/project-b"
    assert proof.target.ssh_url == "git@github.com:example-org/project-b.git"

    # Common origin unchanged
    common_url_after = subprocess.run(
        ["git", "-C", str(base), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert common_url_after == common_url_before

    # Worktree effective origin is normalized SSH for that same target
    wt_url = subprocess.run(
        ["git", "-C", str(wt), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert wt_url == "git@github.com:example-org/project-b.git"

    wt_ssh = subprocess.run(
        ["git", "-C", str(wt), "config", "--worktree", "--get", "core.sshCommand"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert wt_ssh == ssh_cmd
    assert "IdentitiesOnly=yes" in wt_ssh
    assert str(key) in wt_ssh


def test_fingerprint_and_identity_must_match_before_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _write_key(tmp_path / "key")
    base = _init_repo(
        tmp_path / "base",
        origin="git@github.com:example-org/project-b.git",
    )
    wt = _add_worktree(base, "fp")
    ledger = _write_ledger(tmp_path / "ledger.json")
    ssh_cmd = _default_ssh_command(key)
    scripted = ScriptedGit(
        ls_remote=[_ok_ls_remote()],
        push=[_permission_denied_push()],
    )
    monkeypatch.setattr(eg, "key_fingerprint", lambda _p: "SHA256:wrong")
    monkeypatch.setattr(eg, "github_ssh_identity", lambda _c: eg.EXEMPT_BOT_ACCOUNT)
    with pytest.raises(eg.ExemptGitError) as exc:
        eg.prepare_exempt_worktree(
            wt,
            lane="exempt",
            ssh_command=ssh_cmd,
            expected_fingerprint=eg.EXEMPT_BOT_FINGERPRINT,
            ledger_path=ledger,
            run_git=scripted,
        )
    assert exc.value.code == eg.EXEMPT_KEY_FINGERPRINT_MISMATCH
    # No probe calls
    assert not any("ls-remote" in c for c in scripted.calls)

    monkeypatch.setattr(eg, "key_fingerprint", lambda _p: eg.EXEMPT_BOT_FINGERPRINT)
    monkeypatch.setattr(eg, "github_ssh_identity", lambda _c: "markgerrard")
    scripted2 = ScriptedGit(
        ls_remote=[_ok_ls_remote()],
        push=[_permission_denied_push()],
    )
    with pytest.raises(eg.ExemptGitError) as exc2:
        eg.prepare_exempt_worktree(
            wt,
            lane="exempt",
            ssh_command=ssh_cmd,
            expected_fingerprint=eg.EXEMPT_BOT_FINGERPRINT,
            ledger_path=ledger,
            run_git=scripted2,
        )
    assert exc2.value.code == eg.EXEMPT_IDENTITY_MISMATCH
    assert not any("ls-remote" in c for c in scripted2.calls)


def test_readable_target_plus_permission_denied_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _write_key(tmp_path / "key")
    base = _init_repo(
        tmp_path / "base",
        origin="git@github.com:example-org/project-c.git",
    )
    wt = _add_worktree(base, "ok")
    ledger = _write_ledger(tmp_path / "ledger.json")
    monkeypatch.setattr(eg, "key_fingerprint", lambda _p: eg.EXEMPT_BOT_FINGERPRINT)
    monkeypatch.setattr(eg, "github_ssh_identity", lambda _c: eg.EXEMPT_BOT_ACCOUNT)
    scripted = ScriptedGit(
        ls_remote=[_ok_ls_remote()],
        push=[_permission_denied_push()],
    )
    proof = eg.prepare_exempt_worktree(
        wt,
        lane="exempt",
        ssh_command=_default_ssh_command(key),
        expected_fingerprint=eg.EXEMPT_BOT_FINGERPRINT,
        ledger_path=ledger,
        run_git=scripted,
    )
    assert proof.classification == eg.EXEMPT_PUSH_PERMISSION_DENIED
    assert proof.target.ssh_url == "git@github.com:example-org/project-c.git"
    # Fixtures are keyed to resolved URL — proof carries that URL
    assert proof.target_url == proof.target.ssh_url


def test_read_unavailable_blocks_and_never_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _write_key(tmp_path / "key")
    base = _init_repo(
        tmp_path / "base",
        origin="git@github.com:example-org/unprovisioned.git",
    )
    wt = _add_worktree(base, "unprov")
    # Ledger deliberately omits this target
    ledger = _write_ledger(
        tmp_path / "ledger.json",
        repos=[
            {
                "repo": "example-org/project-b",
                "owner_kind": "organization",
                "grant_path": "team Read",
            }
        ],
    )
    monkeypatch.setattr(eg, "key_fingerprint", lambda _p: eg.EXEMPT_BOT_FINGERPRINT)
    monkeypatch.setattr(eg, "github_ssh_identity", lambda _c: eg.EXEMPT_BOT_ACCOUNT)

    # Missing ledger alone is fail-closed before probe
    with pytest.raises(eg.ExemptGitError) as ledger_exc:
        eg.prepare_exempt_worktree(
            wt,
            lane="exempt",
            ssh_command=_default_ssh_command(key),
            expected_fingerprint=eg.EXEMPT_BOT_FINGERPRINT,
            ledger_path=ledger,
            run_git=ScriptedGit(),
        )
    assert ledger_exc.value.code == eg.EXEMPT_LEDGER_ENTRY_MISSING

    # With ledger entry but read fails: read-unavailable; no push; no alternate URL
    ledger2 = _write_ledger(
        tmp_path / "ledger2.json",
        repos=[
            {
                "repo": "example-org/unprovisioned",
                "owner_kind": "organization",
                "grant_path": "team Read",
            }
        ],
    )
    scripted = ScriptedGit(
        ls_remote=[
            (
                128,
                "",
                "ERROR: Repository not found.\nfatal: Could not read from remote repository.\n",
            )
        ],
        push=[_permission_denied_push()],  # must not be consumed
    )
    operator_helpers: list[str] = []
    real_env = os.environ.copy()

    def tracking_git(cmd, **kwargs):
        # Detect fallback to operator agent / helper / alternate URL
        joined = " ".join(cmd)
        if "GIT_SSH_COMMAND" in kwargs.get("env", {}):
            pass
        env = kwargs.get("env")
        if env is not None and env.get("SSH_AUTH_SOCK") and not os.environ.get("SSH_AUTH_SOCK"):
            operator_helpers.append("ssh-agent")
        if any(x in joined for x in ("credential", "gh auth", "markgerrard")):
            operator_helpers.append(joined)
        return scripted(cmd, **kwargs)

    with pytest.raises(eg.ExemptGitError) as read_exc:
        eg.prepare_exempt_worktree(
            wt,
            lane="exempt",
            ssh_command=_default_ssh_command(key),
            expected_fingerprint=eg.EXEMPT_BOT_FINGERPRINT,
            ledger_path=ledger2,
            run_git=tracking_git,
        )
    assert read_exc.value.code == eg.EXEMPT_REMOTE_NOT_FOUND
    assert not any("push" in c for c in scripted.calls)
    assert operator_helpers == []
    assert len(scripted.push) == 1  # push fixture never consumed
    del real_env  # silence unused


def test_writable_dry_run_fails_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key = _write_key(tmp_path / "key")
    base = _init_repo(
        tmp_path / "base",
        origin="git@github.com:example-org/project-b.git",
    )
    wt = _add_worktree(base, "writable")
    ledger = _write_ledger(tmp_path / "ledger.json")
    monkeypatch.setattr(eg, "key_fingerprint", lambda _p: eg.EXEMPT_BOT_FINGERPRINT)
    monkeypatch.setattr(eg, "github_ssh_identity", lambda _c: eg.EXEMPT_BOT_ACCOUNT)
    scripted = ScriptedGit(
        ls_remote=[_ok_ls_remote()],
        push=[(0, "ok\n", "")],
    )
    with pytest.raises(eg.ExemptGitError) as exc:
        eg.prepare_exempt_worktree(
            wt,
            lane="exempt",
            ssh_command=_default_ssh_command(key),
            expected_fingerprint=eg.EXEMPT_BOT_FINGERPRINT,
            ledger_path=ledger,
            run_git=scripted,
        )
    assert exc.value.code == eg.EXEMPT_PUSH_CREDENTIAL_WRITABLE


def test_arbitrary_nonzero_is_denial_unproven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _write_key(tmp_path / "key")
    base = _init_repo(
        tmp_path / "base",
        origin="git@github.com:example-org/project-b.git",
    )
    wt = _add_worktree(base, "unproven")
    ledger = _write_ledger(tmp_path / "ledger.json")
    monkeypatch.setattr(eg, "key_fingerprint", lambda _p: eg.EXEMPT_BOT_FINGERPRINT)
    monkeypatch.setattr(eg, "github_ssh_identity", lambda _c: eg.EXEMPT_BOT_ACCOUNT)
    scripted = ScriptedGit(
        ls_remote=[_ok_ls_remote()],
        push=[(1, "", "something went wrong\n")],
    )
    with pytest.raises(eg.ExemptGitError) as exc:
        eg.prepare_exempt_worktree(
            wt,
            lane="exempt",
            ssh_command=_default_ssh_command(key),
            expected_fingerprint=eg.EXEMPT_BOT_FINGERPRINT,
            ledger_path=ledger,
            run_git=scripted,
        )
    assert exc.value.code == eg.EXEMPT_PUSH_DENIAL_UNPROVEN


@pytest.mark.parametrize(
    "code,stderr,expected",
    [
        (
            128,
            "Permission denied (publickey).\r\nfatal: Could not read from remote repository.\n",
            eg.EXEMPT_AUTH_PUBLICKEY,
        ),
        (
            128,
            "ERROR: Repository not found.\nfatal: Could not read from remote repository.\n",
            eg.EXEMPT_REMOTE_NOT_FOUND,
        ),
        (
            1,
            "remote: This repository has been archived and cannot accept pushes.\n"
            "remote: error: failed to push some refs\n",
            eg.EXEMPT_REMOTE_ARCHIVED,
        ),
        (
            1,
            "remote: error: GH006: Protected branch update failed for refs/heads/main.\n"
            "remote: error: hook declined to update\n",
            eg.EXEMPT_PUSH_HOOK_REJECTED,
        ),
        (
            128,
            "ssh: Could not resolve hostname github.com: nodename nor servname provided\n",
            eg.EXEMPT_NETWORK_DNS,
        ),
        (
            124,
            "git operation timed out after 30s\n",
            eg.EXEMPT_NETWORK_TIMEOUT,
        ),
        (
            128,
            "ssh: connect to host github.com port 22: Connection refused\n",
            eg.EXEMPT_NETWORK_REFUSED,
        ),
        (
            128,
            "kex_exchange_identification: Connection reset by peer\n",
            eg.EXEMPT_NETWORK_RESET,
        ),
    ],
)
def test_push_failure_classes_are_distinct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    stderr: str,
    expected: str,
) -> None:
    key = _write_key(tmp_path / "key")
    base = _init_repo(
        tmp_path / "base",
        origin="git@github.com:example-org/project-b.git",
    )
    wt = _add_worktree(base, f"cls-{expected}")
    ledger = _write_ledger(tmp_path / "ledger.json")
    monkeypatch.setattr(eg, "key_fingerprint", lambda _p: eg.EXEMPT_BOT_FINGERPRINT)
    monkeypatch.setattr(eg, "github_ssh_identity", lambda _c: eg.EXEMPT_BOT_ACCOUNT)
    scripted = ScriptedGit(
        ls_remote=[_ok_ls_remote()],
        push=[(code, "", stderr)],
    )
    with pytest.raises(eg.ExemptGitError) as exc:
        eg.prepare_exempt_worktree(
            wt,
            lane="exempt",
            ssh_command=_default_ssh_command(key),
            expected_fingerprint=eg.EXEMPT_BOT_FINGERPRINT,
            ledger_path=ledger,
            run_git=scripted,
        )
    assert exc.value.code == expected


def test_fixtures_keyed_to_target_url_so_a_cannot_prove_b(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Classification success is bound to the resolved target URL."""
    key = _write_key(tmp_path / "key")
    a = _init_repo(
        tmp_path / "a",
        origin="git@github.com:example-org/project-b.git",
    )
    b = _init_repo(
        tmp_path / "b",
        origin="git@github.com:example-org/project-c.git",
    )
    wta, wtb = _add_worktree(a, "wa"), _add_worktree(b, "wb")
    ledger = _write_ledger(tmp_path / "ledger.json")
    monkeypatch.setattr(eg, "key_fingerprint", lambda _p: eg.EXEMPT_BOT_FINGERPRINT)
    monkeypatch.setattr(eg, "github_ssh_identity", lambda _c: eg.EXEMPT_BOT_ACCOUNT)

    proof_a = eg.prepare_exempt_worktree(
        wta,
        lane="exempt",
        ssh_command=_default_ssh_command(key),
        expected_fingerprint=eg.EXEMPT_BOT_FINGERPRINT,
        ledger_path=ledger,
        run_git=ScriptedGit(
            ls_remote=[_ok_ls_remote()],
            push=[_permission_denied_push()],
        ),
    )
    proof_b = eg.prepare_exempt_worktree(
        wtb,
        lane="exempt",
        ssh_command=_default_ssh_command(key),
        expected_fingerprint=eg.EXEMPT_BOT_FINGERPRINT,
        ledger_path=ledger,
        run_git=ScriptedGit(
            ls_remote=[_ok_ls_remote()],
            push=[_permission_denied_push()],
        ),
    )
    assert proof_a.target_url != proof_b.target_url
    assert proof_a.target.owner_repo == "example-org/project-b"
    assert proof_b.target.owner_repo == "example-org/project-c"
    # Reusing A's proof object for B's URL must not be considered valid.
    assert not eg.proof_covers_target(proof_a, proof_b.target)


def test_ssh_command_requires_identities_only(tmp_path: Path) -> None:
    key = _write_key(tmp_path / "key")
    with pytest.raises(eg.ExemptGitError) as exc:
        eg.validate_ssh_command(f"ssh -i {key}")
    assert exc.value.code == eg.EXEMPT_SSH_CONFIG_MISSING


def test_bridge_exempt_git_remote_url_never_overrides_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KILL: if implementation restores singleton remote, this goes RED."""
    key = _write_key(tmp_path / "key")
    base = _init_repo(
        tmp_path / "base",
        origin="git@github.com:example-org/project-b.git",
    )
    wt = _add_worktree(base, "singleton-kill")
    ledger = _write_ledger(tmp_path / "ledger.json")
    monkeypatch.setenv(
        "BRIDGE_EXEMPT_GIT_REMOTE_URL",
        "git@github.com:example-org/AgentRedisBridge.git",
    )
    monkeypatch.setattr(eg, "key_fingerprint", lambda _p: eg.EXEMPT_BOT_FINGERPRINT)
    monkeypatch.setattr(eg, "github_ssh_identity", lambda _c: eg.EXEMPT_BOT_ACCOUNT)
    proof = eg.prepare_exempt_worktree(
        wt,
        lane="exempt",
        ssh_command=_default_ssh_command(key),
        expected_fingerprint=eg.EXEMPT_BOT_FINGERPRINT,
        ledger_path=ledger,
        run_git=ScriptedGit(
            ls_remote=[_ok_ls_remote()],
            push=[_permission_denied_push()],
        ),
    )
    assert proof.target.owner_repo == "example-org/project-b"
    assert "AgentRedisBridge" not in proof.target.ssh_url


def test_ledger_accepts_redirect_alias_for_moved_arb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Canonical ledger entry example-org/AgentRedisBridge covers markgerrard alias."""
    key = _write_key(tmp_path / "key")
    base = _init_repo(
        tmp_path / "base",
        origin="git@github.com:markgerrard/ARB.git",
    )
    wt = _add_worktree(base, "alias")
    ledger = _write_ledger(tmp_path / "ledger.json")  # includes alias
    monkeypatch.setattr(eg, "key_fingerprint", lambda _p: eg.EXEMPT_BOT_FINGERPRINT)
    monkeypatch.setattr(eg, "github_ssh_identity", lambda _c: eg.EXEMPT_BOT_ACCOUNT)
    proof = eg.prepare_exempt_worktree(
        wt,
        lane="exempt",
        ssh_command=_default_ssh_command(key),
        expected_fingerprint=eg.EXEMPT_BOT_FINGERPRINT,
        ledger_path=ledger,
        run_git=ScriptedGit(
            ls_remote=[_ok_ls_remote()],
            push=[_permission_denied_push()],
        ),
    )
    assert proof.target.owner_repo == "markgerrard/ARB"
    assert proof.ledger_repo == "example-org/AgentRedisBridge"


def test_classify_push_uses_complete_fixture_not_loose_permission_denied() -> None:
    # Bare "permission denied" alone is NOT the accepted proof class.
    code = eg.classify_push_result(
        128,
        "permission denied\n",
        target_url="git@github.com:example-org/project-b.git",
    )
    assert code != eg.EXEMPT_PUSH_PERMISSION_DENIED
    assert code == eg.EXEMPT_PUSH_DENIAL_UNPROVEN

    code_ok = eg.classify_push_result(
        LIVE_PUSH_DENY_EXIT,
        LIVE_PUSH_DENY_STDERR,
        target_url="git@github.com:example-org/AgentRedisBridge.git",
    )
    assert code_ok == eg.EXEMPT_PUSH_PERMISSION_DENIED


# ---------------------------------------------------------------------------
# Remediation R1–R9 lethality (2026-07-27)
# ---------------------------------------------------------------------------


def test_r1_normalize_rejects_extra_path_userinfo_port_query_double_git() -> None:
    with pytest.raises(eg.ExemptGitError) as extra:
        eg.normalize_github_remote("https://github.com/acme/repo/extra")
    assert extra.value.code == eg.EXEMPT_ORIGIN_INVALID

    with pytest.raises(eg.ExemptGitError) as userinfo:
        eg.normalize_github_remote("https://user:token@github.com/acme/repo.git")
    assert userinfo.value.code == eg.EXEMPT_ORIGIN_INVALID

    with pytest.raises(eg.ExemptGitError) as port:
        eg.normalize_github_remote("https://github.com:443/acme/repo.git")
    assert port.value.code == eg.EXEMPT_ORIGIN_INVALID

    with pytest.raises(eg.ExemptGitError) as query:
        eg.normalize_github_remote("https://github.com/acme/repo.git?foo=1")
    assert query.value.code == eg.EXEMPT_ORIGIN_INVALID

    with pytest.raises(eg.ExemptGitError) as frag:
        eg.normalize_github_remote("https://github.com/acme/repo.git#frag")
    assert frag.value.code == eg.EXEMPT_ORIGIN_INVALID

    with pytest.raises(eg.ExemptGitError) as dbl:
        eg.normalize_github_remote("git@github.com:acme/repo.git.git")
    assert dbl.value.code == eg.EXEMPT_ORIGIN_INVALID


def test_r1_multi_value_origin_is_ambiguous(tmp_path: Path) -> None:
    """EXEMPT_ORIGIN_AMBIGUOUS must fire on real multi-valued origin URLs."""
    repo = _init_repo(
        tmp_path / "multi",
        origin="git@github.com:example-org/project-b.git",
    )
    # Plant a second origin URL (multi-valued remote.origin.url).
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "config",
            "--add",
            "remote.origin.url",
            "git@github.com:example-org/project-c.git",
        ],
        check=True,
        capture_output=True,
    )
    with pytest.raises(eg.ExemptGitError) as exc:
        eg.resolve_target_remote(repo)
    assert exc.value.code == eg.EXEMPT_ORIGIN_AMBIGUOUS


def test_r1_post_config_rejects_distinct_pushurl_accumulation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Distinct fetch/push URLs after configure must not pass proof (accumulation hole)."""
    key = _write_key(tmp_path / "key")
    base = _init_repo(
        tmp_path / "base",
        origin="git@github.com:example-org/project-b.git",
    )
    # Common-level pushurl that differs from the bot target — the hole.
    subprocess.run(
        [
            "git",
            "-C",
            str(base),
            "config",
            "--add",
            "remote.origin.pushurl",
            "git@github.com:operator/writable.git",
        ],
        check=True,
        capture_output=True,
    )
    wt = _add_worktree(base, "accum")
    ledger = _write_ledger(tmp_path / "ledger.json")
    monkeypatch.setattr(eg, "key_fingerprint", lambda _p: eg.EXEMPT_BOT_FINGERPRINT)
    monkeypatch.setattr(eg, "github_ssh_identity", lambda _c: eg.EXEMPT_BOT_ACCOUNT)
    scripted = ScriptedGit(
        ls_remote=[_ok_ls_remote()],
        push=[_permission_denied_push()],
    )
    with pytest.raises(eg.ExemptGitError) as exc:
        eg.prepare_exempt_worktree(
            wt,
            lane="exempt",
            ssh_command=_default_ssh_command(key),
            expected_fingerprint=eg.EXEMPT_BOT_FINGERPRINT,
            ledger_path=ledger,
            run_git=scripted,
        )
    assert exc.value.code == eg.EXEMPT_ORIGIN_AMBIGUOUS


def test_r2_validate_ssh_rejects_duplicate_identities_only_and_F_and_agent(
    tmp_path: Path,
) -> None:
    key = _write_key(tmp_path / "key")
    # First IdentitiesOnly=no wins in ssh; command must be rejected.
    with pytest.raises(eg.ExemptGitError) as dup:
        eg.validate_ssh_command(
            f"ssh -i {key} -o IdentitiesOnly=no -o IdentitiesOnly=yes"
        )
    assert dup.value.code == eg.EXEMPT_SSH_CONFIG_MISSING

    with pytest.raises(eg.ExemptGitError) as conf:
        eg.validate_ssh_command(
            f"ssh -F /tmp/evil_config -i {key} -o IdentitiesOnly=yes"
        )
    assert conf.value.code == eg.EXEMPT_SSH_CONFIG_MISSING

    with pytest.raises(eg.ExemptGitError) as agent:
        eg.validate_ssh_command(
            f"ssh -i {key} -o IdentitiesOnly=yes -o IdentityAgent=/tmp/agent.sock"
        )
    assert agent.value.code == eg.EXEMPT_SSH_CONFIG_MISSING


def test_r2_ambient_git_ssh_command_cannot_reach_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lethality: ambient GIT_SSH_COMMAND must not appear in probe env.

    Pre-fix detector inspected kwargs['env'] that production never passed;
    this asserts the production path always pins the validated command.
    """
    key = _write_key(tmp_path / "key")
    base = _init_repo(
        tmp_path / "base",
        origin="git@github.com:example-org/project-b.git",
    )
    wt = _add_worktree(base, "ambient-ssh")
    ledger = _write_ledger(tmp_path / "ledger.json")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /tmp/operator-key -o IdentitiesOnly=yes")
    monkeypatch.setenv("GIT_SSH", "/tmp/evil-ssh")
    monkeypatch.setenv("GIT_ASKPASS", "/tmp/askpass")
    monkeypatch.setenv("SSH_ASKPASS", "/tmp/ssh-askpass")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.sshCommand")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "ssh -i /tmp/operator-key")
    monkeypatch.setattr(eg, "key_fingerprint", lambda _p: eg.EXEMPT_BOT_FINGERPRINT)
    monkeypatch.setattr(eg, "github_ssh_identity", lambda _c: eg.EXEMPT_BOT_ACCOUNT)

    validated = _default_ssh_command(key)
    seen_envs: list[dict] = []
    scripted = ScriptedGit(
        ls_remote=[_ok_ls_remote()],
        push=[_permission_denied_push()],
    )

    def tracking(cmd, **kwargs):
        env = kwargs.get("env")
        assert env is not None, "production must pass an explicit env"
        seen_envs.append(dict(env))
        # Ambient overrides must be scrubbed; pinned identity is the validated cmd.
        assert env.get("GIT_SSH") is None or "evil-ssh" not in str(env.get("GIT_SSH"))
        assert env.get("GIT_ASKPASS") != "/tmp/askpass"
        assert env.get("SSH_ASKPASS") != "/tmp/ssh-askpass"
        assert "GIT_CONFIG_COUNT" not in env
        assert "GIT_CONFIG_KEY_0" not in env
        assert "GIT_CONFIG_VALUE_0" not in env
        assert env.get("GIT_SSH_COMMAND") == validated
        assert env.get("GIT_TERMINAL_PROMPT") == "0"
        assert "/tmp/operator-key" not in (env.get("GIT_SSH_COMMAND") or "")
        return scripted(cmd, **kwargs)

    proof = eg.prepare_exempt_worktree(
        wt,
        lane="exempt",
        ssh_command=validated,
        expected_fingerprint=eg.EXEMPT_BOT_FINGERPRINT,
        ledger_path=ledger,
        run_git=tracking,
    )
    assert proof is not None
    assert seen_envs, "at least one probe must have run with an env"
    assert all(e.get("GIT_SSH_COMMAND") == validated for e in seen_envs)


def test_r3_key_permissions_enforced(tmp_path: Path) -> None:
    loose = _write_key(tmp_path / "loose-key", mode=0o644)
    with pytest.raises(eg.ExemptGitError) as exc:
        eg.validate_ssh_command(f"ssh -i {loose} -o IdentitiesOnly=yes")
    assert exc.value.code == eg.EXEMPT_KEY_PERMISSIONS

    # Parent dir with group bits is also refused.
    parent = tmp_path / "open-dir"
    parent.mkdir(mode=0o755)
    key = _write_key(parent / "k", mode=0o600)
    # Ensure parent really is group-readable (umask may tighten mkdir).
    parent.chmod(0o755)
    with pytest.raises(eg.ExemptGitError) as pexc:
        eg.validate_ssh_command(f"ssh -i {key} -o IdentitiesOnly=yes")
    assert pexc.value.code == eg.EXEMPT_KEY_PERMISSIONS


def test_r4_ledger_path_none_is_hard_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _write_key(tmp_path / "key")
    base = _init_repo(
        tmp_path / "base",
        origin="git@github.com:example-org/project-b.git",
    )
    wt = _add_worktree(base, "noleger")
    monkeypatch.setattr(eg, "key_fingerprint", lambda _p: eg.EXEMPT_BOT_FINGERPRINT)
    monkeypatch.setattr(eg, "github_ssh_identity", lambda _c: eg.EXEMPT_BOT_ACCOUNT)
    with pytest.raises(eg.ExemptGitError) as exc:
        eg.prepare_exempt_worktree(
            wt,
            lane="exempt",
            ssh_command=_default_ssh_command(key),
            expected_fingerprint=eg.EXEMPT_BOT_FINGERPRINT,
            ledger_path=None,
            run_git=ScriptedGit(),
        )
    assert exc.value.code == eg.EXEMPT_LEDGER_ENTRY_MISSING

    with pytest.raises(eg.ExemptGitError) as load_exc:
        eg.load_ledger(None)
    assert load_exc.value.code == eg.EXEMPT_LEDGER_ENTRY_MISSING


def test_r5_fixture_rejects_substring_only_match() -> None:
    """Complete-line fixtures must not accept unanchored substring containment."""
    # Required line is a proper superstring of a required fixture token only as
    # a substring of a longer line — must NOT match.
    code = eg.classify_push_result(
        128,
        "NOTE: ERROR: Write access to repository not granted. (see docs)\n"
        "fatal: Could not read from remote repository.\n",
        target_url="git@github.com:example-org/project-b.git",
    )
    assert code == eg.EXEMPT_PUSH_DENIAL_UNPROVEN

    # Empty / non-SSH target_url cannot accept.
    code2 = eg.classify_push_result(
        LIVE_PUSH_DENY_EXIT,
        LIVE_PUSH_DENY_STDERR,
        target_url="",
    )
    assert code2 == eg.EXEMPT_PUSH_DENIAL_UNPROVEN
    code3 = eg.classify_push_result(
        LIVE_PUSH_DENY_EXIT,
        LIVE_PUSH_DENY_STDERR,
        target_url="https://github.com/example-org/project-b.git",
    )
    assert code3 == eg.EXEMPT_PUSH_DENIAL_UNPROVEN


def test_r6_default_run_git_sets_timeout() -> None:
    """DEFAULT timeout must be applied so exit-124 is reachable in production."""
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with mock.patch("agent_redis_bridge.exempt_git.subprocess.run", fake_run):
        eg._default_run_git(["git", "status"])
    assert captured.get("timeout") == eg.DEFAULT_GIT_TIMEOUT_SEC


def test_r6_github_identity_timeout_and_keygen_oserror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom_timeout(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=30)

    monkeypatch.setattr(eg.subprocess, "run", boom_timeout)
    with pytest.raises(eg.ExemptGitError) as exc:
        eg.github_ssh_identity("ssh -i /tmp/k -o IdentitiesOnly=yes")
    assert exc.value.code == eg.EXEMPT_NETWORK_TIMEOUT

    def boom_os(*_a, **_k):
        raise FileNotFoundError("ssh-keygen")

    monkeypatch.setattr(eg.subprocess, "run", boom_os)
    with pytest.raises(eg.ExemptGitError) as kexc:
        eg.key_fingerprint(tmp_path / "missing")
    # key_fingerprint checks file via ssh-keygen; FileNotFoundError → fingerprint code
    assert kexc.value.code == eg.EXEMPT_KEY_FINGERPRINT_MISMATCH


def test_r8_non_catalog_code_raises() -> None:
    with pytest.raises(ValueError):
        eg.ExemptGitError("not-a-catalog-code", "x")


# ---------------------------------------------------------------------------
# rem2 N1–N4 + P2 folds (2026-07-27)
# ---------------------------------------------------------------------------


def test_n1_https_origin_refused_not_collapsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rem3 rescope of N1: HTTPS origin is refused (no identity-collapse).

    rem2 treated common HTTPS + worktree SSH as one target; owner rescope
    requires a literal SSH-form origin instead.
    """
    key = _write_key(tmp_path / "key")
    base = _init_repo(
        tmp_path / "base",
        origin="https://github.com/example-org/project-b.git",
    )
    wt = _add_worktree(base, "https-common")
    ledger = _write_ledger(tmp_path / "ledger.json")
    monkeypatch.setattr(eg, "key_fingerprint", lambda _p: eg.EXEMPT_BOT_FINGERPRINT)
    monkeypatch.setattr(eg, "github_ssh_identity", lambda _c: eg.EXEMPT_BOT_ACCOUNT)
    scripted = ScriptedGit(
        ls_remote=[_ok_ls_remote()],
        push=[_permission_denied_push()],
    )
    with pytest.raises(eg.ExemptGitError) as exc:
        eg.prepare_exempt_worktree(
            wt,
            lane="exempt",
            ssh_command=_default_ssh_command(key),
            expected_fingerprint=eg.EXEMPT_BOT_FINGERPRINT,
            ledger_path=ledger,
            run_git=scripted,
        )
    assert exc.value.code == eg.EXEMPT_ORIGIN_NOT_SSH


def test_n1_distinct_repos_still_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N1: repository B alongside A remains exempt-origin-ambiguous."""
    key = _write_key(tmp_path / "key")
    base = _init_repo(
        tmp_path / "base",
        origin="git@github.com:example-org/project-b.git",
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(base),
            "config",
            "--add",
            "remote.origin.url",
            "git@github.com:example-org/project-c.git",
        ],
        check=True,
        capture_output=True,
    )
    wt = _add_worktree(base, "two-repos")
    # Multi-value is on common config; resolve itself must refuse.
    with pytest.raises(eg.ExemptGitError) as exc:
        eg.resolve_target_remote(wt)
    assert exc.value.code == eg.EXEMPT_ORIGIN_AMBIGUOUS

    # Post-config single-url path: plant distinct URLs after configure shape.
    base2 = _init_repo(
        tmp_path / "base2",
        origin="git@github.com:example-org/project-b.git",
    )
    wt2 = _add_worktree(base2, "accum2")
    ledger = _write_ledger(tmp_path / "ledger2.json")
    monkeypatch.setattr(eg, "key_fingerprint", lambda _p: eg.EXEMPT_BOT_FINGERPRINT)
    monkeypatch.setattr(eg, "github_ssh_identity", lambda _c: eg.EXEMPT_BOT_ACCOUNT)
    # Common pushurl of a different repo — still ambiguous after configure.
    subprocess.run(
        [
            "git",
            "-C",
            str(base2),
            "config",
            "--add",
            "remote.origin.pushurl",
            "git@github.com:operator/writable.git",
        ],
        check=True,
        capture_output=True,
    )
    scripted = ScriptedGit(
        ls_remote=[_ok_ls_remote()],
        push=[_permission_denied_push()],
    )
    with pytest.raises(eg.ExemptGitError) as pexc:
        eg.prepare_exempt_worktree(
            wt2,
            lane="exempt",
            ssh_command=_default_ssh_command(key),
            expected_fingerprint=eg.EXEMPT_BOT_FINGERPRINT,
            ledger_path=ledger,
            run_git=scripted,
        )
    assert pexc.value.code == eg.EXEMPT_ORIGIN_AMBIGUOUS


def test_n3_build_probe_env_scrubs_git_dir_and_neutralizes_file_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N3: ambient GIT_DIR / GIT_CONFIG_GLOBAL must not reach probes.

    Pre-fix: GIT_DIR passes through build_probe_env; file-based insteadOf
    can rewrite resolution. After fix: scrub + GIT_CONFIG_GLOBAL=/dev/null.
    """
    evil_global = tmp_path / "evil.gitconfig"
    evil_global.write_text(
        "[url \"git@github.com:evil/redirected.git\"]\n"
        "\tinsteadOf = git@github.com:example-org/project-b.git\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "other-repo" / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "other-repo"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "evil-index"))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(tmp_path / "evil-objects"))
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", str(tmp_path / "alt-objs"))
    monkeypatch.setenv("GIT_COMMON_DIR", str(tmp_path / "other-repo" / ".git"))
    monkeypatch.setenv("GIT_CONFIG", str(tmp_path / "evil-single.gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(evil_global))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "system.gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "0")
    monkeypatch.setenv("GIT_EXEC_PATH", str(tmp_path / "evil-git-exec"))
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /tmp/operator-key")

    env = eg.build_probe_env("ssh -i /tmp/validated -o IdentitiesOnly=yes")
    # Derived from git --local-env-vars (+ explicit non-repo controls).
    assert "GIT_DIR" not in env
    assert "GIT_WORK_TREE" not in env
    assert "GIT_INDEX_FILE" not in env
    assert "GIT_OBJECT_DIRECTORY" not in env
    assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in env
    assert "GIT_COMMON_DIR" not in env
    assert "GIT_CONFIG" not in env
    assert "GIT_EXEC_PATH" not in env
    assert env.get("GIT_CONFIG_GLOBAL") == "/dev/null"
    assert env.get("GIT_CONFIG_NOSYSTEM") == "1"
    assert "GIT_CONFIG_SYSTEM" not in env
    assert env.get("GIT_SSH_COMMAND") == "ssh -i /tmp/validated -o IdentitiesOnly=yes"
    assert env.get("GIT_TERMINAL_PROMPT") == "0"


def test_n3_insteadof_via_git_config_global_does_not_rewrite_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N3 lethality: insteadOf in a GIT_CONFIG_GLOBAL file must not change resolve."""
    repo = _init_repo(
        tmp_path / "repo",
        origin="git@github.com:example-org/project-b.git",
    )
    evil_global = tmp_path / "evil.gitconfig"
    evil_global.write_text(
        "[url \"git@github.com:evil/redirected.git\"]\n"
        "\tinsteadOf = git@github.com:example-org/project-b.git\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(evil_global))

    # Without neutralization, git remote get-url may apply insteadOf.
    # Production resolve must use build_probe_env via bound runner.
    key = _write_key(tmp_path / "key")
    validated = _default_ssh_command(key)
    bound = eg._bind_probe_env(eg._default_run_git, validated)
    target = eg.resolve_target_remote(repo, run_git=bound)
    assert target.owner_repo == "example-org/project-b"
    assert target.ssh_url == "git@github.com:example-org/project-b.git"


def test_p2_nonnumeric_https_port_is_catalog_code() -> None:
    """Nonnumeric HTTPS port must raise catalog EXEMPT_ORIGIN_INVALID, not ValueError."""
    with pytest.raises(eg.ExemptGitError) as exc:
        eg.normalize_github_remote("https://github.com:abc/acme/repo.git")
    assert exc.value.code == eg.EXEMPT_ORIGIN_INVALID


def test_p2_github_repo_name_with_dot_git_prefix_is_allowed() -> None:
    """Real org repo org/.github must not be rejected by the .git substring guard."""
    t = eg.normalize_github_remote("git@github.com:example-org/.github.git")
    assert t.owner_repo == "example-org/.github"
    assert t.ssh_url == "git@github.com:example-org/.github.git"
    t2 = eg.normalize_github_remote("https://github.com/org/.github")
    assert t2.owner_repo == "org/.github"


def test_p2_https_trailing_slash_and_doubled_separator_rejected() -> None:
    for bad in (
        "https://github.com/acme/repo/",
        "https://github.com/acme/repo.git/",
        "https://github.com/acme//repo.git",
        "https://github.com//acme/repo.git",
    ):
        with pytest.raises(eg.ExemptGitError) as exc:
            eg.normalize_github_remote(bad)
        assert exc.value.code == eg.EXEMPT_ORIGIN_INVALID, bad


def test_p2_github_ssh_identity_prepends_enforced_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enforced StrictHostKeyChecking/BatchMode must bind (prepend, first-value-wins)."""
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = list(argv)
        return subprocess.CompletedProcess(
            argv, 1, "", "Hi arb-exempt-bot! You've successfully authenticated"
        )

    monkeypatch.setattr(eg.subprocess, "run", fake_run)
    identity = eg.github_ssh_identity(
        "ssh -i /tmp/k -o IdentitiesOnly=yes -o StrictHostKeyChecking=no"
    )
    assert identity == eg.EXEMPT_BOT_ACCOUNT
    argv = captured["argv"]
    # Enforced options appear before any operator -o options (and before host).
    host_idx = argv.index("git@github.com")
    enforced_strict = None
    operator_strict = None
    for i, tok in enumerate(argv[:host_idx]):
        if tok == "-o" and i + 1 < host_idx:
            opt = argv[i + 1]
            if opt.lower().startswith("stricthostkeychecking="):
                if enforced_strict is None and opt.lower().endswith("=yes"):
                    enforced_strict = i
                elif opt.lower().endswith("=no"):
                    operator_strict = i
    assert enforced_strict is not None, f"enforced StrictHostKeyChecking=yes missing: {argv}"
    assert operator_strict is not None, f"operator StrictHostKeyChecking=no missing: {argv}"
    assert enforced_strict < operator_strict, (
        f"enforced option must precede operator (ssh first-value-wins): {argv}"
    )


# ---------------------------------------------------------------------------
# rem3 S1–S3 (owner rescope 2026-07-28 — literal SSH origin only)
# ---------------------------------------------------------------------------


def test_s1_https_written_origin_refused_at_resolve(tmp_path: Path) -> None:
    """S1: HTTPS-written origin is refused with exempt-origin-not-ssh at arm resolve.

    RED prediction (pre-fix): resolve_target_remote accepts HTTPS and returns
    a normalized SSH target (rem2 identity-collapse accommodation).
    Hermetic resolve is required: ambient insteadOf rewrites get-url to SSH.
    """
    https_repo = _init_repo(
        tmp_path / "https",
        origin="https://github.com/example-org/project-b.git",
    )
    with pytest.raises(eg.ExemptGitError) as exc:
        eg.resolve_target_remote(https_repo)
    assert exc.value.code == eg.EXEMPT_ORIGIN_NOT_SSH
    assert "git remote set-url origin" in (exc.value.detail or "")
    assert "git@github.com:example-org/project-b.git" in (exc.value.detail or "")


def test_s1_same_repo_https_pushurl_alongside_worktree_ssh_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1/r3 P1-1: common HTTPS pushurl for the SAME repo + worktree SSH is refused.

    Pre-fix prediction: identity-collapse treats both as one target and PROOF
    PASSES — that is the r3 P1-1 hole. After fix: EXEMPT_ORIGIN_AMBIGUOUS (strict
    string equality on the push view).
    """
    key = _write_key(tmp_path / "key")
    base = _init_repo(
        tmp_path / "base",
        origin="git@github.com:example-org/project-b.git",
    )
    # Common-scope pushurl for the SAME repo in HTTPS form — the r3 hole.
    subprocess.run(
        [
            "git",
            "-C",
            str(base),
            "config",
            "--add",
            "remote.origin.pushurl",
            "https://github.com/example-org/project-b.git",
        ],
        check=True,
        capture_output=True,
    )
    wt = _add_worktree(base, "same-repo-https-pushurl")
    ledger = _write_ledger(tmp_path / "ledger.json")
    monkeypatch.setattr(eg, "key_fingerprint", lambda _p: eg.EXEMPT_BOT_FINGERPRINT)
    monkeypatch.setattr(eg, "github_ssh_identity", lambda _c: eg.EXEMPT_BOT_ACCOUNT)
    scripted = ScriptedGit(
        ls_remote=[_ok_ls_remote()],
        push=[_permission_denied_push()],
    )
    with pytest.raises(eg.ExemptGitError) as exc:
        eg.prepare_exempt_worktree(
            wt,
            lane="exempt",
            ssh_command=_default_ssh_command(key),
            expected_fingerprint=eg.EXEMPT_BOT_FINGERPRINT,
            ledger_path=ledger,
            run_git=scripted,
        )
    assert exc.value.code == eg.EXEMPT_ORIGIN_AMBIGUOUS


def test_s2_proof_legs_bind_to_verified_ssh_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S2: ls-remote and push dry-run dial the verified target.ssh_url, not remote name.

    Binding by remote name lets a multi-URL origin dial HTTPS while the SSH
    denial line still classifies. Explicit URL keeps the proof on the verified target.
    """
    key = _write_key(tmp_path / "key")
    base = _init_repo(
        tmp_path / "base",
        origin="git@github.com:example-org/project-b.git",
    )
    wt = _add_worktree(base, "s2-bind")
    ledger = _write_ledger(tmp_path / "ledger.json")
    monkeypatch.setattr(eg, "key_fingerprint", lambda _p: eg.EXEMPT_BOT_FINGERPRINT)
    monkeypatch.setattr(eg, "github_ssh_identity", lambda _c: eg.EXEMPT_BOT_ACCOUNT)
    scripted = ScriptedGit(
        ls_remote=[_ok_ls_remote()],
        push=[_permission_denied_push()],
    )
    proof = eg.prepare_exempt_worktree(
        wt,
        lane="exempt",
        ssh_command=_default_ssh_command(key),
        expected_fingerprint=eg.EXEMPT_BOT_FINGERPRINT,
        ledger_path=ledger,
        run_git=scripted,
    )
    assert proof is not None
    target_url = "git@github.com:example-org/project-b.git"
    ls_calls = [c for c in scripted.calls if "ls-remote" in c]
    push_calls = [c for c in scripted.calls if "push" in c]
    assert ls_calls, "ls-remote must run"
    assert push_calls, "push dry-run must run"
    for c in ls_calls:
        assert target_url in c, f"ls-remote must dial verified URL, got {c}"
        assert c[c.index("ls-remote") + 1] != "origin" or target_url in c
    for c in push_calls:
        assert target_url in c, f"push must dial verified URL, got {c}"


def test_s3_poisoned_git_common_dir_and_git_config_cannot_retarget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S3: ambient GIT_COMMON_DIR / GIT_CONFIG must not change resolve or probes."""
    real = _init_repo(
        tmp_path / "real",
        origin="git@github.com:example-org/project-b.git",
    )
    other = _init_repo(
        tmp_path / "other",
        origin="git@github.com:evil/redirected.git",
    )
    other_git = other / ".git"
    evil_config = tmp_path / "evil.gitconfig"
    evil_config.write_text(
        "[remote \"origin\"]\n"
        "\turl = git@github.com:evil/from-git-config.git\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_COMMON_DIR", str(other_git))
    monkeypatch.setenv("GIT_CONFIG", str(evil_config))
    monkeypatch.setenv("GIT_GRAFT_FILE", str(tmp_path / "grafts"))
    monkeypatch.setenv("GIT_IMPLICIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_NO_REPLACE_OBJECTS", "1")
    monkeypatch.setenv("GIT_PREFIX", "evil/")
    monkeypatch.setenv("GIT_REPLACE_REF_BASE", "refs/replace-evil")
    monkeypatch.setenv("GIT_SHALLOW_FILE", str(tmp_path / "shallow"))

    env = eg.build_probe_env("ssh -i /tmp/validated -o IdentitiesOnly=yes")
    for key in (
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_GRAFT_FILE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_PREFIX",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
    ):
        assert key not in env, f"{key} must be scrubbed from probe env"

    key = _write_key(tmp_path / "key")
    validated = _default_ssh_command(key)
    bound = eg._bind_probe_env(eg._default_run_git, validated)
    target = eg.resolve_target_remote(real, run_git=bound)
    assert target.owner_repo == "example-org/project-b"
    assert target.ssh_url == "git@github.com:example-org/project-b.git"


def test_s3_scrub_set_derived_from_git_local_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S3: every name from `git rev-parse --local-env-vars` is scrubbed (derived, not hardcoded)."""
    listed = subprocess.run(
        ["git", "rev-parse", "--local-env-vars"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    names = [ln.strip() for ln in listed.splitlines() if ln.strip()]
    assert names, "git must declare local-env-vars on this host"
    for name in names:
        monkeypatch.setenv(name, f"/tmp/poisoned-for-{name}")
    env = eg.build_probe_env("ssh -i /tmp/v -o IdentitiesOnly=yes")
    for name in names:
        if name == "GIT_CONFIG_GLOBAL":
            # Scrubbed then re-pinned to neutralize file-based config.
            assert env.get(name) == "/dev/null"
        elif name == "GIT_CONFIG_NOSYSTEM":
            assert env.get(name) == "1"
        else:
            assert name not in env or env.get(name) != f"/tmp/poisoned-for-{name}", (
                f"{name} must not carry ambient value into probe env"
            )
            assert name not in env, f"{name} from --local-env-vars must be scrubbed"


def test_s3_local_env_vars_failure_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S3 version-drift guard: empty/failed --local-env-vars refuses, never short static list."""

    def boom(*_a, **_k):
        return subprocess.CompletedProcess(
            args=["git", "rev-parse", "--local-env-vars"],
            returncode=1,
            stdout="",
            stderr="fail",
        )

    monkeypatch.setattr(eg.subprocess, "run", boom)
    # Clear any cache so the poisoned run is exercised (test-visible reset).
    eg.reset_local_env_vars_cache()
    with pytest.raises(eg.ExemptGitError) as exc:
        eg.build_probe_env("ssh -i /tmp/v -o IdentitiesOnly=yes")
    assert exc.value.code in {
        eg.EXEMPT_PREP_INTERNAL_ERROR,
        getattr(eg, "EXEMPT_PREP_INTERNAL_ERROR", "exempt-prep-internal-error"),
    }


# ---------------------------------------------------------------------------
# rem4 T1–T3 (scrub floor, narrow SSH admission, pin --dry-run)
# ---------------------------------------------------------------------------


def test_t1_path_poisoned_short_local_env_vars_still_scrubs_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T1: scrub set must not shrink below the known repo-scoping floor.

    RED prediction (pre-fix): a fake `git` on PATH that returns only three
    names shrinks probe_scrub_keys; GIT_COMMON_DIR survives into build_probe_env.
    """
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    # Plausible short list — fails none of today's empty/fail guards.
    fake_git.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"rev-parse\" ] && [ \"$2\" = \"--local-env-vars\" ]; then\n"
        "  printf '%s\\n' GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE\n"
        "  exit 0\n"
        "fi\n"
        "exec /usr/bin/git \"$@\"\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")
    eg.reset_local_env_vars_cache()

    monkeypatch.setenv("GIT_COMMON_DIR", str(tmp_path / "evil" / ".git"))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "evil2" / ".git"))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(tmp_path / "evil-objects"))
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", str(tmp_path / "evil-alt"))

    scrub = eg.probe_scrub_keys()
    floor = {
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
    missing = floor - set(scrub)
    assert not missing, f"scrub set shrank below floor; missing {sorted(missing)}"

    env = eg.build_probe_env("ssh -i /tmp/v -o IdentitiesOnly=yes")
    assert "GIT_COMMON_DIR" not in env, "GIT_COMMON_DIR must still be scrubbed under short derivation"
    assert "GIT_DIR" not in env
    assert "GIT_OBJECT_DIRECTORY" not in env
    assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in env


def test_t2_noncanonical_ssh_origin_refused_at_arm(tmp_path: Path) -> None:
    """T2: non-canonical SSH spellings refuse at arm with exempt-origin-not-ssh.

    RED prediction (pre-fix): git@github.com:owner/repo (no .git) and
    ssh://git@github.com/owner/repo are admitted and normalize to a different
    string — preflight later overwrites to canonical, arm hits ambiguous.
    """
    for label, origin in (
        ("no-dot-git", "git@github.com:example-org/project-b"),
        ("ssh-slash", "ssh://git@github.com/example-org/project-b"),
        ("ssh-slash-git", "ssh://git@github.com/example-org/project-b.git"),
    ):
        repo = _init_repo(tmp_path / label, origin=origin)
        with pytest.raises(eg.ExemptGitError) as exc:
            eg.resolve_target_remote(repo)
        assert exc.value.code == eg.EXEMPT_ORIGIN_NOT_SSH, (
            f"{label}: expected NOT_SSH, got {exc.value.code}"
        )
        detail = exc.value.detail or ""
        assert "git remote set-url origin" in detail
        assert "git@github.com:example-org/project-b.git" in detail

    # Canonical form still admits.
    ok = _init_repo(
        tmp_path / "canonical",
        origin="git@github.com:example-org/project-b.git",
    )
    target = eg.resolve_target_remote(ok)
    assert target.ssh_url == "git@github.com:example-org/project-b.git"
    assert target.owner_repo == "example-org/project-b"


def test_t2_is_literal_ssh_origin_is_canonical_only() -> None:
    """T2: shared admission predicate accepts only git@github.com:owner/repo.git."""
    assert eg._is_literal_ssh_origin("git@github.com:example-org/project-b.git")
    assert not eg._is_literal_ssh_origin("git@github.com:example-org/project-b")
    assert not eg._is_literal_ssh_origin(
        "ssh://git@github.com/example-org/project-b.git"
    )
    assert not eg._is_literal_ssh_origin(
        "https://github.com/example-org/project-b.git"
    )


def test_t3_push_probe_argv_contains_dry_run_and_nonce_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T3: push probe argv must contain --dry-run and a nonce-bearing deny ref.

    RED prediction (pre-fix): suite never asserts --dry-run or the generated
    ref; deleting either property leaves tests green.
    """
    key = _write_key(tmp_path / "key")
    base = _init_repo(
        tmp_path / "base",
        origin="git@github.com:example-org/project-b.git",
    )
    wt = _add_worktree(base, "t3-dry-run")
    ledger = _write_ledger(tmp_path / "ledger.json")
    monkeypatch.setattr(eg, "key_fingerprint", lambda _p: eg.EXEMPT_BOT_FINGERPRINT)
    monkeypatch.setattr(eg, "github_ssh_identity", lambda _c: eg.EXEMPT_BOT_ACCOUNT)

    fixed_nonce = "a1b2c3d4e5f67890"
    monkeypatch.setattr(eg.secrets, "token_hex", lambda _n=8: fixed_nonce)

    scripted = ScriptedGit(
        ls_remote=[_ok_ls_remote()],
        push=[_permission_denied_push()],
    )
    proof = eg.prepare_exempt_worktree(
        wt,
        lane="exempt",
        ssh_command=_default_ssh_command(key),
        expected_fingerprint=eg.EXEMPT_BOT_FINGERPRINT,
        ledger_path=ledger,
        run_git=scripted,
    )
    assert proof is not None
    push_calls = [c for c in scripted.calls if "push" in c]
    assert push_calls, "push probe must run"
    push_argv = push_calls[0]
    assert "--dry-run" in push_argv, (
        f"push probe must pass --dry-run; argv={push_argv}"
    )
    expected_ref = f"refs/heads/arb-exempt-deny-proof-{fixed_nonce}"
    assert any(expected_ref in str(tok) for tok in push_argv), (
        f"push probe must target nonce-bearing ref {expected_ref!r}; argv={push_argv}"
    )
    # Ref must carry the generated token_hex suffix — not a hardcoded constant alone.
    assert fixed_nonce in " ".join(push_argv)
    assert "HEAD:" + expected_ref in " ".join(
        tok if isinstance(tok, str) else "" for tok in push_argv
    ) or any(tok == f"HEAD:{expected_ref}" for tok in push_argv)


def test_local_env_vars_cache_reset_is_test_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2: cache must be resettable so tests are not order-dependent."""
    eg.reset_local_env_vars_cache()
    first = eg._git_local_env_vars()
    assert first
    # Poison the cache with an empty-looking stand-in, then reset.
    monkeypatch.setattr(eg, "_local_env_vars_cache", frozenset({"GIT_DIR"}))
    assert eg._git_local_env_vars() == frozenset({"GIT_DIR"})
    eg.reset_local_env_vars_cache()
    restored = eg._git_local_env_vars()
    assert "GIT_COMMON_DIR" in restored or len(restored) >= 3
    assert restored == first or "GIT_DIR" in restored
