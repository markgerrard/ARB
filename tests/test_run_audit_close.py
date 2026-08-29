import io
import json
import hashlib
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from arb_memory import run
from arb_memory.audit import SEQ_TTL_SECONDS


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.set_calls = []
        self.adds = []
        self.operations = []
        self._seq = {}
        self._lock = threading.Lock()
        self.set_barrier = None

    def set(self, key, value, *, ex=None, nx=False):
        if nx and self.set_barrier is not None:
            self.set_barrier.wait()
        with self._lock:
            self.set_calls.append((key, value, ex, nx))
            self.operations.append("set")
            if nx and key in self.values:
                return False
            self.values[key] = value
            return True

    def get(self, key):
        return self.values.get(key)

    def delete(self, key):
        self.operations.append("delete")
        return int(self.values.pop(key, None) is not None)

    def incr(self, key):
        self.operations.append("incr")
        self._seq[key] = self._seq.get(key, 0) + 1
        return self._seq[key]

    def expire(self, key, seconds):
        self.operations.append("expire")
        return True

    def xadd(self, stream, fields, **kwargs):
        self.operations.append("xadd")
        self.adds.append((stream, fields, kwargs))
        return "1-0"

    def xrange(self, stream, min="-", max="+", count=None):
        return [
            (f"{index}-0", fields)
            for index, (entry_stream, fields, _kwargs) in enumerate(self.adds, start=1)
            if entry_stream == stream
        ]


class FakeConn:
    pass


def _patch_dependencies(monkeypatch, redis, conn, *, reconcile_ok=True, operations=None):
    monkeypatch.setattr(run, "_redis_client", lambda: redis)
    monkeypatch.setattr(run, "_memory_conn", lambda: conn)

    from arb_memory import panel_audit

    def fake_reconcile(*args, **kwargs):
        if operations is not None:
            operations.append("reconcile")
        if reconcile_ok:
            return {"ok": True, "incomplete": False, "gaps": []}
        return {"ok": False, "incomplete": False, "gaps": ["seat missing"]}

    monkeypatch.setattr(panel_audit, "reconcile", fake_reconcile)


def _payload_file(tmp_path, payload):
    path = tmp_path / "verdict.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_audit_close_happy_path_emits_verdict(tmp_path, monkeypatch):
    redis = FakeRedis()
    operations = []
    _patch_dependencies(monkeypatch, redis, FakeConn(), operations=operations)
    payload = {"kind": "verdict", "roster": ["seat:codex"], "stances": {"seat:codex": "approve"}}

    rc = run.main(["audit-close", "--run-id", "run-1", "--payload-file", str(_payload_file(tmp_path, payload))])

    assert rc == 0
    assert len(redis.adds) == 1
    assert json.loads(redis.adds[0][1]["payload"]) == payload
    assert operations[:1] == ["reconcile"]
    key, value, ttl, nx = redis.set_calls[0]
    assert key == "arbmem:audit:run:run-1:verdict_close"
    assert value == hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert ttl == SEQ_TTL_SECONDS
    assert nx is True


def test_audit_close_reconcile_refusal_exits_4_without_claim_or_emit(tmp_path, monkeypatch, capsys):
    redis = FakeRedis()
    _patch_dependencies(monkeypatch, redis, FakeConn(), reconcile_ok=False)
    payload = {"kind": "verdict", "roster": [], "stances": {}}

    rc = run.main(["audit-close", "--run-id", "run-refused", "--payload-file", str(_payload_file(tmp_path, payload))])

    assert rc == 4
    assert redis.set_calls == []
    assert redis.adds == []
    assert "seat missing" in capsys.readouterr().err


def test_audit_close_malformed_json_exits_2_without_connecting(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run, "_redis_client", lambda: pytest.fail("redis must not be opened"))
    monkeypatch.setattr(run, "_memory_conn", lambda: pytest.fail("postgres must not be opened"))
    path = tmp_path / "malformed.json"
    path.write_text('{"kind":', encoding="utf-8")

    rc = run.main(["audit-close", "--run-id", "run-bad", "--payload-file", str(path)])

    assert rc == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_audit_close_same_payload_is_idempotent(tmp_path, monkeypatch):
    redis = FakeRedis()
    _patch_dependencies(monkeypatch, redis, FakeConn())
    payload = {"kind": "verdict", "roster": ["seat:codex"], "stances": {"seat:codex": "approve"}}
    path = _payload_file(tmp_path, payload)

    assert run.main(["audit-close", "--run-id", "run-same", "--payload-file", str(path)]) == 0
    assert run.main(["audit-close", "--run-id", "run-same", "--payload-file", str(path)]) == 0

    assert len(redis.adds) == 1


