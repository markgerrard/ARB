from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from implbench.harness import cli, dispatch
from implbench.harness.dispatch import ScoredDispatchBinding
from implbench.harness.cell_runtime import attempt_id_for, cell_id_for
from implbench.harness.classifier import ClassificationInput, classify
from implbench.harness.controller import CLOSE_PHASES, Controller, ScoredCloseRuntime
from implbench.harness.completion import CompletionVerifier, materialization_digest
from implbench.harness.importer import attest_imported_graph, import_from_descriptor
from implbench.harness.phases import PilotSeal
from implbench.harness.records import canonical_json_bytes, make_identity
from implbench.harness.runner import RunnerError, run_full_matrix
from implbench.harness.schedule import expand_schedule
from implbench.harness.runtime import ProductionRuntimeUnavailable, build_production_runtime
from implbench.harness.scorer_sandbox import PostImportInput
from attempt_service_fixture import lifecycle as attempt_lifecycle


def _task() -> SimpleNamespace:
    return SimpleNamespace(
        task_id="c1-parser",
        expected_artifacts=("src/result.py",),
        allowed_paths=("src/*.py",),
        brief="return the structured result",
        timeout_s=1,
    )


def test_scored_completion_missing_import_attestation_is_authoritative_unknown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        dispatch,
        "_dispatch",
        lambda *args, **kwargs: dispatch.DispatchResult(
            "ok",
            completion={"mode": "receipt-only", "receipt_oids": ("a" * 40,)},
        ),
    )

    schedule_cell = next(
        cell for cell in expand_schedule("00" * 32, [("c1-parser", "a" * 64)] + [(f"c{i}", "a" * 64) for i in range(2, 9)])
        if cell.task_id == "c1-parser"
    )
    cell_root = tmp_path / "cell-root"
    cell_root.mkdir()
    result = dispatch.run_task(
        _task(), "seat-a", "pi-sdk", "a" * 64, "oi-pi-bakeoff-test-20260714T000000Z", tmp_path,
        schedule_cell=schedule_cell,
        fixture_root_oid="c" * 40,
        tool_gid=49123,
        cell_root=cell_root,
        scored_runtime_factory=lambda **_: None,
        scored_lifecycle=attempt_lifecycle(tmp_path),
    )

    assert all(value == "UNKNOWN" for value in result.completion["classification"].values())
    assert "imported_oids" not in result.completion


def test_context_free_controller_cannot_enter_import_score(tmp_path: Path) -> None:
    calls: list[str] = []
    controller = Controller(
        tmp_path / "close.ndjson",
        actions={phase: lambda phase=phase: calls.append(phase) for phase in CLOSE_PHASES},
    )

    result = controller.close()

    assert "IMPORT_SCORE" not in calls
    assert result.classification["G0"] == "PASS"
    assert result.classification["G2"] == "not-delivered"


@pytest.mark.parametrize("field", ["seal_complete", "receipts_authenticated", "imported_graph_attested"])
def test_nonempty_incomplete_or_untrusted_material_is_unknown(field: str) -> None:
    values = {
        "dispatch_status": "ok",
        "receipts": ("a" * 40,),
        "imported_oids": ("a" * 40,),
        "dirty": False,
        "seal_complete": True,
        "receipts_authenticated": True,
        "imported_graph_attested": True,
    }
    values[field] = False

    result = classify(ClassificationInput(**values))

    assert all(value == "UNKNOWN" for value in result.values())


def test_schedule_runtime_and_attempts_share_canonical_id_format() -> None:
    cells = expand_schedule("00" * 32, [(chr(ord("a") + i), "a" * 64) for i in range(8)])
    cell = cells[0]

    assert cell.cell_id == cell_id_for(cell.pair, cell.arm, cell.task_id, cell.repetition, cell.schedule_index)
    assert attempt_id_for(cell.cell_id, 1) == "attempt-" + hashlib.sha256(f"{cell.cell_id}\x001".encode()).hexdigest()


def test_descriptor_import_does_not_resolve_or_reopen_source_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "cell" / ".git" / "objects" / "aa"
    source.mkdir(parents=True)
    body = b"hello\n"
    header = b"blob " + str(len(body)).encode()
    oid = hashlib.sha1(header + b"\0" + body).hexdigest()
    object_path = source.parent / oid[:2] / oid[2:]
    object_path.parent.mkdir(parents=True)
    import zlib

    object_path.write_bytes(zlib.compress(header + b"\0" + body))
    source_fd = os.open(str(tmp_path / "cell"), os.O_RDONLY | os.O_DIRECTORY)
    try:
        monkeypatch.setattr(os, "readlink", lambda *args: (_ for _ in ()).throw(AssertionError("pathname resolved")))
        result = import_from_descriptor(source_fd, tmp_path / "bundle")
    finally:
        os.close(source_fd)

    assert result.object_ids == (oid,)


