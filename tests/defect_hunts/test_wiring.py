from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
GATE_PATH = ROOT / "skills" / "bridge-protocol" / "gate" / "gate.py"
spec = importlib.util.spec_from_file_location("bridge_protocol_gate_wiring", GATE_PATH)
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


def test_h1_standing_check_surfaces_audit_prefix_flag_through_gate(tmp_path):
    repo = tmp_path
    pkg = repo / "pkg"
    pkg.mkdir()
    (pkg / "bus.py").write_text(
        "import os\n\nPREFIX = os.environ.get('ARB_MEMORY_PREFIX', '')\n",
        encoding="utf-8",
    )
    (pkg / "audit.py").write_text("PREFIX = ''\n", encoding="utf-8")
    diff = """diff --git a/pkg/bus.py b/pkg/bus.py
--- a/pkg/bus.py
+++ b/pkg/bus.py
@@ -1 +1,3 @@
+import os
+PREFIX = os.environ.get('ARB_MEMORY_PREFIX', '')
"""

    findings = gate.h1_standing_check(repo, ["pkg/bus.py"], diff)

    assert findings == [
        {
            "id": "H1-config-drift",
            "severity": "P1",
            "file_line": "pkg/audit.py:PREFIX",
            "fix": "Run H1 config-drift follow-up for pkg/audit.py:PREFIX",
            "status": "open",
            "kind": "H1",
            "evidence": 'PREFIX literal sibling equals env default "" for "ARB_MEMORY_PREFIX"',
            "scope": "same-directory .py siblings only; cross-package same-named literals are not scanned",
        }
    ]

    result = gate.evaluate(
        base_phase_input(),
        repo,
        registry=None,
        manifest=[],
        changed_paths=["pkg/bus.py"],
        review_diff=diff,
        readiness=None,
        stage_modes=None,
        trust_root=None,
    )
    assert result["gate_decision"] == "block"
    assert gate.BLOCK_H1_STANDING_CHECK in result["block_reasons"]
    assert result["standing_check_findings"] == findings


def test_h1_standing_check_clean_co_move_surfaces_no_flag(tmp_path):
    repo = tmp_path
    pkg = repo / "pkg"
    pkg.mkdir()
    (pkg / "bus.py").write_text(
        "import os\n\nPREFIX = os.environ.get('ARB_MEMORY_PREFIX', '')\n",
        encoding="utf-8",
    )
    (pkg / "audit.py").write_text(
        "import os\n\nPREFIX = os.environ.get('ARB_MEMORY_PREFIX', '')\n",
        encoding="utf-8",
    )
    diff = """diff --git a/pkg/bus.py b/pkg/bus.py
--- a/pkg/bus.py
+++ b/pkg/bus.py
@@ -1 +1,3 @@
+import os
+PREFIX = os.environ.get('ARB_MEMORY_PREFIX', '')
"""

    assert gate.h1_standing_check(repo, ["pkg/bus.py"], diff) == []


def test_h2_incomplete_section_rejects_and_complete_anchored_section_passes(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "service.py").write_text(
        "import subprocess\n\nRESULT = subprocess.run(['true'])\n",
        encoding="utf-8",
    )
    anchor = tmp_path / "tests" / "test_assumption.py"
    anchor.parent.mkdir()
    anchor.write_text("def test_default_config_contract():\n    pass\n", encoding="utf-8")
    diff = """diff --git a/pkg/service.py b/pkg/service.py
--- a/pkg/service.py
+++ b/pkg/service.py
@@ -1 +1,3 @@
+import subprocess
+RESULT = subprocess.run(['true'])
"""

    incomplete = base_phase_input(
        phase="bridge-protocol-root",
        h2_section={
            "coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []},
            "rows": [
                {
                    "candidate_id": "external:pkg/service.py:subprocess.run#1",
                    "disposition": "answered",
                    "evidence": "tests/test_assumption.py::test_default_config_contract",
                }
            ],
        }
    )
    rejected = gate.evaluate(
        incomplete,
        tmp_path,
        registry=None,
        manifest=[],
        changed_paths=["pkg/service.py"],
        review_diff=diff,
        readiness=None,
        stage_modes=None,
        trust_root=None,
    )
    assert rejected["gate_decision"] == "block"
    assert gate.BLOCK_H2_STANDING_CHECK in rejected["block_reasons"]
    assert rejected["h2_status"] == "shadow"

    complete = base_phase_input(
        phase="bridge-protocol-root",
        h2_section={
            "coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []},
            "rows": [
                {
                    "candidate_id": "external:pkg/service.py:subprocess.run#1",
                    "disposition": "answered",
                    "violating_run": "pytest tests/test_assumption.py::test_default_config_contract with dependency absent",
                    "evidence": "tests/test_assumption.py::test_default_config_contract",
                }
            ],
        }
    )
    accepted = gate.evaluate(
        complete,
        tmp_path,
        registry=None,
        manifest=[],
        changed_paths=["pkg/service.py"],
        review_diff=diff,
        readiness=None,
        stage_modes=None,
        trust_root=None,
    )
    assert accepted["gate_decision"] == "pass"
    assert accepted["h2_status"] == "enforced"
    assert accepted["h2_record"].derived == ["external:pkg/service.py:subprocess.run#1"]


