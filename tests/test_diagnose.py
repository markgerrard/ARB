from __future__ import annotations

import json
import hashlib
import subprocess
import shutil
import pytest
from pathlib import Path

from skills._diagnose_common import assert_run_record_conformant, read_dispatch_log, validate_dispatch_log
from skills.diagnose import derive_scope, run_diagnose, validate_diagnose_input
from skills.diagnose.diagnose import evaluate_run_record, last_event_seq


def make_repo(tmp_path: Path) -> tuple[Path, dict]:
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "app" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "app" / "util.py").write_text(
        "def helper(value):\n    return value + 1\n",
        encoding="utf-8",
    )
    (repo / "app" / "core.py").write_text(
        "from app.util import helper\n\n"
        "def compute(value):\n"
        "    return helper(value)\n",
        encoding="utf-8",
    )
    (repo / "app" / "other.py").write_text("OTHER = True\n", encoding="utf-8")
    (repo / "tests" / "test_core.py").write_text(
        "from app.core import compute\n\n"
        "def test_compute():\n"
        "    assert compute(1) == 3\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(
        ["git", "commit", "-m", "fixture"],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )
    trigger = {
        "failing_test": "tests/test_core.py::test_compute",
    }
    return repo, trigger


def assert_schema_conformant_run(record: dict) -> None:
    assert_run_record_conformant(record)


def assert_real_integrated_record(record: dict) -> None:
    assert_schema_conformant_run(record)
    log_path = Path(record["dispatch_log_ref"])
    assert log_path.exists()
    events = read_dispatch_log(log_path)
    assert validate_dispatch_log(events) == []
    assert last_event_seq(record, "trigger_received") == 1
    assert last_event_seq(record, "artifact_visible") is not None


def fake_dispatcher(blocking: str | None = None):
    target_ids = {
        "blind": "codex-bridge-dev-example",
        "alternative": "agy-bridge-dev",
        "open": "pi-sdk-bridge-dev-minimax-m3",
        "scribe": "scribe",
    }

    def dispatch(sealed_brief):
        if blocking:
            return None
        return {
            "model": f"answered-{sealed_brief['model']}",
            "from": target_ids[sealed_brief["role"]],
            "reply": f"reply for {sealed_brief['role']}",
        }

    return dispatch


def run_case(tmp_path: Path, monkeypatch, blocking: str | None = None):
    repo, trigger = make_repo(tmp_path)
    return run_diagnose(repo, trigger, tmp_path / "work", dispatch=fake_dispatcher(blocking))


requires_sandbox_exec = pytest.mark.skipif(
    shutil.which("sandbox-exec") is None,
    reason="diagnose containment uses macOS sandbox-exec; unavailable hosts fail-closed with test-containment-unavailable",
)


@requires_sandbox_exec
def test_d1_scope_derives_from_recorded_traceback_and_live_run_passes(tmp_path, monkeypatch):
    repo, trigger = make_repo(tmp_path)
    recorded_traceback = {"reproduced": True, "window": {"start": 2, "end": 4}, "traceback": "Traceback"}
    scope = derive_scope(trigger["failing_test"], recorded_traceback, repo)
    assert scope["paths"] == ["app/core.py", "app/util.py", "tests/test_core.py"]
    assert scope["window"] == {"start": 2, "end": 4}

    clean, clean_reasons = run_diagnose(repo, trigger, tmp_path / "clean", dispatch=fake_dispatcher())
    assert_real_integrated_record(clean)
    assert clean_reasons == []
    assert clean["panel_executed"] is True


@requires_sandbox_exec
def test_run_diagnose_authors_live_panel_artifacts_and_recompute_basis(tmp_path, monkeypatch):
    first, first_reasons = run_case(tmp_path, monkeypatch)
    second, second_reasons = run_case(tmp_path / "second", monkeypatch)
    assert_real_integrated_record(first)
    assert_real_integrated_record(second)
    assert first_reasons == second_reasons == []
    assert first["repo_sha"]
    assert first["recorded_traceback"]["traceback_sha256"]
    assert "traceback" not in first["recorded_traceback"]
    assert len(first["sealed_briefs"]) == 4
    assert len(first["submissions"]) == 4
    assert len(first["post_briefs"]) == 2


def test_d4_diagnose_input_schema_rejects_steer_anywhere_and_dispatch_is_own_subset():
    assert validate_diagnose_input({"failing_test": "t.py::test"}) == []
    assert "steer-field" in validate_diagnose_input(
        {"failing_test": "t.py::test", "metadata": {"steer": {"reason": "x"}}}
    )
    assert "unknown-field" in validate_diagnose_input(
        {"failing_test": "t.py::test", "steer": {}}
    )


