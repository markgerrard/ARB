import importlib.util
import json
from pathlib import Path
from importlib.machinery import SourceFileLoader


SCRIPT = Path(__file__).parents[1] / "scripts" / "arb-audit-close-request"
LOADER = SourceFileLoader("arb_audit_close_request", str(SCRIPT))
SPEC = importlib.util.spec_from_loader("arb_audit_close_request", LOADER)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeRedis:
    def __init__(self, result=None):
        self.result = result
        self.adds = []
        self.blpop_timeouts = []

    def xadd(self, stream, fields, **kwargs):
        self.adds.append((stream, fields, kwargs))
        return "1-0"

    def blpop(self, key, timeout):
        self.blpop_timeouts.append(timeout)
        if self.result is None:
            return None
        return key, json.dumps(self.result)


def test_helper_publishes_and_returns_consumer_exit_code(tmp_path, monkeypatch, capsys):
    redis = FakeRedis({"outcome": "refused_reconcile", "exit_code": 4, "gaps": ["seat missing"]})
    monkeypatch.setattr(MODULE.uuid, "uuid4", lambda: type("U", (), {"hex": "req-1"})())
    payload_file = tmp_path / "verdict.json"
    payload_file.write_text('{"kind":"verdict"}', encoding="utf-8")

    rc = MODULE.main(
        ["--run-id", "run-1", "--payload-file", str(payload_file), "--timeout", "3"],
        redis_factory=lambda: redis,
    )

    assert rc == 4
    assert redis.adds[0][0] == "arbmem:audit:close_request"
    assert redis.adds[0][1]["request_id"] == "req-1"
    assert json.loads(redis.adds[0][1]["verdict"]) == {"kind": "verdict"}
    assert "refused_reconcile" in capsys.readouterr().out


def test_helper_timeout_returns_distinct_exit_code(tmp_path, capsys):
    redis = FakeRedis()
    payload_file = tmp_path / "verdict.json"
    payload_file.write_text('{"kind":"verdict"}', encoding="utf-8")

    rc = MODULE.main(
        ["--run-id", "run-timeout", "--payload-file", str(payload_file), "--timeout", "1"],
        redis_factory=lambda: redis,
    )

    assert rc == 7
    assert "no close-consumer response" in capsys.readouterr().err


def test_helper_default_timeout_exceeds_reconcile_poll_window(tmp_path, capsys):
    redis = FakeRedis()
    payload_file = tmp_path / "verdict.json"
    payload_file.write_text('{"kind":"verdict"}', encoding="utf-8")

    rc = MODULE.main(
        ["--run-id", "run-default-timeout", "--payload-file", str(payload_file)],
        redis_factory=lambda: redis,
    )

    assert rc == 7
    assert redis.blpop_timeouts == [90]
    capsys.readouterr()
