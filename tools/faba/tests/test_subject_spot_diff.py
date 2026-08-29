"""Round-close subject-version spot-diff matrices for both FABA drivers."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
FABA = HERE.parent
for path in (str(FABA), str(FABA / "subagent"), str(FABA.parents[1] / "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

import run_author_round
import run_probe_round


@pytest.fixture(autouse=True)
def _admit_existing_driver_tests(monkeypatch):
    monkeypatch.setattr(run_author_round, "gate_hook_wired", lambda repo: None)
    monkeypatch.setattr(run_probe_round, "gate_hook_wired", lambda repo: None)


from faba_launch import PublishGateResult, subject_spot_diff


VALID_BODY = (
    "# Subject artefact\n\n**Change summary:** baseline for spot-diff tests.\n\n"
    + "body " * 80
    + "\n"
)


def observation(value, outcome="ok"):
    return {"value": value, "outcome": outcome}


def gate(passed, *, phase, outcome=None, refusal_cause=None):
    receipt = None if outcome is None else {
        "artefact_outcome": outcome,
        "artefact_id": "art-subject",
        "version": 8,
    }
    return PublishGateResult(passed, "test gate", receipt, None, phase, refusal_cause)


class FakeAuditBus:
    prefix = "test:"

    def __init__(self, *, emit_error=False):
        self.seq = 0
        self.events = []
        self.emit_error = emit_error

    def incr(self, key):
        self.seq += 1
        return self.seq

    def expire(self, key, seconds):
        return True

    def xadd(self, stream, fields, **kwargs):
        if self.emit_error:
            raise RuntimeError("audit down")
        self.events.append((stream, fields))
        return "1-0"


def test_author_verdict_matrix_is_receipt_derived_and_typed():
    cases = [
        (7, 8, "receipt_confirmed", "stored", "clean", 8),
        (7, 7, "receipt_confirmed", "deduped", "clean", 7),
        # Concurrent same-body writer: our receipt deduped, but HEAD moved.
        (7, 8, "receipt_confirmed", "deduped", "drift", 7),
        (7, 7, "not_enqueued", None, "clean", 7),
    ]
    for start, end, phase, outcome, verdict, expected in cases:
        receipt = None if outcome is None else {"artefact_outcome": outcome}
        result = subject_spot_diff(
            observation(start), observation(end), phase=phase, receipt=receipt, probe=False
        )
        assert (result["verdict"], result["expected"]) == (verdict, expected)


def test_author_fresh_receipt_and_refusal_matrix_has_no_none_arithmetic():
    stored = subject_spot_diff(
        observation("absent", "fresh"), observation(1),
        phase="receipt_confirmed", receipt={"artefact_outcome": "stored"}, probe=False,
    )
    refused_absent = subject_spot_diff(
        observation("absent", "fresh"), observation("absent", "not_found"),
        phase="not_enqueued", receipt=None, probe=False,
    )
    refused_existing = subject_spot_diff(
        observation("absent", "fresh"), observation(4),
        phase="not_enqueued", receipt=None, probe=False,
        refusal_cause="fresh_id_already_exists",
    )
    assert (stored["verdict"], stored["expected"]) == ("clean", 1)
    assert refused_absent["verdict"] == "clean"
    assert refused_existing["verdict"] == "pre-existing-id"


def test_unknown_receipt_and_fetch_failure_are_never_drift():
    unknown = subject_spot_diff(
        observation(3), observation(4), phase="receipt_unknown", receipt=None, probe=False
    )
    failed_close = subject_spot_diff(
        observation(3), observation("unobserved", "infra_exhausted"),
        phase="receipt_confirmed", receipt={"artefact_outcome": "stored"}, probe=False,
    )
    assert unknown["verdict"] == "indeterminate"
    assert failed_close["verdict"] == "unobserved"
    assert failed_close["end_outcome"] == "infra_exhausted"


def test_unknown_publish_phase_fails_loudly():
    import pytest

    with pytest.raises(ValueError, match="unknown publish phase"):
        subject_spot_diff(
            observation(3), observation(3), phase="invented", receipt=None, probe=False
        )


def _fake_child(module, pointer, *, called):
    def run(*args, **kwargs):
        called.append(True)
        state = json.loads(pointer.read_text(encoding="utf-8"))
        workspace = Path(state["workspace"])
        if state.get("kind") == "author":
            (workspace / "artefact.md").write_text(VALID_BODY + "folded\n", encoding="utf-8")
        state.update({"gate": "passed", "gate_reason": "ok", "attempts": 1})
        pointer.write_text(json.dumps(state), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")
    return run


def _run_author(tmp_path, monkeypatch, capsys, *, result, fetches, fresh=False, emit_error=False):
    workspace = tmp_path / "author-workspace"
    workspace.mkdir()
    pointer = tmp_path / "author-pointer.json"
    prior = tmp_path / "prior.md"
    prior.write_text(VALID_BODY, encoding="utf-8")
    bus = FakeAuditBus(emit_error=emit_error)
    called = []
    monkeypatch.setenv("ARB_MEMORY_REDIS_URL", "redis://memory/0")
    monkeypatch.setattr(run_author_round, "POINTER", pointer)
    monkeypatch.setattr(run_author_round.redis, "from_url", lambda *a, **k: bus)
    monkeypatch.setattr(run_author_round.tempfile, "mkdtemp", lambda **k: str(workspace))
    monkeypatch.setattr(run_author_round.uuid, "uuid4", lambda: SimpleNamespace(hex="12345678"))
    monkeypatch.setattr(
        run_author_round.subprocess, "run", _fake_child(run_author_round, pointer, called=called)
    )
    monkeypatch.setattr(run_author_round, "publish_artefact_and_gate", lambda *a, **k: result)
    outcomes = iter(fetches)

    def fetch(client, artefact_id, timeout=None):
        return next(outcomes)

    args = [
        "--stage", "design", "--subject-summary", "spot diff", "--task", "test",
        "--env-file", str(tmp_path / "absent.env"),
    ]
    if not fresh:
        args.extend(["--artefact-id", "art-subject", "--prior-record-file", str(prior)])
    code = run_author_round.main(args, fetch_by_id=fetch)
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err, bus, called


def test_author_pass_with_out_of_band_drift_emits_audit_and_exit_3(
    tmp_path, monkeypatch, capsys
):
    code, final, stderr, bus, _ = _run_author(
        tmp_path, monkeypatch, capsys,
        result=gate(True, phase="receipt_confirmed", outcome="stored"),
        fetches=[
            {"outcome": "ok", "artefact_id": "art-subject", "version": 7, "content": VALID_BODY},
            {"outcome": "ok", "artefact_id": "art-subject", "version": 9},
        ],
    )
    assert code == 3
    assert final["subject_spot_diff"]["verdict"] == "drift"
    assert final["subject_spot_diff"]["audit_emitted"] is True
    assert "SUBJECT DRIFT" in stderr
    assert len(bus.events) == 1 and bus.events[0][1]["kind"] == "subject_drift"


def test_author_audit_emit_is_fail_soft(tmp_path, monkeypatch, capsys):
    code, final, stderr, _, _ = _run_author(
        tmp_path, monkeypatch, capsys,
        result=gate(True, phase="receipt_confirmed", outcome="stored"),
        fetches=[
            {"outcome": "ok", "artefact_id": "art-subject", "version": 7, "content": VALID_BODY},
            {"outcome": "ok", "artefact_id": "art-subject", "version": 9},
        ],
        emit_error=True,
    )
    assert code == 3
    assert final["subject_spot_diff"]["audit_emitted"] is False
    assert "audit emit failed" in stderr


def test_author_fresh_preexisting_refusal_is_informational(tmp_path, monkeypatch, capsys):
    code, final, _, bus, _ = _run_author(
        tmp_path, monkeypatch, capsys,
        result=gate(False, phase="not_enqueued", refusal_cause="fresh_id_already_exists"),
        fetches=[{"outcome": "ok", "artefact_id": "art-faba-au-12345678", "version": 4}],
        fresh=True,
    )
    assert code == 1
    assert final["subject_spot_diff"]["verdict"] == "pre-existing-id"
    assert bus.events == []


def test_author_schema_refused_fresh_with_out_of_band_creation_is_drift(
    tmp_path, monkeypatch, capsys
):
    code, final, stderr, bus, _ = _run_author(
        tmp_path, monkeypatch, capsys,
        result=PublishGateResult(
            False, "artefact failed the authored-artefact check", None, None, "not_enqueued"
        ),
        fetches=[{"outcome": "ok", "artefact_id": "art-faba-au-12345678", "version": 4}],
        fresh=True,
    )
    assert code == 1
    assert final["subject_spot_diff"]["verdict"] == "drift"
    assert "SUBJECT DRIFT" in stderr
    assert len(bus.events) == 1 and bus.events[0][1]["kind"] == "subject_drift"


def test_author_clean_is_reported_through_main(tmp_path, monkeypatch, capsys):
    code, final, _, bus, _ = _run_author(
        tmp_path, monkeypatch, capsys,
        result=gate(True, phase="receipt_confirmed", outcome="deduped"),
        fetches=[
            {"outcome": "ok", "artefact_id": "art-subject", "version": 7, "content": VALID_BODY},
            {"outcome": "ok", "artefact_id": "art-subject", "version": 7},
        ],
    )
    assert code == 0
    assert final["subject_spot_diff"]["verdict"] == "clean"
    assert bus.events == []


def test_author_receipt_unknown_is_indeterminate_through_main(
    tmp_path, monkeypatch, capsys
):
    code, final, _, bus, _ = _run_author(
        tmp_path, monkeypatch, capsys,
        result=gate(False, phase="receipt_unknown"),
        fetches=[
            {"outcome": "ok", "artefact_id": "art-subject", "version": 7, "content": VALID_BODY},
            {"outcome": "ok", "artefact_id": "art-subject", "version": 8},
        ],
    )
    assert code == 1
    assert final["subject_spot_diff"]["verdict"] == "indeterminate"
    assert bus.events == []


def _run_probe(tmp_path, monkeypatch, capsys, *, fetches, result=None, emit_error=False):
    workspace = tmp_path / "probe-workspace"
    workspace.mkdir()
    pointer = tmp_path / "probe-pointer.json"
    bus = FakeAuditBus(emit_error=emit_error)
    called = []
    monkeypatch.setenv("ARB_MEMORY_REDIS_URL", "redis://memory/0")
    monkeypatch.setattr(run_probe_round, "POINTER", pointer)
    monkeypatch.setattr(run_probe_round.redis, "from_url", lambda *a, **k: bus)
    monkeypatch.setattr(run_probe_round.tempfile, "mkdtemp", lambda **k: str(workspace))
    monkeypatch.setattr(run_probe_round.uuid, "uuid4", lambda: SimpleNamespace(hex="12345678"))
    monkeypatch.setattr(
        run_probe_round.subprocess, "run", _fake_child(run_probe_round, pointer, called=called)
    )
    monkeypatch.setattr(
        run_probe_round,
        "publish_and_gate",
        lambda *a, **k: result or gate(True, phase="receipt_confirmed", outcome="stored"),
    )
    outcomes = iter(fetches)

    def fetch(client, artefact_id, timeout=None):
        return next(outcomes)

    code = run_probe_round.main(
        [
            "--artefact-id", "art-subject", "--subject-summary", "spot diff",
            "--round", "1", "--task", "test", "--env-file", str(tmp_path / "absent.env"),
        ],
        fetch_by_id=fetch,
    )
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err, bus, called


def test_probe_clean_record_publish_does_not_count(tmp_path, monkeypatch, capsys):
    code, final, _, bus, _ = _run_probe(
        tmp_path, monkeypatch, capsys,
        fetches=[
            {"outcome": "ok", "artefact_id": "art-subject", "version": 5},
            {"outcome": "ok", "artefact_id": "art-subject", "version": 5},
        ],
    )
    assert code == 0 and final["subject_spot_diff"]["verdict"] == "clean"
    assert bus.events == []


def test_probe_drift_emits_and_uses_distinct_exit(tmp_path, monkeypatch, capsys):
    code, final, stderr, bus, _ = _run_probe(
        tmp_path, monkeypatch, capsys,
        fetches=[
            {"outcome": "ok", "artefact_id": "art-subject", "version": 5},
            {"outcome": "ok", "artefact_id": "art-subject", "version": 6},
        ],
    )
    assert code == 3 and final["subject_spot_diff"]["verdict"] == "drift"
    assert "SUBJECT DRIFT" in stderr
    assert len(bus.events) == 1 and bus.events[0][1]["kind"] == "subject_drift"


def test_probe_receipt_unknown_still_spot_diffs_subject(tmp_path, monkeypatch, capsys):
    code, final, stderr, bus, _ = _run_probe(
        tmp_path, monkeypatch, capsys,
        result=gate(False, phase="receipt_unknown"),
        fetches=[
            {"outcome": "ok", "artefact_id": "art-subject", "version": 5},
            {"outcome": "ok", "artefact_id": "art-subject", "version": 6},
        ],
    )
    assert code == 1
    assert final["subject_spot_diff"]["verdict"] == "drift"
    assert "SUBJECT DRIFT" in stderr
    assert len(bus.events) == 1 and bus.events[0][1]["kind"] == "subject_drift"


def test_probe_receipt_unknown_can_still_be_clean(tmp_path, monkeypatch, capsys):
    code, final, _, bus, _ = _run_probe(
        tmp_path, monkeypatch, capsys,
        result=gate(False, phase="receipt_unknown"),
        fetches=[
            {"outcome": "ok", "artefact_id": "art-subject", "version": 5},
            {"outcome": "ok", "artefact_id": "art-subject", "version": 5},
        ],
    )
    assert code == 1
    assert final["subject_spot_diff"]["verdict"] == "clean"
    assert bus.events == []


def test_probe_start_fetch_failure_is_unobserved_and_round_proceeds(
    tmp_path, monkeypatch, capsys
):
    code, final, _, bus, called = _run_probe(
        tmp_path, monkeypatch, capsys,
        fetches=[
            {"outcome": "infra_exhausted"},
            {"outcome": "ok", "artefact_id": "art-subject", "version": 6},
        ],
    )
    assert called
    assert code == 0 and final["subject_spot_diff"]["verdict"] == "unobserved"
    assert final["subject_spot_diff"]["start_outcome"] == "infra_exhausted"
    assert bus.events == []
