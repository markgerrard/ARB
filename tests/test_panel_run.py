"""Unit tests for the panel-run glue (start_run / record_vote / finalize)."""
import json

import pytest

from arb_memory import panel_run
from arb_memory.stance import StanceError


class FakeRedis:
    """Captures audit emits; supports the ops AuditRun.next_seq + audit_emit use."""
    def __init__(self):
        self._seq = {}
        self.emitted = []  # list of (kind, payload-dict)

    def incr(self, key):
        self._seq[key] = self._seq.get(key, 0) + 1
        return self._seq[key]

    def expire(self, key, ttl):
        return True

    def xadd(self, name, fields, **kwargs):
        self.emitted.append({"name": name, "seq": int(fields["seq"]), "kind": fields["kind"],
                             "source": fields["source"], "payload": json.loads(fields["payload"])})
        return f"{len(self.emitted)}-0"


def test_start_run_emits_dispatch_manifest_seq1():
    r = FakeRedis()
    run_id = panel_run.start_run(r, ["codex", "agy", "seat:cold-opus"], run_id="rid1", note="design panel")
    assert run_id == "rid1"
    assert len(r.emitted) == 1
    m = r.emitted[0]
    assert m["kind"] == "dispatch" and m["seq"] == 1
    assert m["payload"]["roster"] == ["codex", "agy", "seat:cold-opus"]
    assert m["payload"]["note"] == "design panel"


def test_start_run_mints_run_id_when_absent():
    r = FakeRedis()
    run_id = panel_run.start_run(r, ["codex"])
    assert run_id and isinstance(run_id, str) and len(run_id) >= 16


def test_record_vote_parses_fenced_stance_and_emits():
    r = FakeRedis()
    reply = 'My review...\n```vote\n{"stance": "needs-changes", "severity": "P1", "refs": ["a.py:10"], "note": "x"}\n```'
    stance = panel_run.record_vote(r, "rid1", "seat:cold-opus", reply_text=reply)
    assert stance["stance"] == "needs-changes" and stance["severity"] == "P1"
    v = r.emitted[-1]
    assert v["kind"] == "vote"
    assert v["payload"]["actor"] == "seat:cold-opus"
    assert v["payload"]["stance"] == "needs-changes" and v["payload"]["severity"] == "P1"


def test_record_vote_fail_loud_on_no_fenced_stance():
    r = FakeRedis()
    with pytest.raises(StanceError):
        panel_run.record_vote(r, "rid1", "seat:cold-opus", reply_text="no vote block here, just prose")
    assert r.emitted == []  # never invents a vote


def test_record_vote_timed_out():
    r = FakeRedis()
    stance = panel_run.record_vote(r, "rid1", "seat:cold-opus", timed_out=True)
    assert stance["stance"] == "timed-out"
    assert r.emitted[-1]["payload"]["stance"] == "timed-out"


def test_finalize_emits_verdict_only_when_reconcile_ok(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(panel_run, "reconcile", lambda conn, rid, payload, **kw: {"ok": True, "incomplete": False, "gaps": []})
    result = panel_run.finalize(object(), r, "rid1", roster=["codex"], stances={"codex": "approve"}, overall="APPROVE")
    assert result["ok"]
    v = r.emitted[-1]
    assert v["kind"] == "verdict" and v["payload"]["stances"] == {"codex": "approve"} and v["payload"]["overall"] == "APPROVE"


def test_finalize_refuses_and_does_not_emit_when_reconcile_fails(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(panel_run, "reconcile",
                        lambda conn, rid, payload, **kw: {"ok": False, "incomplete": False, "gaps": ["seat 'agy' never voted"]})
    result = panel_run.finalize(object(), r, "rid1", roster=["codex", "agy"], stances={"codex": "approve", "agy": "block"})
    assert not result["ok"] and "agy" in result["gaps"][0]
    assert r.emitted == []  # verdict NOT emitted on a non-reconciling (laundered/incomplete) run
