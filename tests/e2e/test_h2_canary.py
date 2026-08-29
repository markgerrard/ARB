from pathlib import Path
import unittest.mock as mock

import pytest

from tests.e2e import spine
from tests.e2e.corpus import iter_corpus
from tests.e2e.h2_harness import run_case


pytestmark = pytest.mark.e2e


ROOT = Path(__file__).resolve().parents[2]


def test_symbols_are_real(real_gate, real_coll, real_grad):
    checks = [
        (real_gate.h2_standing_check, "bridge_protocol_gate", ROOT / "skills/bridge-protocol/gate/gate.py"),
        (real_gate._h2_candidate_files, "bridge_protocol_gate", ROOT / "skills/bridge-protocol/gate/gate.py"),
        (real_coll.append_record, "h2_collector", ROOT / "skills/bridge-protocol/gate/h2_collector.py"),
        (real_coll.shadow_log_path, "h2_collector", ROOT / "skills/bridge-protocol/gate/h2_collector.py"),
        (
            real_grad.is_graduation_ready,
            "skills.defect_hunts.h2_graduation",
            ROOT / "skills/defect_hunts/h2_graduation.py",
        ),
    ]
    for func, module_name, real_path in checks:
        spine.assert_real_symbol(func, module_name=module_name, real_path=real_path)


def test_canary_trips_on_mock():
    with pytest.raises(AssertionError, match="Mock"):
        spine.assert_real_symbol(mock.Mock(), module_name="h2_collector", real_path=ROOT / "x.py")


def test_both_side_effects(tmp_path):
    out = run_case(dict(iter_corpus())["enumerated/redis-from-url"], tmp_path)
    spine.assert_h2_boundary_honest(out)


_REDIS_DIFF = (
    "diff --git a/pkg/a.py b/pkg/a.py\n--- a/pkg/a.py\n+++ b/pkg/a.py\n"
    "@@ -0,0 +1,2 @@\n+import redis\n+client = redis.from_url('redis://localhost:6379/0')\n"
)
_BASE = {
    "diff": _REDIS_DIFF,
    "changed_paths": ["pkg/a.py"],
    "phase_input": {
        "h2_section": {"coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []}, "rows": []}
    },
}


def test_producer_reads_disk_not_just_diff(tmp_path):
    # The read boundary is LOAD-BEARING, not incidental (codex review P1): derive() ASTs the seeded
    # FILE content (via _h2_candidate_files) and only emits a candidate when the call exists in the
    # file AND on an added diff line. So the SAME diff claiming the call must derive NOTHING when the
    # seeded file lacks the call — proving the producer read the disk, not just the supplied diff.
    no_call = dict(_BASE, files={"pkg/a.py": "import redis\nx = 1\n"})
    out_neg = run_case(no_call, tmp_path / "neg")
    assert out_neg["record_payload"]["derived"] == [], "producer derived from the diff alone — disk read not load-bearing"

    with_call = dict(_BASE, files={"pkg/a.py": "import redis\nclient = redis.from_url('redis://localhost:6379/0')\n"})
    out_pos = run_case(with_call, tmp_path / "pos")
    assert out_pos["record_payload"]["derived"] == ["redis:pkg/a.py:redis.from_url#1"]
