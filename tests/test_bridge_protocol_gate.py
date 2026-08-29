from __future__ import annotations

import importlib.util
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = ROOT / "skills" / "bridge-protocol" / "gate" / "gate.py"
spec = importlib.util.spec_from_file_location("bridge_protocol_gate", GATE_PATH)
gate = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(gate)


def base_phase_input(**overrides):
    doc = {
        "phase": "merge-gate",
        "phase_class": "declarative",
        "artifact_sha": "abc",
        "correctness_basis": "manual-panel",
        "reviewer_reports": [
            {
                "seat": "codex",
                "verdict": "READY",
                "findings": [
                    {
                        "id": "F1",
                        "severity": "P2",
                        "file_line": "x:1",
                        "fix": "fix",
                        "status": "resolved",
                    }
                ],
                "certified_components": [],
            }
        ],
        "hard_signal_evidence": None,
        "manifest_ref": "manifest.json",
        "escaped_defect": {
            "triggered": False,
            "changelog": False,
            "corpus_row": False,
            "standing_rule": "n/a",
            "state": "fixed",
        },
    }
    doc.update(overrides)
    return doc


def test_task1_all_schemas_load_and_have_required_fields():
    for name in gate.SCHEMA_NAMES:
        schema = gate.load_schema(ROOT, name)
        assert schema["name"] == name
        assert "required" in schema


def test_task1_phase_input_rejects_builder_supplied_gate_outputs():
    doc = base_phase_input(gate_decision="pass")
    with pytest.raises(gate.GateError, match="forbidden"):
        gate.validate_doc(doc, gate.load_schema(ROOT, "phase_input"))


def test_task1_phase_input_rejects_malformed_nested_fields():
    doc = base_phase_input()
    del doc["reviewer_reports"][0]["findings"][0]["status"]
    with pytest.raises(gate.GateError, match="finding missing"):
        gate.validate_doc(doc, gate.load_schema(ROOT, "phase_input"))


def test_task1_phase_input_accepts_bounded_valid_shape():
    gate.validate_doc(base_phase_input(), gate.load_schema(ROOT, "phase_input"))


def init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)
    return repo


def commit_all(repo: Path, message: str) -> None:
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=repo, check=True, stdout=subprocess.PIPE)


def init_bridge_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "bridge-repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "branch", "-M", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    dst = repo / "skills" / "bridge-protocol"
    shutil.copytree(ROOT / "skills" / "bridge-protocol", dst)
    defect_hunts = repo / "skills" / "defect_hunts"
    defect_hunts.mkdir(parents=True)
    for name in ("h2_assumptions.py", "h2_derive.py", "h2_graduation.py"):
        shutil.copy2(ROOT / "skills" / "defect_hunts" / name, defect_hunts / name)
    pending = dst / "gate" / "trust_root.pending.json"
    if pending.exists():
        pending.unlink()
    (repo / "docs").mkdir()
    (repo / "manifest.json").write_text("[]\n", encoding="utf-8")
    root = {
        "certified_object_sha": gate.certified_object_sha(repo),
        "certifying_seats": ["external-a", "external-b", "external-c"],
        "human_approver": "mark",
        "judged_not_verified": True,
    }
    (dst / "gate" / "trust_root.json").write_text(json.dumps(root, indent=2), encoding="utf-8")
    commit_all(repo, "base bridge protocol")
    return repo


def default_phase(**overrides):
    doc = base_phase_input(
        phase="design-panel",
        phase_class="declarative",
        correctness_basis="manual-panel",
        manifest_ref="manifest.json",
    )
    doc.update(overrides)
    return doc



def eval_unit(doc, repo, **kwargs):
    kwargs.setdefault("trust_root", None)
    kwargs.setdefault("readiness", None)
    kwargs.setdefault("stage_modes", None)
    return gate.evaluate(doc, repo, **kwargs)

