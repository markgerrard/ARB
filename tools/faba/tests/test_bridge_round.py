from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

SUBAGENT = Path(__file__).resolve().parents[1] / "subagent"
if str(SUBAGENT) not in sys.path:
    sys.path.insert(0, str(SUBAGENT))

import bridge_round
import run_probe_round


class FakeDispatch:
    def __init__(self, root: Path, replies: list[dict], arm_extra: dict | None = None):
        self.root = root
        self.replies = list(replies)
        self.calls = []
        self.on_run = None
        self.arm_extra = dict(arm_extra or {})

    def __call__(self, **kw):
        self.calls.append(kw)
        operation = kw["operation"]
        if operation == "worktree_arm":
            return {
                "ok": True, "lease_id": "lease-1", "path": str(self.root),
                "expires_at": time.time() + 20_000, "base_oid": "a" * 40,
                **self.arm_extra,
            }
        if operation == "worktree_release":
            return {"ok": True, "lease_id": kw["lease_id"]}
        if self.on_run:
            self.on_run(len([c for c in self.calls if c["operation"] == "worktree_run"]), kw)
        return self.replies.pop(0)


def run(tmp_path, monkeypatch, replies, *, target="codex-seat", validate=lambda text: []):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setattr(bridge_round, "_exclude_faba", lambda path: None)
    monkeypatch.setattr(bridge_round, "LOCKS", tmp_path / "locks")
    fake = FakeDispatch(worktree, replies)
    published = []

    def publish(workspace):
        published.append((workspace / "artefact.md").read_text())
        return (True, "published", {"artefact_outcome": "stored"}, None)

    result, pub = bridge_round.run_bridge_round(
        target_id=target, env_file=tmp_path / "env", run_id="run-1",
        artefact_lock_id="art-1", base_oid="a" * 40, lease_ttl=4000,
        turn_timeout=900, staged_files={"input.json": b"{}\n"},
        output_name="artefact.md", expected_exit_key="artefact_id",
        expected_exit_id="art-1", make_brief=lambda workspace: "contract",
        validate=validate, publish=publish, ledger_path=tmp_path / "ledger.json",
        forensics_root=tmp_path / "forensics", dispatch=fake,
    )
    return result, pub, fake, published


def test_valid_reply_publishes_from_forensics(tmp_path, monkeypatch):
    reply = {"ok": True, "result": 'FABA_EXIT {"artefact_id":"art-1"}', "thread_id": "th-1"}
    tmp2 = tmp_path / "second"
    tmp2.mkdir()
    monkeypatch.setattr(bridge_round, "LOCKS", tmp2 / "locks")
    worktree2 = tmp2 / "worktree"; worktree2.mkdir()
    fake2 = FakeDispatch(worktree2, [reply])
    fake2.on_run = lambda _n, _kw: (worktree2 / ".faba" / "run-1" / "artefact.md").write_text("valid")
    monkeypatch.setattr(bridge_round, "_exclude_faba", lambda path: None)
    published2 = []
    result, _ = bridge_round.run_bridge_round(
        target_id="codex-seat", env_file=tmp2 / "env", run_id="run-1",
        artefact_lock_id="art-1", base_oid="a"*40, lease_ttl=4000,
        turn_timeout=900, staged_files={"input.json": b"{}\n"}, output_name="artefact.md",
        expected_exit_key="artefact_id", expected_exit_id="art-1",
        make_brief=lambda workspace: "contract", validate=lambda text: [],
        publish=lambda ws: (published2.append((ws / "artefact.md").read_text()) or (True, "ok", None, None)),
        ledger_path=tmp2 / "ledger.json", forensics_root=tmp2 / "forensics", dispatch=fake2,
    )
    assert result.passed and published2 == ["valid"]
    assert [c["operation"] for c in fake2.calls] == ["worktree_arm", "worktree_run", "worktree_release"]


def test_resume_bounce_captures_first_thread_and_reuses_lease(tmp_path, monkeypatch):
    replies = [
        {"ok": True, "result": 'FABA_EXIT {"artefact_id":"art-1"}', "thread_id": "th-first"},
        {"ok": True, "result": 'FABA_EXIT {"artefact_id":"art-1"}', "thread_id": "th-first"},
    ]
    worktree = tmp_path / "worktree"; worktree.mkdir()
    monkeypatch.setattr(bridge_round, "_exclude_faba", lambda path: None)
    monkeypatch.setattr(bridge_round, "LOCKS", tmp_path / "locks")
    fake = FakeDispatch(worktree, replies)
    def outputs(n, _kw):
        (worktree / ".faba" / "run-1" / "artefact.md").write_text("bad" if n == 1 else "good")
    fake.on_run = outputs
    result, _ = bridge_round.run_bridge_round(
        target_id="codex-seat", env_file=tmp_path/"env", run_id="run-1", artefact_lock_id="art",
        base_oid="a"*40, lease_ttl=4000, turn_timeout=900, staged_files={"input": b"x"},
        output_name="artefact.md", expected_exit_key="artefact_id", expected_exit_id="art-1",
        make_brief=lambda ws: "brief", validate=lambda text: ["bad verbatim"] if text == "bad" else [],
        publish=lambda ws: (True, "ok", None, None), ledger_path=tmp_path/"ledger",
        forensics_root=tmp_path/"forensics", dispatch=fake,
    )
    runs = [c for c in fake.calls if c["operation"] == "worktree_run"]
    assert result.passed and result.thread_id == "th-first"
    assert runs[1]["thread_id"] == "th-first"
    assert runs[0]["lease_id"] == runs[1]["lease_id"] == "lease-1"
    assert "bad verbatim" in runs[1]["task"]


