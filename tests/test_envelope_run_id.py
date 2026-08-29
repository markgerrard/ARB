import json

import pytest

from agent_redis_bridge.envelope import Envelope, EnvelopeError


def _base(**over):
    d = {
        "id": "i1",
        "from": "claude",
        "branch": "manual",
        "to": "codex",
        "kind": "request",
        "sent_at": "2026-06-23T00:00:00+01:00",
        "payload": {"task": "do x"},
    }
    d.update(over)
    return json.dumps(d)


def test_run_id_round_trips_when_present():
    env = Envelope.from_json(_base(run_id="run-abc"))
    assert env.run_id == "run-abc"
    assert env.to_dict()["run_id"] == "run-abc"


def test_run_id_absent_is_none_and_omitted():
    env = Envelope.from_json(_base())
    assert env.run_id is None
    assert "run_id" not in env.to_dict()


def test_blank_run_id_rejected():
    with pytest.raises(EnvelopeError, match="invalid-run_id"):
        Envelope.from_json(_base(run_id=""))


def test_whitespace_run_id_rejected():
    with pytest.raises(EnvelopeError, match="invalid-run_id"):
        Envelope.from_json(_base(run_id="   "))


def test_non_string_run_id_rejected():
    with pytest.raises(EnvelopeError, match="invalid-run_id"):
        Envelope.from_json(_base(run_id=123))


def test_payload_smuggled_run_id_is_ignored():
    env = Envelope.from_json(_base(payload={"task": "do x", "run_id": "smuggled"}))
    assert env.run_id is None  # top-level field is the only source of truth