def make_hard_signal(repo: Path, output_text: str = "1 passed\n"):
    output = repo / "test-output.txt"
    output.write_text(output_text, encoding="utf-8")
    subprocess.run(["git", "add", "test-output.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "record output"], cwd=repo, check=True, stdout=subprocess.PIPE)
    return {
        "command": "pytest tests/test_bridge_protocol_gate.py",
        "cwd": str(repo),
        "test_count": 1,
        "check_id": "dogfood",
        "commit_sha": gate.git_head(repo),
        "captured_output_path": str(output),
        "tree_hash": gate.git_tree_hash(repo),
        "captured_output_sha256": gate.sha256_file(output),
        "exit_code": 0,
        "start_time": "2026-06-19T00:00:00Z",
        "end_time": "2026-06-19T00:00:01Z",
        "runner_id": "pytest",
    }


def test_task2_ground_truth_and_fresh_attestation_pass(tmp_path):
    repo = init_repo(tmp_path)
    evidence = make_hard_signal(repo)
    truth = gate.derive_ground_truth(repo)
    assert truth["head"] == evidence["commit_sha"]
    assert truth["tree_hash"] == evidence["tree_hash"]
    assert truth["clean_tree"] is True
    assert gate.hard_signal_blocks(repo, evidence) == []


def test_task2_stale_sha_output_or_tree_blocks(tmp_path):
    repo = init_repo(tmp_path)
    evidence = make_hard_signal(repo)
    stale_sha = dict(evidence, commit_sha="0" * 40)
    assert gate.hard_signal_blocks(repo, stale_sha) == [gate.BLOCK_HARD_SIGNAL]

    (Path(evidence["captured_output_path"])).write_text("stale\n", encoding="utf-8")
    blocks = gate.hard_signal_blocks(repo, evidence)
    assert blocks == [gate.BLOCK_HARD_SIGNAL]


def sample_registry():
    return {
        "excluded_roots": ["tests/**", "docs/**", "*.md"],
        "rules": [
            {
                "pattern": "engines/**",
                "layer": "engine",
                "required_dimensions": ["latency", "state"],
                "owner": "bridge",
            },
            {
                "pattern": "skills/bridge-protocol/gate/**",
                "layer": "adapter",
                "required_dimensions": ["interface"],
                "owner": "bridge-protocol",
            },
            {"pattern": "**", "layer": "unclassified", "required_dimensions": [], "owner": "bridge"},
        ],
    }


def test_task3_classification_is_production_by_default_and_excludes_tests():
    registry = sample_registry()
    assert gate.classify_path("tests/test_bridge_protocol_gate.py", registry)["production"] is False
    engine = gate.classify_path("engines/new_engine.py", registry)
    assert engine["production"] is True
    assert engine["layer"] == "engine"
    _, blocks = gate.classify_changes(["newtop/module.py"], registry)
    assert blocks == [gate.BLOCK_UNCLASSIFIED]


def test_task3_registry_without_catchall_is_setup_error():
    registry = sample_registry()
    registry["rules"] = registry["rules"][:-1]
    with pytest.raises(gate.GateError, match="catch-all"):
        gate.classify_path("lib/engines.py", registry)


def resolved_reports():
    doc = base_phase_input()
    doc["reviewer_reports"][0]["seat"] = "codex"
    doc["reviewer_reports"][0]["findings"][0]["id"] = "W1"
    doc["reviewer_reports"][0]["findings"][0]["status"] = "resolved"
    return doc["reviewer_reports"]


def test_task4_missing_registry_dimension_and_scalar_evasion_block():
    registry = sample_registry()
    classified = [gate.classify_path("engines/new_engine.py", registry)]
    manifest = [
        {
            "component": "engines/new_engine.py",
            "layer": "engine",
            "costly_dimensions": ["interface"],
            "dimensions_considered_and_excluded": [],
            "production_component": "engines/new_engine.py",
            "dimension_preserving_tests": {"interface": "test_interface"},
            "dimension_evidence": {"interface": "real interface"},
            "fake_tests_allowed_for": [],
        }
    ]
    assert gate.manifest_blocks(classified, manifest, resolved_reports()) == [
        gate.BLOCK_MISSING_DIMENSION
    ]


def test_task4_dimension_faithful_twin_passes_and_bad_waiver_blocks():
    registry = sample_registry()
    classified = [gate.classify_path("engines/new_engine.py", registry)]
    faithful = [
        {
            "component": "engines/new_engine.py",
            "layer": "engine",
            "costly_dimensions": ["latency", "state"],
            "dimensions_considered_and_excluded": [],
            "production_component": "engines/new_engine.py",
            "dimension_preserving_tests": {"latency": "test_latency", "state": "test_state"},
            "dimension_evidence": {"latency": "slow fake", "state": "real state"},
            "fake_tests_allowed_for": [],
        }
    ]
    assert gate.manifest_blocks(classified, faithful, resolved_reports()) == []

    bad_waiver = [dict(faithful[0], costly_dimensions=["latency"])]
    bad_waiver[0]["dimension_preserving_tests"] = {"latency": "test_latency"}
    bad_waiver[0]["dimension_evidence"] = {"latency": "slow fake"}
    bad_waiver[0]["dimensions_considered_and_excluded"] = [
        {
            "dimension": "state",
            "why_not_load_bearing": "not used",
            "waiver_reviewer": "codex",
            "waiver_finding_id": "MISSING",
            "waiver_status": "approved",
        }
    ]
    assert gate.manifest_blocks(classified, bad_waiver, resolved_reports()) == [
        gate.BLOCK_MISSING_DIMENSION
    ]

    good_waiver = [dict(bad_waiver[0])]
    good_waiver[0]["dimensions_considered_and_excluded"] = [
        {
            "dimension": "state",
            "why_not_load_bearing": "not used",
            "waiver_reviewer": "codex",
            "waiver_finding_id": "W1",
            "waiver_status": "approved",
        }
    ]
    assert gate.manifest_blocks(classified, good_waiver, resolved_reports()) == []


def readiness(status_diagnose="absent", status_steer="absent"):
    return {
        "validators": [
            {"validator_id": "diagnose", "mode": "blind", "status": status_diagnose},
            {"validator_id": "diagnose-steer", "mode": "steered", "status": status_steer},
        ]
    }


def modes():
    return {
        "stages": {
            "design-panel": {"mode": "blind", "eligible_validator": "diagnose"},
            "steered-panel": {"mode": "steered", "eligible_validator": "diagnose-steer"},
            "root": {"mode": "external", "eligible_validator": "external-base-case"},
        }
    }


def test_task5_genesis_uses_manual_base_case_without_stale():
    doc = base_phase_input(phase="design-panel", correctness_basis="manual-panel")
    assert gate.basis_available(readiness(), modes(), "HEAD", "design-panel") == "manual-panel"
    assert gate.basis_blocks(doc, readiness(), modes(), "HEAD") == []


def test_task5_upward_and_downward_stale_basis_block():
    doc = base_phase_input(phase="design-panel", correctness_basis="manual-panel")
    assert gate.basis_blocks(doc, readiness(status_diagnose="verified"), modes(), "HEAD") == [
        gate.BLOCK_STALE_BASIS
    ]
    invalidated = base_phase_input(phase="design-panel", correctness_basis="diagnose")
    assert gate.basis_blocks(invalidated, readiness(status_diagnose="invalidated"), modes(), "HEAD") == [
        gate.BLOCK_STALE_BASIS
    ]


def test_task5_per_mode_readiness_blocks_early_steered_upgrade():
    doc = base_phase_input(phase="steered-panel", correctness_basis="diagnose-steer")
    assert gate.basis_blocks(doc, readiness(status_steer="merged-unverified"), modes(), "HEAD") == [
        gate.BLOCK_CIRCULAR_VALIDATOR
    ]


def write_logic_set(repo: Path):
    base = repo / "skills" / "bridge-protocol"
    defect_hunts = repo / "skills" / "defect_hunts"
    (base / "gate" / "schemas").mkdir(parents=True, exist_ok=True)
    defect_hunts.mkdir(parents=True, exist_ok=True)
    (base / "gate" / "gate.py").write_text("print('gate')\n", encoding="utf-8")
    (base / "gate" / "schemas" / "phase_input.json").write_text("{}", encoding="utf-8")
    (base / "SKILL.md").write_text("contract\n", encoding="utf-8")
    (base / "gate" / "root_rules.md").write_text("root rules\n", encoding="utf-8")
    (base / "gate" / "layer_registry.json").write_text('{"data": true}\n', encoding="utf-8")
    (defect_hunts / "h2_assumptions.py").write_text("H2_ASSUMPTIONS = True\n", encoding="utf-8")
    (defect_hunts / "h2_derive.py").write_text("H2_DERIVE = True\n", encoding="utf-8")
    (defect_hunts / "h2_graduation.py").write_text("H2_GRADUATION = True\n", encoding="utf-8")


def test_task6_certified_hash_tracks_logic_set_only(tmp_path):
    write_logic_set(tmp_path)
    original = gate.certified_object_sha(tmp_path)
    (tmp_path / "skills" / "bridge-protocol" / "gate" / "layer_registry.json").write_text(
        '{"data": false}\n', encoding="utf-8"
    )
    assert gate.certified_object_sha(tmp_path) == original

    # SKILL.md left the certified object on 2026-08-08. This assertion used to
    # be `!=`; it encoded the contract under which a documentation edit
    # invalidated the trust root. Kept as `==` rather than deleted so the new
    # contract fails loudly if SKILL.md is ever pulled back in silently.
    (tmp_path / "skills" / "bridge-protocol" / "SKILL.md").write_text("changed\n", encoding="utf-8")
    assert gate.certified_object_sha(tmp_path) == original

    # ...and the rules, which did move in, must still trip it.
    (tmp_path / "skills" / "bridge-protocol" / "gate" / "root_rules.md").write_text(
        "rules changed\n", encoding="utf-8"
    )
    assert gate.certified_object_sha(tmp_path) != original


def test_task6_stale_root_self_cert_and_bad_rotation_block(tmp_path):
    write_logic_set(tmp_path)
    sha = gate.certified_object_sha(tmp_path)
    assert gate.trust_root_blocks(tmp_path, {"certified_object_sha": sha}) == []
    assert gate.trust_root_blocks(tmp_path, {"certified_object_sha": "bad"}) == [gate.BLOCK_STALE_ROOT]

    doc = base_phase_input(**{"skill_under_review": "bridge-protocol"})
    doc["reviewer_reports"][0]["seat"] = "bridge-protocol"
    assert gate.self_cert_blocks(doc) == [gate.BLOCK_SELF_CERT]

    current = {"certified_object_sha": sha, "certifying_seats": ["codex", "cold-Opus"]}
    bad_rotation = {
        "old_sha": sha,
        "new_sha": sha,
        "reason": "update",
        "certifying_seats": ["codex"],
        "human_approver": "mark",
        "change_author": "codex",
        "invalidated_basis_records": ["basis-1"],
    }
    assert gate.rotation_blocks(current, bad_rotation, sha, {"basis-1"}) == [gate.BLOCK_STALE_ROOT]


def test_task7_evaluator_blocks_open_findings_and_escaped_defects():
    doc = base_phase_input()
    doc["reviewer_reports"][0]["findings"][0]["severity"] = "P1"
    doc["reviewer_reports"][0]["findings"][0]["status"] = "open"
    doc["escaped_defect"] = {
        "triggered": True,
        "changelog": False,
        "corpus_row": False,
        "standing_rule": False,
        "state": "open",
    }
    result = eval_unit(doc, ROOT, registry=sample_registry(), manifest=[], changed_paths=[])
    assert result["gate_decision"] == "block"
    assert result["block_reasons"] == [gate.BLOCK_ESCAPED_DEFECT, gate.BLOCK_OPEN_FINDING]


def test_task7_evaluator_covers_core_block_conditions(tmp_path):
    repo = init_repo(tmp_path)
    evidence = make_hard_signal(repo)
    executable = base_phase_input(phase_class="executable", correctness_basis="hard-signal")
    executable["hard_signal_evidence"] = evidence
    assert eval_unit(executable, repo, registry=sample_registry(), manifest=[], changed_paths=[])["gate_decision"] == "pass"

    stale = base_phase_input(phase_class="executable", correctness_basis="hard-signal")
    stale["hard_signal_evidence"] = dict(evidence, commit_sha="0" * 40)
    assert eval_unit(stale, repo, registry=sample_registry(), manifest=[], changed_paths=[])["block_reasons"] == [
        gate.BLOCK_HARD_SIGNAL
    ]

    declarative = base_phase_input(verified=True)
    assert eval_unit(declarative, ROOT, registry=sample_registry(), manifest=[], changed_paths=[])["block_reasons"] == [
        gate.BLOCK_BUILDER_DECISION,
        gate.BLOCK_DECLARATIVE_VERIFIED,
    ]


def test_task7_evaluator_covers_registry_manifest_basis_and_self_cert(tmp_path):
    doc = base_phase_input(phase="design-panel", correctness_basis="manual-panel")
    result = eval_unit(
        doc,
        ROOT,
        registry=sample_registry(),
        manifest=[],
        changed_paths=["engines/new_engine.py"],
        readiness=readiness(status_diagnose="verified"),
        stage_modes=modes(),
    )
    assert result["block_reasons"] == [
        gate.BLOCK_CHEAP_FAKE,
        gate.BLOCK_STALE_BASIS,
    ]

    self_cert = base_phase_input(**{"skill_under_review": "bridge-protocol"})
    self_cert["reviewer_reports"][0]["seat"] = "bridge-protocol"
    assert eval_unit(self_cert, ROOT, registry=sample_registry(), manifest=[], changed_paths=[])["block_reasons"] == [
        gate.BLOCK_SELF_CERT
    ]


def faithful_engine_manifest():
    return [
        {
            "component": "engines/new_engine.py",
            "layer": "engine",
            "costly_dimensions": ["latency", "state", "interface"],
            "dimensions_considered_and_excluded": [],
            "production_component": "engines/new_engine.py",
            "dimension_preserving_tests": {
                "latency": "test_latency",
                "state": "test_state",
                "interface": "test_interface",
            },
            "dimension_evidence": {
                "latency": "slow fake",
                "state": "real state",
                "interface": "real interface",
            },
            "fake_tests_allowed_for": ["unit formatting"],
        }
    ]


def test_task9_cheap_fake_block_and_dimension_faithful_twin_pass():
    doc = base_phase_input()
    blocked = eval_unit(
        doc, ROOT, registry=sample_registry(), manifest=[], changed_paths=["engines/new_engine.py"]
    )
    assert blocked["block_reasons"] == [gate.BLOCK_CHEAP_FAKE]
    passed = eval_unit(
        doc,
        ROOT,
        registry=sample_registry(),
        manifest=faithful_engine_manifest(),
        changed_paths=["engines/new_engine.py"],
    )
    assert passed["gate_decision"] == "pass"


def test_task9_unit_mock_outside_layers_passes():
    result = eval_unit(
        base_phase_input(),
        ROOT,
        registry=sample_registry(),
        manifest=[],
        changed_paths=["tests/test_mock_engine.py"],
    )
    assert result["gate_decision"] == "pass"


def test_task9_costly_dimension_evasion_blocks_and_twin_passes():
    evasion = [dict(faithful_engine_manifest()[0], costly_dimensions=["interface"])]
    evasion[0]["dimension_preserving_tests"] = {"interface": "test_interface"}
    evasion[0]["dimension_evidence"] = {"interface": "real calls"}
    blocked = eval_unit(
        base_phase_input(),
        ROOT,
        registry=sample_registry(),
        manifest=evasion,
        changed_paths=["engines/new_engine.py"],
    )
    assert blocked["block_reasons"] == [gate.BLOCK_MISSING_DIMENSION]
    passed = eval_unit(
        base_phase_input(),
        ROOT,
        registry=sample_registry(),
        manifest=faithful_engine_manifest(),
        changed_paths=["engines/new_engine.py"],
    )
    assert passed["gate_decision"] == "pass"


def test_task9_unclassified_both_directions_and_classified_pass():
    registry = sample_registry()
    assert eval_unit(base_phase_input(), ROOT, registry=registry, manifest=[], changed_paths=["lib/engines.py"])[
        "block_reasons"
    ] == [gate.BLOCK_UNCLASSIFIED]
    assert eval_unit(base_phase_input(), ROOT, registry=registry, manifest=[], changed_paths=["newtop/module.py"])[
        "block_reasons"
    ] == [gate.BLOCK_UNCLASSIFIED]
    assert eval_unit(
        base_phase_input(),
        ROOT,
        registry=registry,
        manifest=faithful_engine_manifest(),
        changed_paths=["engines/new_engine.py"],
    )["gate_decision"] == "pass"


def test_task9_declarative_verified_blocks_and_judged_passes():
    assert eval_unit(base_phase_input(verified=True), ROOT, registry=sample_registry(), manifest=[], changed_paths=[])[
        "block_reasons"
    ] == [gate.BLOCK_BUILDER_DECISION, gate.BLOCK_DECLARATIVE_VERIFIED]
    assert eval_unit(base_phase_input(), ROOT, registry=sample_registry(), manifest=[], changed_paths=[])[
        "gate_decision"
    ] == "pass"


def test_task9_forged_stale_output_blocks_and_fresh_output_passes(tmp_path):
    repo = init_repo(tmp_path)
    evidence = make_hard_signal(repo)
    fresh = base_phase_input(phase_class="executable", correctness_basis="hard-signal")
    fresh["hard_signal_evidence"] = evidence
    assert eval_unit(fresh, repo, registry=sample_registry(), manifest=[], changed_paths=[])["gate_decision"] == "pass"
    forged = base_phase_input(phase_class="executable", correctness_basis="hard-signal")
    forged["hard_signal_evidence"] = dict(evidence, captured_output_sha256="bad")
    assert eval_unit(forged, repo, registry=sample_registry(), manifest=[], changed_paths=[])["block_reasons"] == [
        gate.BLOCK_HARD_SIGNAL
    ]


def test_task9_builder_supplied_decision_blocks():
    assert eval_unit(base_phase_input(gate_decision="pass"), ROOT, registry=sample_registry(), manifest=[], changed_paths=[])[
        "block_reasons"
    ] == [gate.BLOCK_BUILDER_DECISION]


def test_task9_plausible_waiver_passes_and_unresolved_waiver_blocks():
    good_waiver = [dict(faithful_engine_manifest()[0], costly_dimensions=["latency"])]
    good_waiver[0]["dimension_preserving_tests"] = {"latency": "test_latency"}
    good_waiver[0]["dimension_evidence"] = {"latency": "slow fake"}
    good_waiver[0]["dimensions_considered_and_excluded"] = [
        {
            "dimension": "state",
            "why_not_load_bearing": "panel accepted",
            "waiver_reviewer": "codex",
            "waiver_finding_id": "W1",
            "waiver_status": "approved",
        }
    ]
    waiver_doc = base_phase_input()
    waiver_doc["reviewer_reports"] = resolved_reports()
    assert eval_unit(
        waiver_doc,
        ROOT,
        registry=sample_registry(),
        manifest=good_waiver,
        changed_paths=["engines/new_engine.py"],
    )["gate_decision"] == "pass"
    bad = [dict(good_waiver[0])]
    bad[0]["dimensions_considered_and_excluded"] = [dict(good_waiver[0]["dimensions_considered_and_excluded"][0])]
    bad[0]["dimensions_considered_and_excluded"][0]["waiver_finding_id"] = "NOPE"
    assert eval_unit(
        waiver_doc,
        ROOT,
        registry=sample_registry(),
        manifest=bad,
        changed_paths=["engines/new_engine.py"],
    )["block_reasons"] == [gate.BLOCK_MISSING_DIMENSION]


def test_task9_stale_basis_up_down_and_correct_basis_pass():
    assert eval_unit(
        base_phase_input(phase="design-panel", correctness_basis="manual-panel"),
        ROOT,
        registry=sample_registry(),
        manifest=[],
        changed_paths=[],
        readiness=readiness(status_diagnose="verified"),
        stage_modes=modes(),
    )["block_reasons"] == [gate.BLOCK_STALE_BASIS]
    assert eval_unit(
        base_phase_input(phase="design-panel", correctness_basis="diagnose"),
        ROOT,
        registry=sample_registry(),
        manifest=[],
        changed_paths=[],
        readiness=readiness(status_diagnose="invalidated"),
        stage_modes=modes(),
    )["block_reasons"] == [gate.BLOCK_STALE_BASIS]
    assert eval_unit(
        base_phase_input(phase="design-panel", correctness_basis="diagnose"),
        ROOT,
        registry=sample_registry(),
        manifest=[],
        changed_paths=[],
        readiness=readiness(status_diagnose="verified"),
        stage_modes=modes(),
    )["gate_decision"] == "pass"


def test_task9_self_cert_blocks():
    doc = base_phase_input(**{"skill_under_review": "bridge-protocol"})
    doc["reviewer_reports"][0]["seat"] = "bridge-protocol"
    assert eval_unit(doc, ROOT, registry=sample_registry(), manifest=[], changed_paths=[])["block_reasons"] == [
        gate.BLOCK_SELF_CERT
    ]


def test_task9_stale_root_logic_changes_block_but_data_change_passes(tmp_path):
    write_logic_set(tmp_path)
    sha = gate.certified_object_sha(tmp_path)
    root = {"certified_object_sha": sha}
    assert gate.trust_root_blocks(tmp_path, root) == []
    (tmp_path / "skills" / "bridge-protocol" / "gate" / "schemas" / "phase_input.json").write_text(
        '{"changed": true}\n', encoding="utf-8"
    )
    assert gate.trust_root_blocks(tmp_path, root) == [gate.BLOCK_STALE_ROOT]

    write_logic_set(tmp_path)
    sha = gate.certified_object_sha(tmp_path)
    (tmp_path / "skills" / "bridge-protocol" / "gate" / "layer_registry.json").write_text(
        '{"data": "changed"}\n', encoding="utf-8"
    )
    assert gate.trust_root_blocks(tmp_path, {"certified_object_sha": sha}) == []


def test_task9_stale_root_h2_logic_change_blocks_until_repin(tmp_path):
    write_logic_set(tmp_path)
    sha = gate.certified_object_sha(tmp_path)
    root = {"certified_object_sha": sha}
    assert gate.trust_root_blocks(tmp_path, root) == []

    (tmp_path / "skills" / "defect_hunts" / "h2_assumptions.py").write_text(
        "H2_ASSUMPTIONS = False\n", encoding="utf-8"
    )
    new_sha = gate.certified_object_sha(tmp_path)
    assert new_sha != sha
    assert gate.trust_root_blocks(tmp_path, root) == [gate.BLOCK_STALE_ROOT]
    assert gate.trust_root_blocks(tmp_path, {"certified_object_sha": new_sha}) == []


def test_task9_per_mode_circular_blocks_and_manual_fallback_passes():
    assert eval_unit(
        base_phase_input(phase="steered-panel", correctness_basis="diagnose-steer"),
        ROOT,
        registry=sample_registry(),
        manifest=[],
        changed_paths=[],
        readiness=readiness(status_steer="merged-unverified"),
        stage_modes=modes(),
    )["block_reasons"] == [gate.BLOCK_CIRCULAR_VALIDATOR]
    assert eval_unit(
        base_phase_input(phase="steered-panel", correctness_basis="manual-panel"),
        ROOT,
        registry=sample_registry(),
        manifest=[],
        changed_paths=[],
        readiness=readiness(status_steer="absent"),
        stage_modes=modes(),
    )["gate_decision"] == "pass"


def test_task9_open_finding_and_escaped_defect_matched_pairs():
    open_doc = base_phase_input()
    open_doc["reviewer_reports"][0]["findings"][0]["severity"] = "P1"
    open_doc["reviewer_reports"][0]["findings"][0]["status"] = "open"
    assert eval_unit(open_doc, ROOT, registry=sample_registry(), manifest=[], changed_paths=[])["block_reasons"] == [
        gate.BLOCK_OPEN_FINDING
    ]
    resolved = base_phase_input()
    resolved["reviewer_reports"][0]["findings"][0]["severity"] = "P1"
    resolved["reviewer_reports"][0]["findings"][0]["status"] = "resolved"
    assert eval_unit(resolved, ROOT, registry=sample_registry(), manifest=[], changed_paths=[])["gate_decision"] == "pass"

    escaped = base_phase_input(
        escaped_defect={
            "triggered": True,
            "changelog": False,
            "corpus_row": False,
            "standing_rule": False,
            "state": "open",
        }
    )
    assert eval_unit(escaped, ROOT, registry=sample_registry(), manifest=[], changed_paths=[])["block_reasons"] == [
        gate.BLOCK_ESCAPED_DEFECT
    ]
    fixed = base_phase_input(
        escaped_defect={
            "triggered": True,
            "changelog": True,
            "corpus_row": True,
            "standing_rule": "n/a",
            "state": "fixed",
        }
    )
    assert eval_unit(fixed, ROOT, registry=sample_registry(), manifest=[], changed_paths=[])["gate_decision"] == "pass"


def test_default_evaluate_loads_trust_root_and_blocks_logic_drift(tmp_path):
    repo = init_bridge_repo(tmp_path)
    (repo / "docs" / "note.md").write_text("doc only\n", encoding="utf-8")
    commit_all(repo, "doc only")
    assert gate.evaluate(default_phase(), repo)["gate_decision"] == "pass"

    (repo / "skills" / "bridge-protocol" / "gate" / "gate.py").write_text("print('changed')\n", encoding="utf-8")
    commit_all(repo, "drift gate")
    result = gate.evaluate(default_phase(), repo)
    assert gate.BLOCK_STALE_ROOT in result["block_reasons"]


def test_default_evaluate_uses_real_diff_for_unclassified_and_classified_paths(tmp_path):
    repo = init_bridge_repo(tmp_path)
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, check=True, stdout=subprocess.PIPE)
    (repo / "lib").mkdir()
    (repo / "lib" / "engines.py").write_text("class Engine: pass\n", encoding="utf-8")
    commit_all(repo, "unclassified production")
    result = gate.evaluate(default_phase(), repo)
    assert result["block_reasons"] == [gate.BLOCK_UNCLASSIFIED]

    repo2 = init_bridge_repo(tmp_path / "classified")
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo2, check=True, stdout=subprocess.PIPE)
    (repo2 / "tests").mkdir()
    (repo2 / "tests" / "test_mock_engine.py").write_text("class MockEngine: pass\n", encoding="utf-8")
    commit_all(repo2, "test only")
    assert gate.evaluate(default_phase(), repo2)["gate_decision"] == "pass"


