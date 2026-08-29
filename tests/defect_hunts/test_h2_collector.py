from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

from skills.defect_hunts.h2_assumptions import H2Record


ROOT = Path(__file__).resolve().parents[2]
COLLECTOR_PATH = ROOT / "skills" / "bridge-protocol" / "gate" / "h2_collector.py"
GATE_PATH = ROOT / "skills" / "bridge-protocol" / "gate" / "gate.py"

collector_spec = importlib.util.spec_from_file_location("bridge_protocol_h2_collector", COLLECTOR_PATH)
h2_collector = importlib.util.module_from_spec(collector_spec)
assert collector_spec.loader
collector_spec.loader.exec_module(h2_collector)

gate_spec = importlib.util.spec_from_file_location("bridge_protocol_gate_for_h2_collector", GATE_PATH)
gate = importlib.util.module_from_spec(gate_spec)
assert gate_spec.loader
gate_spec.loader.exec_module(gate)


def record(**overrides):
    base = {
        "run_id": "run-1",
        "h2_mode": "shadow",
        "derived": ["redis:pkg/service.py:redis.from_url#1"],
        "dispositions": [
            {
                "candidate_id": "redis:pkg/service.py:redis.from_url#1",
                "disposition": "answered",
                "valid": True,
            }
        ],
        "coverage_acknowledged": True,
        "complete": True,
    }
    base.update(overrides)
    return H2Record(**base)


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


def git_status_short():
    return subprocess.run(
        ["git", "status", "--short"],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout


def test_append_writes_one_jsonl_line(tmp_path):
    log_path = tmp_path / "state" / "h2-shadow-log.jsonl"
    h2_collector.append_record(record(), log_path=log_path)

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {
        "run_id": "run-1",
        "h2_mode": "shadow",
        "derived": ["redis:pkg/service.py:redis.from_url#1"],
        "dispositions": [
            {
                "candidate_id": "redis:pkg/service.py:redis.from_url#1",
                "disposition": "answered",
                "valid": True,
            }
        ],
        "coverage_acknowledged": True,
        "complete": True,
    }


def test_append_is_append_only(tmp_path):
    log_path = tmp_path / "h2-shadow-log.jsonl"
    h2_collector.append_record(record(run_id="first"), log_path=log_path)
    h2_collector.append_record(record(run_id="second"), log_path=log_path)

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["run_id"] for line in lines] == ["first", "second"]


def test_default_path_resolution(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit.jsonl"
    monkeypatch.setenv("ARB_H2_SHADOW_LOG", str(explicit))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    assert h2_collector.shadow_log_path() == explicit

    monkeypatch.delenv("ARB_H2_SHADOW_LOG")
    assert h2_collector.shadow_log_path() == tmp_path / "xdg" / "arb" / "h2-shadow-log.jsonl"

    monkeypatch.delenv("XDG_STATE_HOME")
    assert h2_collector.shadow_log_path() == tmp_path / "home" / ".local" / "state" / "arb" / "h2-shadow-log.jsonl"


def test_log_path_is_outside_repo_tree(tmp_path):
    repo = tmp_path / "repo"
    pkg = repo / "pkg"
    pkg.mkdir(parents=True)
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
        base_phase_input(),
        repo,
        registry=None,
        manifest=[],
        changed_paths=["pkg/service.py"],
        review_diff=diff,
        readiness=None,
        stage_modes=None,
        trust_root=None,
    )

    log_path = tmp_path / "outside-state" / "h2-shadow-log.jsonl"
    assert os.path.commonpath([ROOT, log_path]) != str(ROOT)
    before = git_status_short()
    h2_collector.append_record(result["h2_record"], log_path=log_path)
    after = git_status_short()

    assert log_path.exists()
    assert after == before