def test_g4_schema_uses_outcome_enum_and_rejects_legacy_key() -> None:
    from implbench.harness.scorer_sandbox import ScorerInputError, validate_g4_receipts

    with pytest.raises(ScorerInputError):
        validate_g4_receipts(
            [{"cell_id": "cell-" + "1" * 64, "attempt_id": "attempt-" + "2" * 32,
              "commit_oid": "a" * 40, "public_suite_oid": "b" * 40, "public_suite_digest": "c" * 64,
              "public_suite_digest_version": "public-suite-v1", "outcome": "PASS", "controller_sequence": 1, "nonce": "d" * 64}],
            expected_oids=["a" * 40], public_suite_oid="b" * 40, public_suite_digest="c" * 64,
        )
    validate_g4_receipts(
        [{"cell_id": "cell-" + "1" * 64, "attempt_id": "attempt-" + "2" * 32,
          "commit_oid": "a" * 40, "public_suite_oid": "b" * 40, "public_suite_digest": "c" * 64,
          "public_suite_digest_version": "public-suite-v1", "outcome_enum": "PASS", "controller_sequence": 1, "nonce": "d" * 64}],
        expected_oids=["a" * 40], public_suite_oid="b" * 40, public_suite_digest="c" * 64,
    )


def test_controller_scored_runtime_uses_verifier_importer_attestation_and_scorer_in_order(tmp_path: Path) -> None:
    events: list[str] = []
    materialization = tmp_path / "materialization"
    materialization.mkdir()
    (materialization / "result.txt").write_text("trusted\n")

    class Completion:
        decision = "agent-delivered"
        payload = {"completion": "digest"}

    class Verifier:
        def verify(self, receipts, status, worktree):
            events.append("verify")
            return Completion()

    runtime = ScoredCloseRuntime(
        completion_verifier=Verifier(),
        descriptor_importer=lambda payload: events.append("import") or {"graph": payload},
        attestation_verifier=lambda imported, completion: events.append("attest") or {
            "attested": True,
            "object_ids": ("b" * 40,),
            "materialization": materialization,
            "materialization_digest": materialization_digest(materialization),
        },
        scorer=lambda imported, attestation: events.append("score") or {
            "g1": "PASS", "g3": "PASS", "g4": "PASS", "g5": "PASS", "g6": "PASS", "g7": "PASS",
            "g4_receipts": (),
        },
        receipts=[], status={}, worktree=materialization,
    )

    runtime.import_and_score()

    assert events == ["verify", "import", "attest", "score"]
    assert runtime.result_context["imported_oids"] == ("b" * 40,)
    assert runtime.result_context["g1"] == "PASS"


def test_scored_runtime_rejects_incomplete_or_malformed_scorer_projection(tmp_path: Path) -> None:
    materialization = tmp_path / "materialization"
    materialization.mkdir()
    (materialization / "result.txt").write_text("trusted\n")

    class Completion:
        decision = "agent-delivered"
        payload = {"completion": "digest"}

    class Verifier:
        def verify(self, receipts, status, worktree):
            return Completion()

    def make_runtime(score):
        return ScoredCloseRuntime(
            completion_verifier=Verifier(),
            descriptor_importer=lambda payload: {"graph": payload},
            attestation_verifier=lambda imported, completion: {
                "attested": True,
                "object_ids": ("b" * 40,),
                "materialization": materialization,
                "materialization_digest": materialization_digest(materialization),
            },
            scorer=lambda imported, attestation: score,
            receipts=[], status={}, worktree=materialization,
        )

    with pytest.raises(RuntimeError, match="missing closed gates"):
        make_runtime({"g1": "PASS"}).import_and_score()
    with pytest.raises(RuntimeError, match="invalid g3 verdict"):
        make_runtime({
            "g1": "PASS", "g3": "MAYBE", "g4": "PASS", "g5": "PASS", "g6": "PASS", "g7": "PASS",
            "g4_receipts": (),
        }).import_and_score()


