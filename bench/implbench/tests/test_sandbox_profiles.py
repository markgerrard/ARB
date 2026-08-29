from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys

import pytest

from implbench.harness.sandbox import (
    MACH_ALLOWLIST,
    MACH_ALLOWLIST_DIGEST,
    PROFILE_ROLES,
    PROFILE_TEMPLATE_DIGESTS,
    SandboxPaths,
    generate_profile,
    mach_allowlist_digest,
    profile_digest,
    template_digest,
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


def test_all_canonical_templates_exist_and_are_pinned() -> None:
    profile_root = Path(__file__).resolve().parents[1] / "profiles"
    assert set(PROFILE_ROLES) == {"control", "tool", "git-service", "importer", "scorer"}
    for role in PROFILE_ROLES:
        path = profile_root / f"{role}.sb"
        assert path.is_file()
        assert template_digest(role) == hashlib.sha256(path.read_bytes()).hexdigest()
        assert PROFILE_TEMPLATE_DIGESTS[role] == template_digest(role)
    assert mach_allowlist_digest() == MACH_ALLOWLIST_DIGEST


def test_control_allows_only_declared_provider_and_bus_egress(paths: SandboxPaths) -> None:
    profile = generate_profile("control", paths, provider_endpoints=("api.example.test:443",), bus_endpoints=("redis.example.test:6380",))
    assert "(deny default)" in profile
    assert '(allow network-outbound (remote tcp "api.example.test:443"))' in profile
    assert '(allow network-outbound (remote tcp "redis.example.test:6380"))' in profile
    assert "network-inbound" not in profile or "(deny network-inbound)" in profile
    assert '(deny file-read* (subpath "' + str(paths.git_dir) + '"))' in profile
    assert '(deny file-write* (subpath "' + str(paths.git_dir) + '"))' in profile


def test_tool_is_no_network_including_loopback_and_denies_git_surface(paths: SandboxPaths) -> None:
    profile = generate_profile("tool", paths)
    for rule in ("(deny network-outbound)", "(deny network-inbound)", '(deny network-outbound (remote tcp "127.0.0.1:*"))', '(deny network-outbound (remote tcp "[::1]:*"))'):
        assert rule in profile
    for forbidden in (paths.git_dir, paths.evidence_root, paths.base_checkout, paths.sibling_worktree, paths.credential_root, paths.key_root):
        assert f'(deny file-read* (subpath "{forbidden}"))' in profile
    assert '(deny process-exec (literal "/usr/bin/git"))' in profile
    assert '(deny process-exec (literal "/usr/local/bin/git"))' in profile
    assert '(deny process-exec (literal "/opt/homebrew/bin/git"))' in profile
    assert '(deny process-exec (literal "/usr/bin/xcrun"))' in profile
    assert '(allow network-outbound' not in profile


def test_git_service_is_no_network_and_fixed_to_git_paths(paths: SandboxPaths) -> None:
    profile = generate_profile("git-service", paths)
    assert "(deny network-outbound)" in profile
    assert "(deny network-inbound)" in profile
    assert f'(allow file-read* (subpath "{paths.git_dir}"))' in profile
    assert f'(allow file-write* (subpath "{paths.git_dir}"))' in profile
    assert f'(allow file-read* (subpath "{paths.worktree}"))' in profile
    assert f'(allow file-write* (subpath "{paths.worktree}"))' in profile
    assert "(allow file-read* (subpath \"/\"))" not in profile
    assert "user-group" not in profile


def test_scorer_is_no_network_with_read_only_materialization(paths: SandboxPaths) -> None:
    profile = generate_profile(
        "scorer", paths,
        process_exec_paths=(Path("/usr/bin/true"),),
        runtime_read_paths=(Path(sys.base_prefix),),
    )
    assert "(deny network-outbound)" in profile
    assert "(deny network-inbound)" in profile
    assert f'(allow file-read* (subpath "{paths.worktree}"))' in profile
    assert f'(allow file-write* (subpath "{paths.worktree}"))' not in profile
    assert f'(allow file-write* (subpath "{paths.runtime}"))' in profile
    assert '(allow process-exec (literal "/usr/bin/true"))' in profile
    assert f'(allow file-read* (subpath "{Path(sys.base_prefix).resolve()}"))' in profile


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is a macOS enforcement boundary")
def test_scorer_profile_can_execute_its_exact_allowlisted_binary(paths: SandboxPaths) -> None:
    profile = generate_profile(
        "scorer", paths, process_exec_paths=(Path("/usr/bin/true"),),
        runtime_read_paths=(Path("/usr/lib"), Path("/System/Library")),
    )
    result = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, "/usr/bin/true"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_profiles_use_fixed_mach_allowlist_without_wildcards(paths: SandboxPaths) -> None:
    assert MACH_ALLOWLIST == tuple(sorted(set(MACH_ALLOWLIST)))
    assert all("*" not in service and "?" not in service for service in MACH_ALLOWLIST)
    for role in PROFILE_ROLES:
        profile = generate_profile(role, paths)
        assert '(deny mach-lookup)' in profile
        assert '(allow mach-lookup (global-name "*"))' not in profile
        assert profile_digest(role, paths) == profile_digest(role, paths)


def test_profiles_have_no_broad_filesystem_or_user_group_grants(paths: SandboxPaths) -> None:
    for role in PROFILE_ROLES:
        profile = generate_profile(role, paths)
        assert '(allow file-read* (subpath "/"))' not in profile
        assert '(allow file-write* (subpath "/"))' not in profile
        assert '(allow user-group)' not in profile
        assert '(allow process-info*)' not in profile


def test_profile_digest_changes_when_paths_change(paths: SandboxPaths) -> None:
    original = profile_digest("tool", paths)
    paths.worktree = paths.worktree / "changed"  # type: ignore[misc]
    assert profile_digest("tool", paths) != original
