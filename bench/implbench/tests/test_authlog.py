from __future__ import annotations

import os
from pathlib import Path

import pytest

from implbench.harness.authlog import AuthLog, AuthLogError, BoundedQuarantine
from implbench.harness.records import make_identity


def _record(n: int = 0) -> dict[str, object]:
    base = {
        "run_id": "oi-pi-bakeoff-test-20260714T000000Z", "cell_id": "cell-" + "a" * 64,
        "attempt_id": "attempt-" + "b" * 32, "pair": "GLM", "arm": "glm-pi", "task": "c1-parser",
        "repetition": 0, "schedule_index": n, "fixture_sha": "f" * 64, "model_declared": "glm-5.2",
        "model_verified_via": "provider-runtime-ack", "engine_version": "v1", "harness_version": "v1",
        "corpus_version": "implbench-corpus-v1", "config_digest": "1" * 64, "capability_manifest_digest": "2" * 64,
        "reasoning_requested": "medium", "reasoning_effective": "medium", "reasoning_verified_via": "provider-runtime-ack",
        "started_at": "2026-07-14T00:00:00Z", "ended_at": "2026-07-14T00:00:01Z", "wall_time_s": 1,
        "terminal_status": "completed", "retry_count": 0, "tool_call_count": 0, "schema_version": "record-v2",
        "prior_record_digest": None,
        "controls": {name: {"requested": "UNSUPPORTED", "effective": "UNSUPPORTED", "verified_via": "provider-runtime-ack"} for name in ("temperature", "top_p", "top_k", "seed", "penalties", "maximum_output", "stop_behavior", "tool_choice", "parallel_tool_behavior", "retry", "backoff", "timeouts")},
    }
    return make_identity(base, record_type="telemetry", payload={"event": "turn-start", "value": n})


def test_authlog_fsyncs_chain_and_verifies_after_restart(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "attempt.ndjson"
    calls: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: calls.append(fd) or real_fsync(fd))
    log = AuthLog(path, b"k" * 32)
    first = log.append(_record())
    second = log.append(_record(1))
    assert second["sequence"] == 2
    assert len(calls) >= 4
    assert AuthLog(path, b"k" * 32).verify() == 2
    assert first["nonce"] != second["nonce"]


@pytest.mark.parametrize("mutation", ["replace", "truncate", "sequence", "nonce", "replay"])
def test_authlog_rejects_mutation_and_replay_on_restart(tmp_path: Path, mutation: str) -> None:
    path = tmp_path / "attempt.ndjson"
    log = AuthLog(path, b"k" * 32)
    log.append(_record())
    log.append(_record(1))
    lines = path.read_bytes().splitlines(keepends=True)
    if mutation == "replace":
        lines[0] = lines[0].replace(b"turn-start", b"turn-stop")
    elif mutation == "truncate":
        lines[-1] = lines[-1][:-3]
    elif mutation == "sequence":
        lines[1] = lines[1].replace(b'"sequence":2', b'"sequence":9')
    elif mutation == "nonce":
        lines[1] = lines[1].replace(b'"nonce":"', b'"nonce":"0', 1)
    else:
        lines.append(lines[0])
    path.write_bytes(b"".join(lines))
    with pytest.raises(AuthLogError):
        AuthLog(path, b"k" * 32).verify()


def test_bounded_quarantine_only_exposes_digest(tmp_path: Path) -> None:
    quarantine = BoundedQuarantine(tmp_path / "private.ndjson", max_bytes=256)
    digest = quarantine.store("secret diagnostic")
    assert len(digest) == 64
    private = (tmp_path / "private.ndjson").read_bytes()
    assert digest.encode() in private
    assert b"secret diagnostic" not in private
    with pytest.raises(AuthLogError):
        quarantine.store("x" * 65)