def test_trigger_rejects_error_log_and_accepts_real_pytest_node_ids():
    assert "unknown-field" in validate_diagnose_input({"failing_test": "t.py::x", "error_log": "boom"})
    for ok in [
        "pkg/t.py::test_a",
        "pkg/sub/t.py::TestClass::test_method",
        "pkg/t.py::test_x[param-1]",
        "a/b/t.py::TestC::test_y[case 2-id]",
        "pkg/t.py::TestA::TestB::test_c",
    ]:
        assert validate_diagnose_input({"failing_test": ok}) == [], ok
    assert "invalid-field" in validate_diagnose_input({"failing_test": "not a node id"})
    assert "invalid-field" in validate_diagnose_input({"failing_test": "pkg/t.py"})
    assert "invalid-field" in validate_diagnose_input({"failing_test": "pkgnopy::test_a"})


def test_recorded_traceback_failclosed_when_uncontained(monkeypatch, tmp_path):
    import skills.diagnose.diagnose as D

    monkeypatch.setattr(
        D,
        "run_contained",
        lambda *a, **k: {
            "contained": False,
            "reproduced": False,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
        },
    )
    record = D.record_traceback(str(tmp_path), "pkg/t.py::test_a")
    assert record["blocking"] == "test-containment-unavailable"


def test_recorded_traceback_failclosed_on_nonreproduction(monkeypatch, tmp_path):
    import skills.diagnose.diagnose as D

    monkeypatch.setattr(
        D,
        "run_contained",
        lambda *a, **k: {
            "contained": True,
            "reproduced": False,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
        },
    )
    record = D.record_traceback(str(tmp_path), "pkg/t.py::test_a")
    assert record["blocking"] == "test-nonreproduction"


