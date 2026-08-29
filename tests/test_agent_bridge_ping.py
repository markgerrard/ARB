"""agent-bridge-ping consumer-check polarity, including pre-token daemons.

Drives the real script with a stubbed ``redis-cli`` on PATH (same pattern as
test_watch_go_dispatches): each test declares the bus state as a mapping from
the redis-cli argument tail to its reply.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess


SCRIPT = Path(__file__).parents[1] / "scripts" / "agent-bridge-ping"
AGENT = "codex-project-c-dev"
REG = f"agent_scratch:registry:{AGENT}"
STATUS = f"agent_scratch:agent:{AGENT}:status"
CONSUMER = f"agent_scratch:agent:{AGENT}:consumer"


def _run_ping(
    tmp_path: Path,
    replies: dict[str, str],
    env_extra: dict[str, str] | None = None,
    argv: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "redis-cli"
    lines = ["#!/bin/sh", 'case "$*" in']
    for tail, reply in replies.items():
        lines.append(f'  *"{tail}") echo "{reply}" ;;')
    lines.extend(['  *) echo "" ;;', "esac"])
    stub.write_text("\n".join(lines) + "\n", encoding="utf-8")
    stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "AGENT_ID": AGENT,
        "AGENT_PROJECT": "project-c",
        "AGENT_ENV_FILE": "/dev/null",
        "AGENT_REDIS_HOST": "127.0.0.1",
        "AGENT_REDIS_PORT": "6379",
        "AGENT_REDIS_DB": "12",
        "AGENT_REDIS_PREFIX": "agent_scratch:",
        **(env_extra or {}),
    }
    return subprocess.run(
        [str(SCRIPT), *(argv or [])],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def _legacy_alive(agent: str) -> dict[str, str]:
    reg = f"agent_scratch:registry:{agent}"
    status = f"agent_scratch:agent:{agent}:status"
    return {
        f"HGET {reg} registered_at": "2026-07-11T00:00:00Z",
        f"TTL {status}": "42",
        f"HGET {reg} pid": "123",
        f"HGET {reg} owner_token": "",
        f"GET {status}": "alive:123",
    }


def _printed_agent_id(stdout: str) -> str:
    for tok in stdout.split():
        if tok.startswith("agent_id="):
            return tok.split("=", 1)[1]
    raise AssertionError(f"no agent_id in stdout: {stdout!r}")


# --- config-resolution failure must be LOUD (regression: it was silent) --------------------
#
# Found live by panel round art-faba-sa-guide-v4-certify-20260727T014554Z: with an env file
# that does not EXIST, get_env's awk exited 2, its stderr was discarded, and `set -euo
# pipefail` killed the script inside a command substitution — exit 2 and ZERO output. An
# operator reads that as a dead seat and starts diagnosing the bridge, when the fault is a
# path in their own environment.
#
# Why the tests above never caught it: they pass AGENT_ENV_FILE=/dev/null, which IS readable,
# so awk succeeds and returns nothing. The missing-file branch was untested, not merely
# unbroken. These use a path that genuinely does not exist.

_SCRUBBED = {"PATH": os.environ["PATH"], "HOME": "/tmp"}


def _run_unresolvable(tmp_path: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
    env = {**_SCRUBBED, "AGENT_ENV_FILE": str(tmp_path / "nope.env"),
           "AGENT_PROJECT": "bridge", **overrides}
    return subprocess.run([str(SCRIPT)], text=True, capture_output=True, env=env, check=False)


def test_unresolvable_config_fails_loudly_not_silently(tmp_path: Path) -> None:
    """Exit 2 with no output WAS the bug, so an empty stderr must fail this test."""
    result = _run_unresolvable(tmp_path)
    assert result.returncode == 2
    assert result.stderr.strip(), "health check died silently — that is the whole defect"
    assert "cannot resolve" in result.stderr
    assert "nope.env" in result.stderr, "the error must name the path it could not read"
    assert "NOT READABLE" in result.stderr


def test_config_error_is_not_reported_as_a_seat_verdict(tmp_path: Path) -> None:
    """The failure being prevented is an operator concluding the SEAT is dead when their own
    env path is wrong, so the message must disclaim that and emit no verdict of its own."""
    result = _run_unresolvable(tmp_path)
    assert "NOT evidence about the seat's health" in result.stderr
    assert "registry=" not in result.stdout


def test_each_missing_required_value_is_named(tmp_path: Path) -> None:
    """Name WHICH values are missing. A generic error returns the operator to guessing."""
    result = _run_unresolvable(tmp_path)
    for var in ("AGENT_REDIS_HOST", "AGENT_REDIS_PORT", "AGENT_REDIS_DB"):
        assert var in result.stderr


def test_absent_env_file_is_not_fatal_when_the_environment_supplies_the_values(
    tmp_path: Path,
) -> None:
    """The env file is a source of DEFAULTS, not a requirement: the fix must not have turned a
    missing file into an error. Uses the redis-cli stub, so this stays hermetic."""
    result = _run_ping(
        tmp_path,
        {
            f"HGET {REG} registered_at": "2026-07-11T00:00:00Z",
            f"TTL {STATUS}": "42",
            f"HGET {REG} pid": "123",
            f"HGET {REG} owner_token": "",
            f"GET {STATUS}": "alive:123",
        },
        env_extra={"AGENT_ENV_FILE": str(tmp_path / "absent.env")},
    )
    assert "cannot resolve" not in result.stderr, (
        "a missing env file became fatal even though the environment supplied every value"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "consumer=legacy" in result.stdout


def test_pre_token_daemon_passes_without_consumer_key(tmp_path: Path) -> None:
    result = _run_ping(
        tmp_path,
        {
            f"HGET {REG} registered_at": "2026-07-11T00:00:00Z",
            f"TTL {STATUS}": "42",
            f"HGET {REG} pid": "123",
            f"HGET {REG} owner_token": "",
            f"GET {STATUS}": "alive:123",
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "consumer=legacy" in result.stdout


def test_token_daemon_without_consumer_key_is_dead(tmp_path: Path) -> None:
    result = _run_ping(
        tmp_path,
        {
            f"HGET {REG} registered_at": "2026-07-11T00:00:00Z",
            f"TTL {STATUS}": "42",
            f"HGET {REG} pid": "123",
            f"HGET {REG} owner_token": "tok-a",
            f"GET {STATUS}": "alive:tok-a",
            f"TTL {CONSUMER}": "-2",
        },
    )

    assert result.returncode == 1
    assert "consumer=dead" in result.stdout


def test_token_daemon_with_consumer_key_is_alive(tmp_path: Path) -> None:
    result = _run_ping(
        tmp_path,
        {
            f"HGET {REG} registered_at": "2026-07-11T00:00:00Z",
            f"TTL {STATUS}": "42",
            f"HGET {REG} pid": "123",
            f"HGET {REG} owner_token": "tok-a",
            f"GET {STATUS}": "alive:tok-a",
            f"TTL {CONSUMER}": "55",
            f"GET {CONSUMER}": "consuming:tok-a",
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "consumer=alive" in result.stdout


def test_pre_token_daemon_with_foreign_status_is_mismatch(tmp_path: Path) -> None:
    result = _run_ping(
        tmp_path,
        {
            f"HGET {REG} registered_at": "2026-07-11T00:00:00Z",
            f"TTL {STATUS}": "42",
            f"HGET {REG} pid": "123",
            f"HGET {REG} owner_token": "",
            f"GET {STATUS}": "alive:456",
        },
    )

    assert result.returncode == 1
    assert "owner-mismatch" in result.stdout


# --- --role must address role-suffixed seats (the helper used to miss them) ----------------
#
# The daemon registers as <tool>-<project>-<workspace>-<role> when --role is set. Ping
# derived only three segments, so a live role-suffixed seat reported registry=missing.
# Passing --role was worse: `*) break` left --role as $1, so WORKSPACE=--role and the
# printed id was `<tool>-<project>---role`. Reproduced 2026-08-21 against
# pi-sdk-bridge-dev-oxa-impl / pi-sdk-bridge-dev-oxa-rev.


def test_role_flag_addresses_role_suffixed_seat(tmp_path: Path) -> None:
    """Ping must look up the four-segment id; a missing-registry verdict for a live
    role-suffixed seat is the defect. Also: --role must not become the workspace."""
    agent = "codex-project-c-dev-impl"
    result = _run_ping(
        tmp_path,
        _legacy_alive(agent),
        env_extra={"AGENT_ID": ""},
        argv=["--role", "impl"],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _printed_agent_id(result.stdout) == agent
    assert "---role" not in result.stdout
    assert "registry=missing" not in result.stdout
    assert "consumer=legacy" in result.stdout


def test_no_role_derives_three_segment_id(tmp_path: Path) -> None:
    """--role is opt-in; with AGENT_ID unset the derived id stays tool-project-workspace."""
    result = _run_ping(
        tmp_path,
        _legacy_alive(AGENT),
        env_extra={"AGENT_ID": ""},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _printed_agent_id(result.stdout) == AGENT


def test_help_lists_role_flag(tmp_path: Path) -> None:
    result = _run_ping(tmp_path, {}, argv=["--help"])
    assert result.returncode == 0
    assert "--role" in result.stderr


def test_agent_role_env_addresses_role_suffixed_seat(tmp_path: Path) -> None:
    """AGENT_ROLE is the process-env analogue of AGENT_WORKSPACE — not read from the
    shared env file, which would silently redirect every ping in that environment."""
    agent = "codex-project-c-dev-impl"
    result = _run_ping(
        tmp_path,
        _legacy_alive(agent),
        env_extra={"AGENT_ID": "", "AGENT_ROLE": "impl"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _printed_agent_id(result.stdout) == agent


def test_role_flag_overrides_agent_role_env(tmp_path: Path) -> None:
    agent = "codex-project-c-dev-rev"
    result = _run_ping(
        tmp_path,
        _legacy_alive(agent),
        env_extra={"AGENT_ID": "", "AGENT_ROLE": "impl"},
        argv=["--role", "rev"],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _printed_agent_id(result.stdout) == agent


def test_role_flag_with_engine_matches_live_seat_shape(tmp_path: Path) -> None:
    """The 2026-08-21 repro: ping --engine pi-sdk --role oxa-impl against project=bridge."""
    agent = "pi-sdk-bridge-dev-oxa-impl"
    result = _run_ping(
        tmp_path,
        _legacy_alive(agent),
        env_extra={"AGENT_ID": "", "AGENT_PROJECT": "bridge"},
        argv=["--engine", "pi-sdk", "--role", "oxa-impl"],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _printed_agent_id(result.stdout) == agent
