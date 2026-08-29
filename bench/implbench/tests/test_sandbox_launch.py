from __future__ import annotations

import os
from pathlib import Path

import pytest

from implbench.harness.sandbox import (
    LaunchError,
    SandboxPaths,
    build_launch_spec,
    launch,
    verify_launch_spec,
)


@pytest.fixture
def paths(tmp_path: Path) -> SandboxPaths:
    root = tmp_path / "run"
    return SandboxPaths(
        cell_root=root,
        worktree=root / "worktree",
        git_dir=root / "git",
        evidence_root=root / "evidence",
        base_checkout=tmp_path / "base",
        sibling_worktree=tmp_path / "sibling",
        credential_root=tmp_path / "credentials",
        key_root=tmp_path / "key",
        home=root / "home",
        runtime=root / "runtime",
    )


def test_control_environment_is_scrubbed_and_allowlisted(paths: SandboxPaths) -> None:
    spec = build_launch_spec("control", paths, uid=41001, argv=("bridge", "--fresh-context"), provider_endpoint="https://api.example.test:443", bus_endpoint="rediss://redis.example.test:6380")
    assert set(spec.env) == {
        "HOME", "TMPDIR", "PATH", "PYTHONDONTWRITEBYTECODE", "ARB_PROVIDER_ENDPOINT", "ARB_BUS_ENDPOINT",
        "INTERPRETER_HOME", "PI_CODING_AGENT_DIR", "XDG_STATE_HOME",
        "BRIDGE_INTERPRETER_RETIRE_AFTER_TURN", "BRIDGE_PI_RETIRE_AFTER_TURN",
    }
    assert spec.env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert spec.env["HOME"] == str(paths.home)
    assert all(key not in spec.env for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"))
    assert all(not key.startswith(prefix) for key in spec.env for prefix in ("CLAUDE_", "CODEX_", "MCP_", "OPENAI_API_KEY", "ANTHROPIC_"))


def test_control_arm_home_state_and_retirement_are_exactly_bound(paths: SandboxPaths) -> None:
    spec = build_launch_spec(
        "control",
        paths,
        uid=41001,
        argv=("bridge",),
        provider_endpoint="https://api.example.test:443",
        bus_endpoint="rediss://redis.example.test:6380",
        extra_env={
            "INTERPRETER_HOME": str(paths.home),
            "XDG_STATE_HOME": str(paths.home / "state"),
            "BRIDGE_INTERPRETER_RETIRE_AFTER_TURN": "1",
        },
    )
    verify_launch_spec(spec)
    with pytest.raises(LaunchError):
        build_launch_spec(
            "control", paths, uid=41001, argv=("bridge",),
            provider_endpoint="https://api.example.test:443", bus_endpoint="rediss://redis.example.test:6380",
            extra_env={"INTERPRETER_HOME": "/host/home"},
        )


def test_tool_launch_has_no_inherited_host_environment_or_secret_descriptors(paths: SandboxPaths, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/host/home")
    monkeypatch.setenv("IMPLBENCH_BATTERY_KEY", "secret")
    spec = build_launch_spec("tool", paths, uid=41002, argv=("tool-broker",), inherited_fds=())
    assert spec.inherited_fds == ()
    assert spec.env["HOME"] == str(paths.home)
    assert spec.env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert "IMPLBENCH_BATTERY_KEY" not in spec.env
    assert "GIT_SHIM_SOCKET" in spec.env


def test_git_service_gets_only_fixed_git_surface(paths: SandboxPaths) -> None:
    spec = build_launch_spec("git-service", paths, uid=41003, argv=("git-service",), git_socket=paths.runtime / "git.sock")
    assert spec.env["GIT_DIR"] == str(paths.git_dir)
    assert spec.env["GIT_WORK_TREE"] == str(paths.worktree)
    assert spec.env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert "HOME" in spec.env
    assert "ARB_PROVIDER_ENDPOINT" not in spec.env
    assert "IMPLBENCH_BATTERY_KEY" not in spec.env


def test_launch_is_fresh_and_rejects_resume_fork_or_warm_paths(paths: SandboxPaths) -> None:
    spec = build_launch_spec("control", paths, uid=41001, argv=("bridge", "--fresh-context"), provider_endpoint="https://api.example.test:443", bus_endpoint="rediss://redis.example.test:6380")
    assert spec.fresh_context is True
    assert spec.resume is False
    assert spec.fork_from is None
    assert spec.warm_process is False
    with pytest.raises(LaunchError):
        build_launch_spec("control", paths, uid=41001, argv=("bridge",), provider_endpoint="https://api.example.test:443", bus_endpoint="rediss://redis.example.test:6380", resume=True)
    with pytest.raises(LaunchError):
        build_launch_spec("control", paths, uid=41001, argv=("bridge",), provider_endpoint="https://api.example.test:443", bus_endpoint="rediss://redis.example.test:6380", fork_from="prior")
    with pytest.raises(LaunchError):
        build_launch_spec("control", paths, uid=41001, argv=("bridge",), provider_endpoint="https://api.example.test:443", bus_endpoint="rediss://redis.example.test:6380", warm_process=True)


def test_launch_verifies_exact_uid_and_root_ownership(paths: SandboxPaths) -> None:
    spec = build_launch_spec("tool", paths, uid=41002, argv=("tool-broker",))
    assert spec.uid == 41002
    assert spec.root_uid == 0
    assert spec.root_gid == 0
    verify_launch_spec(spec)
    with pytest.raises(LaunchError):
        verify_launch_spec(spec.__class__(**{**spec.__dict__, "uid": 0}))


def test_launch_uses_stubbed_os_launcher_and_never_shells(paths: SandboxPaths) -> None:
    spec = build_launch_spec("tool", paths, uid=41002, argv=("tool-broker", "--stdio"))
    observed: list[object] = []

    def stub(request):
        observed.append(request)
        return "pid-1"

    assert launch(spec, launcher=stub) == "pid-1"
    request = observed[0]
    assert request.argv == ("tool-broker", "--stdio")
    assert request.shell is False
    assert request.env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert request.uid == 41002


def test_profile_mismatch_fails_closed(paths: SandboxPaths) -> None:
    spec = build_launch_spec("tool", paths, uid=41002, argv=("tool-broker",))
    broken = spec.__class__(**{**spec.__dict__, "profile_digest": "0" * 64})
    with pytest.raises(LaunchError):
        verify_launch_spec(broken)


def test_launch_rejects_unknown_environment_override(paths: SandboxPaths) -> None:
    with pytest.raises(LaunchError):
        build_launch_spec("tool", paths, uid=41002, argv=("tool-broker",), extra_env={"MCP_SERVER": "bad"})


def test_launch_rejects_invalid_uid_and_shell(paths: SandboxPaths) -> None:
    with pytest.raises(LaunchError):
        build_launch_spec("tool", paths, uid=0, argv=("tool-broker",))
    with pytest.raises(LaunchError):
        build_launch_spec("tool", paths, uid=os.getuid(), argv=("/bin/sh", "-c", "bad"), shell=True)
