"""Seat-preflight checks against real temporary seat files."""
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import plistlib
import subprocess
import sys
from pathlib import Path


_path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "seat-preflight"
_spec = importlib.util.spec_from_loader(
    "seat_preflight",
    importlib.machinery.SourceFileLoader("seat_preflight", str(_path)),
)
seat = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seat)


def _redis_env() -> dict[str, str]:
    return {
        "AGENT_REDIS_HOST": "redis.example",
        "AGENT_REDIS_PORT": "6379",
        "AGENT_REDIS_DB": "12",
        "AGENT_REDIS_PREFIX": "agent_scratch:",
    }


def test_missing_env_file_fails_check_one(tmp_path):
    ok, message = seat.check_env_file(tmp_path / ".env")
    assert not ok
    assert ".env" in message


def test_required_redis_keys_pass_and_missing_key_is_named():
    ok, message = seat.check_redis_required(_redis_env())
    assert ok, message

    env = _redis_env()
    del env["AGENT_REDIS_PREFIX"]
    ok, message = seat.check_redis_required(env)
    assert not ok
    assert "AGENT_REDIS_PREFIX" in message


def test_local_mcp_without_dsn_quotes_downstream_runtime_error():
    ok, message = seat.check_local_mcp_dsn({"ARB_MEMORY_LOCAL_MCP": "1"}, pathlib.Path("/tmp"))
    assert not ok
    assert "ARB_MEMORY_LOCAL_DSN is missing/empty" in message


def test_local_mcp_dev_derives_tier_dsn_from_readers_file(tmp_path):
    readers = tmp_path / ".arb-memory-local" / "readers.env"
    readers.parent.mkdir()
    readers.write_text(
        "export ARB_MEMORY_LOCAL_DSN_DEV='postgresql://reader@localhost:5432/local'\n"
    )

    ok, message = seat.check_local_mcp_dsn({"ARB_MEMORY_LOCAL_MCP": "dev"}, tmp_path)
    assert ok, message


def test_read_env_file_strips_export_prefix_and_quotes(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("export   EXPORTED_KEY='exported value'\n")

    assert seat.read_env_file(env_file) == {"EXPORTED_KEY": "exported value"}


def test_cross_store_same_host_and_db_passes():
    env = {
        "ARB_MEMORY_DSN": "postgresql://writer:pw@db.example:5432/bridge",
        "ARB_MEMORY_LOCAL_DSN": "postgresql://reader:pw@db.example:5432/bridge",
    }
    ok, message = seat.check_cross_store(env)
    assert ok, message


def test_cross_store_different_host_or_db_fails_and_override_warns():
    env = {
        "ARB_MEMORY_DSN": "postgresql://writer:pw@writer.example:5432/bridge",
        "ARB_MEMORY_LOCAL_DSN": "postgresql://reader:pw@reader.example:5432/local",
    }
    ok, message = seat.check_cross_store(env)
    assert not ok
    assert "ARB_MEMORY_DSN" in message

    env["ARB_MEMORY_LOCAL_ALLOW_CROSS_STORE"] = "1"
    ok, message = seat.check_cross_store(env)
    assert ok
    assert "WARNING" in message


def test_cross_store_different_port_fails_via_fallback(monkeypatch):
    monkeypatch.setitem(sys.modules, "arb_memory.local_read_policy", None)
    env = {
        "ARB_MEMORY_DSN": "postgresql://writer:pw@db.example:5432/bridge",
        "ARB_MEMORY_LOCAL_DSN": "postgresql://reader:pw@db.example:5433/bridge",
    }

    ok, message = seat.check_cross_store(env)

    assert not ok
    assert "ARB_MEMORY_DSN" in message


def test_workdir_happy_and_sad_paths(tmp_path):
    ok, message = seat.check_workdir({"AGENT_WORKDIR": str(tmp_path)})
    assert ok, message

    ok, message = seat.check_workdir({"AGENT_WORKDIR": str(tmp_path / "gone")})
    assert not ok
    assert "AGENT_WORKDIR" in message


def test_program_and_working_directory_happy_and_sad_paths(tmp_path):
    program = tmp_path / "engine"
    program.write_text("#!/bin/sh\nexit 0\n")
    program.chmod(program.stat().st_mode | 0o111)
    working = tmp_path / "work"
    working.mkdir()

    ok, message = seat.check_program(
        {"ProgramArguments": [str(program)], "WorkingDirectory": str(working)}
    )
    assert ok, message

    ok, message = seat.check_program(
        {"ProgramArguments": [str(tmp_path / "missing")], "WorkingDirectory": str(working)}
    )
    assert not ok
    assert "ProgramArguments[0]" in message

    ok, message = seat.check_program(
        {"ProgramArguments": [str(program)], "WorkingDirectory": str(tmp_path / "missing")}
    )
    assert not ok
    assert "WorkingDirectory" in message


def test_bakeoff_manifest_mode_rejects_non_value_free_diagnostics():
    ok, message = seat.check_bakeoff_manifest({"schema_version": "manifest-v2", "arms": [], "extensions": {}})
    assert not ok
    assert "arms" in message


def test_load_input_resolves_program_arguments_over_environment_variables(tmp_path):
    cli_env_file = tmp_path / "cli.env"
    cli_workdir = tmp_path / "cli-workdir"
    cli_workdir.mkdir()
    wrong_env_file = tmp_path / "wrong.env"
    wrong_workdir = tmp_path / "wrong-workdir"
    program = tmp_path / "engine"
    program.write_text("#!/bin/sh\nexit 0\n")
    program.chmod(program.stat().st_mode | 0o111)
    plist_path = tmp_path / "seat.plist"
    with plist_path.open("wb") as handle:
        plistlib.dump(
            {
                "ProgramArguments": [
                    str(program),
                    "--env-file",
                    str(cli_env_file),
                    "--workdir",
                    str(cli_workdir),
                    "--project",
                    "cli-project",
                ],
                "EnvironmentVariables": {
                    "AGENT_ENV_FILE": str(wrong_env_file),
                    "AGENT_WORKDIR": str(wrong_workdir),
                    "AGENT_PROJECT": "env-project",
                },
            },
            handle,
        )

    plist, env_path, process_env, mode = seat._load_input(plist_path)

    assert plist["ProgramArguments"][0] == str(program)
    assert env_path == cli_env_file
    assert process_env["AGENT_ENV_FILE"] == str(cli_env_file)
    assert process_env["AGENT_WORKDIR"] == str(cli_workdir)
    assert process_env["AGENT_PROJECT"] == "cli-project"
    assert mode == "plist"


def test_trusted_senders_happy_and_sad_paths():
    ok, message = seat.check_trusted_senders(
        {"AGENT_TRUSTED_SENDERS": "claude-project-dev=trusted,human-mark=human"}
    )
    assert ok, message

    ok, message = seat.check_trusted_senders(
        {"AGENT_TRUSTED_SENDERS": "claude-project-dev,=trusted"}
    )
    assert not ok
    assert "AGENT_TRUSTED_SENDERS" in message

    ok, message = seat.check_trusted_senders({})
    assert ok
    assert "WARNING" in message


def _write_seat_plist(tmp_path, *, local_mcp=""):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AGENT_REDIS_HOST=redis.example\n"
        "AGENT_REDIS_PORT=6379\n"
        "AGENT_REDIS_DB=12\n"
        "AGENT_REDIS_PREFIX=agent_scratch:\n"
        "AGENT_TRUSTED_SENDERS=claude-project-dev=trusted\n"
    )
    workdir = tmp_path / "work"
    workdir.mkdir()
    program = tmp_path / "engine"
    program.write_text("#!/bin/sh\nexit 0\n")
    program.chmod(program.stat().st_mode | 0o111)
    plist_path = tmp_path / "seat.plist"
    # Mirrors live launchd shape: vantage + flag posture in EnvironmentVariables;
    # BRIDGE_SUPERVISOR_PYTHON pins the redis-capable interpreter (E25(a)).
    environment = {
        "AGENT_ENV_FILE": str(env_file),
        "AGENT_WORKDIR": str(workdir),
        "BRIDGE_WORKER_VANTAGE": "bridge-test",
        "BRIDGE_CLAIM_GATE": "0",
        "BRIDGE_TASK_REF_REQUIRED": "0",
        "BRIDGE_SUPERVISOR_PYTHON": sys.executable,
    }
    if local_mcp:
        environment["ARB_MEMORY_LOCAL_MCP"] = local_mcp
    with plist_path.open("wb") as handle:
        plistlib.dump(
            {
                "ProgramArguments": [str(program)],
                "WorkingDirectory": str(workdir),
                "EnvironmentVariables": environment,
            },
            handle,
        )
    return plist_path


def test_plist_main_passes_healthy_synthetic_seat(tmp_path):
    plist_path = _write_seat_plist(tmp_path)
    assert seat.run_main([str(plist_path)]) == 0


def test_plist_main_fails_when_local_mcp_dsn_fault_is_injected(tmp_path):
    plist_path = _write_seat_plist(tmp_path, local_mcp="1")
    assert seat.run_main([str(plist_path)]) == 1


