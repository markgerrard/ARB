"""agent-dispatch --audit-panel marker tests (Slice 1d-iv pre-minted dry-run)."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from agent_redis_bridge.dispatch_authority import content_hash_for_brief
from agent_redis_bridge.redis_io import RedisCli, RedisConfig

DISPATCH = str(Path(__file__).parents[1] / "scripts" / "agent-dispatch")
ROOT = Path(__file__).resolve().parents[1]
TARGET = "codex-bridge-dev"
VANTAGE = "mac-host-dev"
GEN = "audit-panel-gen-1"


def _local_redis():
    try:
        cli = RedisCli(
            RedisConfig(
                host="127.0.0.1",
                port="6379",
                db="12",
                prefix="agent_scratch:",
            )
        )
        cli.client.ping()
        return cli
    except Exception:
        return None


@pytest.fixture
def registered_target():
    cli = _local_redis()
    if cli is None:
        pytest.skip("no local redis on 127.0.0.1:6379")
    cfg = cli.config
    reg = cfg.registry_key(TARGET)
    status = cfg.status_key(TARGET)
    cli.client.hset(
        reg,
        mapping={
            "tool": "codex",
            "project": "bridge",
            "workspace": "dev",
            "current_branch": "dev",
            "path": "/tmp",
            "registered_at": "now",
            "pid": "1",
            "owner_token": "tok-audit",
            "env_scrub": "",
            "worker_vantage": VANTAGE,
            "task_wire": "legacy-or-ref-v1",
            "brief_hydrate": "",
            "registration_generation": GEN,
        },
    )
    cli.client.set(status, "alive:tok-audit", ex=120)
    try:
        yield cli
    finally:
        cli.client.delete(reg, status)


def _brief_and_receipt(td: Path) -> tuple[Path, Path]:
    body = (
        "# Dispatch brief\n\n"
        "## Assumptions\n```json\n"
        + json.dumps(
            {
                "items": [
                    {
                        "statement": "bus is up",
                        "status": "assumed",
                        "vantage": VANTAGE,
                    }
                ]
            },
            indent=2,
        )
        + "\n```\n\n## Instructions\n\nhello\n"
    )
    brief = td / "brief.md"
    brief.write_text(body, encoding="utf-8")
    receipt = {
        "artefact_id": "art-audit-1",
        "version": 1,
        "target_agent_id": TARGET,
        "registration_generation": GEN,
        "worker_vantage": VANTAGE,
        "content_hash": content_hash_for_brief(body),
    }
    rec = td / "receipt.json"
    rec.write_text(json.dumps(receipt), encoding="utf-8")
    return brief, rec


def _env() -> dict[str, str]:
    return {
        **os.environ,
        "BRANCH": "dev",
        "FROM_AGENT_ID": "claude-bridge-dev",
        "AGENT_REDIS_HOST": "127.0.0.1",
        "AGENT_REDIS_PORT": "6379",
        "AGENT_REDIS_DB": "12",
        "AGENT_REDIS_PREFIX": "agent_scratch:",
        "PATH": f"{ROOT / '.venv' / 'bin'}:{os.environ.get('PATH', '')}",
        "PYTHONPATH": str(ROOT / "src"),
    }


def _envelope(extra_args, *, brief: Path, receipt: Path):
    out = subprocess.run(
        [
            DISPATCH,
            *extra_args,
            "--target-id",
            TARGET,
            "--artefact-id",
            "art-audit-1",
            "--version",
            "1",
            "--receipt",
            str(receipt),
            "--brief",
            str(brief),
            "--dry-run-envelope",
        ],
        capture_output=True,
        text=True,
        env=_env(),
        cwd=str(ROOT),
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _stable(env):
    env = dict(env)
    env.pop("id", None)
    env.pop("sent_at", None)
    return env


def test_audit_panel_flag_adds_vote_expected_marker(registered_target):
    with tempfile.TemporaryDirectory() as td:
        brief, receipt = _brief_and_receipt(Path(td))
        base = json.loads(_envelope(["--run-id", "panel-x"], brief=brief, receipt=receipt))
        withp = json.loads(
            _envelope(
                ["--run-id", "panel-x", "--audit-panel"],
                brief=brief,
                receipt=receipt,
            )
        )
    assert "audit_vote_expected" not in base.get("payload", {})
    assert withp["payload"]["audit_vote_expected"] is True
    # marker is the ONLY difference, and lives in payload (matches spec: payload.audit_vote_expected)
    del withp["payload"]["audit_vote_expected"]
    assert _stable(base) == _stable(withp)


def test_audit_vote_expected_survives_envelope_parse(registered_target):
    from agent_redis_bridge.envelope import Envelope

    with tempfile.TemporaryDirectory() as td:
        brief, receipt = _brief_and_receipt(Path(td))
        raw = _envelope(
            ["--run-id", "panel-x", "--audit-panel"],
            brief=brief,
            receipt=receipt,
        )
    env = Envelope.from_json(raw)
    assert env.payload.get("audit_vote_expected") is True


def test_audit_panel_flag_is_accepted(registered_target):
    with tempfile.TemporaryDirectory() as td:
        brief, receipt = _brief_and_receipt(Path(td))
        out = subprocess.run(
            [
                DISPATCH,
                "--audit-panel",
                "--run-id",
                "p",
                "--target-id",
                TARGET,
                "--artefact-id",
                "art-audit-1",
                "--version",
                "1",
                "--receipt",
                str(receipt),
                "--brief",
                str(brief),
                "--dry-run-envelope",
            ],
            capture_output=True,
            text=True,
            env=_env(),
            cwd=str(ROOT),
        )
    assert out.returncode == 0 and "unknown" not in out.stderr.lower()


def test_audit_panel_without_run_id_fails_loud():
    # No redis/registry needed: --audit-panel without --run-id is refused before
    # the pre-minted/authority path (same early exit as before 1d-iv).
    out = subprocess.run(
        [DISPATCH, "--audit-panel", "--dry-run-envelope", "hi"],
        capture_output=True,
        text=True,
        env=_env(),
        cwd=str(ROOT),
    )
    assert out.returncode != 0
    assert "--audit-panel requires --run-id" in out.stderr


def test_audit_panel_requires_nothing_when_off(registered_target):
    # Off path still needs pre-minted quartet (Slice 1d-iv ordinary path); the
    # original claim is that run_id is not required / not forced when the flag is off.
    with tempfile.TemporaryDirectory() as td:
        brief, receipt = _brief_and_receipt(Path(td))
        out = subprocess.run(
            [
                DISPATCH,
                "--target-id",
                TARGET,
                "--artefact-id",
                "art-audit-1",
                "--version",
                "1",
                "--receipt",
                str(receipt),
                "--brief",
                str(brief),
                "--dry-run-envelope",
            ],
            capture_output=True,
            text=True,
            env=_env(),
            cwd=str(ROOT),
        )
    assert out.returncode == 0 and '"run_id"' not in out.stdout