def test_default_evaluate_uses_real_diff_for_manifest_miss_and_faithful_twin(tmp_path):
    repo = init_bridge_repo(tmp_path)
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, check=True, stdout=subprocess.PIPE)
    (repo / "engines").mkdir()
    (repo / "engines" / "new_engine.py").write_text("class Engine: pass\n", encoding="utf-8")
    commit_all(repo, "engine without manifest")
    result = gate.evaluate(default_phase(), repo)
    assert result["block_reasons"] == [gate.BLOCK_CHEAP_FAKE]

    repo2 = init_bridge_repo(tmp_path / "faithful")
    (repo2 / "manifest.json").write_text(json.dumps(faithful_engine_manifest()), encoding="utf-8")
    subprocess.run(["git", "add", "manifest.json"], cwd=repo2, check=True)
    subprocess.run(["git", "commit", "--amend", "--no-edit"], cwd=repo2, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo2, check=True, stdout=subprocess.PIPE)
    (repo2 / "engines").mkdir()
    (repo2 / "engines" / "new_engine.py").write_text("class Engine: pass\n", encoding="utf-8")
    commit_all(repo2, "engine with manifest")
    assert gate.evaluate(default_phase(), repo2)["gate_decision"] == "pass"


def test_default_evaluate_missing_required_artifacts_fail_closed(tmp_path):
    repo = init_bridge_repo(tmp_path)
    (repo / "skills" / "bridge-protocol" / "gate" / "trust_root.json").unlink()
    commit_all(repo, "remove trust root")
    assert gate.evaluate(default_phase(), repo)["block_reasons"] == [gate.BLOCK_STALE_ROOT]

    repo2 = init_bridge_repo(tmp_path / "missing-readiness")
    (repo2 / "skills" / "bridge-protocol" / "gate" / "validator_readiness.json").unlink()
    commit_all(repo2, "remove readiness")
    result = gate.evaluate(default_phase(), repo2)
    assert gate.BLOCK_SETUP in result["block_reasons"]

    repo3 = init_bridge_repo(tmp_path / "missing-stage-modes")
    (repo3 / "skills" / "bridge-protocol" / "gate" / "stage_modes.json").unlink()
    commit_all(repo3, "remove stage modes")
    assert gate.BLOCK_SETUP in gate.evaluate(default_phase(), repo3)["block_reasons"]

    repo4 = init_bridge_repo(tmp_path / "missing-registry")
    (repo4 / "skills" / "bridge-protocol" / "gate" / "layer_registry.json").unlink()
    commit_all(repo4, "remove registry")
    assert gate.BLOCK_SETUP in gate.evaluate(default_phase(), repo4)["block_reasons"]