def test_real_scored_runtime_recovery_preserves_failed_gate_without_rescore(tmp_path: Path) -> None:
    journal = tmp_path / "close-real-runtime.ndjson"
    materialization = tmp_path / "materialization"
    materialization.mkdir()
    (materialization / "result.txt").write_text("trusted\n")
    calls: list[str] = []

    class Completion:
        decision = "agent-delivered"
        payload = {"completion": "digest"}

    class Verifier:
        def verify(self, receipts, status, worktree):
            calls.append("verify")
            return Completion()

    runtime = ScoredCloseRuntime(
        completion_verifier=Verifier(),
        descriptor_importer=lambda payload: calls.append("import") or {"graph": payload},
        attestation_verifier=lambda imported, completion: {
            "attested": True,
            "object_ids": ("a" * 40,),
            "materialization": materialization,
            "materialization_digest": materialization_digest(materialization),
        },
        scorer=lambda imported, attestation: calls.append("score") or {
            "g1": "FAIL", "g3": "PASS", "g4": "PASS", "g5": "PASS", "g6": "PASS", "g7": "PASS",
            "g4_receipts": (),
        },
        receipts=[], status={}, worktree=materialization,
    )
    context = {
        "dispatch_status": "ok", "receipts": ("a" * 40,), "imported_oids": (), "dirty": False,
        "seal_complete": True, "receipts_authenticated": True, "imported_graph_attested": False,
        "infrastructure_failure": None,
    }
    Controller(journal, runtime=runtime, close_context=context)._run("IMPORT_SCORE")
    assert calls == ["verify", "import", "score"]

    recovered = Controller(
        journal,
        runtime=SimpleNamespace(result_context={}, import_and_score=lambda: calls.append("rescore")),
        close_context=context,
    ).recover()
    assert calls == ["verify", "import", "score"]
    assert recovered.classification["G1"] == "FAIL"


def test_import_score_context_is_rehydrated_without_reimport_after_recovery(tmp_path: Path) -> None:
    journal = tmp_path / "close.ndjson"
    first_runtime = SimpleNamespace(
        result_context={
            "imported_oids": ("a" * 40,), "imported_graph_attested": True, "scorer_failure": None,
            "g1": "PASS", "g3": "PASS", "g4": "PASS", "g5": "PASS", "g6": "PASS", "g7": "PASS",
            "g4_receipts": (),
        },
        import_and_score=lambda: None,
    )
    context = {
        "dispatch_status": "ok", "receipts": ("a" * 40,), "imported_oids": (), "dirty": False,
        "seal_complete": True, "receipts_authenticated": True, "imported_graph_attested": False,
        "infrastructure_failure": None,
    }
    first = Controller(journal, runtime=first_runtime, close_context=context)
    first._run("IMPORT_SCORE")

    calls: list[str] = []
    recovered_runtime = SimpleNamespace(
        result_context={},
        import_and_score=lambda: calls.append("reimport"),
    )
    recovered = Controller(journal, runtime=recovered_runtime, close_context=context)
    result = recovered.recover()

    assert calls == []
    assert result.classification["G2"] == "agent-delivered"
    assert result.classification["G0"] == "PASS"


def test_import_score_recovery_preserves_non_default_scorer_gates(tmp_path: Path) -> None:
    journal = tmp_path / "close-gates.ndjson"
    gate_context = {
        "imported_oids": ("a" * 40,),
        "imported_graph_attested": True,
        "scorer_failure": None,
        "g1": "FAIL",
        "g3": "FAIL",
        "g4": "PASS",
        "g5": "FAIL",
        "g6": "UNKNOWN",
        "g7": "FAIL",
        "g4_receipts": ({"outcome_enum": "FAIL"}, {"outcome_enum": "PASS"}),
    }
    first_runtime = SimpleNamespace(result_context=gate_context, import_and_score=lambda: None)
    context = {
        "dispatch_status": "ok", "receipts": ("a" * 40,), "imported_oids": (), "dirty": False,
        "seal_complete": True, "receipts_authenticated": True, "imported_graph_attested": False,
        "infrastructure_failure": None,
    }
    first = Controller(journal, runtime=first_runtime, close_context=context)
    first._run("IMPORT_SCORE")

    calls: list[str] = []
    recovered_runtime = SimpleNamespace(result_context={}, import_and_score=lambda: calls.append("reimport"))
    recovered = Controller(journal, runtime=recovered_runtime, close_context=context)
    result = recovered.recover()

    assert calls == []
    assert result.classification["G1"] == "FAIL"
    assert result.classification["G3"] == "FAIL"
    assert result.classification["G4"] == "PASS"
    assert result.classification["G5"] == "FAIL"
    assert result.classification["G6"] == "UNKNOWN"
    assert result.classification["G7"] == "FAIL"


