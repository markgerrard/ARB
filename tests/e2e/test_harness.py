from pathlib import Path

import pytest

from tests.e2e.h2_harness import run_case


pytestmark = pytest.mark.e2e


CLEAN = {
    "files": {"pkg/a.py": "import os\n"},
    "diff": "diff --git a/pkg/a.py b/pkg/a.py\n--- a/pkg/a.py\n+++ b/pkg/a.py\n@@ -0,0 +1 @@\n+import os\n",
    "changed_paths": ["pkg/a.py"],
    "phase_input": {
        "h2_section": {
            "coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []},
            "rows": [],
        }
    },
}


REDIS = {
    "files": {"pkg/a.py": "import redis\nclient = redis.from_url('redis://localhost:6379/0')\n"},
    "diff": "diff --git a/pkg/a.py b/pkg/a.py\n--- a/pkg/a.py\n+++ b/pkg/a.py\n@@ -0,0 +1,2 @@\n+import redis\n+client = redis.from_url('redis://localhost:6379/0')\n",
    "changed_paths": ["pkg/a.py"],
    "phase_input": {
        "h2_section": {
            "coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []},
            "rows": [
                {
                    "candidate_id": "redis:pkg/a.py:redis.from_url#1",
                    "disposition": "not_load_bearing",
                    "reason": "seeded e2e redis fixture",
                    "evidence": "pkg/a.py",
                }
            ],
        }
    },
}


def test_seam_crosses_both_boundaries(tmp_path):
    out = run_case(CLEAN, tmp_path)
    assert out["status"] in {"shadow", "enforced", "flagged", "static-only-unacknowledged"}
    assert "pkg/a.py" in out["read_evidence"]
    assert Path(out["log_path"]).read_text(encoding="utf-8").strip() != ""
    assert isinstance(out["log_records"], list)


def test_read_evidence_depends_on_seeded_file_bytes(tmp_path):
    out = run_case(REDIS, tmp_path)
    assert out["record_payload"]["derived"] == ["redis:pkg/a.py:redis.from_url#1"]
    assert out["read_evidence"]["pkg/a.py"]["sha256"]
    assert out["read_evidence"]["pkg/a.py"]["explains"] == ["redis:pkg/a.py:redis.from_url#1"]


def test_run_case_refuses_unhermetic_env(tmp_path, monkeypatch):
    # The fail-closed hermeticity guard (review P2): with ARB_H2_SHADOW_LOG unset, run_case must
    # REFUSE rather than fall back to the production shadow log. Deleting the guard greens this →
    # the guard is verified, not assumed.
    monkeypatch.delenv("ARB_H2_SHADOW_LOG", raising=False)
    with pytest.raises(RuntimeError, match="ARB_H2_SHADOW_LOG"):
        run_case(CLEAN, tmp_path)
