from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
from unittest import mock

import pytest

import agent_redis_bridge.bridge as arb_bridge
from agent_redis_bridge.envelope import Envelope
from agent_redis_bridge.engines.base import TurnResult
from agent_redis_bridge.lane_writer import LaneStoreUnreachable
from agent_redis_bridge.worktree_lease import WorktreeLeaseError, WorktreeLeaseRecord
from test_bridge_handle_raw import FakeRedis
from test_bridge_worktree import FakeEngine, EscapingEngine, init_git_repo, request_json


class FakeLaneWriter:
    """In-memory lane writer for two-record lifecycle tests."""

    def __init__(
        self,
        *,
        consumer_id: str = "codex-project-c-dev",
        lane: str = "gated",
        arm_error: Exception | None = None,
        retire_error: Exception | None = None,
        retire_returns: bool = True,
    ) -> None:
        self.consumer_id = consumer_id
        self.lane = lane
        self.arm_error = arm_error
        self.retire_error = retire_error
        self.retire_returns = retire_returns
        self._rows: dict[str, dict] = {}
        self.arm_calls: list[str] = []
        self.retire_calls: list[str] = []
        self.closed = False

    def assert_ready(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def arm(self, lease_id: str) -> dict:
        self.arm_calls.append(lease_id)
        if self.arm_error is not None:
            raise self.arm_error
        row = {
            "lease_id": lease_id,
            "lane": self.lane,
            "armed_by": self.consumer_id,
            "armed_at": time.time(),
        }
        self._rows[lease_id] = row
        return dict(row)

    def retire(self, lease_id: str) -> bool:
        self.retire_calls.append(lease_id)
        if self.retire_error is not None:
            raise self.retire_error
        if not self.retire_returns:
            return False
        self._rows.pop(lease_id, None)
        return True

    def rows(self) -> list[dict]:
        return [dict(v) for v in self._rows.values()]


def make_lease_bridge(repo: str, lease_root: str, *extra: str) -> arb_bridge.Bridge:
    with tempfile.TemporaryDirectory() as temp_dir:
        env_file = Path(temp_dir) / ".env"
        env_file.write_text(
            "AGENT_REDIS_HOST=127.0.0.1\nAGENT_REDIS_PORT=6390\nAGENT_REDIS_DB=12\n"
            "AGENT_REDIS_PREFIX=agent_scratch:\nAGENT_WORKSPACE=dev\nAGENT_PROJECT=project-c\n"
        )
        args = arb_bridge.build_parser().parse_args(
            [
                "--env-file", str(env_file), "--workdir", repo,
                "--worktree-lease-root", lease_root,
                "--sender-policy", "claude-project-c-dev=trusted",
                "--sender-policy", "other-project-c-dev=trusted",
                *extra,
            ]
        )
        bridge = arb_bridge.Bridge(args)
        bridge.redis = FakeRedis()  # type: ignore[assignment]
        return bridge


def make_writer_lease_bridge(
    repo: str,
    lease_root: str,
    writer: FakeLaneWriter | None = None,
    *extra: str,
    lane: str = "gated",
) -> arb_bridge.Bridge:
    """Bridge with an attached in-memory lane writer (gate-off + writer active)."""
    bridge = make_lease_bridge(repo, lease_root, *extra)
    bridge.worktree_lane = lane
    if writer is None:
        writer = FakeLaneWriter(consumer_id=bridge.agent_id, lane=lane)
    else:
        writer.consumer_id = bridge.agent_id
        writer.lane = lane
    bridge.lane_writer = writer  # type: ignore[assignment]
    return bridge


def arm(bridge: arb_bridge.Bridge, name: str, *, request_id: str = "arm", sender: str = "claude-project-c-dev", ttl: int | None = None):
    payload: dict[str, object] = {
        "operation": "worktree_arm",
        "worktree": {"name": name, "base_ref": "HEAD", "cleanup": "keep"},
    }
    if ttl is not None:
        payload["lease_ttl"] = ttl
    bridge.handle_raw(request_json(request_id, payload=payload, sender=sender, recipient=bridge.agent_id))
    reply = json.loads(bridge.redis.replies[-1][1])["payload"]
    return reply


def run(bridge: arb_bridge.Bridge, lease_id: str, *, request_id: str = "run", sender: str = "claude-project-c-dev", thread_id: str | None = None):
    payload: dict[str, object] = {
        "operation": "worktree_run", "worktree_lease": lease_id, "task": "do work",
    }
    if thread_id is not None:
        payload["thread_id"] = thread_id
    bridge.handle_raw(request_json(request_id, payload=payload, sender=sender, recipient=bridge.agent_id))
    bridge.join_active_thread()
    return json.loads(bridge.redis.replies[-1][1])["payload"]


@pytest.fixture
def lease_env():
    repo = init_git_repo()
    with tempfile.TemporaryDirectory(prefix="lease-store-") as lease_root:
        yield repo, lease_root


def test_arm_stage_run_reenter_and_release_lifecycle(lease_env) -> None:
    repo, lease_root = lease_env
    bridge = make_lease_bridge(repo, lease_root, "--no-enforce-completion")
    armed = arm(bridge, "lease-life")
    assert armed["ok"] is True
    assert len(armed["lease_id"]) >= 40
    # The arm reply advertises the seat's actual resume capability so FABA
    # drivers pick bounce mode from ground truth, not the per-family table
    # (panel-pisdk-rebaseline finding, 2026-07-23).
    assert armed["thread_resume"] == bridge.engine_supports_resume
    assert isinstance(armed["thread_resume"], bool)
    worktree = Path(armed["path"])
    (worktree / "staged.txt").write_text("before turn", encoding="utf-8")
    created: list[FakeEngine] = []

    def factory(args, *, cwd):
        engine = FakeEngine(cwd)
        created.append(engine)
        return engine

    with mock.patch("agent_redis_bridge.bridge.build_engine", side_effect=factory):
        assert run(bridge, armed["lease_id"], request_id="run-1")["ok"] is True
        assert run(bridge, armed["lease_id"], request_id="run-2")["ok"] is True
    worktree_engines = [engine for engine in created if Path(engine.cwd).resolve() == worktree.resolve()]
    assert len(worktree_engines) == 2
    release_payload = {"operation": "worktree_release", "worktree_lease": armed["lease_id"]}
    bridge.handle_raw(request_json("release", payload=release_payload, recipient=bridge.agent_id))
    released = json.loads(bridge.redis.replies[-1][1])["payload"]
    assert released["ok"] is True
    assert not worktree.exists()
    assert bridge.worktree_lease_store.load(armed["lease_id"]).tombstone_reason == "released"
    assert len(bridge.redis.results) == 4  # arm, two runs, and release are all durable


def test_arm_has_no_engine_pool_cost_and_duplicate_id_is_idempotent_in_process(lease_env) -> None:
    repo, lease_root = lease_env
    bridge = make_lease_bridge(repo, lease_root)
    bridge.pool.acquire = mock.Mock(side_effect=AssertionError("arm must not reserve an engine"))  # type: ignore[method-assign]
    first = arm(bridge, "lease-duplicate", request_id="same-arm")
    bridge.handle_raw(request_json(
        "same-arm", payload={"operation": "worktree_arm", "worktree": {"name": "other"}},
        recipient=bridge.agent_id,
    ))
    assert first["ok"] is True
    assert len(bridge.worktree_lease_store.records()) == 1


def test_atomic_create_failure_leaves_no_visible_or_temporary_record(lease_env) -> None:
    repo, lease_root = lease_env
    bridge = make_lease_bridge(repo, lease_root)
    with mock.patch("agent_redis_bridge.worktree_lease.os.write", side_effect=OSError("torn write")):
        with pytest.raises(WorktreeLeaseError, match="worktree-lease-write-failed"):
            bridge.worktree_lease_store.create(
                sender="claude-project-c-dev", worktree_name="atomic",
                repo_identity=bridge.canonical_repo_identity(),
                base_oid=bridge.resolve_base_oid("HEAD"), ttl=7200,
            )
    assert list(bridge.worktree_lease_store.root.glob("*.json")) == []
    assert list(bridge.worktree_lease_store.root.glob("*.tmp")) == []


def test_corrupt_record_is_quarantined_and_does_not_block_arm_or_reconcile(lease_env) -> None:
    repo, lease_root = lease_env
    bridge = make_lease_bridge(repo, lease_root)
    bridge.worktree_lease_store.root.mkdir(parents=True, exist_ok=True)
    torn = bridge.worktree_lease_store.root / "torn.json"
    torn.write_bytes(b"")

    with mock.patch("agent_redis_bridge.worktree_lease.logger.error") as logged:
        armed = arm(bridge, "after-torn", request_id="arm-after-torn")

    assert armed["ok"] is True
    assert not torn.exists()
    assert list(bridge.worktree_lease_store.root.glob("torn*.quarantine"))
    assert logged.called
    assert bridge.reconcile_worktree_leases() == []


def test_direct_load_keeps_fail_loud_corrupt_record_semantics(lease_env) -> None:
    repo, lease_root = lease_env
    bridge = make_lease_bridge(repo, lease_root)
    corrupt = bridge.worktree_lease_store._path("known-lease")
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_bytes(b"")

    with pytest.raises(WorktreeLeaseError, match="worktree-lease-corrupt"):
        bridge.worktree_lease_store.load("known-lease")

    assert corrupt.exists()


def test_arm_rejects_untrusted_and_enforces_sender_quota(lease_env) -> None:
    repo, lease_root = lease_env
    bridge = make_lease_bridge(
        repo, lease_root, "--max-armed-worktrees", "2", "--max-armed-worktrees-per-sender", "1"
    )
    rejected = arm(bridge, "untrusted", request_id="arm-human", sender="human-project-c-dev")
    assert rejected["ok"] is False
    first = arm(bridge, "quota-a", request_id="arm-a")
    second = arm(bridge, "quota-b", request_id="arm-b")
    assert first["ok"] is True
    assert second["error"] == "worktree-armed-sender-quota"


def test_default_ttl_is_two_hours_and_override_is_capped(lease_env) -> None:
    repo, lease_root = lease_env
    bridge = make_lease_bridge(repo, lease_root, "--worktree-lease-ttl-max", "7300")
    before = time.time()
    defaulted = arm(bridge, "ttl-default", request_id="ttl-default")
    assert 7199 <= defaulted["expires_at"] - before <= 7205
    too_long = arm(bridge, "ttl-too-long", request_id="ttl-long", sender="other-project-c-dev", ttl=7301)
    assert too_long["error"] == "worktree-lease-ttl-invalid"


def test_lease_validation_owner_registration_name_and_spec_guards(lease_env) -> None:
    repo, lease_root = lease_env
    bridge = make_lease_bridge(repo, lease_root, "--no-enforce-completion")
    armed = arm(bridge, "validate-a")
    assert run(bridge, armed["lease_id"], sender="other-project-c-dev")["error"] == "worktree-lease-owner-mismatch"

    mixed = request_json(
        "mixed", recipient=bridge.agent_id,
        payload={
            "operation": "worktree_run", "worktree_lease": armed["lease_id"], "task": "x",
            "worktree": {"name": "override"},
        },
    )
    bridge.handle_raw(mixed)
    assert json.loads(bridge.redis.replies[-1][1])["payload"]["error"] == "worktree-spec-and-lease-incompatible"

    record = bridge.worktree_lease_store.load(armed["lease_id"])
    bridge.worktree_lease_store._replace(replace(record, worktree_name="../escape"))
    assert run(bridge, armed["lease_id"], request_id="bad-name")["error"] == "worktree-lease-corrupt"

    armed2 = arm(bridge, "validate-b", request_id="arm-2")
    bridge.remove_worktree(Path(armed2["path"]))
    assert run(bridge, armed2["lease_id"], request_id="missing")["error"] == "worktree-lease-unavailable"


def test_lease_run_requires_explicit_matching_operation(lease_env) -> None:
    repo, lease_root = lease_env
    bridge = make_lease_bridge(repo, lease_root, "--no-enforce-completion")
    armed = arm(bridge, "strict-operation")
    bridge.pool.acquire = mock.Mock(side_effect=AssertionError("invalid lease run must not acquire engine"))  # type: ignore[method-assign]

    for request_id, payload in (
        ("operation-omitted", {"worktree_lease": armed["lease_id"], "task": "x"}),
        (
            "operation-mismatch",
            {"operation": "something_else", "worktree_lease": armed["lease_id"], "task": "x"},
        ),
    ):
        bridge.handle_raw(request_json(request_id, payload=payload, recipient=bridge.agent_id))
        reply = json.loads(bridge.redis.replies[-1][1])["payload"]
        assert reply["error"] == "worktree-lease-requires-operation-worktree_run"


def test_busy_lease_rejected_and_non_resume_thread_guard(lease_env) -> None:
    repo, lease_root = lease_env
    bridge = make_lease_bridge(repo, lease_root, "--engine", "pi-sdk", "--no-enforce-completion")
    armed = arm(bridge, "busy-a")
    lock = bridge.worktree_lease_store.acquire(armed["lease_id"])
    try:
        bridge.pool.acquire = mock.Mock(return_value=mock.Mock())  # type: ignore[method-assign]
        assert run(bridge, armed["lease_id"], request_id="busy")["error"] == "worktree-lease-busy"
    finally:
        lock.release()
    assert run(bridge, armed["lease_id"], request_id="threaded", thread_id="thread-x")["error"] == "thread-affinity-worktree-incompatible"
    with mock.patch("agent_redis_bridge.bridge.build_engine", side_effect=lambda args, *, cwd: FakeEngine(cwd)):
        assert run(bridge, armed["lease_id"], request_id="stateless")["ok"] is True


def test_codex_resume_uses_leased_cwd(lease_env) -> None:
    repo, lease_root = lease_env
    bridge = make_lease_bridge(repo, lease_root, "--no-enforce-completion")
    armed = arm(bridge, "codex-resume")
    created: list[FakeEngine] = []

    def factory(args, *, cwd):
        engine = FakeEngine(cwd)
        created.append(engine)
        return engine

    with mock.patch("agent_redis_bridge.bridge.build_engine", side_effect=factory):
        payload = run(bridge, armed["lease_id"], thread_id="codex-thread")
    assert payload["ok"] is True
    worktree_engine = next(
        engine for engine in created if Path(engine.cwd).resolve() == Path(armed["path"]).resolve()
    )
    assert worktree_engine.resumed_threads == ["codex-thread"]


def test_agent_sdk_lease_must_agree_with_continuation_store(lease_env) -> None:
    repo, lease_root = lease_env
    bridge = make_lease_bridge(repo, lease_root, "--engine", "agent-sdk", "--no-enforce-completion")
    agreed = arm(bridge, "sdk-agree", request_id="arm-agree")
    mismatch = arm(bridge, "sdk-other", request_id="arm-other")
    bridge.agent_sdk_continuation_store().record(
        thread_id="sdk-thread", sender="claude-project-c-dev", worktree_name="sdk-agree"
    )
    with mock.patch("agent_redis_bridge.bridge.build_engine", side_effect=lambda args, *, cwd: FakeEngine(cwd)):
        assert run(bridge, agreed["lease_id"], request_id="agree", thread_id="sdk-thread")["ok"] is True
    assert run(bridge, mismatch["lease_id"], request_id="disagree", thread_id="sdk-thread")["error"] == "continuation-worktree-lease-mismatch"


def test_scored_plane_rejects_lease_before_lookup(lease_env) -> None:
    repo, lease_root = lease_env
    bridge = make_lease_bridge(repo, lease_root)
    raw = json.loads(request_json(
        "score", recipient=bridge.agent_id,
        payload={"operation": "worktree_run", "worktree_lease": "does-not-exist", "task": "x"},
    ))
    raw["run_id"] = "oi-pi-bakeoff-test"
    bridge.handle_raw(json.dumps(raw))
    assert json.loads(bridge.redis.replies[-1][1])["payload"]["error"] == "scored request rejects worktree leases"


def test_prover_runs_for_lease_and_escape_invalidates_it(lease_env) -> None:
    repo, lease_root = lease_env
    bridge = make_lease_bridge(repo, lease_root, "--no-auto-commit")
    armed = arm(bridge, "prove-lease")
    with (
        mock.patch("agent_redis_bridge.bridge.build_engine", side_effect=lambda args, *, cwd: FakeEngine(cwd)),
        mock.patch.object(bridge, "_verify_base_isolation", wraps=bridge._verify_base_isolation) as prover,
    ):
        run(bridge, armed["lease_id"], request_id="prove")
    prover.assert_called_once()

    escape = arm(bridge, "escape-lease", request_id="arm-escape")
    with mock.patch("agent_redis_bridge.bridge.build_engine", side_effect=lambda args, *, cwd: EscapingEngine(cwd)):
        payload = run(bridge, escape["lease_id"], request_id="escape")
    assert payload["ok"] is False
    assert payload["completion"]["state"] == "worktree_escape"
    assert bridge.worktree_lease_store.load(escape["lease_id"]).tombstone_reason == "worktree_escape"
    (Path(repo) / "escaped.txt").unlink()


def test_restart_reconcile_and_expiry_respect_active_lock(lease_env) -> None:
    repo, lease_root = lease_env
    first = make_lease_bridge(repo, lease_root)
    armed = arm(first, "restart-live")
    second = make_lease_bridge(repo, lease_root)
    assert second.reconcile_worktree_leases() == []
    assert second.worktree_lease_store.load(armed["lease_id"]).state == "armed"

    record = second.worktree_lease_store.load(armed["lease_id"])
    second.worktree_lease_store._replace(replace(record, expires_at=time.time() - 1))
    lock = second.worktree_lease_store.acquire(armed["lease_id"])
    try:
        assert second.reconcile_worktree_leases() == []
        assert Path(armed["path"]).exists()
    finally:
        lock.release()
    assert second.reconcile_worktree_leases() == [(armed["lease_id"], "expired")]
    assert not Path(armed["path"]).exists()

    missing = arm(second, "restart-missing", request_id="arm-missing")
    second.remove_worktree(Path(missing["path"]))
    third = make_lease_bridge(repo, lease_root)
    assert (missing["lease_id"], "missing-registration") in third.reconcile_worktree_leases()
    assert third.worktree_lease_store.load(missing["lease_id"]).tombstone_reason == "missing-registration"


def test_reconcile_garbage_collects_only_aged_tombstones(lease_env) -> None:
    repo, lease_root = lease_env
    bridge = make_lease_bridge(repo, lease_root)
    old = arm(bridge, "tombstone-old", request_id="arm-old")
    fresh = arm(bridge, "tombstone-fresh", request_id="arm-fresh")
    now = time.time()
    old_record = bridge.worktree_lease_store.load(old["lease_id"])
    fresh_record = bridge.worktree_lease_store.load(fresh["lease_id"])
    bridge.worktree_lease_store.tombstone(
        old_record, "test", now=now - bridge.args.result_ttl - 1
    )
    bridge.worktree_lease_store.tombstone(fresh_record, "test", now=now)

    actions = bridge.reconcile_worktree_leases()

    assert (old["lease_id"], "tombstone-gc") in actions
    assert bridge.worktree_lease_store.load(old["lease_id"]) is None
    assert bridge.worktree_lease_store.load(fresh["lease_id"]).state == "tombstoned"


def test_scored_process_preflight_structurally_releases_lease_lock(lease_env) -> None:
    repo, lease_root = lease_env
    bridge = make_lease_bridge(repo, lease_root)
    raw = json.loads(request_json("scored-direct", payload={"task": "x"}, recipient=bridge.agent_id))
    raw["run_id"] = "oi-pi-bakeoff-direct"
    envelope = Envelope.from_json(json.dumps(raw))
    record = mock.Mock()
    lease_lock = mock.Mock()
    bridge.pool.release = mock.Mock()  # type: ignore[method-assign]

    bridge.process_request(
        envelope, policy="trusted", engine=mock.Mock(),
        worktree_lease_record=record, worktree_lease_lock=lease_lock,
    )

    lease_lock.release.assert_called_once_with()
    bridge.pool.release.assert_called_once_with(envelope.id)


def test_git_op_helper_times_out_instead_of_hanging():
    # 2026-07-22: launchd-context git against the external volume wedged in
    # getcwd/open indefinitely; one hung child froze the whole seat inbox
    # loop (fable5 arm, glm/opus48 startup). Daemon-side git must be bounded.
    result = arb_bridge.run_git_op(["/bin/sleep", "30"], timeout=0.2)
    assert result.returncode != 0
    assert "timed out" in (result.stderr or "")


def test_resolve_base_oid_maps_timeout_to_lease_error(tmp_path):
    repo = init_git_repo()
    bridge = make_lease_bridge(repo, str(tmp_path / "leases"))
    with mock.patch.object(
        arb_bridge, "run_git_op",
        return_value=arb_bridge.subprocess.CompletedProcess(
            args=["git"], returncode=124, stdout="", stderr="git operation timed out after 60s"
        ),
    ):
        with pytest.raises(WorktreeLeaseError, match="worktree-lease-base-ref-invalid"):
            bridge.resolve_base_oid("HEAD")


def test_git_branch_returns_unknown_on_timeout(monkeypatch):
    monkeypatch.setattr(
        arb_bridge, "run_git_op",
        lambda *a, **k: arb_bridge.subprocess.CompletedProcess(
            args=["git"], returncode=124, stdout="", stderr="git operation timed out after 60s"
        ),
    )
    assert arb_bridge.git_branch(Path("/tmp")) == "unknown"


# ---------------------------------------------------------------------------
# Slice 1c: enabled-gate lifecycle scope
# ---------------------------------------------------------------------------


class _RaisingResolver:
    def seat_requires_claim_ref(self, seat_id):
        raise AssertionError("lifecycle control must not consult the claim store")

    def lease_lane(self, lease_id):
        raise AssertionError("lifecycle control must not consult the claim store")

    def claim(self, claim_ref):
        raise AssertionError("lifecycle control must not consult the claim store")


class _LaneResolver:
    def __init__(self, lane: str | None):
        self.lane = lane

    def seat_requires_claim_ref(self, seat_id):
        return True

    def lease_lane(self, lease_id):
        return self.lane

    def claim(self, claim_ref):
        return None


def test_enabled_gate_arm_succeeds_without_consulting_resolver(lease_env) -> None:
    repo, lease_root = lease_env
    bridge = make_lease_bridge(repo, lease_root, "--no-enforce-completion")
    bridge.claim_gate_enabled = True
    bridge.claim_resolver = _RaisingResolver()
    armed = arm(bridge, "gate-arm", request_id="gate-arm")
    assert armed["ok"] is True


def test_enabled_gate_exempt_worktree_run_reaches_engine(lease_env) -> None:
    repo, lease_root = lease_env
    bridge = make_lease_bridge(repo, lease_root, "--no-enforce-completion")
    bridge.claim_gate_enabled = True
    armed = arm(bridge, "gate-exempt-run", request_id="gate-ex-arm")
    assert armed["ok"] is True
    bridge.claim_resolver = _LaneResolver("exempt")
    created: list[FakeEngine] = []

    def factory(args, *, cwd):
        engine = FakeEngine(cwd)
        created.append(engine)
        return engine

    with mock.patch("agent_redis_bridge.bridge.build_engine", side_effect=factory):
        result = run(bridge, armed["lease_id"], request_id="gate-ex-run")
    assert result["ok"] is True
    assert created, "exempt worktree_run must reach engine work"


def test_enabled_gate_gated_worktree_run_refused_before_engine(lease_env) -> None:
    repo, lease_root = lease_env
    bridge = make_lease_bridge(repo, lease_root, "--no-enforce-completion")
    bridge.claim_gate_enabled = True
    armed = arm(bridge, "gate-gated-run", request_id="gate-g-arm")
    assert armed["ok"] is True
    bridge.claim_resolver = _LaneResolver("gated")
    bridge.pool.acquire = mock.Mock(
        side_effect=AssertionError("gated worktree_run must not reserve an engine")
    )  # type: ignore[method-assign]
    result = run(bridge, armed["lease_id"], request_id="gate-g-run")
    assert result["ok"] is False
    assert result["error"].split(":", 1)[0] == "missing_claim_ref"


def test_enabled_gate_release_succeeds_without_resolver_for_both_lanes(lease_env) -> None:
    repo, lease_root = lease_env
    bridge = make_lease_bridge(repo, lease_root, "--no-enforce-completion")
    for lane_label in ("gated", "exempt"):
        armed = arm(bridge, f"rel-{lane_label}", request_id=f"arm-rel-{lane_label}")
        assert armed["ok"] is True
        bridge.claim_gate_enabled = True
        bridge.claim_resolver = _RaisingResolver()
        bridge.handle_raw(
            request_json(
                f"rel-{lane_label}",
                payload={
                    "operation": "worktree_release",
                    "worktree_lease": armed["lease_id"],
                },
                recipient=bridge.agent_id,
            )
        )
        released = json.loads(bridge.redis.replies[-1][1])["payload"]
        assert released["ok"] is True, lane_label


def test_lifecycle_shaped_payload_with_extra_task_never_reaches_engine(lease_env) -> None:
    """Closed schema is the boundary; classification is not a general bypass."""
    repo, lease_root = lease_env
    bridge = make_lease_bridge(repo, lease_root, "--no-enforce-completion")
    bridge.claim_gate_enabled = True
    bridge.claim_resolver = _RaisingResolver()
    bridge.pool.acquire = mock.Mock(
        side_effect=AssertionError("invalid arm schema must not start engine")
    )  # type: ignore[method-assign]
    bridge.handle_raw(
        request_json(
            "bad-arm",
            payload={
                "operation": "worktree_arm",
                "task": "smuggle",
                "claim_ref": "c-1",
                "worktree": {"name": "smuggle", "base_ref": "HEAD", "cleanup": "keep"},
            },
            recipient=bridge.agent_id,
        )
    )
    reply = json.loads(bridge.redis.replies[-1][1])["payload"]
    assert reply["ok"] is False
    assert "worktree-arm-invalid-schema" in reply["error"]


# ---------------------------------------------------------------------------
# Slice 1d-ii: locked two-record arm/release/reconcile + crash replay
# ---------------------------------------------------------------------------


def test_record_defaults_lane_gated_and_arm_request_id_none() -> None:
    record = WorktreeLeaseRecord(
        lease_id="L1",
        sender="s",
        worktree_name="n",
        repo_identity="/repo",
        base_oid="abc",
        created_at=1.0,
        expires_at=2.0,
    )
    assert record.lane == "gated"
    assert record.arm_request_id is None


def test_mint_lease_id_is_split_from_create(lease_env) -> None:
    repo, lease_root = lease_env
    bridge = make_lease_bridge(repo, lease_root)
    store = bridge.worktree_lease_store
    lease_id = store.mint_lease_id()
    assert isinstance(lease_id, str) and len(lease_id) >= 40
    record = store.create(
        lease_id=lease_id,
        sender="claude-project-c-dev",
        worktree_name="minted",
        repo_identity=bridge.canonical_repo_identity(),
        base_oid=bridge.resolve_base_oid("HEAD"),
        ttl=7200,
        lane="exempt",
        arm_request_id="req-mint-1",
    )
    assert record.lease_id == lease_id
    assert record.lane == "exempt"
    assert record.arm_request_id == "req-mint-1"
    loaded = store.load(lease_id)
    assert loaded is not None
    assert loaded.lane == "exempt"
    assert loaded.arm_request_id == "req-mint-1"


def test_arm_call_order_mint_lock_worktree_fs_row_result_reply(lease_env) -> None:
    repo, lease_root = lease_env
    bridge = make_writer_lease_bridge(repo, lease_root)
    writer: FakeLaneWriter = bridge.lane_writer  # type: ignore[assignment]
    events: list[str] = []

    real_mint = bridge.worktree_lease_store.mint_lease_id
    real_acquire = bridge.worktree_lease_store.acquire
    real_create_wt = bridge.create_worktree
    real_create_fs = bridge.worktree_lease_store.create
    real_arm = writer.arm
    real_op = bridge._operation_result

    def mint():
        events.append("mint")
        return real_mint()

    def acquire(lease_id: str):
        events.append(f"lock:{lease_id[:8]}")
        lock = real_acquire(lease_id)
        orig_release = lock.release

        def tracked_release():
            events.append("unlock")
            return orig_release()

        lock.release = tracked_release  # type: ignore[method-assign]
        return lock

    def create_wt(spec):
        events.append("worktree")
        return real_create_wt(spec)

    def create_fs(**kwargs):
        events.append("filesystem")
        return real_create_fs(**kwargs)

    def arm_row(lease_id: str):
        events.append("row-arm")
        return real_arm(lease_id)

    def op_result(envelope, result, *, fields=None):
        events.append("result-reply")
        return real_op(envelope, result, fields=fields)

    bridge.worktree_lease_store.mint_lease_id = mint  # type: ignore[method-assign]
    bridge.worktree_lease_store.acquire = acquire  # type: ignore[method-assign]
    bridge.create_worktree = create_wt  # type: ignore[method-assign]
    bridge.worktree_lease_store.create = create_fs  # type: ignore[method-assign]
    writer.arm = arm_row  # type: ignore[method-assign]
    bridge._operation_result = op_result  # type: ignore[method-assign]

    armed = arm(bridge, "order-a", request_id="order-req")
    assert armed["ok"] is True
    # mint → lock → worktree → filesystem → row → result/reply → unlock
    assert events[0] == "mint"
    assert events[1].startswith("lock:")
    assert events[2:6] == ["worktree", "filesystem", "row-arm", "result-reply"]
    assert events[-1] == "unlock"
    row = writer.rows()[0]
    assert row["lease_id"] == armed["lease_id"]
    assert row["armed_by"] == bridge.agent_id
    assert row["lane"] == "gated"
    record = bridge.worktree_lease_store.load(armed["lease_id"])
    assert record is not None
    assert record.arm_request_id == "order-req"
    assert record.lane == "gated"


def test_filesystem_create_failure_never_attempts_row_write(lease_env) -> None:
    repo, lease_root = lease_env
    bridge = make_writer_lease_bridge(repo, lease_root)
    writer: FakeLaneWriter = bridge.lane_writer  # type: ignore[assignment]
    with mock.patch.object(
        bridge.worktree_lease_store,
        "create",
        side_effect=WorktreeLeaseError("worktree-lease-write-failed"),
    ):
        reply = arm(bridge, "fs-fail", request_id="fs-fail")
    assert reply["ok"] is False
    assert reply["error"] == "worktree-lease-write-failed"
    assert writer.arm_calls == []
    assert writer.rows() == []


def test_row_insert_failure_reclaims_and_tombstones_lane_row_arm_failed(lease_env) -> None:
    repo, lease_root = lease_env
    writer = FakeLaneWriter(arm_error=LaneStoreUnreachable("insert failed"))
    bridge = make_writer_lease_bridge(repo, lease_root, writer)
    reply = arm(bridge, "row-fail", request_id="row-fail")
    assert reply["ok"] is False
    assert reply["error"] == "worktree-lane-arm-store-failed"
    assert writer.arm_calls  # attempted
    assert writer.rows() == []
    records = bridge.worktree_lease_store.records()
    assert len(records) == 1
    assert records[0].state == "tombstoned"
    assert records[0].tombstone_reason == "lane-row-arm-failed"
    assert not bridge.is_registered_worktree(
        bridge.worktree_path(records[0].worktree_name)
    )


def test_row_insert_and_compensation_failure_returns_composite_error(lease_env) -> None:
    """P1-C: composite refusal + durable tombstone + use-site refusal.

    Injecting only reclaim failure must still tombstone (tombstone→no-op RED).
    A surviving half-armed pair (reclaim+tombstone both fail) must not be served
    (validation-gap mutation RED).
    """
    repo, lease_root = lease_env
    writer = FakeLaneWriter(arm_error=LaneStoreUnreachable("insert failed"))
    bridge = make_writer_lease_bridge(repo, lease_root, writer)

    def reclaim_boom(record):
        raise WorktreeLeaseError("worktree-lease-reclaim-failed")

    bridge.reclaim_leased_worktree = reclaim_boom  # type: ignore[method-assign]
    reply = arm(bridge, "comp-fail", request_id="comp-fail")
    assert reply["ok"] is False
    assert "worktree-lane-arm-store-failed" in reply["error"]
    assert "compensation" in reply["error"] or "reclaim" in reply["error"]
    # Must never look like success
    assert reply.get("lease_id") is None or reply["ok"] is False

    # Durable post-condition: reclaim failed but tombstone still ran.
    # Mutating WorktreeLeaseStore.tombstone to a no-op must RED this block.
    records = bridge.worktree_lease_store.records()
    assert len(records) == 1
    assert records[0].state == "tombstoned"
    assert records[0].tombstone_reason == "lane-row-arm-failed"
    assert writer.rows() == []

    # Use-site: tombstoned / row-less pair is not servable.
    lease_id = records[0].lease_id
    with mock.patch(
        "agent_redis_bridge.bridge.build_engine",
        side_effect=lambda args, *, cwd: FakeEngine(cwd),
    ):
        run_reply = run(bridge, lease_id, request_id="run-after-comp-fail")
    assert run_reply["ok"] is False
    assert run_reply["error"] in {
        "worktree-lease-unavailable",
        "worktree-lane-row-missing",
    }


def test_total_compensation_failure_half_armed_run_refuses(lease_env) -> None:
    """P1-B/C: when reclaim AND tombstone both fail, FS stays armed with no row.

    worktree_run must refuse with worktree-lane-row-missing (not serve). Deleting
    the lane-row check from validate_worktree_lease must RED this test.
    """
    repo, lease_root = lease_env
    writer = FakeLaneWriter(arm_error=LaneStoreUnreachable("insert failed"))
    bridge = make_writer_lease_bridge(repo, lease_root, writer)

    def reclaim_boom(record):
        raise WorktreeLeaseError("injected-reclaim-failed")

    bridge.reclaim_leased_worktree = reclaim_boom  # type: ignore[method-assign]
    with mock.patch.object(
        bridge.worktree_lease_store,
        "tombstone",
        side_effect=WorktreeLeaseError("injected-tombstone-failed"),
    ):
        reply = arm(bridge, "half-armed", request_id="half-armed-req")

    assert reply["ok"] is False
    assert "worktree-lane-arm-store-failed" in reply["error"]
    assert "compensation-failed" in reply["error"]
    assert "reclaim" in reply["error"]
    assert "tombstone" in reply["error"]
    assert reply.get("lease_id") is None or reply["ok"] is False

    records = bridge.worktree_lease_store.records()
    assert len(records) == 1
    record = records[0]
    assert record.state == "armed"
    assert writer.rows() == []
    assert bridge.is_registered_worktree(bridge.worktree_path(record.worktree_name))

    with mock.patch(
        "agent_redis_bridge.bridge.build_engine",
        side_effect=lambda args, *, cwd: FakeEngine(cwd),
    ):
        run_reply = run(bridge, record.lease_id, request_id="run-half-armed")
    assert run_reply["ok"] is False
    assert run_reply["error"] == "worktree-lane-row-missing"


def test_arm_store_unreachable_replies_coded_and_allows_post_recovery_redelivery(
    lease_env,
) -> None:
    """P1-A: store outage on arm path replies a specific code; redelivery after
    recovery completes (not silenced by is_duplicate / durable failure).
    """
    repo, lease_root = lease_env
    bridge = make_writer_lease_bridge(repo, lease_root)

    class BoomWriter(FakeLaneWriter):
        def rows(self):
            raise LaneStoreUnreachable("store down")

    bridge.lane_writer = BoomWriter(consumer_id=bridge.agent_id, lane="gated")  # type: ignore[assignment]

    n_replies_before = len(bridge.redis.replies)
    bridge.handle_raw(
        request_json(
            "store-out-req",
            payload={
                "operation": "worktree_arm",
                "worktree": {
                    "name": "store-out",
                    "base_ref": "HEAD",
                    "cleanup": "keep",
                },
            },
            recipient=bridge.agent_id,
        )
    )
    assert len(bridge.redis.replies) == n_replies_before + 1, (
        "store outage must produce a coded client reply, not silent timeout"
    )
    first = json.loads(bridge.redis.replies[-1][1])["payload"]
    assert first["ok"] is False
    assert first["error"] == "worktree-lane-store-unreachable"

    # Must NOT mark the envelope durably-processed — redelivery after recovery
    # must be able to complete the arm, not replay a durable failure / drop.
    result_key = bridge.redis_config.task_result_key("store-out-req")
    assert bridge.redis.get_str(result_key) is None

    # Store recovers: same envelope id must arm successfully.
    bridge.lane_writer = FakeLaneWriter(consumer_id=bridge.agent_id, lane="gated")  # type: ignore[assignment]
    n_replies_mid = len(bridge.redis.replies)
    bridge.handle_raw(
        request_json(
            "store-out-req",
            payload={
                "operation": "worktree_arm",
                "worktree": {
                    "name": "store-out",
                    "base_ref": "HEAD",
                    "cleanup": "keep",
                },
            },
            recipient=bridge.agent_id,
        )
    )
    assert len(bridge.redis.replies) == n_replies_mid + 1, (
        "post-recovery redelivery must not be silenced by is_duplicate"
    )
    second = json.loads(bridge.redis.replies[-1][1])["payload"]
    assert second["ok"] is True, second
    assert second.get("lease_id")
    assert Path(second["path"]).exists()


def test_release_reclaim_failure_leaves_row_and_refuses(lease_env) -> None:
    repo, lease_root = lease_env
    bridge = make_writer_lease_bridge(repo, lease_root)
    armed = arm(bridge, "rel-reclaim-fail", request_id="arm-rrf")
    assert armed["ok"] is True
    writer: FakeLaneWriter = bridge.lane_writer  # type: ignore[assignment]
    assert writer.rows()

    def reclaim_boom(record):
        raise WorktreeLeaseError("worktree-lease-reclaim-failed")

    bridge.reclaim_leased_worktree = reclaim_boom  # type: ignore[method-assign]
    bridge.handle_raw(
        request_json(
            "rel-rrf",
            payload={"operation": "worktree_release", "worktree_lease": armed["lease_id"]},
            recipient=bridge.agent_id,
        )
    )
    released = json.loads(bridge.redis.replies[-1][1])["payload"]
    assert released["ok"] is False
    assert released["error"] == "worktree-lease-reclaim-failed"
    assert any(r["lease_id"] == armed["lease_id"] for r in writer.rows())
    assert bridge.worktree_lease_store.load(armed["lease_id"]).state == "armed"


def test_release_row_delete_failure_leaves_lease_unavailable(lease_env) -> None:
    repo, lease_root = lease_env
    writer = FakeLaneWriter(retire_error=LaneStoreUnreachable("delete failed"))
    bridge = make_writer_lease_bridge(repo, lease_root, writer)
    armed = arm(bridge, "rel-row-fail", request_id="arm-rrf2")
    assert armed["ok"] is True
    path = Path(armed["path"])
    assert path.exists()

    n_replies_before = len(bridge.redis.replies)
    bridge.handle_raw(
        request_json(
            "rel-row-fail",
            payload={"operation": "worktree_release", "worktree_lease": armed["lease_id"]},
            recipient=bridge.agent_id,
        )
    )
    release_replies = [
        json.loads(body)["payload"]
        for _, body in bridge.redis.replies[n_replies_before:]
        if json.loads(body).get("in_reply_to") == "rel-row-fail"
        or True  # FakeRedis replies are only for this handle when isolated
    ]
    # Must not emit an intermediate success (mutation: reply-before-retire).
    assert all(r.get("ok") is not True for r in release_replies[-2:]), release_replies
    released = json.loads(bridge.redis.replies[-1][1])["payload"]
    assert released["ok"] is False
    assert released["error"] == "worktree-lane-release-store-failed"
    assert not any(r.get("ok") is True for r in release_replies)
    # Filesystem lease already reclaimed/tombstoned — unavailable for execution
    record = bridge.worktree_lease_store.load(armed["lease_id"])
    assert record is not None
    assert record.state == "tombstoned"
    assert not path.exists()
    # Row remains for reconcile
    assert any(r["lease_id"] == armed["lease_id"] for r in writer.rows())


def test_release_false_retire_is_named_mismatch_not_success(lease_env) -> None:
    repo, lease_root = lease_env
    writer = FakeLaneWriter(retire_returns=False)
    bridge = make_writer_lease_bridge(repo, lease_root, writer)
    armed = arm(bridge, "rel-false", request_id="arm-false")
    assert armed["ok"] is True
    # Keep a synthetic row so false retire is meaningful
    writer._rows[armed["lease_id"]] = {
        "lease_id": armed["lease_id"],
        "lane": "gated",
        "armed_by": bridge.agent_id,
        "armed_at": time.time(),
    }
    n_replies_before = len(bridge.redis.replies)
    bridge.handle_raw(
        request_json(
            "rel-false",
            payload={"operation": "worktree_release", "worktree_lease": armed["lease_id"]},
            recipient=bridge.agent_id,
        )
    )
    release_replies = [
        json.loads(body)["payload"] for _, body in bridge.redis.replies[n_replies_before:]
    ]
    assert not any(r.get("ok") is True for r in release_replies), release_replies
    released = json.loads(bridge.redis.replies[-1][1])["payload"]
    assert released["ok"] is False
    assert released["error"] in {
        "worktree-lane-release-store-failed",
        "worktree-lane-release-mismatch",
    }


def test_reconcile_armed_record_missing_row_reclaims(lease_env) -> None:
    repo, lease_root = lease_env
    bridge = make_writer_lease_bridge(repo, lease_root)
    armed = arm(bridge, "recon-missing-row", request_id="arm-mmr")
    writer: FakeLaneWriter = bridge.lane_writer  # type: ignore[assignment]
    writer._rows.clear()  # row vanished
    assert Path(armed["path"]).exists()
    actions = bridge.reconcile_worktree_leases()
    assert any(a[0] == armed["lease_id"] for a in actions)
    record = bridge.worktree_lease_store.load(armed["lease_id"])
    assert record is not None
    assert record.state == "tombstoned"
    assert not Path(armed["path"]).exists()


def test_reconcile_skips_missing_row_while_lease_lock_held(lease_env) -> None:
    repo, lease_root = lease_env
    bridge = make_writer_lease_bridge(repo, lease_root)
    armed = arm(bridge, "recon-busy", request_id="arm-busy")
    writer: FakeLaneWriter = bridge.lane_writer  # type: ignore[assignment]
    writer._rows.clear()
    lock = bridge.worktree_lease_store.acquire(armed["lease_id"])
    try:
        actions = bridge.reconcile_worktree_leases()
        assert not any(a[0] == armed["lease_id"] for a in actions)
        assert bridge.worktree_lease_store.load(armed["lease_id"]).state == "armed"
        assert Path(armed["path"]).exists()
    finally:
        lock.release()
    # After unlock, reconcile reclaims
    actions = bridge.reconcile_worktree_leases()
    assert any(a[0] == armed["lease_id"] for a in actions)
    assert bridge.worktree_lease_store.load(armed["lease_id"]).state == "tombstoned"


def test_reconcile_deletes_orphan_row_without_armed_record(lease_env) -> None:
    repo, lease_root = lease_env
    bridge = make_writer_lease_bridge(repo, lease_root)
    writer: FakeLaneWriter = bridge.lane_writer  # type: ignore[assignment]
    writer._rows["orphan-lease"] = {
        "lease_id": "orphan-lease",
        "lane": "gated",
        "armed_by": bridge.agent_id,
        "armed_at": time.time(),
    }
    actions = bridge.reconcile_worktree_leases()
    assert any(a[0] == "orphan-lease" for a in actions)
    assert writer.rows() == []


def test_reconcile_lane_mismatch_makes_lease_unavailable(lease_env) -> None:
    repo, lease_root = lease_env
    bridge = make_writer_lease_bridge(repo, lease_root, lane="gated")
    armed = arm(bridge, "lane-mis", request_id="arm-mis")
    writer: FakeLaneWriter = bridge.lane_writer  # type: ignore[assignment]
    writer._rows[armed["lease_id"]]["lane"] = "exempt"  # mismatch
    actions = bridge.reconcile_worktree_leases()
    assert any(a[0] == armed["lease_id"] for a in actions)
    record = bridge.worktree_lease_store.load(armed["lease_id"])
    assert record is not None
    assert record.state == "tombstoned"
    assert not Path(armed["path"]).exists()


def test_reconcile_store_unavailable_refuses_never_defaults_lane(lease_env) -> None:
    repo, lease_root = lease_env
    bridge = make_writer_lease_bridge(repo, lease_root)
    armed = arm(bridge, "store-down", request_id="arm-sd")
    assert armed["ok"] is True

    class BoomWriter(FakeLaneWriter):
        def rows(self):
            raise LaneStoreUnreachable("store down")

    bridge.lane_writer = BoomWriter(consumer_id=bridge.agent_id)  # type: ignore[assignment]
    with pytest.raises(LaneStoreUnreachable, match="store down"):
        bridge.reconcile_worktree_leases()


def test_adversarial_heartbeat_reconcile_during_fs_to_row_window(lease_env) -> None:
    """Stage exit: real reconcile between FS create and row INSERT cannot reclaim.

    Barriers (not sleeps): pause after durable FS create and before the row
    write completes; arm must still hold the per-lease lock across that window
    so heartbeat reconcile observes busy and takes no action. Arm then either
    completes both records and success or fails closed — never success on a
    reclaimed tree.
    """
    repo, lease_root = lease_env
    bridge = make_writer_lease_bridge(repo, lease_root)
    writer: FakeLaneWriter = bridge.lane_writer  # type: ignore[assignment]

    fs_ready = threading.Event()
    continue_arm = threading.Event()
    arm_outcome: dict = {}

    # Barrier at the entry to row arm: FS create has returned (durable), row
    # not yet written. Lock must still be held here; unlock-after-create RED.
    real_row_arm = writer.arm

    def row_arm_barrier(lease_id: str):
        fs_ready.set()
        assert continue_arm.wait(timeout=5), "arm not resumed after reconcile"
        return real_row_arm(lease_id)

    writer.arm = row_arm_barrier  # type: ignore[method-assign]

    def run_arm():
        try:
            arm_outcome["reply"] = arm(bridge, "race-arm", request_id="race-req-1")
        except Exception as exc:  # noqa: BLE001 - surface into main thread
            arm_outcome["exc"] = exc

    thread = threading.Thread(target=run_arm, name="arm-race")
    thread.start()
    assert fs_ready.wait(timeout=5), "filesystem create never reached"

    # Same path heartbeat_loop uses (bridge.py ~963-965).
    actions = bridge.reconcile_worktree_leases()
    # Must not reclaim/tombstone the in-flight lease.
    for lease_id, action in actions:
        assert action not in {
            "lane-row-missing",
            "missing-registration",
            "lane-mismatch",
            "expired",
        }, (lease_id, action)

    # FS record exists and is still armed; worktree still present; no row yet.
    records = [
        r for r in bridge.worktree_lease_store.records() if r.state == "armed"
    ]
    assert len(records) == 1
    assert bridge.is_registered_worktree(
        bridge.worktree_path(records[0].worktree_name)
    )
    assert writer.rows() == []

    continue_arm.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert "exc" not in arm_outcome, arm_outcome.get("exc")
    reply = arm_outcome["reply"]
    assert reply["ok"] is True, reply
    assert Path(reply["path"]).exists()
    assert bridge.worktree_lease_store.load(reply["lease_id"]).state == "armed"
    assert any(r["lease_id"] == reply["lease_id"] for r in writer.rows())


def test_adversarial_reconcile_at_row_arm_barrier_still_succeeds_or_fails_closed(
    lease_env,
) -> None:
    repo, lease_root = lease_env
    bridge = make_writer_lease_bridge(repo, lease_root)
    writer: FakeLaneWriter = bridge.lane_writer  # type: ignore[assignment]

    row_ready = threading.Event()
    continue_arm = threading.Event()
    arm_outcome: dict = {}

    real_arm = writer.arm

    def arm_and_barrier(lease_id: str):
        row = real_arm(lease_id)
        row_ready.set()
        assert continue_arm.wait(timeout=5)
        return row

    writer.arm = arm_and_barrier  # type: ignore[method-assign]

    def run_arm():
        try:
            arm_outcome["reply"] = arm(bridge, "race-row", request_id="race-row-1")
        except Exception as exc:  # noqa: BLE001
            arm_outcome["exc"] = exc

    thread = threading.Thread(target=run_arm)
    thread.start()
    assert row_ready.wait(timeout=5)
    actions = bridge.reconcile_worktree_leases()
    for _, action in actions:
        assert action not in {"lane-row-missing", "missing-registration", "expired"}
    continue_arm.set()
    thread.join(timeout=10)
    reply = arm_outcome.get("reply")
    assert reply is not None and reply["ok"] is True
    assert Path(reply["path"]).exists()
    assert any(r["lease_id"] == reply["lease_id"] for r in writer.rows())


def test_crash_replay_matrix_single_mint_for_fixed_envelope_id(lease_env) -> None:
    """Crash after each durability point; replay same envelope id → one mint."""
    repo, lease_root = lease_env
    bridge = make_writer_lease_bridge(repo, lease_root)
    writer: FakeLaneWriter = bridge.lane_writer  # type: ignore[assignment]
    request_id = "crash-replay-fixed"

    mint_ids: list[str] = []
    real_mint = bridge.worktree_lease_store.mint_lease_id

    def counting_mint():
        lease_id = real_mint()
        mint_ids.append(lease_id)
        return lease_id

    bridge.worktree_lease_store.mint_lease_id = counting_mint  # type: ignore[method-assign]

    # --- Crash after filesystem create (before row arm): leave partial FS record ---
    # Inject AFTER create returns (not inside create): the create try/except would
    # otherwise reclaim the worktree and make the partial look like missing-registration.
    partial_from_crash: list[WorktreeLeaseRecord] = []
    real_compensate = bridge._arm_lane_row_or_compensate

    def crash_after_fs_before_row(record: WorktreeLeaseRecord):
        partial_from_crash.append(record)
        raise RuntimeError("injected-crash-after-fs")

    bridge._arm_lane_row_or_compensate = crash_after_fs_before_row  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="injected-crash-after-fs"):
        raw = request_json(
            request_id,
            payload={
                "operation": "worktree_arm",
                "worktree": {"name": "crash-fs", "base_ref": "HEAD", "cleanup": "keep"},
            },
            recipient=bridge.agent_id,
        )
        bridge.handle_raw(raw)
    bridge._arm_lane_row_or_compensate = real_compensate  # type: ignore[method-assign]
    assert partial_from_crash, "crash must leave a durable FS record"
    assert partial_from_crash[0].arm_request_id == request_id
    assert partial_from_crash[0].state == "armed"
    assert bridge.is_registered_worktree(
        bridge.worktree_path(partial_from_crash[0].worktree_name)
    )
    # No row was written (crash before arm).
    assert writer.rows() == []

    # Replay same envelope: armed FS + missing row → closed refusal, no new mint.
    bridge.seen_request_ids.clear()
    bridge.redis.results.clear()
    mints_after_crash = len(mint_ids)
    refused_partial = arm(bridge, "crash-fs-replay", request_id=request_id)
    assert refused_partial["ok"] is False
    assert refused_partial["error"] == "worktree-lane-arm-store-failed"
    assert len(mint_ids) == mints_after_crash  # no replacement mint

    # Durable the refusal and prove result-level replay (crash after result write).
    bridge.seen_request_ids.clear()
    refused_again = arm(bridge, "crash-fs-replay-2", request_id=request_id)
    assert refused_again["ok"] is False
    assert refused_again["error"] == "worktree-lane-arm-store-failed"
    assert len(mint_ids) == mints_after_crash

    # Clean up the partial so a fresh arm can succeed under a *new* request id.
    for rec in list(bridge.worktree_lease_store.records()):
        if rec.state == "armed":
            try:
                bridge.reclaim_leased_worktree(rec)
            except Exception:
                pass
            bridge.worktree_lease_store.tombstone(rec, "test-cleanup")
        writer._rows.pop(rec.lease_id, None)

    # --- Success path: one mint; durable result replay returns same fields ---
    success_id = "crash-replay-success"
    mint_before_success = len(mint_ids)
    first = arm(bridge, "crash-ok", request_id=success_id)
    assert first["ok"] is True
    assert len(mint_ids) == mint_before_success + 1
    first_lease = first["lease_id"]
    first_path = first["path"]
    first_expires = first["expires_at"]
    first_base = first["base_oid"]

    bridge.seen_request_ids.clear()
    second = arm(bridge, "crash-ok-ignored-name", request_id=success_id)
    assert second["ok"] is True
    assert second["lease_id"] == first_lease
    assert second["path"] == first_path
    assert second["expires_at"] == first_expires
    assert second["base_oid"] == first_base
    assert len(mint_ids) == mint_before_success + 1  # no second mint

    # Tombstoned partial for another request id → same closed refusal, no mint.
    partial_id = "crash-partial-refuse"
    partial_lease = bridge.worktree_lease_store.mint_lease_id()
    lock = bridge.worktree_lease_store.acquire(partial_lease)
    try:
        bridge.create_worktree(
            {"name": "partial-ref", "base_ref": first_base, "cleanup": "keep"}
        )
        rec = bridge.worktree_lease_store.create(
            lease_id=partial_lease,
            sender="claude-project-c-dev",
            worktree_name="partial-ref",
            repo_identity=bridge.canonical_repo_identity(),
            base_oid=first_base,
            ttl=7200,
            lane="gated",
            arm_request_id=partial_id,
        )
        bridge.reclaim_leased_worktree(rec)
        bridge.worktree_lease_store.tombstone(rec, "lane-row-arm-failed")
    finally:
        lock.release()

    bridge.seen_request_ids.clear()
    bridge.redis.results.clear()
    mints_before = len(mint_ids)
    refused = arm(bridge, "partial-name", request_id=partial_id)
    assert refused["ok"] is False
    assert refused["error"] == "worktree-lane-arm-store-failed"
    bridge.seen_request_ids.clear()
    bridge.redis.results.clear()
    refused2 = arm(bridge, "partial-name-2", request_id=partial_id)
    assert refused2["ok"] is False
    assert refused2["error"] == "worktree-lane-arm-store-failed"
    assert len(mint_ids) == mints_before

    # New envelope id may create a new lease
    third = arm(bridge, "new-env", request_id="brand-new-envelope")
    assert third["ok"] is True
    assert third["lease_id"] != first_lease