def test_audit_close_different_payload_exits_5_without_second_emit(tmp_path, monkeypatch, capsys):
    redis = FakeRedis()
    _patch_dependencies(monkeypatch, redis, FakeConn())
    first = {
        "kind": "verdict",
        "roster": ["seat:codex"],
        "stances": {"seat:codex": "approve"},
        "rationale": "first",
    }
    second = {
        "kind": "verdict",
        "roster": ["seat:codex"],
        "stances": {"seat:codex": "approve"},
        "rationale": "second",
    }

    assert run.main(["audit-close", "--run-id", "run-different", "--payload-file", str(_payload_file(tmp_path, first))]) == 0
    assert run.main(["audit-close", "--run-id", "run-different", "--payload-file", str(_payload_file(tmp_path, second))]) == 5

    assert len(redis.adds) == 1
    assert "different verdict" in capsys.readouterr().err


def test_audit_close_concurrent_same_payload_has_exactly_one_emit(tmp_path, monkeypatch):
    redis = FakeRedis()
    redis.set_barrier = threading.Barrier(2)
    _patch_dependencies(monkeypatch, redis, FakeConn())
    payload = {"kind": "verdict", "roster": ["seat:codex"], "stances": {"seat:codex": "approve"}}
    path = _payload_file(tmp_path, payload)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _index: run.main(
                    ["audit-close", "--run-id", "run-concurrent", "--payload-file", str(path)]
                ),
                range(2),
            )
        )

    assert sorted(results) in ([0, 0], [0, 6])
    assert len(redis.adds) == 1


def test_audit_close_oversize_emit_failure_cleans_claim_and_retry_works(tmp_path, monkeypatch, capsys):
    redis = FakeRedis()
    _patch_dependencies(monkeypatch, redis, FakeConn())
    oversized = {"kind": "verdict", "rationale": "x" * 16_384}
    claim_key = "arbmem:audit:run:run-oversize:verdict_close"

    rc = run.main(
        ["audit-close", "--run-id", "run-oversize", "--payload-file", str(_payload_file(tmp_path, oversized))]
    )

    assert rc != 0
    error = capsys.readouterr().err
    assert "audit payload exceeds AUDIT_MAX_PAYLOAD_BYTES" in error
    assert "Traceback" not in error
    assert claim_key not in redis.values
    assert redis.adds == []

    valid = {"kind": "verdict", "rationale": "retry"}
    assert run.main(
        ["audit-close", "--run-id", "run-oversize", "--payload-file", str(_payload_file(tmp_path, valid))]
    ) == 0
    assert len(redis.adds) == 1


def test_audit_close_orphaned_same_hash_exits_6_without_emit(tmp_path, monkeypatch, capsys):
    redis = FakeRedis()
    _patch_dependencies(monkeypatch, redis, FakeConn())
    payload = {"kind": "verdict", "rationale": "crashed before emit"}
    payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    claim_key = "arbmem:audit:run:run-orphaned:verdict_close"
    redis.values[claim_key] = payload_hash

    rc = run.main(
        ["audit-close", "--run-id", "run-orphaned", "--payload-file", str(_payload_file(tmp_path, payload))]
    )

    assert rc == 6
    assert redis.adds == []
    error = capsys.readouterr().err
    assert "no verdict emitted" in error
    assert "DEL" in error


def test_audit_close_reads_quote_and_newline_safe_payload_from_stdin(monkeypatch):
    redis = FakeRedis()
    _patch_dependencies(monkeypatch, redis, FakeConn())
    payload = {
        "kind": "verdict",
        "rationale": "A \"double\" quote, an 'apostrophe', and\na newline",
    }
    monkeypatch.setattr(run.sys, "stdin", io.StringIO(json.dumps(payload)))

    rc = run.main(["audit-close", "--run-id", "run-stdin", "--payload-file", "-"])

    assert rc == 0
    assert json.loads(redis.adds[0][1]["payload"]) == payload


def test_audit_close_honors_memory_prefix(tmp_path, monkeypatch):
    redis = FakeRedis()
    _patch_dependencies(monkeypatch, redis, FakeConn())
    monkeypatch.setenv("ARB_MEMORY_PREFIX", "fleet:")
    payload = {"kind": "verdict", "roster": ["seat:codex"], "stances": {"seat:codex": "approve"}}

    assert run.main(["audit-close", "--run-id", "run-prefixed", "--payload-file", str(_payload_file(tmp_path, payload))]) == 0

    assert redis.set_calls[0][0] == "fleet:arbmem:audit:run:run-prefixed:verdict_close"
    assert redis.adds[0][0] == "fleet:arbmem:audit"


@pytest.mark.parametrize(
    ("service", "handler"),
    [
        ("memory", "run_memory"),
        ("audit", "run_audit"),
        ("eval", "run_eval"),
        ("eval-purge", "run_eval_purge"),
        ("transcript", "run_transcript"),
        ("transcript-purge", "run_transcript_purge"),
        ("setup-schema", "run_setup_schema"),
        ("mcp", "run_mcp"),
        ("local-read-mcp", "run_local_read_mcp"),
        ("writer", "run_writer"),
        ("grants", "run_grants"),
        ("visibility", "run_visibility"),
    ],
)
def test_preexisting_service_verbs_still_dispatch(monkeypatch, service, handler):
    calls = []
    monkeypatch.setattr(run, handler, lambda: calls.append(service))

    assert run.main([service]) == 0
    assert calls == [service]