def test_unanswered_candidate_notices_in_shadow_not_blocks(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "service.py").write_text(
        "import redis\n\nCLIENT = redis.from_url('redis://localhost')\n",
        encoding="utf-8",
    )
    diff = """diff --git a/pkg/service.py b/pkg/service.py
--- a/pkg/service.py
+++ b/pkg/service.py
@@ -1 +1,3 @@
+import redis
+CLIENT = redis.from_url('redis://localhost')
"""

    result = gate.evaluate(
        base_phase_input(phase="bridge-protocol-root"),
        tmp_path,
        registry=None,
        manifest=[],
        changed_paths=["pkg/service.py"],
        review_diff=diff,
        readiness=None,
        stage_modes=None,
        trust_root=None,
    )

    assert result["gate_decision"] == "pass"
    assert gate.BLOCK_H2_STANDING_CHECK not in result["block_reasons"]
    assert result["h2_status"] == "shadow"
    assert result["h2_notice"] == "H2 derived candidates were not disposed in h2_section"
    assert result["h2_record"].derived == ["redis:pkg/service.py:redis.from_url#1"]


def test_unanswered_candidate_blocks_in_block_mode(tmp_path, monkeypatch):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "service.py").write_text(
        "import redis\n\nCLIENT = redis.from_url('redis://localhost')\n",
        encoding="utf-8",
    )
    diff = """diff --git a/pkg/service.py b/pkg/service.py
--- a/pkg/service.py
+++ b/pkg/service.py
@@ -1 +1,3 @@
+import redis
+CLIENT = redis.from_url('redis://localhost')
"""
    monkeypatch.setattr(gate, "H2_MODE", "block")

    result = gate.evaluate(
        base_phase_input(phase="bridge-protocol-root"),
        tmp_path,
        registry=None,
        manifest=[],
        changed_paths=["pkg/service.py"],
        review_diff=diff,
        readiness=None,
        stage_modes=None,
        trust_root=None,
    )

    assert result["gate_decision"] == "block"
    assert gate.BLOCK_H2_STANDING_CHECK in result["block_reasons"]
    assert result["h2_status"] == "shadow"


def test_flag_disposition_blocks_in_both_modes(tmp_path, monkeypatch):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "service.py").write_text(
        "import redis\n\nCLIENT = redis.from_url('redis://localhost')\n",
        encoding="utf-8",
    )
    anchor = tmp_path / "tests" / "test_assumption.py"
    anchor.parent.mkdir()
    anchor.write_text("def test_redis_contract():\n    pass\n", encoding="utf-8")
    diff = """diff --git a/pkg/service.py b/pkg/service.py
--- a/pkg/service.py
+++ b/pkg/service.py
@@ -1 +1,3 @@
+import redis
+CLIENT = redis.from_url('redis://localhost')
"""
    phase = base_phase_input(
        phase="bridge-protocol-root",
        h2_section={
            "coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []},
            "rows": [
                {
                    "candidate_id": "redis:pkg/service.py:redis.from_url#1",
                    "disposition": "flag",
                    "assumption": "Redis is required but not available in the target environment",
                    "evidence": "tests/test_assumption.py::test_redis_contract",
                }
            ],
        }
    )

    for mode in ("shadow", "block"):
        monkeypatch.setattr(gate, "H2_MODE", mode)
        result = gate.evaluate(
            phase,
            tmp_path,
            registry=None,
            manifest=[],
            changed_paths=["pkg/service.py"],
            review_diff=diff,
            readiness=None,
            stage_modes=None,
            trust_root=None,
        )

        assert result["gate_decision"] == "block"
        assert gate.BLOCK_H2_STANDING_CHECK in result["block_reasons"]
        assert result["h2_status"] == "flagged"