def test_arm_without_lane_writer_preserves_filesystem_only_lifecycle(lease_env) -> None:
    """Gate-off / no writer DSN: existing FS-only arm still works (rollout posture)."""
    repo, lease_root = lease_env
    bridge = make_lease_bridge(repo, lease_root)
    assert bridge.lane_writer is None
    armed = arm(bridge, "legacy-fs", request_id="legacy-fs")
    assert armed["ok"] is True
    assert bridge.worktree_lease_store.load(armed["lease_id"]).state == "armed"


# ---------------------------------------------------------------------------
# Slice 1d-iii: exempt push-deny at arm (before FS lease / row writer)
# ---------------------------------------------------------------------------


def test_gated_arm_does_not_invoke_exempt_proof(lease_env) -> None:
    repo, lease_root = lease_env
    bridge = make_writer_lease_bridge(repo, lease_root, lane="gated")
    with mock.patch.object(
        bridge, "_prepare_exempt_worktree_or_raise", wraps=bridge._prepare_exempt_worktree_or_raise
    ) as prep:
        armed = arm(bridge, "gated-no-exempt", request_id="gated-no-exempt")
    assert armed["ok"] is True
    prep.assert_called_once()
    # Gated path must not raise and must still publish FS + row.
    assert bridge.lane_writer.arm_calls  # type: ignore[union-attr]