def _write_luna_plist(tmp_path, *, fault=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    env_file = tmp_path / "luna.env"
    env_file.write_text(
        "AGENT_REDIS_HOST=redis.example\n"
        "AGENT_REDIS_PORT=6379\n"
        "AGENT_REDIS_DB=12\n"
        "AGENT_REDIS_PREFIX=agent_scratch:\n"
        "AGENT_TRUSTED_SENDERS=claude-project-dev=trusted\n"
    )
    home = tmp_path / "home"
    readers = home / ".arb-memory-local" / "readers.env"
    if not fault:
        readers.parent.mkdir(parents=True)
        readers.write_text(
            "export ARB_MEMORY_LOCAL_DSN_DEV='postgresql://reader@localhost:5432/local'\n"
        )
    workdir = tmp_path / "luna-workdir"
    workdir.mkdir()
    program = tmp_path / "luna-engine"
    program.write_text("#!/bin/sh\nexit 0\n")
    program.chmod(program.stat().st_mode | 0o111)
    plist_path = tmp_path / "luna.plist"
    with plist_path.open("wb") as handle:
        plistlib.dump(
            {
                "ProgramArguments": [str(program)],
                "WorkingDirectory": str(workdir),
                "EnvironmentVariables": {
                    "AGENT_ENV_FILE": str(env_file),
                    "AGENT_WORKDIR": str(workdir),
                    "ARB_MEMORY_LOCAL_MCP": "dev",
                    "HOME": str(home),
                    "BRIDGE_WORKER_VANTAGE": "bridge-test",
                    "BRIDGE_CLAIM_GATE": "0",
                    "BRIDGE_TASK_REF_REQUIRED": "0",
                    "BRIDGE_SUPERVISOR_PYTHON": sys.executable,
                },
            },
            handle,
        )
    return plist_path


def _write_registry_x_plist(tmp_path, *, fault=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    env_file = tmp_path / "registry-x.env"
    env_file.write_text(
        "AGENT_REDIS_HOST=redis.example\n"
        "AGENT_REDIS_PORT=6379\n"
        "AGENT_REDIS_DB=12\n"
        "AGENT_REDIS_PREFIX=agent_scratch:\n"
        "AGENT_TRUSTED_SENDERS=claude-project-dev=trusted\n"
    )
    workdir = tmp_path / "registry-x-workdir"
    workdir.mkdir()
    missing_workdir = tmp_path / "missing-registry-x-workdir"
    program = tmp_path / "registry-x-engine"
    program.write_text("#!/bin/sh\nexit 0\n")
    program.chmod(program.stat().st_mode | 0o111)
    plist_path = tmp_path / "registry-x.plist"
    with plist_path.open("wb") as handle:
        plistlib.dump(
            {
                "ProgramArguments": [
                    str(program),
                    "--env-file",
                    str(env_file),
                    "--workdir",
                    str(missing_workdir if fault else workdir),
                    "--project",
                    "registry-x",
                ],
                "EnvironmentVariables": {
                    "AGENT_ENV_FILE": str(tmp_path / "wrong.env"),
                    "AGENT_WORKDIR": str(tmp_path / "wrong-workdir"),
                    "BRIDGE_WORKER_VANTAGE": "bridge-test",
                    "BRIDGE_CLAIM_GATE": "0",
                    "BRIDGE_TASK_REF_REQUIRED": "0",
                    "BRIDGE_SUPERVISOR_PYTHON": sys.executable,
                },
                "WorkingDirectory": str(workdir),
            },
            handle,
        )
    return plist_path


def test_luna_shaped_plist_passes_and_fault_fails(tmp_path):
    assert seat.run_main([str(_write_luna_plist(tmp_path / "healthy"))]) == 0
    assert seat.run_main([str(_write_luna_plist(tmp_path / "fault", fault=True))]) == 1


def test_registry_x_shaped_plist_passes_and_resolved_workdir_fault_fails(tmp_path):
    assert seat.run_main([str(_write_registry_x_plist(tmp_path / "healthy"))]) == 0
    assert seat.run_main([str(_write_registry_x_plist(tmp_path / "fault", fault=True))]) == 1


def test_bare_invocation_passes_luna_shaped_plist(tmp_path):
    plist_path = _write_luna_plist(tmp_path)
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}

    result = subprocess.run(
        [str(_path), str(plist_path)],
        cwd=pathlib.Path(__file__).resolve().parent.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_main_returns_usage_code_for_bad_arguments():
    assert seat.run_main([]) == 2
    assert seat.run_main(["seat.plist", "--unknown"]) == 2


# ---------------------------------------------------------------------------
# Slice 1d-iii: exempt Git preflight contract
# ---------------------------------------------------------------------------


def test_gated_seat_skips_exempt_git_variables():
    ok, message = seat.check_exempt_git({**_redis_env(), "BRIDGE_WORKTREE_LANE": "gated"})
    assert ok, message
    assert "not required" in message


def test_exempt_seat_requires_ledger_and_workdir(tmp_path):
    ok, message = seat.check_exempt_git(
        {
            **_redis_env(),
            "BRIDGE_WORKTREE_LANE": "exempt",
            "BRIDGE_EXEMPT_GIT_SSH_COMMAND": "ssh -i /no/such/key -o IdentitiesOnly=yes",
        }
    )
    assert not ok
    assert "AGENT_WORKDIR" in message or "ledger" in message.lower() or "BRID" in message

    ok2, message2 = seat.check_exempt_git(
        {
            **_redis_env(),
            "BRIDGE_WORKTREE_LANE": "exempt",
            "AGENT_WORKDIR": str(tmp_path),
            "BRIDGE_EXEMPT_GIT_SSH_COMMAND": "ssh -i /no/such/key -o IdentitiesOnly=yes",
        }
    )
    assert not ok2
    assert "LEDGER" in message2.upper() or "ledger" in message2.lower()


def _init_checkout_with_origin(path: Path, origin: str) -> Path:
    """Minimal git checkout with origin so preflight can resolve a target."""
    import subprocess

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
        ["git", "commit", "-q", "-m", "init"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "remote", "add", "origin", origin],
        cwd=path,
        check=True,
        capture_output=True,
    )
    return path


def _write_preflight_key(tmp_path: Path) -> Path:
    """Mode-safe machine-user key for preflight tests (parent ≤0700, key 0600)."""
    key_dir = tmp_path / "sshdir"
    key_dir.mkdir(mode=0o700)
    key = key_dir / "k"
    key.write_text("dummy\n", encoding="utf-8")
    key.chmod(0o600)
    return key


def test_exempt_remote_url_singleton_is_not_authority(tmp_path, monkeypatch):
    """BRIDGE_EXEMPT_GIT_REMOTE_URL must not redirect preflight target resolution."""
    import agent_redis_bridge.exempt_git as eg

    calls: list = []

    def fake_prepare(worktree, **kwargs):
        calls.append((worktree, kwargs))
        from agent_redis_bridge.exempt_git import (
            EXEMPT_PUSH_PERMISSION_DENIED,
            ExemptProof,
            ResolvedTarget,
        )

        target = ResolvedTarget(
            owner_repo="example-org/project-b",
            ssh_url="git@github.com:example-org/project-b.git",
            raw_origin="git@github.com:example-org/project-b.git",
        )
        return ExemptProof(
            classification=EXEMPT_PUSH_PERMISSION_DENIED,
            target=target,
            target_url=target.ssh_url,
            ledger_repo="example-org/project-b",
            read_exit=0,
            push_exit=128,
            push_stderr_lines=("ERROR: Write access to repository not granted.",),
        )

    monkeypatch.setattr(eg, "prepare_exempt_worktree", fake_prepare)
    # Re-import check path uses package module; patch where check imports from.
    monkeypatch.setattr(
        "agent_redis_bridge.exempt_git.prepare_exempt_worktree", fake_prepare
    )

    key = _write_preflight_key(tmp_path)
    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        '{"machine_user":"arb-exempt-bot","fingerprint":"%s","targets":[]}'
        % eg.EXEMPT_BOT_FINGERPRINT,
        encoding="utf-8",
    )
    checkout = _init_checkout_with_origin(
        tmp_path / "checkout",
        "git@github.com:example-org/project-b.git",
    )
    env = {
        **_redis_env(),
        "BRIDGE_WORKTREE_LANE": "exempt",
        "AGENT_WORKDIR": str(checkout),
        "BRIDGE_EXEMPT_GIT_SSH_COMMAND": f"ssh -i {key} -o IdentitiesOnly=yes",
        "BRIDGE_EXEMPT_PROVISIONING_LEDGER": str(ledger),
        "BRIDGE_EXEMPT_GIT_REMOTE_URL": "git@github.com:example-org/AgentRedisBridge.git",
    }
    ok, message = seat.check_exempt_git(env)
    assert ok, message
    assert "project-b" in message
    assert "not authoritative" in message
    assert calls, "prepare must be invoked"
    # R9: prepare runs against a throwaway clone, not the live AGENT_WORKDIR.
    assert Path(calls[0][0]) != checkout
    assert Path(calls[0][0]).is_dir() or not Path(calls[0][0]).exists()


def test_exempt_proof_failure_surfaces_catalog_code(tmp_path, monkeypatch):
    from agent_redis_bridge.exempt_git import (
        EXEMPT_REMOTE_READ_UNAVAILABLE,
        ExemptGitError,
    )

    def boom(*_a, **_k):
        raise ExemptGitError(EXEMPT_REMOTE_READ_UNAVAILABLE, "no read")

    monkeypatch.setattr(
        "agent_redis_bridge.exempt_git.prepare_exempt_worktree", boom
    )
    key = _write_preflight_key(tmp_path)
    ledger = tmp_path / "ledger.json"
    ledger.write_text("{}", encoding="utf-8")
    checkout = _init_checkout_with_origin(
        tmp_path / "checkout",
        "git@github.com:example-org/project-b.git",
    )
    ok, message = seat.check_exempt_git(
        {
            **_redis_env(),
            "BRIDGE_WORKTREE_LANE": "exempt",
            "AGENT_WORKDIR": str(checkout),
            "BRIDGE_EXEMPT_GIT_SSH_COMMAND": f"ssh -i {key} -o IdentitiesOnly=yes",
            "BRIDGE_EXEMPT_PROVISIONING_LEDGER": str(ledger),
        }
    )
    assert not ok
    assert EXEMPT_REMOTE_READ_UNAVAILABLE in message


