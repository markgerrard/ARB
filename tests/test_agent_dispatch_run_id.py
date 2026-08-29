"""agent-dispatch --run-id appears on dry-run envelope (Slice 1d-iv pre-minted)."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from agent_redis_bridge.dispatch_authority import content_hash_for_brief
from agent_redis_bridge.redis_io import RedisCli, RedisConfig

ROOT = Path(__file__).resolve().parents[1]
TARGET = "codex-bridge-dev"
VANTAGE = "mac-host-dev"
GEN = "run-id-gen-1"


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


def test_run_id_appears_in_dry_run_envelope():
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
            "owner_token": "tok-run",
            "env_scrub": "",
            "worker_vantage": VANTAGE,
            "task_wire": "legacy-or-ref-v1",
            "brief_hydrate": "",
            "registration_generation": GEN,
        },
    )
    cli.client.set(status, "alive:tok-run", ex=120)
    try:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
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
            brief = td_path / "brief.md"
            brief.write_text(body, encoding="utf-8")
            receipt = {
                "artefact_id": "art-run-1",
                "version": 1,
                "target_agent_id": TARGET,
                "registration_generation": GEN,
                "worker_vantage": VANTAGE,
                "content_hash": content_hash_for_brief(body),
            }
            rec = td_path / "receipt.json"
            rec.write_text(json.dumps(receipt), encoding="utf-8")
            env = {
                **os.environ,
                "AGENT_ENV_FILE": "/Users/<user>/<workspace>/envs/agent-redis-bridge-dev.env",
                "BRANCH": "dev",
                "FROM_AGENT_ID": "claude-bridge-dev",
                "AGENT_REDIS_HOST": "127.0.0.1",
                "AGENT_REDIS_PORT": "6379",
                "AGENT_REDIS_DB": "12",
                "AGENT_REDIS_PREFIX": "agent_scratch:",
                "PATH": f"{ROOT / '.venv' / 'bin'}:{os.environ.get('PATH', '')}",
                "PYTHONPATH": str(ROOT / "src"),
            }
            out = subprocess.run(
                [
                    "scripts/agent-dispatch",
                    "--engine",
                    "codex",
                    "--target-id",
                    TARGET,
                    "--run-id",
                    "run-xyz",
                    "--artefact-id",
                    "art-run-1",
                    "--version",
                    "1",
                    "--receipt",
                    str(rec),
                    "--brief",
                    str(brief),
                    "--dry-run-envelope",
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
    finally:
        cli.client.delete(reg, status)

    assert out.returncode == 0, out.stderr
    # the envelope JSON is printed on stdout
    line = [l for l in out.stdout.splitlines() if l.strip().startswith("{")][-1]
    env_json = json.loads(line)
    assert env_json["run_id"] == "run-xyz"