def test_exempt_arm_runs_proof_before_filesystem_and_row(lease_env) -> None:
    repo, lease_root = lease_env
    bridge = make_writer_lease_bridge(repo, lease_root, lane="exempt")
    writer: FakeLaneWriter = bridge.lane_writer  # type: ignore[assignment]
    events: list[str] = []

    real_create_wt = bridge.create_worktree
    real_create_fs = bridge.worktree_lease_store.create
    real_arm = writer.arm

    def create_wt(spec):
        events.append("worktree")
        return real_create_wt(spec)

    def prep(path):
        events.append("exempt-proof")
        return None

    def create_fs(**kwargs):
        events.append("filesystem")
        return real_create_fs(**kwargs)

    def arm_row(lease_id: str):
        events.append("row-arm")
        return real_arm(lease_id)

    bridge.create_worktree = create_wt  # type: ignore[method-assign]
    bridge._prepare_exempt_worktree_or_raise = prep  # type: ignore[method-assign]
    bridge.worktree_lease_store.create = create_fs  # type: ignore[method-assign]
    writer.arm = arm_row  # type: ignore[method-assign]

    armed = arm(bridge, "exempt-order", request_id="exempt-order")
    assert armed["ok"] is True
    assert events == ["worktree", "exempt-proof", "filesystem", "row-arm"]
    assert writer.rows()[0]["lane"] == "exempt"