def test_default_evaluate_diff_scope_includes_non_tip_feature_commits(tmp_path):
    repo = init_bridge_repo(tmp_path)
    subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, check=True, stdout=subprocess.PIPE)
    (repo / "lib").mkdir()
    (repo / "lib" / "engines.py").write_text("class Engine: pass\n", encoding="utf-8")
    commit_all(repo, "non-tip production")
    (repo / "docs" / "later.md").write_text("tip docs\n", encoding="utf-8")
    commit_all(repo, "tip docs")

    result = gate.evaluate(default_phase(target_branch="main"), repo)
    assert result["block_reasons"] == [gate.BLOCK_UNCLASSIFIED]


def test_partial_injection_still_loads_trust_root_and_readiness_by_default(tmp_path):
    repo = init_bridge_repo(tmp_path)
    (repo / "skills" / "bridge-protocol" / "gate" / "gate.py").write_text("print('changed')\n", encoding="utf-8")
    commit_all(repo, "drift gate")

    result = gate.evaluate(default_phase(), repo, registry=sample_registry(), changed_paths=[])
    assert gate.BLOCK_STALE_ROOT in result["block_reasons"]


def test_registry_scopes_adapter_rule_to_gate_code_not_schema_or_data():
    registry = gate.read_json(ROOT / "skills/bridge-protocol/gate/layer_registry.json")
    assert eval_unit(
        base_phase_input(),
        ROOT,
        registry=registry,
        manifest=[],
        changed_paths=["skills/bridge-protocol/gate/schemas/phase_input.json"],
    )["gate_decision"] == "pass"
    assert eval_unit(
        base_phase_input(),
        ROOT,
        registry=registry,
        manifest=[],
        changed_paths=["skills/bridge-protocol/gate/layer_registry.json"],
    )["gate_decision"] == "pass"
    assert eval_unit(
        base_phase_input(),
        ROOT,
        registry=registry,
        manifest=[],
        changed_paths=["skills/bridge-protocol/gate/gate.py"],
    )["block_reasons"] == [gate.BLOCK_CHEAP_FAKE]


