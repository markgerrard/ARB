from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from implbench.harness.completion import (
    CompletionError,
    CompletionVerifier,
    materialization_digest,
    verify_post_g4_attestation,
)
from implbench.harness.receipts import ReceiptChain, make_git_receipt


def _identity() -> dict[str, object]:
    return {
        "run_id": "oi-pi-bakeoff-test-20260714T000000Z", "cell_id": "cell-" + "a" * 64,
        "attempt_id": "attempt-" + "b" * 32, "pair": "GLM", "arm": "glm-pi", "task": "c1-parser",
        "repetition": 1, "schedule_index": 0, "fixture_sha": "f" * 64, "model_declared": "glm-5.2",
        "model_verified_via": "provider-runtime-ack", "engine_version": "v1", "harness_version": "v1",
        "corpus_version": "implbench-corpus-v1", "config_digest": "1" * 64, "capability_manifest_digest": "2" * 64,
        "reasoning_requested": "medium", "reasoning_effective": "medium", "reasoning_verified_via": "provider-runtime-ack",
        "started_at": "2026-07-14T00:00:00Z", "ended_at": "2026-07-14T00:00:01Z", "wall_time_s": 1,
        "terminal_status": "completed", "retry_count": 0, "tool_call_count": 1, "schema_version": "record-v2",
        "prior_record_digest": None,
        "controls": {name: {"requested": "UNSUPPORTED", "effective": "UNSUPPORTED", "verified_via": "provider-runtime-ack"} for name in ("temperature", "top_p", "top_k", "seed", "penalties", "maximum_output", "stop_behavior", "tool_choice", "parallel_tool_behavior", "retry", "backoff", "timeouts")},
    }


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("fixture\n")
    _git(repo, "add", "README.md")
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=fixture", "-c", "user.email=fixture@localhost", "commit", "-q", "-m", "fixture"], check=True)
    return repo, _git(repo, "rev-parse", "HEAD")


def test_red_materialization_is_no_follow_whole_worktree_digest(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    first = materialization_digest(repo)
    (repo / "untracked.txt").write_text("untracked\n")
    second = materialization_digest(repo)
    assert first != second
    (repo / "link").symlink_to("untracked.txt")
    assert materialization_digest(repo) != second
    (repo / ".git" / "ignored-by-final-tree").write_text("metadata\n")
    assert materialization_digest(repo) != materialization_digest(repo, exclude_git_metadata=False)
    (repo / "bad-dir").symlink_to(repo, target_is_directory=True)
    with pytest.raises(CompletionError):
        materialization_digest(repo)


def test_red_completion_empty_dirty_nonempty_truth_table(tmp_path: Path) -> None:
    repo, fixture = _repo(tmp_path)
    verifier = CompletionVerifier(b"k" * 32, identity=_identity(), fixture_root_oid=fixture)
    digest = materialization_digest(repo)
    empty = verifier.verify([], {"head": fixture, "dirty": False, "final_tree_digest": digest, "final_tree_digest_version": "final-tree-v1"}, repo)
    assert empty.decision == "not-delivered"
    (repo / "README.md").write_text("dirty\n")
    dirty_empty = verifier.verify([], {"head": fixture, "dirty": True, "final_tree_digest": materialization_digest(repo), "final_tree_digest_version": "final-tree-v1"}, repo)
    assert dirty_empty.decision == "not-delivered"
    with pytest.raises(CompletionError, match="fixture root"):
        verifier.verify([], {"head": "b" * 40, "dirty": False, "final_tree_digest": materialization_digest(repo), "final_tree_digest_version": "final-tree-v1"}, repo)


def test_red_completion_rejects_version_mismatch_and_final_status_unavailable_without_throw(tmp_path: Path) -> None:
    repo, fixture = _repo(tmp_path)
    verifier = CompletionVerifier(b"k" * 32, identity=_identity(), fixture_root_oid=fixture)
    with pytest.raises(CompletionError):
        verifier.verify([], {"head": fixture, "dirty": False, "final_tree_digest": "e" * 64, "final_tree_digest_version": "wrong"}, repo)
    unavailable = verifier.final_status(lambda: (_ for _ in ()).throw(OSError("service dead")))
    assert unavailable["status"] == "UNAVAILABLE"


def test_red_post_g4_attestation_rejects_replay_pin_and_digest_mismatch(tmp_path: Path) -> None:
    repo, fixture = _repo(tmp_path)
    chain = ReceiptChain(tmp_path / "receipts.ndjson", b"k" * 32, identity=_identity(), fixture_root_oid=fixture, allowed_paths=("**",))
    completion = {"pre_scorer_attestation_digest": "3" * 64, "g4_receipts_digest": "4" * 64}
    row = chain.append_post_g4_attestation(completion)
    assert verify_post_g4_attestation(row, identity=_identity(), expected=completion, seen_digests=set())
    with pytest.raises(CompletionError):
        verify_post_g4_attestation(row, identity=_identity(), expected=completion, seen_digests={row["mac"]})
    with pytest.raises(CompletionError):
        verify_post_g4_attestation(row, identity={**_identity(), "attempt_id": "attempt-" + "c" * 32}, expected=completion, seen_digests=set())


def test_red_completion_rejects_tampered_authenticated_receipt(tmp_path: Path) -> None:
    repo, fixture = _repo(tmp_path)
    chain = ReceiptChain(tmp_path / "receipts.ndjson", b"k" * 32, identity=_identity(), fixture_root_oid=fixture, allowed_paths=("src/**",))
    payload = make_git_receipt(cell_id=_identity()["cell_id"], attempt_id=_identity()["attempt_id"], fixture_root_oid=fixture, ordered_parent_oids=[fixture], commit_oid="b" * 40, tree_oid="c" * 40, changed_paths=["src/main.py"], tree_digest="d" * 64, head_oid="b" * 40, dirty=False, controller_sequence=1)
    row = chain.append(payload)
    tampered = dict(row)
    tampered["mac"] = "0" * 64
    verifier = CompletionVerifier(b"k" * 32, identity=_identity(), fixture_root_oid=fixture)
    with pytest.raises(CompletionError, match="authentication"):
        verifier.verify([tampered], {"head": fixture, "dirty": False, "final_tree_digest": materialization_digest(repo), "final_tree_digest_version": "final-tree-v1"}, repo)


def test_red_completion_rejects_raw_receipt_mapping_compatibility(tmp_path: Path) -> None:
    repo, fixture = _repo(tmp_path)
    verifier = CompletionVerifier(b"k" * 32, identity=_identity(), fixture_root_oid=fixture)
    with pytest.raises(CompletionError, match="authenticated git-receipt"):
        verifier.verify(
            [{"cell_id": _identity()["cell_id"], "attempt_id": _identity()["attempt_id"]}],
            {"head": fixture, "dirty": False, "final_tree_digest": materialization_digest(repo), "final_tree_digest_version": "final-tree-v1"},
            repo,
        )