def test_checkout_at_cache_is_keyed_by_repo_sha(tmp_path):
    import skills.diagnose.diagnose as D

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "marker.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    commit_env = {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", "commit", "-m", "one"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=commit_env)
    sha_one = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
    (repo / "marker.txt").write_text("two\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "commit", "-m", "two"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=commit_env)
    sha_two = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()

    checkout_one = D._checkout_at(repo, sha_one, tmp_path / "work")
    checkout_two = D._checkout_at(repo, sha_two, tmp_path / "work")

    assert (checkout_one / "marker.txt").read_text(encoding="utf-8") == "one\n"
    assert (checkout_two / "marker.txt").read_text(encoding="utf-8") == "two\n"
    assert checkout_one != checkout_two


@requires_sandbox_exec
def test_evaluate_run_record_uses_real_dispatch_log_not_hand_built_fixture(tmp_path, monkeypatch):
    record, reasons = run_case(tmp_path, monkeypatch)
    assert_real_integrated_record(record)
    assert reasons == []
    record["dispatch_log_ref"] = str(tmp_path / "missing.jsonl")
    assert "dispatch-log-invalid" in evaluate_run_record(record)


@requires_sandbox_exec
def test_live_panel_run_is_verified_when_validator_panel_evidence_passes(tmp_path, monkeypatch):
    record, reasons = run_case(tmp_path, monkeypatch)
    assert_real_integrated_record(record)
    assert reasons == []
    assert record["panel_executed"] is True
    assert record["verified"] is True
    assert "harness_only" not in record


def test_run_diagnose_exposes_no_production_contamination_seam():
    source = Path("skills/diagnose/diagnose.py").read_text(encoding="utf-8")
    assert "contamination" not in source


@requires_sandbox_exec
def test_run_diagnose_failclosed_when_panel_incomplete(tmp_path, monkeypatch):
    record, reasons = run_case(tmp_path, monkeypatch, blocking="incomplete-panel")
    assert record["panel_executed"] is False
    assert record["verified"] is False
    assert record["harness_only"] is True
    assert record["blocking_real_use"] == "incomplete-panel"
    assert reasons == ["incomplete-panel"]


def test_panel_constants_committed_and_decorrelated():
    constants = json.loads(Path("skills/diagnose/panel_constants.json").read_text(encoding="utf-8"))
    assert all(
        key in constants
        for key in ["roster", "scribe", "certifier", "role_assignment", "collation_order"]
    )
    assert {item["model"] for item in [*constants["roster"], constants["scribe"]]} == {
        "codex",
        "agy",
        "minimax/MiniMax-M3",
        "claude-haiku",
    }
    assert {item["role"] for item in constants["roster"]} == {"blind", "alternative", "open"}
    assert {item["channel"] for item in [*constants["roster"], constants["scribe"]]} == {"bridge", "agent-tool"}
    assert "synthesize" not in constants["scribe"]["system_prompt"].lower()
    assert constants["certifier"]["rule"] == "model!=author_model and not reciprocal"
    assert constants["collation_order"] == "by-seat-id-asc"


def test_author_briefs_is_pure_recomputable_and_no_raw_traceback_prose(tmp_path):
    from skills.diagnose.briefs import author_briefs

    repo, trigger = make_repo(tmp_path)
    constants = json.loads(Path("skills/diagnose/panel_constants.json").read_text(encoding="utf-8"))
    constants["_repo_root"] = str(repo)
    recorded_traceback = {
        "reproduced": True,
        "window": {"start": 3, "end": 9},
        "traceback": "tests/test_core.py:4 boom UNIQUEPROSE",
        "blocking": None,
    }
    repo_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()

    first = author_briefs(trigger["failing_test"], repo_sha, recorded_traceback, constants)
    second = author_briefs(trigger["failing_test"], repo_sha, recorded_traceback, constants)

    assert [item["seal"] for item in first] == [item["seal"] for item in second]
    assert len(first) == 4
    assert "UNIQUEPROSE" not in json.dumps(first)
    assert {item["role"] for item in first} == {"scribe", "blind", "alternative", "open"}
    for item in first:
        assert item["seal"]
        assert item["brief"]["observables"]
        assert all(not obs["path"].startswith("/") for obs in item["brief"]["observables"])


def sealed_briefs_fixture() -> list[dict]:
    return [
        {"role": "blind", "model": "codex", "seal": "a" * 64, "brief": {"task": "blind"}},
        {"role": "alternative", "model": "agy", "seal": "b" * 64, "brief": {"task": "alt"}},
    ]


def test_run_panel_records_bus_reply_consistency_and_writes_outside_repo(tmp_path, monkeypatch):
    from skills.diagnose import panel

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    sent = []

    def fake_dispatch(sealed_brief):
        sent.append(sealed_brief)
        body = f"reply for {sealed_brief['role']}"
        return {"model": f"actual-{sealed_brief['role']}", "reply": body}

    work_dir = tmp_path / "work"
    result = panel.run_panel(sealed_briefs_fixture(), fake_dispatch, work_dir, repo_root=repo_root)

    assert result["blocking"] is None
    assert sent == sealed_briefs_fixture()
    assert len(result["submissions"]) == 2
    for submission in result["submissions"]:
        reply = f"reply for {submission['role']}"
        assert submission["model"] == f"actual-{submission['role']}"
        assert submission["bus_reply_ref"].startswith("file://")
        assert submission["bus_reply_sha256"] == hashlib.sha256(reply.encode("utf-8")).hexdigest()
        path = Path(submission["bus_reply_ref"].removeprefix("file://"))
        assert not path.is_relative_to(repo_root)
        assert path.is_relative_to(work_dir)
        assert path.read_text(encoding="utf-8") == reply


def test_run_panel_blocks_partial_and_bridge_unavailable(tmp_path, monkeypatch):
    from skills.diagnose import panel

    calls = []

    def partial_dispatch(sealed_brief):
        calls.append(sealed_brief["role"])
        if sealed_brief["role"] == "alternative":
            return None
        return {"model": "codex", "reply": "ok"}

    partial = panel.run_panel(sealed_briefs_fixture(), partial_dispatch, tmp_path / "partial")
    assert partial["blocking"] == "incomplete-panel"
    assert calls == ["blind", "alternative"]

    def down_dispatch(sealed_brief):
        raise RuntimeError("bridge down")

    down = panel.run_panel(sealed_briefs_fixture(), down_dispatch, tmp_path / "down")
    assert down["blocking"] == "bridge-unavailable"


def test_bridge_dispatch_uses_real_agent_dispatch_flags(monkeypatch):
    from skills.diagnose import panel

    # Credential required for harness publish; supply it so the test reaches the
    # agent-dispatch argv contract it was written to prove (not the new gate).
    monkeypatch.setenv("ARB_MEMORY_REDIS_URL", "redis://memory-test/0")
    calls = []
    brief_texts = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))

        class Result:
            def __init__(self, returncode=0, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        if argv and argv[0] == "scripts/arb-memory-harness-publish":
            target = argv[argv.index("--target-agent-id") + 1]
            return Result(
                stdout=json.dumps(
                    {
                        "artefact_id": "art-diag-1",
                        "version": 1,
                        "target_agent_id": target,
                        "registration_generation": "gen-1",
                        "worker_vantage": "diagnose-panel",
                        "content_hash": "hash-diag-1",
                    }
                )
            )
        # Temp brief unlinked after dispatch returns — snapshot while live.
        if "--brief" in argv:
            brief_texts.append(Path(argv[argv.index("--brief") + 1]).read_text(encoding="utf-8"))
        return Result(
            stdout=json.dumps(
                {"model": "codex-seat", "from": "codex-project-c-dev", "reply": "ok"}
            )
        )

    monkeypatch.setattr(panel.subprocess, "run", fake_run)
    dispatch = panel.bridge_dispatch("codex-project-c-dev", "codex", role="diagnose-blind")
    result = dispatch({"role": "blind", "model": "codex", "seal": "a" * 64, "brief": {"task": "x"}})

    # Publish then enqueue: second call is agent-dispatch.
    assert len(calls) == 2
    assert calls[0][0][0] == "scripts/arb-memory-harness-publish"
    argv, kwargs = calls[1]
    assert argv[:9] == [
        "scripts/agent-dispatch",
        "--workspace",
        "dev",
        "--engine",
        "codex",
        "--target-id",
        "codex-project-c-dev",
        "--role",
        "diagnose-blind",
    ]
    assert "--model" not in argv
    assert "--ceiling" not in argv
    assert "--work-dir" not in argv
    assert "input" not in kwargs
    assert kwargs["env"]["FROM_AGENT_ID"] == "claude-bridge-dev"
    # Publish credential stripped for non-FABA enqueue.
    assert "ARB_MEMORY_REDIS_URL" not in kwargs["env"]
    # Pre-minted quartet replaces free-form positional task body.
    assert "--artefact-id" in argv
    assert "--version" in argv
    assert "--receipt" in argv
    assert "--brief" in argv
    assert brief_texts and "blind" in brief_texts[0]
    assert result == {"model": "codex-seat", "from": "codex-project-c-dev", "reply": "ok"}


def test_bridge_dispatch_surfaces_subprocess_stderr_on_failure(monkeypatch, capsys):
    from skills.diagnose import panel

    # Original claim: agent-dispatch sender-policy failures surface on stderr.
    # Supply publish credential so the credential gate does not mask that path.
    monkeypatch.setenv("ARB_MEMORY_REDIS_URL", "redis://memory-test/0")

    def fake_run(argv, **kwargs):
        class Result:
            def __init__(self, returncode=0, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        if argv and argv[0] == "scripts/arb-memory-harness-publish":
            target = argv[argv.index("--target-agent-id") + 1]
            return Result(
                stdout=json.dumps(
                    {
                        "artefact_id": "art-diag-1",
                        "version": 1,
                        "target_agent_id": target,
                        "registration_generation": "gen-1",
                        "worker_vantage": "diagnose-panel",
                        "content_hash": "hash-diag-1",
                    }
                )
            )
        return Result(
            returncode=1,
            stderr="sender-rejected: claude-bridge-dev is not trusted",
        )

    monkeypatch.setattr(panel.subprocess, "run", fake_run)
    dispatch = panel.bridge_dispatch("codex-project-c-dev", "codex", role="diagnose-blind")
    result = dispatch(
        {
            "role": "blind",
            "model": "codex",
            "seal": "a" * 64,
            "brief": {"task": "SECRET_PAYLOAD_SHOULD_NOT_BE_LOGGED"},
        }
    )

    captured = capsys.readouterr()
    assert result is None
    assert "bridge_dispatch failed:" in captured.err
    assert "sender-rejected: claude-bridge-dev is not trusted" in captured.err
    assert "SECRET_PAYLOAD_SHOULD_NOT_BE_LOGGED" not in captured.err


def test_author_post_briefs_is_pure_over_sealed_submissions():
    from skills.diagnose.briefs import author_post_briefs

    constants = json.loads(Path("skills/diagnose/panel_constants.json").read_text(encoding="utf-8"))
    submissions = [
        {
            "role": "blind",
            "seat": "codex-bridge-dev-example",
            "seal": "a" * 64,
            "bus_reply_ref": "file://reply-a",
            "bus_reply_sha256": "b" * 64,
        },
        {
            "role": "alternative",
            "seat": "agy-bridge-dev",
            "seal": "c" * 64,
            "bus_reply_ref": "file://reply-c",
            "bus_reply_sha256": "d" * 64,
        },
    ]
    predicates = [{"author_model": "codex"}]
    first = author_post_briefs(constants, submissions, predicates)
    second = author_post_briefs(constants, list(reversed(submissions)), predicates)

    assert first == second
    assert {item["role"] for item in first} == {"certifier", "collation"}
    for item in first:
        assert item["seal"]
        assert item["brief"]["sealed_submissions"]
    certifier = next(item for item in first if item["role"] == "certifier")
    assert certifier["model"] != "codex"