def test_bridge_protocol_passes_its_own_gate_default_path():
    result = gate.evaluate(
        default_phase(
            phase="bridge-protocol-root",
            correctness_basis="external-base-case",
            manifest_ref="skills/bridge-protocol/gate/load_bearing_components.json",
            target_branch="main",
        ),
        ROOT,
    )
    assert result["gate_decision"] == "pass"


def test_live_panel_files_have_real_gate_manifest_coverage():
    registry = gate.read_json(ROOT / "skills/bridge-protocol/gate/layer_registry.json")
    manifest = gate.read_json(ROOT / "skills/bridge-protocol/gate/load_bearing_components.json")
    expected_tests = {
        "skills/_diagnose_common/canonical.py": {
            "byte-form-determinism": "tests/test_diagnose_common.py::test_canonical_bytes_env_invariant",
            "neutrality": "tests/test_diagnose_common.py::test_c0_neutrality_guard_scans_all_common_modules",
        },
        "skills/diagnose/containment.py": {
            "integrated-run": "tests/test_diagnose.py::test_d1_scope_derives_from_recorded_traceback_and_live_run_passes",
            "test-containment": "tests/test_diagnose_containment.py::test_hanging_test_and_children_reaped",
        },
        "skills/diagnose/briefs.py": {
            "integrated-run": "tests/test_diagnose.py::test_run_diagnose_authors_live_panel_artifacts_and_recompute_basis",
        },
        "skills/diagnose/panel.py": {
            "integrated-run": "tests/test_diagnose.py::test_run_panel_records_bus_reply_consistency_and_writes_outside_repo",
        },
    }
    classified, class_blocks = gate.classify_changes(list(expected_tests), registry)
    assert class_blocks == []
    assert gate.manifest_blocks(classified, manifest, resolved_reports()) == []
    for path, tests in expected_tests.items():
        entry = next(item for item in manifest if item["component"] == path)
        for dimension, test_name in tests.items():
            assert entry["dimension_preserving_tests"][dimension] == test_name
    assert gate.classify_path("skills/diagnose-steer/confidence_constants.json", registry)["production"] is False