def test_r9_preflight_does_not_mutate_agent_workdir_config(tmp_path, monkeypatch):
    """N4/R9: real prepare path must leave AGENT_WORKDIR config bytes untouched.

    Lethality: CONFIG-WRITE leg is real (configure_exempt_worktree runs on the
    throwaway clone). Probe legs are scripted. Reverting R9 so preflight prepares
    the live checkout turns this RED because live origin/wtc/sshCommand change.
    """
    import agent_redis_bridge.exempt_git as eg
    import subprocess

    checkout = _init_checkout_with_origin(
        tmp_path / "checkout",
        "git@github.com:example-org/project-b.git",
    )
    before_list = subprocess.run(
        ["git", "-C", str(checkout), "config", "--list", "--local"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    before_url = subprocess.run(
        ["git", "-C", str(checkout), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    before_wtc = subprocess.run(
        ["git", "-C", str(checkout), "config", "--get", "extensions.worktreeConfig"],
        capture_output=True,
        text=True,
        check=False,
    )
    before_wtc_val = before_wtc.stdout.strip() if before_wtc.returncode == 0 else ""

    key_dir = tmp_path / "sshdir"
    key_dir.mkdir(mode=0o700)
    key2 = key_dir / "k"
    key2.write_text("dummy\n", encoding="utf-8")
    key2.chmod(0o600)

    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "machine_user": eg.EXEMPT_BOT_ACCOUNT,
                "fingerprint": eg.EXEMPT_BOT_FINGERPRINT,
                "targets": [
                    {
                        "repo": "example-org/project-b",
                        "owner_kind": "organization",
                        "grant_path": "team Read",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    # Real prepare: identity/fingerprint stubbed; probe legs scripted; config writes REAL.
    monkeypatch.setattr(eg, "key_fingerprint", lambda _p: eg.EXEMPT_BOT_FINGERPRINT)
    monkeypatch.setattr(eg, "github_ssh_identity", lambda _c: eg.EXEMPT_BOT_ACCOUNT)

    live_deny = (
        "ERROR: Write access to repository not granted.\n"
        "fatal: Could not read from remote repository.\n"
    )
    real_default = eg._default_run_git

    def scripted_probes(cmd, **kwargs):
        if "ls-remote" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "abc\tHEAD\n", "")
        if "push" in cmd:
            return subprocess.CompletedProcess(cmd, 128, "", live_deny)
        return real_default(cmd, **kwargs)

    monkeypatch.setattr(eg, "_default_run_git", scripted_probes)

    ok, message = seat.check_exempt_git(
        {
            **_redis_env(),
            "BRIDGE_WORKTREE_LANE": "exempt",
            "AGENT_WORKDIR": str(checkout),
            "BRIDGE_EXEMPT_GIT_SSH_COMMAND": f"ssh -i {key2} -o IdentitiesOnly=yes",
            "BRIDGE_EXEMPT_PROVISIONING_LEDGER": str(ledger),
        }
    )
    assert ok, message
    assert "project-b" in message

    after_list = subprocess.run(
        ["git", "-C", str(checkout), "config", "--list", "--local"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert after_list == before_list

    after_url = subprocess.run(
        ["git", "-C", str(checkout), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert after_url == before_url

    after_wtc = subprocess.run(
        ["git", "-C", str(checkout), "config", "--get", "extensions.worktreeConfig"],
        capture_output=True,
        text=True,
        check=False,
    )
    after_wtc_val = after_wtc.stdout.strip() if after_wtc.returncode == 0 else ""
    assert after_wtc_val == before_wtc_val

    ssh = subprocess.run(
        ["git", "-C", str(checkout), "config", "--worktree", "--get", "core.sshCommand"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ssh.returncode != 0 or not ssh.stdout.strip()


def test_s1_preflight_refuses_https_written_origin(tmp_path, monkeypatch):
    """S1/S4: preflight applies the same SSH-only admission as arm."""
    import agent_redis_bridge.exempt_git as eg

    key_dir = tmp_path / "sshdir"
    key_dir.mkdir(mode=0o700)
    key = key_dir / "k"
    key.write_text("dummy\n", encoding="utf-8")
    key.chmod(0o600)

    checkout = _init_checkout_with_origin(
        tmp_path / "checkout",
        "https://github.com/example-org/project-b.git",
    )
    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "machine_user": eg.EXEMPT_BOT_ACCOUNT,
                "fingerprint": eg.EXEMPT_BOT_FINGERPRINT,
                "targets": [
                    {
                        "repo": "example-org/project-b",
                        "owner_kind": "organization",
                        "grant_path": "team Read",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(eg, "key_fingerprint", lambda _p: eg.EXEMPT_BOT_FINGERPRINT)
    monkeypatch.setattr(eg, "github_ssh_identity", lambda _c: eg.EXEMPT_BOT_ACCOUNT)

    ok, message = seat.check_exempt_git(
        {
            **_redis_env(),
            "BRIDGE_WORKTREE_LANE": "exempt",
            "AGENT_WORKDIR": str(checkout),
            "BRIDGE_EXEMPT_GIT_SSH_COMMAND": f"ssh -i {key} -o IdentitiesOnly=yes",
            "BRIDGE_EXEMPT_PROVISIONING_LEDGER": str(ledger),
        }
    )
    assert not ok
    assert eg.EXEMPT_ORIGIN_NOT_SSH in message
    assert "git remote set-url origin" in message


def test_n2_preflight_resolve_and_clone_use_probe_env(tmp_path, monkeypatch):
    """N2: every preflight git subprocess must run under build_probe_env(validated).

    Pre-fix prediction: resolve_target_remote / clone / set-url pass no env=,
    so ambient GIT_SSH_COMMAND / GIT_CONFIG_GLOBAL reach them.
    """
    import agent_redis_bridge.exempt_git as eg
    import subprocess

    key_dir = tmp_path / "sshdir"
    key_dir.mkdir(mode=0o700)
    key = key_dir / "k"
    key.write_text("dummy\n", encoding="utf-8")
    key.chmod(0o600)
    validated = f"ssh -i {key} -o IdentitiesOnly=yes"

    checkout = _init_checkout_with_origin(
        tmp_path / "checkout",
        "git@github.com:example-org/project-b.git",
    )
    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "machine_user": eg.EXEMPT_BOT_ACCOUNT,
                "fingerprint": eg.EXEMPT_BOT_FINGERPRINT,
                "targets": [
                    {
                        "repo": "example-org/project-b",
                        "owner_kind": "organization",
                        "grant_path": "team Read",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /tmp/operator-key -o IdentitiesOnly=yes")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "wrong" / ".git"))
    monkeypatch.setattr(eg, "key_fingerprint", lambda _p: eg.EXEMPT_BOT_FINGERPRINT)
    monkeypatch.setattr(eg, "github_ssh_identity", lambda _c: eg.EXEMPT_BOT_ACCOUNT)

    live_deny = (
        "ERROR: Write access to repository not granted.\n"
        "fatal: Could not read from remote repository.\n"
    )
    seen_envs: list[dict] = []
    real_default = eg._default_run_git
    real_subproc = subprocess.run

    def tracking_default(cmd, **kwargs):
        env = kwargs.get("env")
        if env is not None:
            seen_envs.append(dict(env))
            assert env.get("GIT_SSH_COMMAND") == validated
            assert "GIT_DIR" not in env
            assert env.get("GIT_CONFIG_GLOBAL") == "/dev/null"
            assert "/tmp/operator-key" not in (env.get("GIT_SSH_COMMAND") or "")
        if "ls-remote" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "abc\tHEAD\n", "")
        if "push" in cmd:
            return subprocess.CompletedProcess(cmd, 128, "", live_deny)
        return real_default(cmd, **kwargs)

    def tracking_subproc(cmd, **kwargs):
        # Preflight's direct clone/set-url must also pin env.
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git":
            env = kwargs.get("env")
            assert env is not None, f"preflight git must pass env=: {cmd}"
            seen_envs.append(dict(env))
            assert env.get("GIT_SSH_COMMAND") == validated
            assert "GIT_DIR" not in env
            assert env.get("GIT_CONFIG_GLOBAL") == "/dev/null"
        return real_subproc(cmd, **kwargs)

    monkeypatch.setattr(eg, "_default_run_git", tracking_default)
    # check_exempt_git does `import subprocess` locally — patch the module.
    monkeypatch.setattr(subprocess, "run", tracking_subproc)

    ok, message = seat.check_exempt_git(
        {
            **_redis_env(),
            "BRIDGE_WORKTREE_LANE": "exempt",
            "AGENT_WORKDIR": str(checkout),
            "BRIDGE_EXEMPT_GIT_SSH_COMMAND": validated,
            "BRIDGE_EXEMPT_PROVISIONING_LEDGER": str(ledger),
        }
    )
    assert ok, message
    assert seen_envs, "preflight/arm probes must pass an explicit sanitized env"
    assert all(e.get("GIT_SSH_COMMAND") == validated for e in seen_envs)


def test_t2_preflight_refuses_noncanonical_ssh_origins(tmp_path, monkeypatch):
    """T2: preflight shares arm's canonical-only SSH admission.

    RED prediction (pre-fix): non-canonical SSH is admitted; preflight
    overwrites the throwaway clone to the canonical URL and PASSES while arm
    would refuse (or hit ambiguous on multi-value common+worktree).
    """
    import agent_redis_bridge.exempt_git as eg

    key_dir = tmp_path / "sshdir"
    key_dir.mkdir(mode=0o700)
    key = key_dir / "k"
    key.write_text("dummy\n", encoding="utf-8")
    key.chmod(0o600)

    ledger = tmp_path / "ledger.json"
    ledger.write_text(
        json.dumps(
            {
                "machine_user": eg.EXEMPT_BOT_ACCOUNT,
                "fingerprint": eg.EXEMPT_BOT_FINGERPRINT,
                "targets": [
                    {
                        "repo": "example-org/project-b",
                        "owner_kind": "organization",
                        "grant_path": "team Read",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(eg, "key_fingerprint", lambda _p: eg.EXEMPT_BOT_FINGERPRINT)
    monkeypatch.setattr(eg, "github_ssh_identity", lambda _c: eg.EXEMPT_BOT_ACCOUNT)

    for label, origin in (
        ("no-dot-git", "git@github.com:example-org/project-b"),
        ("ssh-slash", "ssh://git@github.com/example-org/project-b.git"),
    ):
        checkout = _init_checkout_with_origin(tmp_path / label, origin)
        ok, message = seat.check_exempt_git(
            {
                **_redis_env(),
                "BRIDGE_WORKTREE_LANE": "exempt",
                "AGENT_WORKDIR": str(checkout),
                "BRIDGE_EXEMPT_GIT_SSH_COMMAND": f"ssh -i {key} -o IdentitiesOnly=yes",
                "BRIDGE_EXEMPT_PROVISIONING_LEDGER": str(ledger),
            }
        )
        assert not ok, f"{label}: preflight must refuse non-canonical SSH, got ok={ok} {message}"
        assert eg.EXEMPT_ORIGIN_NOT_SSH in message
        assert "git remote set-url origin" in message
        assert "git@github.com:example-org/project-b.git" in message

    # Canonical still passes when probes are scripted.
    import subprocess

    checkout = _init_checkout_with_origin(
        tmp_path / "canonical",
        "git@github.com:example-org/project-b.git",
    )
    live_deny = (
        "ERROR: Write access to repository not granted.\n"
        "fatal: Could not read from remote repository.\n"
    )
    real_default = eg._default_run_git

    def scripted_probes(cmd, **kwargs):
        if "ls-remote" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "abc\tHEAD\n", "")
        if "push" in cmd:
            return subprocess.CompletedProcess(cmd, 128, "", live_deny)
        return real_default(cmd, **kwargs)

    monkeypatch.setattr(eg, "_default_run_git", scripted_probes)
    ok, message = seat.check_exempt_git(
        {
            **_redis_env(),
            "BRIDGE_WORKTREE_LANE": "exempt",
            "AGENT_WORKDIR": str(checkout),
            "BRIDGE_EXEMPT_GIT_SSH_COMMAND": f"ssh -i {key} -o IdentitiesOnly=yes",
            "BRIDGE_EXEMPT_PROVISIONING_LEDGER": str(ledger),
        }
    )
    assert ok, message


# ---------------------------------------------------------------------------
# Slice 1d-vi / Task 7: rollout prerequisite checks + checklist modes
# ---------------------------------------------------------------------------
#
# Real shapes mirrored by synthetic doubles (rail 1):
# - Plist EnvironmentVariables carries ARB_MEMORY_LOCAL_DSN and
#   BRIDGE_WORKER_VANTAGE (live launchd seats; env-file does not feed daemon
#   process env for those credentials).
# - Registry evidence records match redis_io.register capability fields:
#   worker_vantage, task_wire=legacy-or-ref-v1, brief_hydrate=v1 (E26).
# - Flag defaults stay "0" (envelope.TASK_REF_REQUIRED_ENV / BRIDGE_CLAIM_GATE).


def _base_seat_env(**extra) -> dict[str, str]:
    env = {
        **_redis_env(),
        "AGENT_TRUSTED_SENDERS": "claude-project-dev=trusted",
        "BRIDGE_WORKER_VANTAGE": "bridge-test",
        "BRIDGE_CLAIM_GATE": "0",
        "BRIDGE_TASK_REF_REQUIRED": "0",
        "BRIDGE_WORKTREE_LANE": "gated",
    }
    env.update(extra)
    return env


def test_worker_vantage_requires_nonblank():
    ok, message = seat.check_worker_vantage(_base_seat_env())
    assert ok, message
    assert "BRIDGE_WORKER_VANTAGE" in message

    ok, message = seat.check_worker_vantage(_base_seat_env(BRIDGE_WORKER_VANTAGE=""))
    assert not ok
    assert "worker-vantage" in message or "BRIDGE_WORKER_VANTAGE" in message

    ok, message = seat.check_worker_vantage(
        {k: v for k, v in _base_seat_env().items() if k != "BRIDGE_WORKER_VANTAGE"}
    )
    assert not ok
    assert "BRIDGE_WORKER_VANTAGE" in message


def test_flag_posture_default_off_and_rejects_invalid():
    ok, message = seat.check_flag_posture(_base_seat_env())
    assert ok, message
    assert "BRIDGE_CLAIM_GATE=0" in message
    assert "BRIDGE_TASK_REF_REQUIRED=0" in message

    ok, message = seat.check_flag_posture(
        _base_seat_env(BRIDGE_CLAIM_GATE="maybe")
    )
    assert not ok
    assert "BRIDGE_CLAIM_GATE" in message

    ok, message = seat.check_flag_posture(
        _base_seat_env(BRIDGE_TASK_REF_REQUIRED="2")
    )
    assert not ok
    assert "BRIDGE_TASK_REF_REQUIRED" in message


def test_gate_reader_required_when_claim_gate_on():
    ok, message = seat.check_gate_reader(
        _base_seat_env(BRIDGE_CLAIM_GATE="0"), required=False
    )
    assert ok, message

    ok, message = seat.check_gate_reader(
        _base_seat_env(BRIDGE_CLAIM_GATE="1"), required=True
    )
    assert not ok
    assert "ARB_GATE_READER_DSN" in message

    ok, message = seat.check_gate_reader(
        _base_seat_env(
            BRIDGE_CLAIM_GATE="1",
            ARB_GATE_READER_DSN="postgresql://arb_gate_reader:pw@db:5432/arb?sslmode=require",
        ),
        required=True,
    )
    assert not ok
    assert "ARB_GATE_READER_ROLE" in message

    ok, message = seat.check_gate_reader(
        _base_seat_env(
            BRIDGE_CLAIM_GATE="1",
            ARB_GATE_READER_DSN="postgresql://arb_gate_reader:pw@db:5432/arb?sslmode=require",
            ARB_GATE_READER_ROLE="arb_gate_reader",
        ),
        required=True,
    )
    assert ok, message


def test_lane_writer_required_when_claim_gate_on_or_dsn_partial():
    ok, message = seat.check_lane_writer(
        _base_seat_env(BRIDGE_CLAIM_GATE="0"), required=False
    )
    assert ok, message

    ok, message = seat.check_lane_writer(
        _base_seat_env(BRIDGE_CLAIM_GATE="1"), required=True
    )
    assert not ok
    assert "ARB_GATE_LANE_WRITER_DSN" in message

    ok, message = seat.check_lane_writer(
        _base_seat_env(
            ARB_GATE_LANE_WRITER_DSN="postgresql://seat:pw@db:5432/arb?sslmode=require",
        ),
        required=False,
    )
    assert not ok
    assert "ARB_GATE_LANE_WRITER_ROLE" in message

    # ROLE-without-DSN must fail (symmetric with gate-reader need computation).
    ok, message = seat.check_lane_writer(
        _base_seat_env(ARB_GATE_LANE_WRITER_ROLE="arb_gate_writer_seat"),
        required=False,
    )
    assert not ok
    assert "ARB_GATE_LANE_WRITER_DSN" in message

    ok, message = seat.check_lane_writer(
        _base_seat_env(
            ARB_GATE_LANE_WRITER_DSN="postgresql://seat:pw@db:5432/arb?sslmode=require",
            ARB_GATE_LANE_WRITER_ROLE="arb_gate_writer_seat",
        ),
        required=True,
    )
    assert ok, message


def test_lane_binding_keys_requires_keys_when_required():
    ok, message = seat.check_lane_binding_keys(_base_seat_env(), required=False)
    assert ok, message
    assert "lane-binding-keys" in message
    assert "readiness" not in message.lower()

    ok, message = seat.check_lane_binding_keys(
        _base_seat_env(
            ARB_GATE_LANE_WRITER_DSN="postgresql://seat:pw@db:5432/arb?sslmode=require",
            ARB_GATE_LANE_WRITER_ROLE="arb_gate_writer_seat",
        ),
        required=True,
    )
    assert not ok
    assert "ARB_GATE_LANE_WRITER_CONSUMER_ID" in message or "binding" in message.lower()

    ok, message = seat.check_lane_binding_keys(
        _base_seat_env(
            ARB_GATE_LANE_WRITER_DSN="postgresql://seat:pw@db:5432/arb?sslmode=require",
            ARB_GATE_LANE_WRITER_ROLE="arb_gate_writer_seat",
            ARB_GATE_LANE_WRITER_CONSUMER_ID="codex-bridge-dev",
            ARB_GATE_LANE_WRITER_LANE="gated",
            BRIDGE_WORKTREE_LANE="gated",
        ),
        required=True,
    )
    assert ok, message
    assert "binding keys present" in message
    assert "readiness" not in message.lower()

    ok, message = seat.check_lane_binding_keys(
        _base_seat_env(
            ARB_GATE_LANE_WRITER_DSN="postgresql://seat:pw@db:5432/arb?sslmode=require",
            ARB_GATE_LANE_WRITER_ROLE="arb_gate_writer_seat",
            ARB_GATE_LANE_WRITER_CONSUMER_ID="codex-bridge-dev",
            ARB_GATE_LANE_WRITER_LANE="exempt",
            BRIDGE_WORKTREE_LANE="gated",
        ),
        required=True,
    )
    assert not ok
    assert "lane" in message.lower()


def test_worktree_lane_rejects_typo():
    ok, message = seat.check_worktree_lane(_base_seat_env(BRIDGE_WORKTREE_LANE="exmept"))
    assert not ok
    assert "exmept" in message
    assert "gated|exempt" in message


def test_local_hydration_reuses_prove_brief_hydrate_readiness(monkeypatch):
    calls = []

    def fake_prove(*, env=None, runtime_root=None, allow_missing_dsn=False):
        calls.append({"env": dict(env or {}), "allow_missing_dsn": allow_missing_dsn})
        return True

    monkeypatch.setattr(
        "agent_redis_bridge.brief_hydrate_ready.prove_brief_hydrate_readiness",
        fake_prove,
    )
    ok, message = seat.check_local_hydration(
        _base_seat_env(ARB_MEMORY_LOCAL_DSN="postgresql://reader@localhost/db")
    )
    assert ok, message
    assert calls and calls[0]["env"].get("ARB_MEMORY_LOCAL_DSN")

    def fail_prove(**_k):
        return False

    monkeypatch.setattr(
        "agent_redis_bridge.brief_hydrate_ready.prove_brief_hydrate_readiness",
        fail_prove,
    )
    ok, message = seat.check_local_hydration(
        _base_seat_env(ARB_MEMORY_LOCAL_DSN="postgresql://reader@localhost/db")
    )
    assert not ok
    assert message == (
        "local-hydration: prove_brief_hydrate_readiness returned False "
        "(brief_hydrate not ready)"
    )


def test_local_hydration_optional_when_no_dsn_and_not_required():
    ok, message = seat.check_local_hydration(_base_seat_env(), required=False)
    assert ok, message
    assert "not configured" in message.lower() or "skipped" in message.lower()

    ok, message = seat.check_local_hydration(_base_seat_env(), required=True)
    assert not ok
    assert "ARB_MEMORY_LOCAL_DSN" in message or "hydration" in message.lower()


def test_supervisor_interpreter_pinned_pass():
    """Pinned interpreter branch: exact PASS message on redis-importable python."""
    ok, message = seat.check_supervisor_interpreter(
        {"BRIDGE_SUPERVISOR_PYTHON": sys.executable}
    )
    assert ok, message
    assert message == (
        f"supervisor-interpreter: {sys.executable} imports redis "
        f"(pinned BRIDGE_SUPERVISOR_PYTHON={sys.executable})"
    )


def test_supervisor_interpreter_fails_when_python3_missing_redis(monkeypatch, tmp_path):
    fake_py = tmp_path / "python3"
    fake_py.write_text("#!/bin/sh\nexit 1\n")
    fake_py.chmod(0o755)

    def fake_which(name, path=None):
        if name == "python3":
            return str(fake_py)
        return None

    monkeypatch.setattr(seat.shutil, "which", fake_which)
    monkeypatch.setattr(
        seat.subprocess,
        "run",
        lambda *a, **k: type(
            "R", (), {"returncode": 1, "stdout": "", "stderr": "No module named redis"}
        )(),
    )
    ok, message = seat.check_supervisor_interpreter({})
    assert not ok
    assert "redis" in message.lower()


def test_supervisor_interpreter_uses_checked_input_path(monkeypatch, tmp_path):
    """V5: PATH in checked input is probed; operator PATH is ignored."""
    seat_bin = tmp_path / "seat-bin"
    seat_bin.mkdir()
    seat_py = seat_bin / "python3"
    seat_py.write_text("#!/bin/sh\nexit 1\n")
    seat_py.chmod(0o755)
    seen = {}

    def fake_which(name, path=None):
        seen["path"] = path
        if path == str(seat_bin) and name == "python3":
            return str(seat_py)
        # Operator path would resolve something else — must not be used.
        if path is None:
            return "/operator/python3"
        return None

    def fake_run(cmd, **kwargs):
        seen["env"] = kwargs.get("env")
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": "No module named redis"})()

    monkeypatch.setattr(seat.shutil, "which", fake_which)
    monkeypatch.setattr(seat.subprocess, "run", fake_run)
    ok, message = seat.check_supervisor_interpreter({"PATH": str(seat_bin)})
    assert not ok
    assert seen.get("path") == str(seat_bin)
    assert seen.get("env") is not None and seen["env"].get("PATH") == str(seat_bin)
    assert "checked input PATH" in message


def _write_registry_evidence(
    path: Path,
    targets: list[dict],
    *,
    roster: list[str] | None = None,
    parse_only: list[str] | None = None,
) -> Path:
    if roster is None:
        roster = [t["agent_id"] for t in targets if isinstance(t, dict) and t.get("agent_id")]
    payload = {
        "roster": roster,
        "parse_only": [] if parse_only is None else parse_only,
        "targets": targets,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_ref_required_checklist_target_dual_parse_and_hydration(tmp_path):
    reg = _write_registry_evidence(
        tmp_path / "reg.json",
        [
            {
                "agent_id": "seat-a",
                "worker_vantage": "bridge-dev-mac",
                "task_wire": "legacy-or-ref-v1",
                "brief_hydrate": "v1",
            },
            {
                "agent_id": "seat-b",
                "worker_vantage": "bridge-dev-mac",
                "task_wire": "legacy",
                "brief_hydrate": "",
            },
        ],
        roster=["seat-a", "seat-b"],
    )
    env = _base_seat_env(BRIDGE_PREFLIGHT_TARGET_REGISTRY=str(reg))
    ok, message = seat.check_target_dual_parse(env)
    assert not ok
    assert "seat-b" in message
    assert "task_wire" in message

    ok, message = seat.check_target_advertises_hydrate(env)
    assert not ok
    assert "seat-b" in message
    assert "brief_hydrate" in message

    reg2 = _write_registry_evidence(
        tmp_path / "reg2.json",
        [
            {
                "agent_id": "seat-a",
                "worker_vantage": "bridge-dev-mac",
                "task_wire": "legacy-or-ref-v1",
                "brief_hydrate": "v1",
            }
        ],
        roster=["seat-a"],
    )
    env2 = _base_seat_env(BRIDGE_PREFLIGHT_TARGET_REGISTRY=str(reg2))
    ok, message = seat.check_target_dual_parse(env2)
    assert ok, message
    assert "evidence file reports" in message
    ok, message = seat.check_target_advertises_hydrate(env2)
    assert ok, message
    assert "evidence file reports" in message


def test_roster_uncovered_targets_named_fail(tmp_path):
    """V1(a): evidence covering 1 of N roster targets → RED naming uncovered."""
    reg = _write_registry_evidence(
        tmp_path / "reg.json",
        [
            {
                "agent_id": "seat-a",
                "worker_vantage": "bridge-dev-mac",
                "task_wire": "legacy-or-ref-v1",
                "brief_hydrate": "v1",
            }
        ],
        roster=["seat-a", "seat-b", "seat-c"],
    )
    env = _base_seat_env(BRIDGE_PREFLIGHT_TARGET_REGISTRY=str(reg))
    ok, message = seat.check_target_dual_parse(env)
    assert not ok
    assert "seat-b" in message and "seat-c" in message
    assert "missing from dual-parse entries" in message

    ok, message = seat.check_target_advertises_hydrate(env)
    assert not ok
    assert "seat-b" in message and "seat-c" in message


def test_ref_required_sender_migration_and_ref_hydration_evidence(tmp_path):
    env = _base_seat_env()
    ok, message = seat.check_sender_migration(env)
    assert not ok
    assert "sender-migration" in message.lower() or "BRIDGE_PREFLIGHT_SENDER_MIGRATION" in message

    mig = tmp_path / "mig.json"
    mig.write_text(json.dumps({"all_senders_migrated": False, "remaining": ["old-caller"]}), encoding="utf-8")
    ok, message = seat.check_sender_migration(
        _base_seat_env(BRIDGE_PREFLIGHT_SENDER_MIGRATION=str(mig))
    )
    assert not ok
    assert "old-caller" in message or "migrated" in message.lower()

    mig.write_text(json.dumps({"all_senders_migrated": True, "remaining": []}), encoding="utf-8")
    ok, message = seat.check_sender_migration(
        _base_seat_env(BRIDGE_PREFLIGHT_SENDER_MIGRATION=str(mig))
    )
    assert ok, message

    ok, message = seat.check_ref_hydration_success(env)
    assert not ok
    assert "BRIDGE_PREFLIGHT" in message or "roster" in message.lower() or "ref" in message.lower()

    reg = _write_registry_evidence(
        tmp_path / "reg.json",
        [
            {
                "agent_id": "seat-a",
                "worker_vantage": "bridge-dev-mac",
                "task_wire": "legacy-or-ref-v1",
                "brief_hydrate": "v1",
            },
            {
                "agent_id": "seat-b",
                "worker_vantage": "bridge-dev-mac",
                "task_wire": "legacy-or-ref-v1",
                "brief_hydrate": "v1",
            },
        ],
        roster=["seat-a", "seat-b"],
    )
    hyd = tmp_path / "hyd.json"
    hyd.write_text(
        json.dumps(
            {
                "targets": [
                    {"agent_id": "seat-a", "ok": True},
                    {"agent_id": "seat-b", "ok": False, "error": "timeout"},
                ]
            }
        ),
        encoding="utf-8",
    )
    # V1(b): ok!=true for one roster target → RED naming it
    ok, message = seat.check_ref_hydration_success(
        _base_seat_env(
            BRIDGE_PREFLIGHT_TARGET_REGISTRY=str(reg),
            BRIDGE_PREFLIGHT_REF_HYDRATION=str(hyd),
        )
    )
    assert not ok
    assert "seat-b" in message

    hyd.write_text(
        json.dumps(
            {
                "targets": [
                    {"agent_id": "seat-a", "ok": True},
                    {"agent_id": "seat-b", "ok": True},
                ]
            }
        ),
        encoding="utf-8",
    )
    ok, message = seat.check_ref_hydration_success(
        _base_seat_env(
            BRIDGE_PREFLIGHT_TARGET_REGISTRY=str(reg),
            BRIDGE_PREFLIGHT_REF_HYDRATION=str(hyd),
        )
    )
    assert ok, message
    assert "evidence file reports" in message

    # One-of-N coverage: only seat-a in hydration evidence for two-seat roster
    hyd.write_text(
        json.dumps({"targets": [{"agent_id": "seat-a", "ok": True}]}),
        encoding="utf-8",
    )
    ok, message = seat.check_ref_hydration_success(
        _base_seat_env(
            BRIDGE_PREFLIGHT_TARGET_REGISTRY=str(reg),
            BRIDGE_PREFLIGHT_REF_HYDRATION=str(hyd),
        )
    )
    assert not ok
    assert "seat-b" in message
    assert "missing from hydration evidence" in message


def test_parse_only_targets_must_legacy_emit_and_refuse_ref(tmp_path):
    reg = _write_registry_evidence(
        tmp_path / "reg.json",
        [
            {
                "agent_id": "seat-a",
                "worker_vantage": "bridge-dev-mac",
                "task_wire": "legacy-or-ref-v1",
                "brief_hydrate": "v1",
            }
        ],
        roster=["seat-a"],
        parse_only=["parse-only-a"],
    )
    po = tmp_path / "parse_only.json"
    po.write_text(
        json.dumps(
            {
                "targets": [
                    {
                        "agent_id": "parse-only-a",
                        "task_wire": "legacy-or-ref-v1",
                        "brief_hydrate": "",
                        "authority_emits": "legacy",
                        "direct_ref_error": "brief_hydration_unavailable",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    env = _base_seat_env(
        BRIDGE_PREFLIGHT_TARGET_REGISTRY=str(reg),
        BRIDGE_PREFLIGHT_PARSE_ONLY=str(po),
    )
    ok, message = seat.check_parse_only_legacy_emission(env)
    assert ok, message
    ok, message = seat.check_parse_only_ref_refusal(env)
    assert ok, message

    po.write_text(
        json.dumps(
            {
                "targets": [
                    {
                        "agent_id": "parse-only-a",
                        "task_wire": "legacy-or-ref-v1",
                        "brief_hydrate": "v1",
                        "authority_emits": "ref",
                        "direct_ref_error": "ok",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    env_bad = _base_seat_env(
        BRIDGE_PREFLIGHT_TARGET_REGISTRY=str(reg),
        BRIDGE_PREFLIGHT_PARSE_ONLY=str(po),
    )
    ok, message = seat.check_parse_only_legacy_emission(env_bad)
    assert not ok
    assert "parse-only-a" in message
    ok, message = seat.check_parse_only_ref_refusal(env_bad)
    assert not ok
    assert "brief_hydration_unavailable" in message


def test_parse_only_empty_fails_unless_registry_declares_zero(tmp_path):
    """Empty parse-only evidence is not a vacuous pass without registry parse_only=[]."""
    reg_missing = tmp_path / "reg_bad.json"
    reg_missing.write_text(
        json.dumps(
            {
                "roster": ["seat-a"],
                "targets": [
                    {
                        "agent_id": "seat-a",
                        "task_wire": "legacy-or-ref-v1",
                        "brief_hydrate": "v1",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    po = tmp_path / "po.json"
    po.write_text(json.dumps({"targets": []}), encoding="utf-8")
    env = _base_seat_env(
        BRIDGE_PREFLIGHT_TARGET_REGISTRY=str(reg_missing),
        BRIDGE_PREFLIGHT_PARSE_ONLY=str(po),
    )
    ok, message = seat.check_parse_only_legacy_emission(env)
    assert not ok
    assert "parse_only" in message

    reg_zero = _write_registry_evidence(
        tmp_path / "reg_ok.json",
        [
            {
                "agent_id": "seat-a",
                "worker_vantage": "bridge-dev-mac",
                "task_wire": "legacy-or-ref-v1",
                "brief_hydrate": "v1",
            }
        ],
        roster=["seat-a"],
        parse_only=[],
    )
    env_ok = _base_seat_env(
        BRIDGE_PREFLIGHT_TARGET_REGISTRY=str(reg_zero),
        BRIDGE_PREFLIGHT_PARSE_ONLY=str(po),
    )
    ok, message = seat.check_parse_only_legacy_emission(env_ok)
    assert ok, message
    assert "zero parse-only" in message


def test_legacy_removal_requires_zero_legacy_observation(tmp_path):
    from datetime import datetime, timedelta, timezone

    ok, message = seat.check_zero_legacy_observation(_base_seat_env())
    assert not ok
    assert "BRIDGE_PREFLIGHT_LEGACY_OBSERVATION" in message or "zero" in message.lower()

    now = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)
    start = (now - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (now - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")

    obs = tmp_path / "obs.json"
    obs.write_text(
        json.dumps(
            {
                "window_start": start,
                "window_end": end,
                "zero_authority_legacy": False,
                "legacy_count": 3,
            }
        ),
        encoding="utf-8",
    )
    ok, message = seat.check_zero_legacy_observation(
        _base_seat_env(BRIDGE_PREFLIGHT_LEGACY_OBSERVATION=str(obs)), now=now
    )
    assert not ok
    assert "legacy" in message.lower()

    obs.write_text(
        json.dumps(
            {
                "window_start": start,
                "window_end": end,
                "zero_authority_legacy": True,
                "legacy_count": 0,
            }
        ),
        encoding="utf-8",
    )
    ok, message = seat.check_zero_legacy_observation(
        _base_seat_env(BRIDGE_PREFLIGHT_LEGACY_OBSERVATION=str(obs)), now=now
    )
    assert ok, message
    assert "evidence file reports" in message


def test_zero_legacy_window_bounds_enforced(tmp_path):
    """V2: end<=start, short duration, missing bound, staleness each named FAIL."""
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)
    obs = tmp_path / "obs.json"

    # Contradictory window: end before start
    obs.write_text(
        json.dumps(
            {
                "window_start": "2026-07-29T00:00:00Z",
                "window_end": "2026-07-28T00:00:00Z",
                "zero_authority_legacy": True,
            }
        ),
        encoding="utf-8",
    )
    ok, message = seat.check_zero_legacy_observation(
        _base_seat_env(BRIDGE_PREFLIGHT_LEGACY_OBSERVATION=str(obs)), now=now
    )
    assert not ok
    assert "window_end <= window_start" in message

    # Missing bound
    obs.write_text(
        json.dumps({"window_start": "2026-07-28T00:00:00Z", "zero_authority_legacy": True}),
        encoding="utf-8",
    )
    ok, message = seat.check_zero_legacy_observation(
        _base_seat_env(BRIDGE_PREFLIGHT_LEGACY_OBSERVATION=str(obs)), now=now
    )
    assert not ok
    assert "window_end" in message and "missing" in message

    # Duration below floor (< 24h)
    obs.write_text(
        json.dumps(
            {
                "window_start": "2026-07-29T00:00:00Z",
                "window_end": "2026-07-29T01:00:00Z",
                "zero_authority_legacy": True,
            }
        ),
        encoding="utf-8",
    )
    ok, message = seat.check_zero_legacy_observation(
        _base_seat_env(BRIDGE_PREFLIGHT_LEGACY_OBSERVATION=str(obs)), now=now
    )
    assert not ok
    assert "below floor" in message

    # Staleness beyond 7 days
    old_end = now - timedelta(days=10)
    old_start = old_end - timedelta(days=2)
    obs.write_text(
        json.dumps(
            {
                "window_start": old_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "window_end": old_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "zero_authority_legacy": True,
            }
        ),
        encoding="utf-8",
    )
    ok, message = seat.check_zero_legacy_observation(
        _base_seat_env(BRIDGE_PREFLIGHT_LEGACY_OBSERVATION=str(obs)), now=now
    )
    assert not ok
    assert "stale" in message


def _capture_run(argv: list[str]) -> tuple[int, str]:
    import io
    from contextlib import redirect_stdout, redirect_stderr

    buf_out, buf_err = io.StringIO(), io.StringIO()
    with redirect_stdout(buf_out), redirect_stderr(buf_err):
        rc = seat.run_main(argv)
    return rc, buf_out.getvalue() + buf_err.getvalue()


def _minimal_env_body(**extra_lines: str) -> str:
    lines = [
        "AGENT_REDIS_HOST=redis.example",
        "AGENT_REDIS_PORT=6379",
        "AGENT_REDIS_DB=12",
        "AGENT_REDIS_PREFIX=agent_scratch:",
        "AGENT_TRUSTED_SENDERS=claude-project-dev=trusted",
        "BRIDGE_WORKER_VANTAGE=bridge-test",
        "BRIDGE_CLAIM_GATE=0",
        "BRIDGE_TASK_REF_REQUIRED=0",
        "BRIDGE_WORKTREE_LANE=gated",
        f"BRIDGE_SUPERVISOR_PYTHON={sys.executable}",
    ]
    for k, v in extra_lines.items():
        lines.append(f"{k}={v}")
    return "\n".join(lines) + "\n"


def test_checklist_claim_gate_mode_fails_named_prerequisites(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_redis_bridge.brief_hydrate_ready.prove_brief_hydrate_readiness",
        lambda **_k: True,
    )
    env_file = tmp_path / ".env"
    env_file.write_text(_minimal_env_body())
    rc, text = _capture_run([str(env_file), "--checklist", "claim-gate"])
    assert rc == 1
    # V4: FAIL lines keep the check name; name must appear on a FAIL line.
    assert "[seat-preflight] FAIL gate-reader:" in text


def test_checklist_ref_required_mode_fails_without_evidence(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(_minimal_env_body())
    rc, text = _capture_run([str(env_file), "--checklist", "ref-required"])
    assert rc == 1
    assert "[seat-preflight] FAIL target-dual-parse:" in text or (
        "[seat-preflight] FAIL" in text and "BRIDGE_PREFLIGHT_TARGET_REGISTRY" in text
    )


def test_checklist_legacy_removal_mode_requires_zero_legacy(tmp_path):
    env_file = tmp_path / ".env"
    reg = _write_registry_evidence(
        tmp_path / "reg.json",
        [
            {
                "agent_id": "seat-a",
                "worker_vantage": "bridge-dev-mac",
                "task_wire": "legacy-or-ref-v1",
                "brief_hydrate": "v1",
            }
        ],
        roster=["seat-a"],
        parse_only=[],
    )
    mig = tmp_path / "mig.json"
    mig.write_text(json.dumps({"all_senders_migrated": True, "remaining": []}), encoding="utf-8")
    hyd = tmp_path / "hyd.json"
    hyd.write_text(
        json.dumps({"targets": [{"agent_id": "seat-a", "ok": True}]}), encoding="utf-8"
    )
    po = tmp_path / "po.json"
    po.write_text(json.dumps({"targets": []}), encoding="utf-8")
    env_file.write_text(
        _minimal_env_body(
            BRIDGE_PREFLIGHT_TARGET_REGISTRY=str(reg),
            BRIDGE_PREFLIGHT_SENDER_MIGRATION=str(mig),
            BRIDGE_PREFLIGHT_REF_HYDRATION=str(hyd),
            BRIDGE_PREFLIGHT_PARSE_ONLY=str(po),
        )
    )
    rc, text = _capture_run([str(env_file), "--checklist", "legacy-removal"])
    assert rc == 1
    assert "[seat-preflight] FAIL zero-legacy-observation:" in text


def test_checklist_unknown_mode_is_usage_error():
    assert seat.run_main([".env", "--checklist", "nope"]) == 2


def test_claim_gate_checklist_forces_required_when_flag_off(tmp_path, monkeypatch):
    """Pin :851 wiring: claim-gate checklist forces required even with flag at 0."""
    monkeypatch.setattr(
        "agent_redis_bridge.brief_hydrate_ready.prove_brief_hydrate_readiness",
        lambda **_k: True,
    )
    env_file = tmp_path / ".env"
    env_file.write_text(_minimal_env_body())  # BRIDGE_CLAIM_GATE=0
    rc, text = _capture_run([str(env_file), "--checklist", "claim-gate"])
    assert rc == 1
    assert "[seat-preflight] FAIL gate-reader:" in text
    assert "ARB_GATE_READER_DSN" in text


def test_worker_vantage_env_file_attested_limitation(tmp_path):
    """V3 option (b): env-file-only vantage prints attested-strength message."""
    env_file = tmp_path / ".env"
    env_file.write_text(_minimal_env_body())
    rc, text = _capture_run([str(env_file)])
    # May fail other checks; assert vantage message shape when it prints OK.
    assert "BRIDGE_WORKER_VANTAGE=bridge-test" in text
    assert "attested from env file only" in text
    assert "bridge.py reads process env" in text


def test_process_env_only_keys_wired_env_file_limitation_per_key(tmp_path, monkeypatch):
    """X1: every PROCESS_ENV_ONLY_KEYS member is attested in env-file mode."""
    monkeypatch.setattr(
        "agent_redis_bridge.brief_hydrate_ready.prove_brief_hydrate_readiness",
        lambda **_k: True,
    )
    monkeypatch.setattr(
        seat,
        "check_supervisor_interpreter",
        lambda env: (True, "interpreter redis-capable (test double)"),
    )
    # Membership wiring: four process-env-only keys (ARB_MEMORY_REDIS_URL is not one —
    # bridge.py:210 supports env-file fallback; see Y1 pin tests).
    assert seat.PROCESS_ENV_ONLY_KEYS == frozenset(
        {
            "BRIDGE_WORKER_VANTAGE",
            "BRIDGE_WORKTREE_LANE",
            "ARB_GATE_LANE_WRITER_DSN",
            "ARB_MEMORY_LOCAL_DSN",
        }
    )
    assert "ARB_MEMORY_REDIS_URL" not in seat.PROCESS_ENV_ONLY_KEYS
    env_file = tmp_path / ".env"
    # Keep lane gated so exempt-git is skipped; key is still present via _minimal_env_body.
    env_file.write_text(
        _minimal_env_body(
            ARB_GATE_LANE_WRITER_DSN="postgresql://seat:pw@db:5432/arb?sslmode=require",
            ARB_GATE_LANE_WRITER_ROLE="arb_gate_writer_seat",
            ARB_MEMORY_LOCAL_DSN="postgresql://reader@localhost/db",
            ARB_MEMORY_REDIS_URL="rediss://:secret@bus:6379/3",
        )
    )
    rc, text = _capture_run([str(env_file), "--checklist", "claim-gate"])
    # claim-gate still needs reader DSN; even so each process-env key must be labelled.
    for key in seat.PROCESS_ENV_ONLY_KEYS:
        assert key in text, f"expected attestation mention of {key}"
    assert text.count("attested from env file only") >= 4
    assert "bridge.py reads process env" in text
    # Y1: env-file-only audit URL must not produce a memory-redis-url FAIL.
    assert "FAIL memory-redis-url" not in text
    assert "ARB_MEMORY_REDIS_URL set" in text


def test_process_env_only_plist_file_only_fails_closed(tmp_path, monkeypatch):
    """X1 choice: plist mode named FAIL when process-env-only key is file-only."""
    monkeypatch.setattr(
        seat,
        "check_supervisor_interpreter",
        lambda env: (True, "interpreter redis-capable (test double)"),
    )
    monkeypatch.setattr(
        "agent_redis_bridge.brief_hydrate_ready.prove_brief_hydrate_readiness",
        lambda **_k: True,
    )
    env_file = tmp_path / "seat.env"
    env_file.write_text(
        _minimal_env_body(
            BRIDGE_WORKTREE_LANE="exempt",
            ARB_GATE_LANE_WRITER_DSN="postgresql://seat:pw@db:5432/arb?sslmode=require",
            ARB_GATE_LANE_WRITER_ROLE="arb_gate_writer_seat",
        )
    )
    # Plist ProgramArguments points at env-file; EnvironmentVariables omits process-env keys.
    plist_path = tmp_path / "seat.plist"
    with plist_path.open("wb") as handle:
        plistlib.dump(
            {
                "Label": "test.seat",
                "ProgramArguments": [
                    "/usr/bin/env",
                    "python3",
                    "-m",
                    "agent_redis_bridge",
                    "--env-file",
                    str(env_file),
                ],
                "EnvironmentVariables": {},
            },
            handle,
        )
    rc, text = _capture_run([str(plist_path)])
    assert rc == 1
    assert "process-env-only key" in text
    assert "BRIDGE_WORKER_VANTAGE" in text or "BRIDGE_WORKTREE_LANE" in text


def test_process_env_only_keys_membership_drives_attestation():
    """X1 KILL (b): helper rejects keys outside PROCESS_ENV_ONLY_KEYS."""
    import pytest

    with pytest.raises(ValueError, match="PROCESS_ENV_ONLY_KEYS"):
        seat._process_env_attestation(
            "NOT_A_PROCESS_ENV_KEY",
            {"NOT_A_PROCESS_ENV_KEY": "x"},
            process_env={},
            mode="env",
        )
    # Direct call for each constant member is accepted (wiring surface).
    for key in seat.PROCESS_ENV_ONLY_KEYS:
        fail, limit = seat._process_env_attestation(
            key, {key: "value"}, process_env={}, mode="env"
        )
        assert fail is None
        assert limit is not None and "attested from env file only" in limit
        fail, limit = seat._process_env_attestation(
            key, {key: "value"}, process_env={}, mode="plist"
        )
        assert fail is not None and "process-env-only key" in fail
        assert limit is None


def test_ok_lines_drop_check_name(tmp_path, monkeypatch):
    """V4 (a): OK lines are message-only; FAIL lines keep the name."""
    monkeypatch.setattr(
        seat,
        "check_supervisor_interpreter",
        lambda env: (True, "interpreter redis-capable (test double)"),
    )
    monkeypatch.setattr(
        seat,
        "check_local_hydration",
        lambda env, required=False, process_env=None, mode=None: (
            True,
            "local hydration skipped (test)",
        ),
    )
    env_file = tmp_path / ".env"
    env_file.write_text(_minimal_env_body())
    rc, text = _capture_run([str(env_file)])
    assert rc == 0
    # No "OK <name>:" form — only "OK:" or summary.
    for line in text.splitlines():
        if line.startswith("[seat-preflight] OK ") and "check(s) passed" not in line:
            assert line.startswith("[seat-preflight] OK:"), line
            assert not line.startswith("[seat-preflight] OK redis-required:")


def test_checklist_modes_exit_zero_with_complete_evidence(tmp_path, monkeypatch):
    """Every checklist mode has an exit-0 path with complete valid evidence."""
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(
        "agent_redis_bridge.brief_hydrate_ready.prove_brief_hydrate_readiness",
        lambda **_k: True,
    )
    monkeypatch.setattr(
        seat,
        "check_supervisor_interpreter",
        lambda env: (True, "interpreter redis-capable (test double)"),
    )

    reg = _write_registry_evidence(
        tmp_path / "reg.json",
        [
            {
                "agent_id": "seat-a",
                "worker_vantage": "bridge-dev-mac",
                "task_wire": "legacy-or-ref-v1",
                "brief_hydrate": "v1",
            }
        ],
        roster=["seat-a"],
        parse_only=[],
    )
    mig = tmp_path / "mig.json"
    mig.write_text(json.dumps({"all_senders_migrated": True, "remaining": []}), encoding="utf-8")
    hyd = tmp_path / "hyd.json"
    hyd.write_text(
        json.dumps({"targets": [{"agent_id": "seat-a", "ok": True}]}), encoding="utf-8"
    )
    po = tmp_path / "po.json"
    po.write_text(json.dumps({"targets": []}), encoding="utf-8")
    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    obs = tmp_path / "obs.json"
    obs.write_text(
        json.dumps(
            {
                "window_start": start,
                "window_end": end,
                "zero_authority_legacy": True,
                "legacy_count": 0,
            }
        ),
        encoding="utf-8",
    )

    claim_body = _minimal_env_body(
        ARB_GATE_READER_DSN="postgresql://arb_gate_reader:pw@db:5432/arb?sslmode=require",
        ARB_GATE_READER_ROLE="arb_gate_reader",
        ARB_GATE_LANE_WRITER_DSN="postgresql://seat:pw@db:5432/arb?sslmode=require",
        ARB_GATE_LANE_WRITER_ROLE="arb_gate_writer_seat",
        ARB_GATE_LANE_WRITER_CONSUMER_ID="codex-bridge-dev",
        ARB_GATE_LANE_WRITER_LANE="gated",
        ARB_MEMORY_LOCAL_DSN="postgresql://reader@localhost/db",
    )
    claim_env = tmp_path / "claim.env"
    claim_env.write_text(claim_body)
    rc, _ = _capture_run([str(claim_env), "--checklist", "claim-gate"])
    assert rc == 0

    ref_env = tmp_path / "ref.env"
    ref_env.write_text(
        _minimal_env_body(
            BRIDGE_PREFLIGHT_TARGET_REGISTRY=str(reg),
            BRIDGE_PREFLIGHT_SENDER_MIGRATION=str(mig),
            BRIDGE_PREFLIGHT_REF_HYDRATION=str(hyd),
            BRIDGE_PREFLIGHT_PARSE_ONLY=str(po),
        )
    )
    rc, _ = _capture_run([str(ref_env), "--checklist", "ref-required"])
    assert rc == 0

    leg_env = tmp_path / "leg.env"
    leg_env.write_text(
        _minimal_env_body(
            BRIDGE_PREFLIGHT_TARGET_REGISTRY=str(reg),
            BRIDGE_PREFLIGHT_SENDER_MIGRATION=str(mig),
            BRIDGE_PREFLIGHT_REF_HYDRATION=str(hyd),
            BRIDGE_PREFLIGHT_PARSE_ONLY=str(po),
            BRIDGE_PREFLIGHT_LEGACY_OBSERVATION=str(obs),
        )
    )
    rc, _ = _capture_run([str(leg_env), "--checklist", "legacy-removal"])
    assert rc == 0


def test_checklist_e2e_one_required_check_fail_is_red(tmp_path, monkeypatch):
    """V4 KILL: otherwise-valid checklist with one required FAIL → e2e RED."""
    monkeypatch.setattr(
        "agent_redis_bridge.brief_hydrate_ready.prove_brief_hydrate_readiness",
        lambda **_k: True,
    )
    monkeypatch.setattr(
        seat,
        "check_supervisor_interpreter",
        lambda env: (True, "interpreter redis-capable (test double)"),
    )
    # Valid claim-gate body except missing reader DSN
    env_file = tmp_path / ".env"
    env_file.write_text(
        _minimal_env_body(
            ARB_GATE_READER_ROLE="arb_gate_reader",
            ARB_GATE_LANE_WRITER_DSN="postgresql://seat:pw@db:5432/arb?sslmode=require",
            ARB_GATE_LANE_WRITER_ROLE="arb_gate_writer_seat",
            ARB_GATE_LANE_WRITER_CONSUMER_ID="codex-bridge-dev",
            ARB_GATE_LANE_WRITER_LANE="gated",
            ARB_MEMORY_LOCAL_DSN="postgresql://reader@localhost/db",
        )
    )
    rc, text = _capture_run([str(env_file), "--checklist", "claim-gate"])
    assert rc == 1
    assert "[seat-preflight] FAIL gate-reader:" in text


def test_gated_seat_still_passes_without_exempt_git_in_default_mode(tmp_path, monkeypatch):
    """Default preflight: gated seat needs vantage/flags, not exempt Git vars."""
    monkeypatch.setattr(
        seat,
        "check_supervisor_interpreter",
        lambda env: (True, "interpreter redis-capable (test double)"),
    )
    monkeypatch.setattr(
        seat,
        "check_local_hydration",
        lambda env, required=False, process_env=None, mode=None: (
            True,
            "local hydration skipped (test)",
        ),
    )
    env_file = tmp_path / ".env"
    env_file.write_text(_minimal_env_body())
    assert seat.run_main([str(env_file)]) == 0


# ---------------------------------------------------------------------------
# rem2: X2 future window, X3 evidence integrity, folds
# ---------------------------------------------------------------------------


def test_zero_legacy_rejects_future_window(tmp_path):
    """X2: wholly future observation window is a named FAIL."""
    from datetime import datetime, timezone

    now = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)
    obs = tmp_path / "obs.json"
    obs.write_text(
        json.dumps(
            {
                "window_start": "2026-08-10T00:00:00Z",
                "window_end": "2026-08-12T00:00:00Z",
                "zero_authority_legacy": True,
                "legacy_count": 0,
            }
        ),
        encoding="utf-8",
    )
    ok, message = seat.check_zero_legacy_observation(
        _base_seat_env(BRIDGE_PREFLIGHT_LEGACY_OBSERVATION=str(obs)), now=now
    )
    assert not ok
    assert "window_end is in the future" in message


def test_legacy_window_future_skew_pinned_at_60s(tmp_path):
    """P2-2: LEGACY_WINDOW_FUTURE_SKEW is 60s — now+30s PASS, now+90s named FAIL."""
    from datetime import datetime, timedelta, timezone

    assert seat.LEGACY_WINDOW_FUTURE_SKEW == timedelta(seconds=60)
    now = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)
    # Duration floor is 24h; start well before end for both cases.
    start = (now - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")

    within = tmp_path / "within.json"
    within.write_text(
        json.dumps(
            {
                "window_start": start,
                "window_end": (now + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "zero_authority_legacy": True,
                "legacy_count": 0,
            }
        ),
        encoding="utf-8",
    )
    ok, message = seat.check_zero_legacy_observation(
        _base_seat_env(BRIDGE_PREFLIGHT_LEGACY_OBSERVATION=str(within)), now=now
    )
    assert ok, message

    beyond = tmp_path / "beyond.json"
    beyond.write_text(
        json.dumps(
            {
                "window_start": start,
                "window_end": (now + timedelta(seconds=90)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "zero_authority_legacy": True,
                "legacy_count": 0,
            }
        ),
        encoding="utf-8",
    )
    ok, message = seat.check_zero_legacy_observation(
        _base_seat_env(BRIDGE_PREFLIGHT_LEGACY_OBSERVATION=str(beyond)), now=now
    )
    assert not ok
    assert "window_end is in the future" in message


def test_registry_roster_parse_only_must_be_disjoint(tmp_path):
    """X3(a): roster ∩ parse_only → named FAIL listing the overlap."""
    reg = _write_registry_evidence(
        tmp_path / "reg.json",
        [
            {
                "agent_id": "seat-a",
                "worker_vantage": "bridge-dev-mac",
                "task_wire": "legacy-or-ref-v1",
                "brief_hydrate": "v1",
            }
        ],
        roster=["seat-a"],
        parse_only=["seat-a"],
    )
    env = _base_seat_env(BRIDGE_PREFLIGHT_TARGET_REGISTRY=str(reg))
    ok, message = seat.check_target_dual_parse(env)
    assert not ok
    assert "overlap" in message
    assert "seat-a" in message


def test_registry_and_targets_reject_duplicate_agent_ids(tmp_path):
    """X3(b): duplicate agent_id in roster / targets → named FAIL (never last-wins)."""
    # Duplicate in roster
    reg = tmp_path / "reg-dup-roster.json"
    reg.write_text(
        json.dumps(
            {
                "roster": ["seat-a", "seat-a"],
                "parse_only": [],
                "targets": [
                    {
                        "agent_id": "seat-a",
                        "worker_vantage": "bridge-dev-mac",
                        "task_wire": "legacy-or-ref-v1",
                        "brief_hydrate": "v1",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    ok, message = seat.check_target_dual_parse(
        _base_seat_env(BRIDGE_PREFLIGHT_TARGET_REGISTRY=str(reg))
    )
    assert not ok
    assert "duplicate agent_id" in message
    assert "seat-a" in message

    # Duplicate targets last-wins was the old bug — both ok:false then ok:true
    reg2 = _write_registry_evidence(
        tmp_path / "reg2.json",
        [
            {
                "agent_id": "seat-a",
                "worker_vantage": "bridge-dev-mac",
                "task_wire": "legacy-or-ref-v1",
                "brief_hydrate": "v1",
            }
        ],
        roster=["seat-a"],
    )
    hyd = tmp_path / "hyd.json"
    hyd.write_text(
        json.dumps(
            {
                "targets": [
                    {"agent_id": "seat-a", "ok": False, "error": "timeout"},
                    {"agent_id": "seat-a", "ok": True},
                ]
            }
        ),
        encoding="utf-8",
    )
    ok, message = seat.check_ref_hydration_success(
        _base_seat_env(
            BRIDGE_PREFLIGHT_TARGET_REGISTRY=str(reg2),
            BRIDGE_PREFLIGHT_REF_HYDRATION=str(hyd),
        )
    )
    assert not ok
    assert "duplicate agent_id" in message
    assert "seat-a" in message


def test_ref_hydration_rejects_ok_true_with_error(tmp_path):
    """X3(c): ok:true + nonempty error → named FAIL naming the contradiction."""
    reg = _write_registry_evidence(
        tmp_path / "reg.json",
        [
            {
                "agent_id": "seat-a",
                "worker_vantage": "bridge-dev-mac",
                "task_wire": "legacy-or-ref-v1",
                "brief_hydrate": "v1",
            }
        ],
        roster=["seat-a"],
    )
    hyd = tmp_path / "hyd.json"
    hyd.write_text(
        json.dumps(
            {"targets": [{"agent_id": "seat-a", "ok": True, "error": "timeout"}]}
        ),
        encoding="utf-8",
    )
    ok, message = seat.check_ref_hydration_success(
        _base_seat_env(
            BRIDGE_PREFLIGHT_TARGET_REGISTRY=str(reg),
            BRIDGE_PREFLIGHT_REF_HYDRATION=str(hyd),
        )
    )
    assert not ok
    assert "ok=true contradicts error" in message
    assert "seat-a" in message


def test_roster_target_requires_nonblank_worker_vantage(tmp_path):
    """Fold: worker_vantage enforced nonblank per roster target (named FAIL)."""
    reg = _write_registry_evidence(
        tmp_path / "reg.json",
        [
            {
                "agent_id": "seat-a",
                "worker_vantage": "",
                "task_wire": "legacy-or-ref-v1",
                "brief_hydrate": "v1",
            }
        ],
        roster=["seat-a"],
    )
    ok, message = seat.check_target_dual_parse(
        _base_seat_env(BRIDGE_PREFLIGHT_TARGET_REGISTRY=str(reg))
    )
    assert not ok
    assert "worker_vantage" in message
    assert "seat-a" in message


def test_extraneous_evidence_targets_emit_named_warning(tmp_path):
    """Fold: targets[] entries outside roster → WARNING (not silent ignore)."""
    reg = _write_registry_evidence(
        tmp_path / "reg.json",
        [
            {
                "agent_id": "seat-a",
                "worker_vantage": "bridge-dev-mac",
                "task_wire": "legacy-or-ref-v1",
                "brief_hydrate": "v1",
            },
            {
                "agent_id": "orphan-seat",
                "worker_vantage": "bridge-dev-mac",
                "task_wire": "legacy-or-ref-v1",
                "brief_hydrate": "v1",
            },
        ],
        roster=["seat-a"],
    )
    ok, message = seat.check_target_dual_parse(
        _base_seat_env(BRIDGE_PREFLIGHT_TARGET_REGISTRY=str(reg))
    )
    assert ok, message
    assert "WARNING" in message
    assert "orphan-seat" in message
    assert "not in roster" in message


# ---------------------------------------------------------------------------
# rem3: Y1 membership fix + Y2 per-call-site attestation binding
# ---------------------------------------------------------------------------


def test_memory_redis_url_not_in_process_env_only_keys():
    """Y1 KILL (i): re-adding ARB_MEMORY_REDIS_URL to the frozenset REDs here."""
    keys = set(seat.PROCESS_ENV_ONLY_KEYS)
    assert seat.MEMORY_REDIS_URL == "ARB_MEMORY_REDIS_URL"
    assert seat.MEMORY_REDIS_URL not in keys
    assert "ARB_MEMORY_REDIS_URL" not in keys


def test_memory_redis_url_env_file_placement_never_fails():
    """Y1: env-file-only audit URL (deploy/.env.example form) is not a FAIL.

    Pins the bridge.py:210 supported fallback so rem2-style process-env
    hard-FAIL cannot return unnoticed.
    """
    env = {
        seat.MEMORY_REDIS_URL: "rediss://default:<pw>@<valkey-host>:25061/<db-index>",
    }
    # Plist mode, empty process_env: rem2 would hard-FAIL; Y1 must PASS.
    ok, message = seat.check_memory_redis_url(env, process_env={}, mode="plist")
    assert ok, message
    assert "process-env-only" not in message
    assert seat.MEMORY_REDIS_URL in message

    ok, message = seat.check_memory_redis_url(env, process_env={}, mode="env")
    assert ok, message
    assert "process-env-only" not in message

    ok, message = seat.check_memory_redis_url({}, process_env={}, mode="plist")
    assert ok, message
    assert "not set" in message


def test_memory_redis_url_env_file_input_no_fail_integration(tmp_path, monkeypatch):
    """Y1 KILL (ii): env-file carrying deploy/.env.example-shaped URL → no memory FAIL."""
    monkeypatch.setattr(
        seat,
        "check_supervisor_interpreter",
        lambda env: (True, "interpreter redis-capable (test double)"),
    )
    env_file = tmp_path / ".env"
    env_file.write_text(
        _minimal_env_body(
            ARB_MEMORY_REDIS_URL="rediss://default:<pw>@<valkey-host>:25061/<db-index>",
        )
    )
    rc, text = _capture_run([str(env_file)])
    assert "FAIL memory-redis-url" not in text
    assert "process-env-only key ARB_MEMORY_REDIS_URL" not in text
    assert "ARB_MEMORY_REDIS_URL set" in text
    # Other process-env keys may still limit; memory-redis-url itself must not FAIL.
    for line in text.splitlines():
        if "memory-redis-url" in line.lower() or (
            "ARB_MEMORY_REDIS_URL" in line and "memory-redis" in line
        ):
            assert "FAIL" not in line, line


def test_attestation_call_site_binds_worker_vantage():
    """Y2: check_worker_vantage must pass WORKER_VANTAGE into attestation.

    The `assert not ok` is the load-bearing RED mechanism; the message assertions
    discriminate the worker-vantage refusal from another process-environment failure.
    """
    ok, message = seat.check_worker_vantage(
        {seat.WORKER_VANTAGE: "bridge-test"},
        process_env={},
        mode="plist",
    )
    assert not ok, message
    assert "process-env-only key" in message
    assert seat.WORKER_VANTAGE in message
    # Discriminating: must not be a FAIL for a different process-env key only.
    assert message.count(seat.WORKER_VANTAGE) >= 1


def test_attestation_call_site_binds_lane_writer_dsn():
    """Y2: check_lane_writer must pass LANE_WRITER_DSN into attestation."""
    ok, message = seat.check_lane_writer(
        {
            seat.LANE_WRITER_DSN: "postgresql://seat:pw@db:5432/arb?sslmode=require",
            seat.LANE_WRITER_ROLE: "arb_gate_writer_seat",
        },
        process_env={},
        mode="plist",
    )
    assert not ok, message
    assert "process-env-only key" in message
    assert seat.LANE_WRITER_DSN in message


def test_attestation_call_site_binds_worktree_lane():
    """Y2: check_worktree_lane must pass WORKTREE_LANE into attestation."""
    ok, message = seat.check_worktree_lane(
        {seat.WORKTREE_LANE: "gated"},
        process_env={},
        mode="plist",
    )
    assert not ok, message
    assert "process-env-only key" in message
    assert seat.WORKTREE_LANE in message


def test_attestation_call_site_binds_local_dsn(monkeypatch):
    """Y2: check_local_hydration must pass LOCAL_DSN into attestation."""
    monkeypatch.setattr(
        "agent_redis_bridge.brief_hydrate_ready.prove_brief_hydrate_readiness",
        lambda **_k: True,
    )
    ok, message = seat.check_local_hydration(
        {seat.LOCAL_DSN: "postgresql://reader@localhost/db"},
        process_env={},
        mode="plist",
    )
    assert not ok, message
    assert "process-env-only key" in message
    assert seat.LOCAL_DSN in message