def test_exempt_proof_failure_removes_worktree_and_never_writes_row(lease_env) -> None:
    from agent_redis_bridge.worktree_lease import WorktreeLeaseError
    from agent_redis_bridge.exempt_git import (
        EXEMPT_PUSH_CREDENTIAL_WRITABLE,
        ExemptGitError,
    )

    repo, lease_root = lease_env
    bridge = make_writer_lease_bridge(repo, lease_root, lane="exempt")
    writer: FakeLaneWriter = bridge.lane_writer  # type: ignore[assignment]

    def failing_prep(path):
        # Mirror production: ExemptGitError → WorktreeLeaseError(code)
        raise WorktreeLeaseError(EXEMPT_PUSH_CREDENTIAL_WRITABLE)

    bridge._prepare_exempt_worktree_or_raise = failing_prep  # type: ignore[method-assign]
    refused = arm(bridge, "exempt-writable", request_id="exempt-writable")
    assert refused["ok"] is False
    assert refused["error"] == EXEMPT_PUSH_CREDENTIAL_WRITABLE
    assert writer.arm_calls == []
    assert writer.rows() == []
    # No armed FS lease published
    armed_records = [r for r in bridge.worktree_lease_store.records() if r.state == "armed"]
    assert armed_records == []
    # Worktree must have been removed (unleased)
    wt = bridge.worktree_path("exempt-writable")
    assert not bridge.is_registered_worktree(wt)