def test_stateless_bounce_is_fresh_and_names_draft_attempt_and_problems(tmp_path, monkeypatch):
    replies = [
        {"ok": True, "result": 'FABA_EXIT {"artefact_id":"art-1"}', "thread_id": None},
        {"ok": True, "result": 'FABA_EXIT {"artefact_id":"art-1"}', "thread_id": None},
    ]
    worktree = tmp_path/"worktree"; worktree.mkdir()
    monkeypatch.setattr(bridge_round, "_exclude_faba", lambda path: None)
    monkeypatch.setattr(bridge_round, "LOCKS", tmp_path/"locks")
    fake = FakeDispatch(worktree, replies)
    fake.on_run = lambda n, kw: (worktree/".faba"/"run-1"/"artefact.md").write_text("bad" if n == 1 else "good")
    result, _ = bridge_round.run_bridge_round(
        target_id="grok-seat", env_file=tmp_path/"env", run_id="run-1", artefact_lock_id="art",
        base_oid="a"*40, lease_ttl=4000, turn_timeout=900, staged_files={"input": b"x"},
        output_name="artefact.md", expected_exit_key="artefact_id", expected_exit_id="art-1",
        make_brief=lambda ws: "brief", validate=lambda text: ["problem exactly"] if text == "bad" else [],
        publish=lambda ws: (True, "ok", None, None), ledger_path=tmp_path/"ledger",
        forensics_root=tmp_path/"forensics", dispatch=fake,
    )
    second = [c for c in fake.calls if c["operation"] == "worktree_run"][1]
    assert result.passed and second["thread_id"] is None
    assert "attempt 2" in second["task"] and "artefact.md" in second["task"]
    assert "problem exactly" in second["task"]


@pytest.mark.parametrize("error", ["worktree_escape", "worktree-lease-expired", "worktree-lease-busy"])
def test_terminal_verdict_never_bounces(tmp_path, monkeypatch, error):
    result, _, fake, published = run(tmp_path, monkeypatch, [{"ok": False, "error": error}])
    assert not result.passed and not published
    assert len([c for c in fake.calls if c["operation"] == "worktree_run"]) == 1


def test_reply_body_bounces_but_oversize_fails_immediately(tmp_path, monkeypatch):
    body = "body\n" + 'FABA_EXIT {"artefact_id":"art-1"}'
    result, _, fake, _ = run(tmp_path, monkeypatch, [
        {"ok": True, "result": body, "thread_id": "t"},
        {"ok": True, "result": "x" * (bridge_round.REPLY_CHAR_CAP + 1), "thread_id": "t"},
    ])
    assert not result.passed
    runs = [c for c in fake.calls if c["operation"] == "worktree_run"]
    assert len(runs) == 2 and "return-channel rule" in runs[1]["task"]


def test_max_bounces_copies_forensics_before_release(tmp_path, monkeypatch):
    replies = [
        {"ok": True, "result": 'FABA_EXIT {"artefact_id":"art-1"}', "thread_id": None}
        for _ in range(3)
    ]
    worktree = tmp_path/"worktree"; worktree.mkdir()
    monkeypatch.setattr(bridge_round, "_exclude_faba", lambda path: None)
    monkeypatch.setattr(bridge_round, "LOCKS", tmp_path/"locks")
    fake = FakeDispatch(worktree, replies)
    fake.on_run = lambda n, kw: (worktree/".faba"/"run-1"/"artefact.md").write_text("always bad")
    result, _ = bridge_round.run_bridge_round(
        target_id="grok-seat", env_file=tmp_path/"env", run_id="run-1", artefact_lock_id="art",
        base_oid="a"*40, lease_ttl=4000, turn_timeout=900, staged_files={"input": b"x"},
        output_name="artefact.md", expected_exit_key="artefact_id", expected_exit_id="art-1",
        make_brief=lambda ws: "brief", validate=lambda text: ["still invalid"],
        publish=lambda ws: pytest.fail("must not publish"), ledger_path=tmp_path/"ledger",
        forensics_root=tmp_path/"forensics", dispatch=fake,
    )
    assert not result.passed and result.attempts == 3
    assert (result.workspace/"bounce-log.json").exists()
    assert fake.calls[-1]["operation"] == "worktree_release"