def test_import_score_recovery_denies_missing_durable_scorer_context(tmp_path: Path) -> None:
    journal = tmp_path / "close-missing.ndjson"
    first_runtime = SimpleNamespace(
        result_context={"imported_oids": ("a" * 40,), "imported_graph_attested": True},
        import_and_score=lambda: None,
    )
    context = {
        "dispatch_status": "ok", "receipts": ("a" * 40,), "imported_oids": (), "dirty": False,
        "seal_complete": True, "receipts_authenticated": True, "imported_graph_attested": False,
        "infrastructure_failure": None,
    }
    Controller(journal, runtime=first_runtime, close_context=context)._run("IMPORT_SCORE")

    recovered = Controller(journal, runtime=SimpleNamespace(result_context={}), close_context=context)
    result = recovered.recover()

    assert all(value == "UNKNOWN" for value in result.classification.values())


def test_controller_provisional_delivery_opens_import_then_final_attestation_classifies(tmp_path: Path) -> None:
    events: list[str] = []
    runtime = SimpleNamespace(
        import_and_score=lambda: events.extend(["import", "attest", "score"]) or None,
        result_context={"imported_oids": ("a" * 40,), "imported_graph_attested": True},
    )
    controller = Controller(
        tmp_path / "close.ndjson",
        runtime=runtime,
        close_context={
            "dispatch_status": "ok", "receipts": ("a" * 40,), "imported_oids": (), "dirty": False,
            "seal_complete": True, "receipts_authenticated": True, "imported_graph_attested": False,
            "infrastructure_failure": None,
        },
    )

    result = controller.close()

    assert events == ["import", "attest", "score"]
    assert result.classification["G2"] == "agent-delivered"


def test_controller_import_or_score_failure_replaces_provisional_delivery_with_unknown(tmp_path: Path) -> None:
    controller = Controller(
        tmp_path / "close.ndjson",
        runtime=SimpleNamespace(),
        actions={"IMPORT_SCORE": lambda: (_ for _ in ()).throw(RuntimeError("scorer down"))},
        close_context={
            "dispatch_status": "ok", "receipts": ("a" * 40,), "imported_oids": (), "dirty": False,
            "seal_complete": True, "receipts_authenticated": True, "imported_graph_attested": False,
            "infrastructure_failure": None,
        },
    )

    result = controller.close()

    assert all(value == "UNKNOWN" for value in result.classification.values())