def test_exempt_read_unavailable_blocks_arm_no_row(lease_env) -> None:
    from agent_redis_bridge.worktree_lease import WorktreeLeaseError
    from agent_redis_bridge.exempt_git import EXEMPT_REMOTE_READ_UNAVAILABLE

    repo, lease_root = lease_env
    bridge = make_writer_lease_bridge(repo, lease_root, lane="exempt")
    writer: FakeLaneWriter = bridge.lane_writer  # type: ignore[assignment]
    bridge._prepare_exempt_worktree_or_raise = (  # type: ignore[method-assign]
        lambda path: (_ for _ in ()).throw(WorktreeLeaseError(EXEMPT_REMOTE_READ_UNAVAILABLE))
    )
    refused = arm(bridge, "exempt-unread", request_id="exempt-unread")
    assert refused["ok"] is False
    assert refused["error"] == EXEMPT_REMOTE_READ_UNAVAILABLE
    assert writer.arm_calls == []


def test_r8_real_prepare_path_translates_exempt_git_error_code(lease_env) -> None:
    """Integration: real _prepare_exempt_worktree_or_raise translates ExemptGitError.

    The three stub-the-method tests keep their scenarios; this pins the production
    translation branch that they never execute.
    """
    from agent_redis_bridge.exempt_git import (
        EXEMPT_PUSH_CREDENTIAL_WRITABLE,
        ExemptGitError,
    )

    repo, lease_root = lease_env
    bridge = make_writer_lease_bridge(repo, lease_root, lane="exempt")
    writer: FakeLaneWriter = bridge.lane_writer  # type: ignore[assignment]

    def boom_prepare(*_a, **_k):
        raise ExemptGitError(EXEMPT_PUSH_CREDENTIAL_WRITABLE, "writable dry-run")

    with mock.patch(
        "agent_redis_bridge.exempt_git.prepare_exempt_worktree",
        boom_prepare,
    ):
        refused = arm(bridge, "exempt-real-translate", request_id="exempt-real-translate")
    assert refused["ok"] is False
    assert refused["error"] == EXEMPT_PUSH_CREDENTIAL_WRITABLE
    assert writer.arm_calls == []
    assert writer.rows() == []


def test_r7_non_exempt_exception_becomes_coded_internal_error(lease_env) -> None:
    """Non-ExemptGitError in prep must never end in inbox-handle-failed silence."""
    repo, lease_root = lease_env
    bridge = make_writer_lease_bridge(repo, lease_root, lane="exempt")
    writer: FakeLaneWriter = bridge.lane_writer  # type: ignore[assignment]

    def boom_prepare(*_a, **_k):
        raise RuntimeError("ssh-keygen exploded unexpectedly")

    with mock.patch(
        "agent_redis_bridge.exempt_git.prepare_exempt_worktree",
        boom_prepare,
    ):
        refused = arm(bridge, "exempt-internal", request_id="exempt-internal")
    assert refused["ok"] is False
    assert refused["error"] == "exempt-prep-internal-error"
    assert writer.arm_calls == []