def test_each_bounce_restages_authoritative_inputs(tmp_path, monkeypatch):
    replies = [
        {"ok": True, "result": 'FABA_EXIT {"artefact_id":"art-1"}', "thread_id": None},
        {"ok": True, "result": 'FABA_EXIT {"artefact_id":"art-1"}', "thread_id": None},
    ]
    worktree = tmp_path/"worktree"; worktree.mkdir()
    monkeypatch.setattr(bridge_round, "_exclude_faba", lambda path: None)
    monkeypatch.setattr(bridge_round, "LOCKS", tmp_path/"locks")
    fake = FakeDispatch(worktree, replies)
    seen = []
    def mutate_then_check(n, kw):
        ws = worktree/".faba"/"run-1"
        seen.append((ws/"input").read_bytes())
        (ws/"artefact.md").write_text("bad" if n == 1 else "good")
        if n == 1:
            (ws/"input").write_bytes(b"seat mutation")
    fake.on_run = mutate_then_check
    result, _ = bridge_round.run_bridge_round(
        target_id="grok-seat", env_file=tmp_path/"env", run_id="run-1", artefact_lock_id="art",
        base_oid="a"*40, lease_ttl=4000, turn_timeout=900, staged_files={"input": b"authoritative"},
        output_name="artefact.md", expected_exit_key="artefact_id", expected_exit_id="art-1",
        make_brief=lambda ws: "brief", validate=lambda text: ["bad"] if text == "bad" else [],
        publish=lambda ws: (True, "ok", None, None), ledger_path=tmp_path/"ledger",
        forensics_root=tmp_path/"forensics", dispatch=fake,
    )
    assert result.passed and seen == [b"authoritative", b"authoritative"]
    assert result.events[0]["input_mutations"] == ["input"]


def test_bounce_restage_refuses_symlink_without_touching_outside(tmp_path, monkeypatch):
    replies = [
        {"ok": True, "result": 'FABA_EXIT {"artefact_id":"art-1"}', "thread_id": None},
    ]
    worktree = tmp_path/"worktree"; worktree.mkdir()
    sentinel = tmp_path/"sentinel"; sentinel.write_bytes(b"outside")
    monkeypatch.setattr(bridge_round, "_exclude_faba", lambda path: None)
    monkeypatch.setattr(bridge_round, "LOCKS", tmp_path/"locks")
    fake = FakeDispatch(worktree, replies)

    def plant_symlink(_n, _kw):
        workspace = worktree/".faba"/"run-1"
        (workspace/"artefact.md").write_text("bad")
        (workspace/"input").unlink()
        (workspace/"input").symlink_to(sentinel)

    fake.on_run = plant_symlink
    with pytest.raises(bridge_round.BridgeRoundError, match="symlink"):
        bridge_round.run_bridge_round(
            target_id="grok-seat", env_file=tmp_path/"env", run_id="run-1",
            artefact_lock_id="art", base_oid="a"*40, lease_ttl=4000,
            turn_timeout=900, staged_files={"input": b"authoritative"},
            output_name="artefact.md", expected_exit_key="artefact_id",
            expected_exit_id="art-1", make_brief=lambda ws: "brief",
            validate=lambda text: ["bad"], publish=lambda ws: (),
            ledger_path=tmp_path/"ledger", forensics_root=tmp_path/"forensics",
            dispatch=fake,
        )
    assert sentinel.read_bytes() == b"outside"


def test_bounce_restage_refuses_fifo_without_hanging(tmp_path, monkeypatch):
    replies = [
        {"ok": True, "result": 'FABA_EXIT {"artefact_id":"art-1"}', "thread_id": None},
    ]
    worktree = tmp_path/"worktree"; worktree.mkdir()
    monkeypatch.setattr(bridge_round, "_exclude_faba", lambda path: None)
    monkeypatch.setattr(bridge_round, "LOCKS", tmp_path/"locks")
    fake = FakeDispatch(worktree, replies)

    def plant_fifo(_n, _kw):
        workspace = worktree/".faba"/"run-1"
        (workspace/"artefact.md").write_text("bad")
        (workspace/"input").unlink()
        os.mkfifo(workspace/"input")

    class FifoOpenTimeout(BaseException):
        pass

    def timeout_handler(_signum, _frame):
        raise FifoOpenTimeout("FIFO re-stage blocked")

    fake.on_run = plant_fifo
    previous_handler = signal.signal(signal.SIGALRM, timeout_handler)
    signal.setitimer(signal.ITIMER_REAL, 2)
    try:
        with pytest.raises(bridge_round.BridgeRoundError, match="seat-owned path"):
            bridge_round.run_bridge_round(
                target_id="grok-seat", env_file=tmp_path/"env", run_id="run-1",
                artefact_lock_id="art", base_oid="a"*40, lease_ttl=4000,
                turn_timeout=900, staged_files={"input": b"authoritative"},
                output_name="artefact.md", expected_exit_key="artefact_id",
                expected_exit_id="art-1", make_brief=lambda ws: "brief",
                validate=lambda text: ["bad"], publish=lambda ws: (),
                ledger_path=tmp_path/"ledger", forensics_root=tmp_path/"forensics",
                dispatch=fake,
            )
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def test_publish_uses_the_same_forensic_bytes_that_were_validated(tmp_path, monkeypatch):
    worktree = tmp_path/"worktree"; worktree.mkdir()
    monkeypatch.setattr(bridge_round, "_exclude_faba", lambda path: None)
    monkeypatch.setattr(bridge_round, "LOCKS", tmp_path/"locks")
    reply = {"ok": True, "result": 'FABA_EXIT {"artefact_id":"art-1"}', "thread_id": None}
    fake = FakeDispatch(worktree, [reply])
    live = worktree/".faba"/"run-1"/"artefact.md"
    fake.on_run = lambda _n, _kw: live.write_text("validated body")
    validated = []
    published = []

    def validate(text):
        validated.append(text)
        live.write_text("different valid body")
        return []

    result, _ = bridge_round.run_bridge_round(
        target_id="grok-seat", env_file=tmp_path/"env", run_id="run-1",
        artefact_lock_id="art", base_oid="a"*40, lease_ttl=4000,
        turn_timeout=900, staged_files={"input": b"x"}, output_name="artefact.md",
        expected_exit_key="artefact_id", expected_exit_id="art-1",
        make_brief=lambda ws: "brief", validate=validate,
        publish=lambda ws: (published.append((ws/"artefact.md").read_text()) or (True, "ok", None, None)),
        ledger_path=tmp_path/"ledger", forensics_root=tmp_path/"forensics", dispatch=fake,
    )
    assert result.passed and published == validated == ["validated body"]