def test_dispatch_production_chain_verifies_imports_attests_recomputes_and_scores(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def git(repo: Path, *args: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    git(worktree, "init", "-q")
    (worktree / "src").mkdir()
    (worktree / "src/result.py").write_text("fixture = True\n")
    git(worktree, "add", "src/result.py")
    subprocess.run(["git", "-C", str(worktree), "-c", "user.name=fixture", "-c", "user.email=fixture@localhost", "commit", "-q", "-m", "fixture"], check=True)
    fixture = git(worktree, "rev-parse", "HEAD")
    (worktree / "src/result.py").write_text("fixture = False\n")
    git(worktree, "add", "src/result.py")
    subprocess.run(["git", "-C", str(worktree), "-c", "user.name=fixture", "-c", "user.email=fixture@localhost", "commit", "-q", "-m", "candidate"], check=True)
    candidate = git(worktree, "rev-parse", "HEAD")
    tree = git(worktree, "rev-parse", "HEAD^{tree}")
    candidate_digest = materialization_digest(worktree)

    source = tmp_path / "descriptor-source"
    (source / "objects").mkdir(parents=True)
    shutil.copytree(worktree / ".git" / "objects", source / "objects", dirs_exist_ok=True)
    (source / "refs" / "implbench").mkdir(parents=True)
    (source / "refs" / "implbench" / "candidate").write_text(candidate + "\n")

    tasks = [("c1-parser", "a" * 64)] + [(f"c{i}", "a" * 64) for i in range(2, 9)]
    cell = next(item for item in expand_schedule("00" * 32, tasks) if item.task_id == "c1-parser")
    attempt = attempt_id_for(cell.cell_id, 1)
    identity = {
        "run_id": "oi-pi-bakeoff-test-20260714T000000Z", "cell_id": cell.cell_id, "attempt_id": attempt,
        "pair": cell.pair, "arm": cell.arm, "task": cell.task_id, "repetition": cell.repetition,
        "schedule_index": cell.schedule_index, "fixture_sha": "a" * 64, "model_declared": "glm-5.2",
        "model_verified_via": "provider-runtime-ack", "engine_version": "v1", "harness_version": "v1",
        "corpus_version": "implbench-corpus-v1", "config_digest": "1" * 64, "capability_manifest_digest": "2" * 64,
        "reasoning_requested": "medium", "reasoning_effective": "medium", "reasoning_verified_via": "provider-runtime-ack",
        "started_at": "2026-07-14T00:00:00Z", "ended_at": "2026-07-14T00:00:01Z", "wall_time_s": 1,
        "terminal_status": "completed", "retry_count": 0, "tool_call_count": 1, "schema_version": "record-v2",
        "prior_record_digest": None,
        "controls": {name: {"requested": "UNSUPPORTED", "effective": "UNSUPPORTED", "verified_via": "provider-runtime-ack"} for name in ("temperature", "top_p", "top_k", "seed", "penalties", "maximum_output", "stop_behavior", "tool_choice", "parallel_tool_behavior", "retry", "backoff", "timeouts")},
    }
    key = b"k" * 32
    payload = {
        "cell_id": cell.cell_id, "attempt_id": attempt, "fixture_root_oid": fixture,
        "ordered_parent_oids": [fixture], "commit_oid": candidate, "tree_oid": tree,
        "changed_paths": ["src/result.py"], "tree_digest": candidate_digest,
        "tree_digest_version": "final-tree-v1", "head_oid": candidate, "dirty": False,
        "controller_sequence": 1, "nonce": "d" * 64,
    }
    record = make_identity(identity, record_type="git-receipt", payload=payload)
    record["sequence"] = 1
    record["nonce"] = "e" * 64
    record["mac"] = hmac.new(key, canonical_json_bytes(record), hashlib.sha256).hexdigest()
    status = {"head": candidate, "dirty": False, "final_tree_digest": candidate_digest, "final_tree_digest_version": "final-tree-v1"}
    events: list[str] = []
    source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
    try:
        def make_runtime(**_kwargs):
            def attest(imported, completion):
                events.append("attest")
                try:
                    return attest_imported_graph(
                        imported, fixture_root_oid=fixture, receipts=completion.payload["receipts"], allowed_paths=("src/*.py",)
                    )
                except Exception as exc:
                    events.append(f"attest_error:{exc}")
                    raise

            return ScoredCloseRuntime.from_descriptor(
                completion_verifier=CompletionVerifier(key, identity=identity, fixture_root_oid=fixture),
                source_fd=source_fd, import_destination=tmp_path / "imported", candidate_ref="refs/implbench/candidate",
                attestation_verifier=attest,
                scorer=lambda post_import, _attestation: events.append("score") or {
                    "g1": "PASS", "g3": "PASS", "g4": "PASS", "g5": "PASS", "g6": "PASS", "g7": "PASS",
                    "g4_receipts": (), "post_import_type_ok": post_import.__class__ is PostImportInput,
                },
                receipts=[record], status=status, worktree=worktree,
            )

        projected_completion = {
            "mode": "receipt-only", "ref_namespace": "cell-attempt", "receipt_oids": (candidate,),
            "dirty": False, "seal_complete": True, "receipts_authenticated": True, "infrastructure_failure": None,
        }
        result = dispatch.run_task(
            _task(), "seat-a", "pi-sdk", "a" * 64, identity["run_id"], tmp_path,
            schedule_cell=cell, scored_runtime_factory=make_runtime,
            fixture_root_oid=fixture, tool_gid=49123, cell_root=tmp_path,
            scored_lifecycle=attempt_lifecycle(
                tmp_path,
                SimpleNamespace(**{
                    name: (lambda name=name: None)
                    for name in ("stop_tools", "drain_rpc", "kill_planes", "close_acl", "final_status", "kill_git", "census_snapshot", "destroy")
                }),
                dispatch_result={"status": "ok", "completion": projected_completion},
            ),
            recorder=SimpleNamespace(path=tmp_path / "dispatch.ndjson"),
        )
    finally:
        os.close(source_fd)

    assert events == ["attest", "score"]
    assert result.completion["cell_id"] == cell.cell_id
    assert result.completion["attempt_id"] == attempt
    assert result.completion["classification"]["G2"] == "agent-delivered"


def test_cli_phase_handlers_pass_a_bound_production_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = object()
    monkeypatch.setattr(cli, "production_runtime", lambda manifest: runtime)
    seen: list[object] = []
    monkeypatch.setattr(cli, "run_calibration", lambda manifest, seat, *, runtime: seen.append(runtime) or object())
    monkeypatch.setattr(cli, "run_pilot", lambda manifest, *, runtime: seen.append(runtime) or object())
    monkeypatch.setattr(cli, "run_full_matrix", lambda manifest, *, runtime: seen.append(runtime) or object())
    manifest = {"run_id": "oi-pi-bakeoff-test", "evidence": {"root": "/tmp"}}

    cli._calibrate_handler(manifest, "seat")
    cli._pilot_handler(manifest)
    cli._run_handler(manifest)

    assert seen == [runtime, runtime, runtime]


@pytest.mark.parametrize("handler", [cli._calibrate_handler, cli._pilot_handler, cli._run_handler])
def test_cli_phase_handlers_fail_closed_when_runtime_is_unavailable(handler) -> None:
    with pytest.raises(cli.CLIError, match="live production runtime"):
        handler({}, "seat") if handler is cli._calibrate_handler else handler({})


def test_matrix_runtime_forwards_seal_inputs_to_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    pilot_seal = PilotSeal("a" * 64, False)
    scored_dispatch = ScoredDispatchBinding(
        run_id="oi-pi-bakeoff-test-20260714T000000Z",
        repo=Path("/tmp/implbench-r10-repo"),
        task_for_cell=lambda cell: _task(),
        seat_for_cell=lambda cell: "seat-a",
        engine_for_cell=lambda cell: "pi-sdk",
        fixture_root_oid_for_cell=lambda cell: "a" * 40,
        tool_gid_for_cell=lambda cell: 49123,
        scored_runtime_factory=lambda **kwargs: None,
        cell_root_for_cell=lambda cell: Path("/Users/Shared/arb-implbench/cell"),
    )
    runtime = SimpleNamespace(
        pilot_seal=pilot_seal,
        execute=lambda cell, attempt: None,
        scored_dispatch=scored_dispatch,
        append_attempt=lambda outcome: None,
        stop_observation=lambda cell: {},
        close_cell=lambda outcome: None,
        freeze_final=lambda outcomes: None,
        config_bytes=b"config",
        refs=(('refs/implbench/runs/test', 'a' * 40),),
        journal_tail=b"journal",
    )
    seen: dict[str, object] = {}

    def fake_run_matrix(manifest, **kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr("implbench.harness.runner.run_matrix", fake_run_matrix)
    result = run_full_matrix({}, runtime=runtime)

    assert result is not None
    assert seen["pilot_seal"] is pilot_seal
    assert seen["execute"] is runtime.scored_dispatch
    assert seen["config_bytes"] == b"config"
    assert seen["refs"] == runtime.refs
    assert seen["journal_tail"] == b"journal"
    assert callable(seen["stop_observation"])


def test_production_runtime_rejects_missing_matrix_dependencies() -> None:
    with pytest.raises(ProductionRuntimeUnavailable, match="task_for_cell"):
        build_production_runtime({"run_id": "oi-pi-bakeoff-test", "source": {"realpath": "/tmp"}}, controller=SimpleNamespace())


def test_production_runtime_rejects_arbitrary_dispatch_substitution() -> None:
    controller = SimpleNamespace(
        repo=Path("/tmp/implbench-repo"),
        dispatch_fn=lambda *args, **kwargs: None,
        task_for_cell=lambda cell: None,
        seat_for_cell=lambda cell: "seat",
        engine_for_cell=lambda cell: "pi-sdk",
        fixture_root_oid_for_cell=lambda cell: "a" * 40,
        tool_gid_for_cell=lambda cell: 49123,
        scored_runtime_factory=lambda **kwargs: None,
        close_cell=lambda outcome: None,
    )
    with pytest.raises(ProductionRuntimeUnavailable, match="run_task"):
        build_production_runtime({"run_id": "oi-pi-bakeoff-test", "source": {"realpath": "/tmp"}}, controller=controller)


def test_full_matrix_requires_a_scored_dispatch_binding() -> None:
    runtime = SimpleNamespace(
        pilot_seal=PilotSeal("a" * 64, False),
        execute=lambda cell, attempt: None,
    )
    with pytest.raises(RunnerError, match="ScoredDispatchBinding"):
        run_full_matrix({}, runtime=runtime)