def test_missing_coverage_acknowledgment_status_is_static_only_unacknowledged(tmp_path, monkeypatch):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "db.py").write_text(
        "import psycopg\n\nCONN = psycopg.connect('postgresql://localhost/db')\n",
        encoding="utf-8",
    )
    anchor = tmp_path / "tests" / "test_db.py"
    anchor.parent.mkdir()
    anchor.write_text("def test_postgres_contract():\n    pass\n", encoding="utf-8")
    diff = """diff --git a/pkg/db.py b/pkg/db.py
--- a/pkg/db.py
+++ b/pkg/db.py
@@ -1 +1,3 @@
+import psycopg
+CONN = psycopg.connect('postgresql://localhost/db')
"""
    phase = base_phase_input(
        phase="bridge-protocol-root",
        h2_section={
            "coverage_acknowledgment": {"acknowledged": False, "additional_assumptions": []},
            "rows": [
                {
                    "candidate_id": "postgres:pkg/db.py:psycopg.connect#1",
                    "disposition": "answered",
                    "violating_run": "pytest tests/test_db.py::test_postgres_contract without postgres",
                    "evidence": "tests/test_db.py::test_postgres_contract",
                }
            ],
        }
    )

    result = gate.evaluate(
        phase,
        tmp_path,
        registry=None,
        manifest=[],
        changed_paths=["pkg/db.py"],
        review_diff=diff,
        readiness=None,
        stage_modes=None,
        trust_root=None,
    )
    assert result["gate_decision"] == "pass"
    assert gate.BLOCK_H2_STANDING_CHECK not in result["block_reasons"]
    assert result["h2_status"] == "static-only-unacknowledged"
    assert result["h2_notice"] == "H2 coverage acknowledgment is missing or false"

    monkeypatch.setattr(gate, "H2_MODE", "block")
    blocked = gate.evaluate(
        phase,
        tmp_path,
        registry=None,
        manifest=[],
        changed_paths=["pkg/db.py"],
        review_diff=diff,
        readiness=None,
        stage_modes=None,
        trust_root=None,
    )
    assert blocked["gate_decision"] == "block"
    assert gate.BLOCK_H2_STANDING_CHECK in blocked["block_reasons"]
    assert blocked["h2_status"] == "static-only-unacknowledged"


def test_no_candidates_and_no_section_is_clean_pass(tmp_path):
    result = gate.evaluate(
        base_phase_input(phase="bridge-protocol-root"),
        tmp_path,
        registry=None,
        manifest=[],
        changed_paths=[],
        review_diff="",
        readiness=None,
        stage_modes=None,
        trust_root=None,
    )

    assert result["gate_decision"] == "pass"
    assert gate.BLOCK_H2_STANDING_CHECK not in result["block_reasons"]
    assert result["h2_status"] == "enforced"
    assert result["h2_notice"] is None
    assert result["h2_record"].derived == []


def test_h2_standing_check_does_no_io(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "service.py").write_text(
        "import redis\n\nCLIENT = redis.from_url('redis://localhost')\n",
        encoding="utf-8",
    )
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    diff = """diff --git a/pkg/service.py b/pkg/service.py
--- a/pkg/service.py
+++ b/pkg/service.py
@@ -1 +1,3 @@
+import redis
+CLIENT = redis.from_url('redis://localhost')
"""

    result = gate.h2_standing_check(
        base_phase_input(phase="bridge-protocol-root"),
        tmp_path,
        ["pkg/service.py"],
        diff,
    )

    after = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    assert after == before
    assert result["record"].derived == ["redis:pkg/service.py:redis.from_url#1"]