def test_resume_missing_thread_fails_closed(tmp_path, monkeypatch):
    result, _, fake, _ = run(tmp_path, monkeypatch, [
        {"ok": True, "result": 'FABA_EXIT {"artefact_id":"art-1"}', "thread_id": None}
    ])
    assert not result.passed and "no thread_id" in result.reason
    assert len([c for c in fake.calls if c["operation"] == "worktree_run"]) == 1


def test_unknown_engine_and_ttl_floor_refuse(tmp_path, monkeypatch):
    with pytest.raises(bridge_round.BridgeRoundError, match="pinned"):
        run(tmp_path, monkeypatch, [], target="mystery-seat")
    with pytest.raises(bridge_round.BridgeRoundError, match="lower bound"):
        bridge_round.run_bridge_round(
            target_id="grok-seat", env_file=tmp_path/"env", run_id="run-1",
            artefact_lock_id="a", base_oid="a"*40, lease_ttl=1, turn_timeout=900,
            staged_files={}, output_name="artefact.md", expected_exit_key="artefact_id",
            expected_exit_id="a", make_brief=lambda ws: "", validate=lambda text: [],
            publish=lambda ws: (), ledger_path=tmp_path/"l", forensics_root=tmp_path/"f",
            dispatch=lambda **kw: {},
        )


def test_symlink_rejected_for_publish_and_forensics(tmp_path):
    root = tmp_path/"root"; root.mkdir()
    outside = tmp_path/"outside"; outside.write_text("secret")
    (root/"artefact.md").symlink_to(outside)
    with pytest.raises(bridge_round.BridgeRoundError, match="symlink"):
        bridge_round.safe_read(root, "artefact.md")
    with pytest.raises(bridge_round.BridgeRoundError, match="symlink"):
        bridge_round.safe_copy_tree(tmp_path, "root", tmp_path/"copy")


def test_startup_ledger_sweeps_own_lease(tmp_path):
    ledger = tmp_path/"ledger.json"
    ledger.write_text(json.dumps([{"lease_id":"old", "target_id":"grok-seat", "env_file":"/e", "run_id":"r"}]))
    calls = []
    released = bridge_round.sweep_ledger(
        ledger_path=ledger,
        dispatch=lambda **kw: calls.append(kw) or {"ok": True},
    )
    assert released == ["old"] and json.loads(ledger.read_text()) == []
    assert calls[0]["operation"] == "worktree_release"


def test_arm_reply_is_ledgered_before_armed_path_is_trusted(tmp_path, monkeypatch):
    missing = tmp_path/"missing-worktree"
    ledger = tmp_path/"ledger.json"
    monkeypatch.setattr(bridge_round, "LOCKS", tmp_path/"locks")

    def dispatch(**kw):
        if kw["operation"] == "worktree_arm":
            return {"ok": True, "lease_id": "orphan", "path": str(missing),
                    "expires_at": time.time() + 20_000}
        return {"ok": False, "error": "release failed"}

    with pytest.raises(bridge_round.BridgeRoundError, match="not co-located"):
        bridge_round.run_bridge_round(
            target_id="grok-seat", env_file=tmp_path/"env", run_id="run-1",
            artefact_lock_id="art", base_oid="a"*40, lease_ttl=4000,
            turn_timeout=900, staged_files={}, output_name="artefact.md",
            expected_exit_key="artefact_id", expected_exit_id="art",
            make_brief=lambda ws: "", validate=lambda text: [], publish=lambda ws: (),
            ledger_path=ledger, forensics_root=tmp_path/"forensics", dispatch=dispatch,
        )
    assert json.loads(ledger.read_text())[0]["lease_id"] == "orphan"
    assert bridge_round.sweep_ledger(
        ledger_path=ledger, dispatch=lambda **kw: {"ok": True},
    ) == ["orphan"]