def make_diagnose_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "diagnose-repo"
    (repo / "app").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "app" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "app" / "core.py").write_text("def compute():\n    return 1\n", encoding="utf-8")
    (repo / "tests" / "test_core.py").write_text(
        "from app.core import compute\n\n"
        "def test_compute():\n"
        "    assert compute() == 2\n",
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
    return repo, subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()


def diagnose_authorship_record(tmp_path: Path):
    from skills.diagnose.briefs import author_briefs, author_post_briefs

    repo, repo_sha = make_diagnose_repo(tmp_path)
    constants = json.loads((ROOT / "skills/diagnose/panel_constants.json").read_text(encoding="utf-8"))
    constants["_repo_root"] = str(repo)
    recorded_traceback_raw = {
        "reproduced": True,
        "window": {"start": 3, "end": 4},
        "traceback": "Traceback\n  File \"tests/test_core.py\", line 4, in test_compute\n",
        "blocking": None,
    }
    sealed_briefs = author_briefs("tests/test_core.py::test_compute", repo_sha, recorded_traceback_raw, constants)
    submissions = []
    bus_records = []
    for brief in sealed_briefs:
        reply = f"reply for {brief['role']}"
        digest = hashlib.sha256(reply.encode("utf-8")).hexdigest()
        submissions.append(
            {
                "role": brief["role"],
                "seat": brief["role"],
                "seal": brief["seal"],
                "bus_reply_ref": f"bus://{brief['role']}",
                "bus_reply_sha256": digest,
            }
        )
        bus_records.append({"ref": f"bus://{brief['role']}", "reply": reply, "sha256": digest})
    post_briefs = author_post_briefs(constants, submissions)
    run_record = {
        "trigger": {"failing_test": "tests/test_core.py::test_compute"},
        "repo_sha": repo_sha,
        "repo_root": str(repo),
        "recorded_traceback": {
            "reproduced": True,
            "window": {"start": 3, "end": 4},
            "traceback_sha256": hashlib.sha256(recorded_traceback_raw["traceback"].encode("utf-8")).hexdigest(),
        },
        "sealed_briefs": sealed_briefs,
        "submissions": submissions,
        "post_briefs": post_briefs,
    }
    return run_record, bus_records


def test_gate_passes_clean_consistent_run(tmp_path):
    run_record, bus_records = diagnose_authorship_record(tmp_path)
    assert gate.brief_authorship_blocks(run_record, bus_records) == []


requires_sandbox_exec = pytest.mark.skipif(
    shutil.which("sandbox-exec") is None,
    reason="diagnose containment uses macOS sandbox-exec; unavailable hosts fail-closed with test-containment-unavailable",
)


@requires_sandbox_exec
def test_gate_validates_real_run_diagnose_output(tmp_path, monkeypatch):
    import skills.diagnose.diagnose as diagnose

    target_ids = {
        "blind": "codex-bridge-dev-example",
        "alternative": "agy-bridge-dev",
        "open": "pi-sdk-bridge-dev-minimax-m3",
        "scribe": "scribe",
    }

    def fake_dispatch(brief):
        return {"model": brief["model"], "from": target_ids[brief["role"]], "reply": f"reply for {brief['role']}"}

    repo, _repo_sha = make_diagnose_repo(tmp_path)
    run_record, reasons = diagnose.run_diagnose(
        repo,
        {"failing_test": "tests/test_core.py::test_compute"},
        tmp_path / "work",
        dispatch=fake_dispatch,
    )
    bus_records = []
    for submission in run_record["submissions"]:
        path = Path(submission["bus_reply_ref"].removeprefix("file://"))
        reply = path.read_text(encoding="utf-8")
        bus_records.append({"ref": submission["bus_reply_ref"], "reply": reply, "sha256": hashlib.sha256(reply.encode("utf-8")).hexdigest()})

    assert reasons == []
    assert gate.brief_authorship_blocks(run_record, bus_records) == []


def test_gate_blocks_transit_tamper(tmp_path):
    run_record, bus_records = diagnose_authorship_record(tmp_path)
    run_record["sealed_briefs"][0]["seal"] = "0" * 64
    assert gate.brief_authorship_blocks(run_record, bus_records) == ["brief-tampered"]


def test_gate_blocks_contaminated_trigger_authorship(tmp_path):
    from skills._diagnose_common import seal

    run_record, bus_records = diagnose_authorship_record(tmp_path)
    run_record["sealed_briefs"][0]["brief"]["observables"][0]["path"] = "app/contaminated.py"
    for brief in run_record["sealed_briefs"]:
        brief["seal"] = seal(brief["brief"])
    assert gate.brief_authorship_blocks(run_record, bus_records) == ["brief-not-skill-authored"]


def test_gate_blocks_swapped_traceback(tmp_path):
    run_record, bus_records = diagnose_authorship_record(tmp_path)
    run_record["recorded_traceback"]["window"] = {"start": 1, "end": 1}
    assert gate.brief_authorship_blocks(run_record, bus_records) == ["brief-not-skill-authored"]


def test_gate_blocks_doctored_tree_authorship(tmp_path):
    from skills._diagnose_common import seal

    run_record, bus_records = diagnose_authorship_record(tmp_path)
    repo = Path(run_record["repo_root"])
    (repo / "app" / "core.py").write_text("def compute():\n    return 999\n", encoding="utf-8")
    doctored_observables = []
    for observable in run_record["sealed_briefs"][0]["brief"]["observables"]:
        rel_path = observable["path"]
        content = (repo / rel_path).read_bytes()
        doctored_observables.append(
            {
                "id": observable["id"],
                "path": rel_path,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
        )
    for brief in run_record["sealed_briefs"]:
        brief["brief"]["observables"] = doctored_observables
        brief["seal"] = seal(brief["brief"])

    assert gate.brief_authorship_blocks(run_record, bus_records) == ["brief-not-skill-authored"]


def test_gate_repo_advance_keeps_original_repo_sha_load_bearing(tmp_path):
    run_record, bus_records = diagnose_authorship_record(tmp_path)
    repo = Path(run_record["repo_root"])
    (repo / "app" / "core.py").write_text("def compute():\n    return 999\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(
        ["git", "commit", "-m", "advance"],
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

    assert gate.brief_authorship_blocks(run_record, bus_records) == []


def test_gate_blocks_submission_inconsistent_with_supplied_bus_records(tmp_path):
    run_record, bus_records = diagnose_authorship_record(tmp_path)
    bus_records[0]["sha256"] = "f" * 64
    bus_records[0]["reply"] = "mismatched"
    assert gate.brief_authorship_blocks(run_record, bus_records) == ["submission-inconsistent"]


def test_evaluate_wires_brief_authorship_blocks(tmp_path):
    run_record, bus_records = diagnose_authorship_record(tmp_path)
    run_record["sealed_briefs"][0]["seal"] = "0" * 64
    result = eval_unit(
        base_phase_input(run_record=run_record, bus_records=bus_records),
        ROOT,
        registry=sample_registry(),
        manifest=[],
        changed_paths=[],
    )
    assert "brief-tampered" in result["block_reasons"]


def test_git_review_diff_tolerates_non_utf8_diff_bytes(tmp_path):
    repo = init_repo(tmp_path)
    subprocess.run(["git", "branch", "-M", "main"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "review"], cwd=repo, check=True)
    # Latin-1 bytes: invalid UTF-8, but no NUL, so git diffs them as text and
    # emits the raw bytes on stdout (the committed-.enc-battery incident shape).
    (repo / "battery.enc").write_bytes(b"caf\xe9 s\xe9curit\xe9\n")
    commit_all(repo, "non-utf8 payload")
    diff = gate.git_review_diff(repo, "main")
    assert "battery.enc" in diff
    assert "�" in diff


# ---------------------------------------------------------------------------
# 2026-08-08. The certified object no longer contains SKILL.md's narrative, and
# the gate is documented as a DRIFT DETECTOR rather than an authorisation
# mechanism. The tests below encode the honest LIMIT as well as the behaviour,
# because the limit is the part that gets quietly "fixed" by someone who has
# not read why. Two external panels landed here:
#   panel-gaterotate-20260808T045453Z-4acada    (blocked: rotation unenforced)
#   panel-gaterotate-r2-20260808T111742Z-fdbcb3 (blocked: wiring was a bypass)
# ---------------------------------------------------------------------------


def test_split_certified_object_covers_root_rules_not_skill_narrative(tmp_path):
    write_logic_set(tmp_path)
    paths = [p.relative_to(tmp_path).as_posix() for p in gate.logic_set_paths(tmp_path)]
    assert "skills/bridge-protocol/gate/root_rules.md" in paths
    assert "skills/bridge-protocol/SKILL.md" not in paths


def test_documented_certified_paths_match_the_real_logic_set():
    """The shipped root_rules.md must list EVERY certified path.

    The pre-split text claimed three (gate.py, schemas/*, SKILL.md) while
    logic_set_paths returned thirteen -- the defect_hunts/h2_*.py files were
    certified and undocumented. Found by grok-bridge-dev in round 2. A certified
    rules file that understates its own scope is a claim stronger than the thing
    it describes, so this asserts they cannot drift apart again.
    """
    rules = (ROOT / "skills" / "bridge-protocol" / "gate" / "root_rules.md").read_text(
        encoding="utf-8"
    )
    documented = set()
    for line in rules.splitlines():
        line = line.strip()
        if line.startswith("- `") and line.endswith("`"):
            rel = line[3:-1].replace("../", "skills/")
            if not rel.startswith("skills/"):
                rel = "skills/bridge-protocol/" + rel
            documented.add(rel)
    actual = {p.relative_to(ROOT).as_posix() for p in gate.logic_set_paths(ROOT)}
    assert documented == actual, (
        f"root_rules.md and logic_set_paths disagree\\n"
        f"  documented only: {sorted(documented - actual)}\\n"
        f"  certified only : {sorted(actual - documented)}"
    )


def test_gate_is_a_drift_detector_not_an_authoriser(tmp_path):
    """CHARACTERISATION TEST — asserts a LIMIT, not a feature.

    Editing certified_object_sha to the running value makes the gate pass, with
    no review, no seats and no approver. This is deliberately pinned so nobody
    mistakes a green gate for evidence that a rotation was authorised.

    It is not a defect to be patched. Every input the gate reads lives in the
    repo being changed and is writable by whoever is changing it, so a check
    added to prevent this would be editable the same way. If this test ever
    starts failing, the fix is NOT to make it pass -- it is to work out what
    changed and whether the new claim is actually true.
    """
    write_logic_set(tmp_path)
    running = gate.certified_object_sha(tmp_path)
    assert gate.trust_root_blocks(tmp_path, {"certified_object_sha": "anything-else"}) == [
        gate.BLOCK_STALE_ROOT
    ]
    # one unreviewed edit, and the gate is satisfied
    assert gate.trust_root_blocks(tmp_path, {"certified_object_sha": running}) == []


def test_rotation_blocks_is_deliberately_not_on_the_gate_path(tmp_path):
    """ANTI-REGRESSION for a design decision, not for a behaviour.

    rotation_blocks is a checklist for humans running a rotation. Wiring it into
    evaluate() looks like an obvious improvement and is not: it was tried on
    2026-08-08 and blocked with two P0s -- it added a second way to pass without
    closing the first (hand-editing the sha still worked), and it consumed
    unvalidated JSON, so `certifying_seats: "fabricated-seat"` became a set of
    characters that trivially satisfied the disjointness clause.

    If you are here because you want to wire it in: the problem is not
    validation strength. It is that the record lives in the repo being changed.
    Solve that first -- e.g. verify against the external panel audit trail --
    and then delete this test with a reason.
    """
    import inspect

    source = inspect.getsource(gate.evaluate)
    assert "rotation_blocks" not in source, (
        "rotation_blocks has been wired into evaluate(); see this test's docstring "
        "for why that was reverted once already"
    )
    # and it still works as a standalone checklist
    write_logic_set(tmp_path)
    sha = gate.certified_object_sha(tmp_path)
    current = {"certified_object_sha": sha, "certifying_seats": ["seat-a"]}
    good = {
        "old_sha": sha, "new_sha": "next", "reason": "r",
        "certifying_seats": ["seat-b"], "human_approver": "mark",
        "change_author": "someone-else", "invalidated_basis_records": [],
    }
    assert gate.rotation_blocks(current, good, "next") == []
    overlapping = dict(good, certifying_seats=["seat-a"])
    assert gate.rotation_blocks(current, overlapping, "next") == [gate.BLOCK_STALE_ROOT]


# ---------------------------------------------------------------------------
# 2026-08-08, found by the muse seat on its first live review turn -- after two
# panels (5 seats total) and the orchestrator had all looked at this function.
#
# The reframing is the useful part. sol's r2 P0 was "unvalidated JSON lets a
# fabricated record pass the GATE". Removing the wiring closed the gate path and
# I treated the finding as closed. It was not: rotation_blocks is now explicitly
# a HUMAN CHECKLIST, so the same malformed input gives a person a false green
# while they are deciding whether to move a trust root. The severity changed;
# the bug did not go away.
# ---------------------------------------------------------------------------


def _rot(**over):
    doc = {
        "old_sha": "x", "new_sha": "y", "reason": "r",
        "certifying_seats": ["seat-b"], "human_approver": "mark",
        "change_author": "someone-else", "invalidated_basis_records": [],
    }
    doc.update(over)
    return doc


CURRENT_ROOT = {"certified_object_sha": "x", "certifying_seats": ["seat-a"]}


def test_rotation_blocks_rejects_string_typed_certifying_seats():
    """`set("fabricated-seat")` is a set of CHARACTERS, which cannot intersect
    real seat ids, so the anti-rubber-stamp clause passed vacuously."""
    assert gate.rotation_blocks(
        CURRENT_ROOT, _rot(certifying_seats="fabricated-seat"), "y"
    ) == [gate.BLOCK_STALE_ROOT]


def test_rotation_blocks_rejects_non_string_seat_entries():
    assert gate.rotation_blocks(
        CURRENT_ROOT, _rot(certifying_seats=["seat-b", 7]), "y"
    ) == [gate.BLOCK_STALE_ROOT]


def test_rotation_blocks_fails_closed_on_null_seats_rather_than_raising():
    """JSON null -> None -> set(None) raised TypeError. A checklist that crashes
    tells the human nothing; it must return a block."""
    assert gate.rotation_blocks(
        CURRENT_ROOT, _rot(certifying_seats=None), "y"
    ) == [gate.BLOCK_STALE_ROOT]


def test_rotation_blocks_fails_closed_on_malformed_current_root_seats():
    """The prior roster is half of the disjointness check. If it is malformed we
    cannot evaluate disjointness at all, so block rather than assume empty."""
    bad_root = {"certified_object_sha": "x", "certifying_seats": None}
    assert gate.rotation_blocks(bad_root, _rot(), "y") == [gate.BLOCK_STALE_ROOT]
    bad_root2 = {"certified_object_sha": "x", "certifying_seats": "seat-a"}
    assert gate.rotation_blocks(bad_root2, _rot(), "y") == [gate.BLOCK_STALE_ROOT]


def test_rotation_blocks_tolerates_a_root_with_no_recorded_seats():
    """Absence is not malformation: a bootstrap root has nothing to be disjoint
    from. Distinguished from null so the fix does not over-block."""
    assert gate.rotation_blocks({"certified_object_sha": "x"}, _rot(), "y") == []


def test_rotation_blocks_fails_closed_on_malformed_invalidated_records():
    assert gate.rotation_blocks(
        CURRENT_ROOT, _rot(invalidated_basis_records="basis-1"), "y"
    ) == [gate.BLOCK_STALE_ROOT]
    assert gate.rotation_blocks(
        CURRENT_ROOT, _rot(invalidated_basis_records=None), "y"
    ) == [gate.BLOCK_STALE_ROOT]


def test_rotation_blocks_still_passes_a_well_formed_record():
    """The point of a fail-closed change is that it must not block valid input."""
    assert gate.rotation_blocks(CURRENT_ROOT, _rot(), "y") == []


# ---------------------------------------------------------------------------
# human_approver: the sentinel hole, open since round 1.
#
# `if not rotation.get("human_approver")` is bare truthiness, so every string
# below is "present" to the checklist. Round 1 of the rotation panel found
# "PENDING-MARK-COSIGN"; round 3's grok seat additionally found that a
# whitespace-only value passes, because " " is truthy in Python.
#
# What this rejects is the OBVIOUS MISTAKE -- a draft record promoted with its
# approver field never filled in. It does NOT establish that a human was
# involved, and nothing in this repository can: the field is a string in a file
# writable by whoever is making the change. Keep that distinction in the
# docstring, not just here.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "PENDING-MARK-COSIGN",   # round 1's actual finding
        "pending",
        "PENDING ANOTHER PANEL",
        "TODO: ask mark",
        "todo-fill-this-in",
        "TBD",
        "tbd - waiting",
        "none",
        "NOBODY",
        "n/a",
        "placeholder",
        "-",
        "   ",                   # round 3 / grok: whitespace is truthy
        "\t\n",
    ],
)
def test_rotation_blocks_rejects_placeholder_human_approver(value):
    assert gate.rotation_blocks(
        CURRENT_ROOT, _rot(human_approver=value), "y"
    ) == [gate.BLOCK_STALE_ROOT]


@pytest.mark.parametrize(
    "value",
    [
        "\u200b",          # ZERO WIDTH SPACE            (Cf) — round 4, cline
        "\ufeff",          # BOM                          (Cf)
        "\u200d",          # ZERO WIDTH JOINER            (Cf)
        "\ufe0f",          # VARIATION SELECTOR-16        (Mn) — round 5, cline
        "\u0301",          # COMBINING ACUTE ACCENT       (Mn)
        "\u20e3",          # COMBINING ENCLOSING KEYCAP   (Me)
        "\u3164",          # HANGUL FILLER                (Lo) — round 5, grok
        "\u2800",          # BRAILLE PATTERN BLANK        (So) — round 5, grok
    ],
)
def test_an_invisible_only_human_approver_is_OUT_OF_SCOPE_and_passes(value):
    """Pins a DELIBERATE NON-GOAL, not a defect. Decided 2026-08-14.

    Every value here renders as nothing and is accepted by
    `_approver_is_placeholder`. Rounds 4 and 5 found them across four Unicode
    categories — Cf, Mn, Me, Lo, So — and an earlier revision tried to exclude
    them by category and called the rule "visibility", which four panel seats
    correctly rejected as prose claiming more than the code enforced.

    The scope was narrowed instead of the enumeration extended. The accident
    this function exists to catch is a field never filled in, which produces ""
    or a sentinel. Against a deliberate actor the function is worthless either
    way: human_approver is a string in a file writable by whoever is making the
    change, so faking approval means typing a name — easier than any invisible
    trick, and undetectable by construction.

    If you are here because you want to make these block: that is a decision to
    re-take, not a bug to fix. Read the docstring on `_approver_is_placeholder`
    first, and note that no rule over Unicode categories closes this class.
    """
    assert gate.rotation_blocks(CURRENT_ROOT, _rot(human_approver=value), "y") == []


@pytest.mark.parametrize("value", [None, True, 1, ["mark"], {"name": "mark"}])
def test_rotation_blocks_fails_closed_on_non_string_human_approver(value):
    """`True` and `1` are truthy and would pass a bare check. A field that is
    not a name cannot be a name, so it must block rather than be coerced."""
    assert gate.rotation_blocks(
        CURRENT_ROOT, _rot(human_approver=value), "y"
    ) == [gate.BLOCK_STALE_ROOT]


@pytest.mark.parametrize(
    "value",
    [
        "mark",
        "Mark Gerrard",
        "  mark  ",       # surrounding whitespace is not a placeholder
        "Todor Penev",    # a bare `todo*` prefix rule would reject a real name
        "Tbdias",         # likewise `tbd*`
        "Nonso Okafor",   # likewise an exact-match rule applied as a prefix
        "Mark\u200bGerrard",  # an invisible INSIDE a real name must not empty it
        "Mårten Öhman",   # non-ASCII letters are visible and must survive
    ],
)
def test_rotation_blocks_does_not_over_block_real_approver_names(value):
    """A fail-closed change that rejects valid input is its own defect. The
    sentinels are matched whole, or followed by a separator -- never as a bare
    prefix, which would reject people whose names start with one."""
    assert gate.rotation_blocks(CURRENT_ROOT, _rot(human_approver=value), "y") == []
