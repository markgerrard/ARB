import hashlib
import json

import redis

from arb_memory import run


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.adds = []

    def set(self, key, value, *, ex=None, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)

    def delete(self, key):
        self.values.pop(key, None)

    def incr(self, key):
        value = int(self.values.get(key, 0)) + 1
        self.values[key] = value
        return value

    def expire(self, key, seconds):
        return True

    def xadd(self, stream, fields, **kwargs):
        self.adds.append((stream, fields, kwargs))
        return "1-0"

    def xrange(self, stream, min="-", max="+"):
        return [
            (f"{idx}-0", fields)
            for idx, (entry_stream, fields, _kwargs) in enumerate(self.adds, start=1)
            if entry_stream == stream
        ]


def _patch_reconcile(monkeypatch, *, ok=True, gaps=None):
    from arb_memory import panel_audit

    monkeypatch.setattr(
        panel_audit,
        "reconcile",
        lambda *args, **kwargs: {"ok": ok, "incomplete": False, "gaps": gaps or []},
    )


def test_close_core_returns_structured_emitted_result(monkeypatch):
    redis = FakeRedis()
    _patch_reconcile(monkeypatch)
    payload = {"kind": "verdict", "rationale": "approved"}

    result = run.close_core(object(), redis, "run-1", payload)

    assert result.outcome == "emitted"
    assert result.exit_code == 0
    assert result.gaps == []
    assert json.loads(redis.adds[0][1]["payload"]) == payload


def test_close_core_returns_refusal_without_claim(monkeypatch):
    redis = FakeRedis()
    _patch_reconcile(monkeypatch, ok=False, gaps=["seat missing"])

    result = run.close_core(object(), redis, "run-refused", {"kind": "verdict"})

    assert (result.outcome, result.exit_code, result.gaps) == (
        "refused_reconcile",
        4,
        ["seat missing"],
    )
    assert redis.values == {}


def test_close_core_returns_different_verdict(monkeypatch):
    redis = FakeRedis()
    _patch_reconcile(monkeypatch)
    first = {"kind": "verdict", "rationale": "first"}
    second = {"kind": "verdict", "rationale": "second"}

    assert run.close_core(object(), redis, "run-different", first).exit_code == 0
    result = run.close_core(object(), redis, "run-different", second)

    assert (result.outcome, result.exit_code) == ("different_verdict", 5)


def test_close_core_returns_orphaned_claim(monkeypatch):
    redis = FakeRedis()
    _patch_reconcile(monkeypatch)
    payload = {"kind": "verdict", "rationale": "orphan"}
    payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    redis.values["arbmem:audit:run:run-orphaned:verdict_close"] = payload_hash

    result = run.close_core(object(), redis, "run-orphaned", payload)

    assert (result.outcome, result.exit_code) == ("orphaned", 6)


def test_close_core_returns_emit_failed_and_releases_claim(monkeypatch):
    redis = FakeRedis()
    _patch_reconcile(monkeypatch)
    payload = {"kind": "verdict", "rationale": "x" * 16_384}

    result = run.close_core(object(), redis, "run-failed", payload)

    assert (result.outcome, result.exit_code) == ("emit_failed", 1)
    assert "arbmem:audit:run:run-failed:verdict_close" not in redis.values


def test_audit_close_cli_returns_one_for_infra_emit_failure(tmp_path, monkeypatch, capsys):
    fake_redis = FakeRedis()
    _patch_reconcile(monkeypatch)
    monkeypatch.setattr(run, "_redis_client", lambda: fake_redis)
    monkeypatch.setattr(run, "_memory_conn", lambda: object())

    from arb_memory.audit import AuditRun

    monkeypatch.setattr(
        AuditRun,
        "emit",
        lambda *args, **kwargs: (_ for _ in ()).throw(redis.ConnectionError("redis unavailable")),
    )
    payload_file = tmp_path / "verdict.json"
    payload_file.write_text('{"kind":"verdict"}', encoding="utf-8")

    rc = run.main(["audit-close", "--run-id", "run-infra", "--payload-file", str(payload_file)])

    assert rc == 1
    error = capsys.readouterr().err
    assert "verdict emit failed" in error
    assert "Traceback" not in error
    assert "arbmem:audit:run:run-infra:verdict_close" not in fake_redis.values