def test_sweep_reclaims_live_reused_pid_with_different_start(tmp_path, monkeypatch):
    ledger = tmp_path/"ledger.json"
    ledger.write_text(json.dumps([{
        "lease_id": "reused", "target_id": "grok-seat", "env_file": "/e",
        "run_id": "r", "pid": 4242, "pid_start": "old-start",
    }]))
    monkeypatch.setattr(bridge_round.os, "kill", lambda pid, signal: None)
    monkeypatch.setattr(bridge_round, "_process_identity", lambda pid: "new-start")
    released = bridge_round.sweep_ledger(
        ledger_path=ledger, dispatch=lambda **kw: {"ok": True},
    )
    assert released == ["reused"] and json.loads(ledger.read_text()) == []


def test_local_artefact_lock_is_exclusive(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_round, "LOCKS", tmp_path/"locks")
    first = bridge_round.acquire_local_lock("same-art")
    try:
        with pytest.raises(bridge_round.BridgeRoundError, match="another local round"):
            bridge_round.acquire_local_lock("same-art")
    finally:
        bridge_round.release_local_lock(first)


def test_reply_headroom_failure_is_terminal_but_forensics_is_copied(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(bridge_round.time, "time", lambda: clock[0])
    worktree = tmp_path/"worktree"; worktree.mkdir()
    monkeypatch.setattr(bridge_round, "_exclude_faba", lambda path: None)
    monkeypatch.setattr(bridge_round, "LOCKS", tmp_path/"locks")
    fake = FakeDispatch(worktree, [{"ok": True, "result": "reply", "thread_id": None}])
    original = fake.__call__
    def dispatch(**kw):
        if kw["operation"] == "worktree_arm":
            value = original(**kw); value["expires_at"] = 1300.0; return value
        if kw["operation"] == "worktree_run":
            (worktree/".faba"/"run-1"/"artefact.md").write_text("draft")
            clock[0] = 1100.0
        return original(**kw)
    result, _ = bridge_round.run_bridge_round(
        target_id="grok-seat", env_file=tmp_path/"env", run_id="run-1", artefact_lock_id="art",
        base_oid="a"*40, lease_ttl=4000, turn_timeout=900, staged_files={"input": b"x"},
        output_name="artefact.md", expected_exit_key="artefact_id", expected_exit_id="art",
        make_brief=lambda ws: "brief", validate=lambda text: [], publish=lambda ws: (),
        ledger_path=tmp_path/"ledger", forensics_root=tmp_path/"forensics", dispatch=dispatch,
    )
    assert not result.passed and "headroom at reply" in result.reason
    assert result.workspace is not None and not result.forensics_loss


def test_pre_dispatch_headroom_failure_records_forensics(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_round.time, "time", lambda: 1000.0)
    worktree = tmp_path/"worktree"; worktree.mkdir()
    monkeypatch.setattr(bridge_round, "_exclude_faba", lambda path: None)
    monkeypatch.setattr(bridge_round, "LOCKS", tmp_path/"locks")
    fake = FakeDispatch(worktree, [])
    original = fake.__call__

    def dispatch(**kw):
        value = original(**kw)
        if kw["operation"] == "worktree_arm":
            value["expires_at"] = 2100.0
        return value

    result, _ = bridge_round.run_bridge_round(
        target_id="grok-seat", env_file=tmp_path/"env", run_id="run-1",
        artefact_lock_id="art", base_oid="a"*40, lease_ttl=4000,
        turn_timeout=900, staged_files={"input": b"preserve"}, output_name="artefact.md",
        expected_exit_key="artefact_id", expected_exit_id="art", make_brief=lambda ws: "brief",
        validate=lambda text: [], publish=lambda ws: (), ledger_path=tmp_path/"ledger",
        forensics_root=tmp_path/"forensics", dispatch=dispatch,
    )
    assert not result.passed and "before run dispatch" in result.reason
    assert result.workspace is not None and not result.forensics_loss
    assert (result.workspace/"input").read_bytes() == b"preserve"
    assert json.loads((result.workspace/"bounce-log.json").read_text())[0]["dispatch_id"] is None


def test_release_failure_preserves_forensics_and_ledger(tmp_path, monkeypatch):
    worktree = tmp_path/"worktree"; worktree.mkdir()
    monkeypatch.setattr(bridge_round, "_exclude_faba", lambda path: None)
    monkeypatch.setattr(bridge_round, "LOCKS", tmp_path/"locks")
    fake = FakeDispatch(worktree, [
        {"ok": True, "result": 'FABA_EXIT {"artefact_id":"art-1"}', "thread_id": None}
    ])
    fake.on_run = lambda n, kw: (worktree/".faba"/"run-1"/"artefact.md").write_text("valid")
    def dispatch(**kw):
        if kw["operation"] == "worktree_release":
            fake.calls.append(kw)
            return {"ok": False, "error": "release failed"}
        return fake(**kw)
    ledger = tmp_path/"ledger"
    result, _ = bridge_round.run_bridge_round(
        target_id="grok-seat", env_file=tmp_path/"env", run_id="run-1", artefact_lock_id="art",
        base_oid="a"*40, lease_ttl=4000, turn_timeout=900, staged_files={"input": b"x"},
        output_name="artefact.md", expected_exit_key="artefact_id", expected_exit_id="art-1",
        make_brief=lambda ws: "brief", validate=lambda text: [],
        publish=lambda ws: (True, "ok", None, None), ledger_path=ledger,
        forensics_root=tmp_path/"forensics", dispatch=dispatch,
    )
    assert result.passed and (result.workspace/"artefact.md").read_text() == "valid"
    assert json.loads(ledger.read_text())[0]["lease_id"] == "lease-1"


def test_git_path_exclude_resolution(tmp_path):
    repo = tmp_path/"repo"; repo.mkdir()
    import subprocess
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    bridge_round._exclude_faba(repo)
    exclude = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-path", "info/exclude"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    exclude = Path(exclude) if Path(exclude).is_absolute() else repo/Path(exclude)
    assert "/.faba/" in exclude.read_text().splitlines()


def test_probe_bridge_materialises_subject_and_skips_native_doctor(tmp_path, monkeypatch, capsys):
    env_file = tmp_path / "env"
    env_file.write_text("ARB_MEMORY_REDIS_URL=redis://unused\n")
    monkeypatch.setattr(run_probe_round, "POINTER", tmp_path / "pointer.json")
    monkeypatch.setattr(run_probe_round.redis, "from_url", lambda *a, **k: object())
    monkeypatch.setattr(
        run_probe_round, "gate_hook_wired",
        lambda repo: (_ for _ in ()).throw(AssertionError("bridge mode ran native doctor")),
    )
    monkeypatch.setattr(bridge_round, "sweep_ledger", lambda **k: [])
    observed = {}

    def fake_round(**kw):
        observed.update(kw["staged_files"])
        return bridge_round.BridgeRoundResult(
            False, "test stop", tmp_path / "forensics", 1, "grok", "stateless"
        ), None

    monkeypatch.setattr(bridge_round, "run_bridge_round", fake_round)
    fetched = {
        "outcome": "ok", "artefact_id": "art-subject", "version": 3,
        "content": "# Subject\n\nmaterialised source\n",
    }
    code = run_probe_round.main(
        ["--engine", "bridge:grok-seat", "--run-id", "probe-1",
         "--artefact-id", "art-subject", "--subject-summary", "subject",
         "--round", "1", "--task", "review", "--env-file", str(env_file)],
        fetch_by_id=lambda *a, **k: fetched,
    )
    assert code == 1
    assert observed["subject.md"] == fetched["content"].encode()
    round_input = json.loads(observed["round-input.json"])
    assert round_input["subject_file"] == "subject.md"
    assert round_input["subject_sha256"] == __import__("hashlib").sha256(observed["subject.md"]).hexdigest()
    assert not run_probe_round.POINTER.exists()
    capsys.readouterr()


def test_agent_sdk_guard_refuses_seat_without_scrub_capability(tmp_path):
    with pytest.raises(bridge_round.BridgeRoundError, match="env_scrub"):
        bridge_round.require_agent_sdk_scrub(
            "asdk-piext-dev-fable5",
            env_file=tmp_path / "bus.env",
            registry_lookup=lambda target_id, env_file: None,
        )


def test_agent_sdk_guard_accepts_advertised_scrub_capability(tmp_path):
    bridge_round.require_agent_sdk_scrub(
        "asdk-piext-dev-fable5",
        env_file=tmp_path / "bus.env",
        registry_lookup=lambda target_id, env_file: "bus-and-gate-daemon-creds-v2",
    )


def test_agent_sdk_guard_capability_string_matches_bridge_constant():
    import sys as _sys
    from pathlib import Path as _Path

    repo_src = _Path(__file__).resolve().parents[3] / "src"
    if str(repo_src) not in _sys.path:
        _sys.path.insert(0, str(repo_src))
    from agent_redis_bridge.engines._stdio import ENV_SCRUB_CAPABILITY

    assert bridge_round.AGENT_SDK_ENV_SCRUB_CAPABILITY == ENV_SCRUB_CAPABILITY


def test_run_bridge_round_consults_registry_for_agent_sdk_targets(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_round, "LOCKS", tmp_path / "locks")
    looked_up = []

    def registry_lookup(target_id, env_file):
        looked_up.append(target_id)
        return None

    with pytest.raises(bridge_round.BridgeRoundError, match="env_scrub"):
        bridge_round.run_bridge_round(
            target_id="asdk-piext-dev-fable5", env_file=tmp_path / "env",
            run_id="run-asdk-1", artefact_lock_id="art-1", base_oid="a" * 40,
            lease_ttl=4000, turn_timeout=900, staged_files={"input.json": b"{}\n"},
            output_name="artefact.md", expected_exit_key="artefact_id",
            expected_exit_id="art-1", make_brief=lambda workspace: "contract",
            validate=lambda text: [], publish=lambda path: ("ok",),
            ledger_path=tmp_path / "ledger.json", forensics_root=tmp_path / "forensics",
            dispatch=FakeDispatch(tmp_path, []), registry_lookup=registry_lookup,
        )
    assert looked_up == ["asdk-piext-dev-fable5"]


LONG_RUN_ID = "faba-sa-panel-arb-role-polisher-final-20260722T015446Z-39bf73-resume-child-round-attempt"


def test_worktree_name_is_passthrough_for_short_run_ids():
    assert bridge_round.worktree_name_for_run("run-1") == "faba-run-1"


def test_worktree_name_is_bounded_and_deterministic_for_long_run_ids():
    assert len(LONG_RUN_ID) > bridge_round.MAX_WORKTREE_RUN_ID
    name = bridge_round.worktree_name_for_run(LONG_RUN_ID)
    assert name == bridge_round.worktree_name_for_run(LONG_RUN_ID)
    assert len(name) <= bridge_round.MAX_WORKTREE_RUN_ID + len("faba-")
    assert name.startswith("faba-")
    # distinct long ids must not collide after bounding
    other = bridge_round.worktree_name_for_run(LONG_RUN_ID + "-x")
    assert name != other


def test_long_run_id_dispatches_with_full_audit_id_and_bounded_worktree(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge_round, "_exclude_faba", lambda path: None)
    monkeypatch.setattr(bridge_round, "LOCKS", tmp_path / "locks")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    reply = {"ok": True, "result": 'FABA_EXIT {"artefact_id":"art-1"}', "thread_id": "th-1"}
    fake = FakeDispatch(worktree, [reply])
    fake.on_run = lambda _n, _kw: (
        worktree / ".faba" / LONG_RUN_ID / "artefact.md"
    ).write_text("valid")

    result, _ = bridge_round.run_bridge_round(
        target_id="codex-seat", env_file=tmp_path / "env", run_id=LONG_RUN_ID,
        artefact_lock_id="art-1", base_oid="a" * 40, lease_ttl=4000,
        turn_timeout=900, staged_files={"input.json": b"{}\n"},
        output_name="artefact.md", expected_exit_key="artefact_id",
        expected_exit_id="art-1", make_brief=lambda workspace: "contract",
        validate=lambda text: [], publish=lambda ws: (True, "ok", {}, None),
        ledger_path=tmp_path / "ledger.json", forensics_root=tmp_path / "forensics",
        dispatch=fake,
    )

    arm = next(c for c in fake.calls if c["operation"] == "worktree_arm")
    assert arm["worktree"] == bridge_round.worktree_name_for_run(LONG_RUN_ID)
    assert arm["run_id"] == LONG_RUN_ID  # audit identity is never truncated
    assert result.passed


def test_traversal_shaped_run_id_still_refused(tmp_path):
    with pytest.raises(bridge_round.BridgeRoundError, match="run-id"):
        bridge_round.run_bridge_round(
            target_id="codex-seat", env_file=tmp_path / "env", run_id="../evil",
            artefact_lock_id="art-1", base_oid="a" * 40, lease_ttl=4000,
            turn_timeout=900, staged_files={}, output_name="artefact.md",
            expected_exit_key="artefact_id", expected_exit_id="art-1",
            make_brief=lambda workspace: "contract", validate=lambda text: [],
            publish=lambda ws: (True, "ok", {}, None),
            ledger_path=tmp_path / "ledger.json", forensics_root=tmp_path / "forensics",
            dispatch=FakeDispatch(tmp_path, []),
        )


def test_engine_mode_names_the_fix_for_audit_roster_labels():
    with pytest.raises(bridge_round.BridgeRoundError, match=r"drop the 'seat:' prefix.*codex-bridge-dev-example"):
        bridge_round.engine_mode("seat:codex-bridge-dev-example")


def test_engine_mode_asdk_is_stateless_for_oneshot_fleet():
    # Chain B r2c (2026-07-22): fleet asdk seats run --agent-sdk-oneshot, so
    # supports_thread_resume is False at runtime even though the engine class
    # defaults True; oneshot seats still return a thread_id that cannot be
    # resumed, and a resume bounce carrying it is refused bridge-side as
    # thread-affinity-worktree-incompatible. The driver must bounce stateless.
    assert bridge_round.engine_mode("asdk") == ("agent-sdk", "stateless")
    assert bridge_round.engine_mode("asdk-piext-dev-fable5") == ("agent-sdk", "stateless")


def test_arm_pinned_base_oid_captured_on_result(tmp_path, monkeypatch):
    # Chain B r2a (2026-07-22): base refs must be seat-resolved ("HEAD"), so
    # driver provenance has to record the OID the seat pinned at arm time
    # rather than assuming the caller-side value survived.
    reply = {"ok": True, "result": 'FABA_EXIT {"artefact_id":"art-1"}', "thread_id": "th-1"}
    worktree = tmp_path / "worktree"; worktree.mkdir()
    monkeypatch.setattr(bridge_round, "_exclude_faba", lambda path: None)
    monkeypatch.setattr(bridge_round, "LOCKS", tmp_path / "locks")
    fake = FakeDispatch(worktree, [reply])
    fake.on_run = lambda _n, _kw: (worktree / ".faba" / "run-1" / "artefact.md").write_text("valid")
    result, _ = bridge_round.run_bridge_round(
        target_id="codex-seat", env_file=tmp_path / "env", run_id="run-1",
        artefact_lock_id="art-1", base_oid="HEAD", lease_ttl=4000,
        turn_timeout=900, staged_files={"input.json": b"{}\n"}, output_name="artefact.md",
        expected_exit_key="artefact_id", expected_exit_id="art-1",
        make_brief=lambda ws: "brief", validate=lambda text: [],
        publish=lambda ws: (True, "ok", None, None), ledger_path=tmp_path / "ledger",
        forensics_root=tmp_path / "forensics", dispatch=fake,
    )
    assert result.passed
    assert result.base_oid == "a" * 40


def _capability_round(tmp_path, monkeypatch, *, target, arm_extra, replies):
    worktree = tmp_path / "worktree"; worktree.mkdir()
    monkeypatch.setattr(bridge_round, "_exclude_faba", lambda path: None)
    monkeypatch.setattr(bridge_round, "LOCKS", tmp_path / "locks")
    fake = FakeDispatch(worktree, replies, arm_extra=arm_extra)
    fake.on_run = lambda n, _kw: (worktree / ".faba" / "run-1" / "artefact.md").write_text(
        "bad" if n == 1 else "good")
    result, _ = bridge_round.run_bridge_round(
        target_id=target, env_file=tmp_path / "env", run_id="run-1",
        artefact_lock_id="art-1", base_oid="HEAD", lease_ttl=4000,
        turn_timeout=900, staged_files={"input.json": b"{}\n"}, output_name="artefact.md",
        expected_exit_key="artefact_id", expected_exit_id="art-1",
        make_brief=lambda ws: "brief",
        validate=lambda text: ["problem"] if text == "bad" else [],
        publish=lambda ws: (True, "ok", None, None), ledger_path=tmp_path / "ledger",
        forensics_root=tmp_path / "forensics", dispatch=fake,
        registry_lookup=lambda target_id, env_file: "bus-and-gate-daemon-creds-v2",
    )
    return result, [c for c in fake.calls if c["operation"] == "worktree_run"]


def test_arm_thread_resume_true_overrides_asdk_stateless_fallback(tmp_path, monkeypatch):
    # panel-pisdk-rebaseline (2026-07-23): the blanket asdk->stateless table
    # wrongly covered non-oneshot seats (asdk-bridge-dev-haiku45). A seat that
    # advertises thread_resume=true at arm time gets RESUME bounces: the
    # captured attempt-0 thread_id is forwarded on the bounce.
    replies = [
        {"ok": True, "result": 'FABA_EXIT {"artefact_id":"art-1"}', "thread_id": "th-live"},
        {"ok": True, "result": 'FABA_EXIT {"artefact_id":"art-1"}', "thread_id": "th-live"},
    ]
    result, runs = _capability_round(
        tmp_path, monkeypatch, target="asdk-bridge-dev-haiku45",
        arm_extra={"thread_resume": True}, replies=replies)
    assert result.passed
    assert runs[1]["thread_id"] == "th-live"


def test_arm_thread_resume_false_forces_stateless_even_for_codex(tmp_path, monkeypatch):
    # The advertisement wins in BOTH directions: a codex-family seat that
    # reports thread_resume=false bounces stateless despite the resume default.
    replies = [
        {"ok": True, "result": 'FABA_EXIT {"artefact_id":"art-1"}', "thread_id": "th-1"},
        {"ok": True, "result": 'FABA_EXIT {"artefact_id":"art-1"}', "thread_id": "th-1"},
    ]
    result, runs = _capability_round(
        tmp_path, monkeypatch, target="codex-seat",
        arm_extra={"thread_resume": False}, replies=replies)
    assert result.passed
    assert runs[1]["thread_id"] is None


def test_asdk_stateless_bounce_never_forwards_captured_thread_id(tmp_path, monkeypatch):
    # Chain B r2c regression guard (kimi finding, panel-pisdk-rebaseline
    # 2026-07-23): with NO advertisement, an asdk seat whose attempt-0 reply
    # carries a (non-resumable) thread_id must still get a stateless bounce —
    # forwarding it is refused bridge-side as
    # thread-affinity-worktree-incompatible.
    replies = [
        {"ok": True, "result": 'FABA_EXIT {"artefact_id":"art-1"}', "thread_id": "th-bait"},
        {"ok": True, "result": 'FABA_EXIT {"artefact_id":"art-1"}', "thread_id": "th-bait"},
    ]
    result, runs = _capability_round(
        tmp_path, monkeypatch, target="asdk-piext-dev-fable5",
        arm_extra={}, replies=replies)
    assert result.passed
    assert runs[1]["thread_id"] is None
