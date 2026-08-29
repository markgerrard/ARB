import json

import pytest

from tests.e2e.runner import EXIT_CODES, run_suite
from tests.e2e.spine import E2EStatus


pytestmark = pytest.mark.e2e


def test_green_subset_writes_pass_status(tmp_path):
    result = run_suite(case_ids=["enumerated/redis-from-url"], tmp=tmp_path, status_path=tmp_path / "e2e_status.json")
    assert result.status is E2EStatus.PASS
    assert EXIT_CODES[result.status] == 0
    status = json.loads((tmp_path / "e2e_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "pass"
    assert status["case_count"] == 1


def test_empty_subset_blocks_unrun(tmp_path):
    result = run_suite(case_ids=[], tmp=tmp_path, status_path=tmp_path / "e2e_status.json")
    assert result.status is E2EStatus.BLOCK_UNRUN
    assert EXIT_CODES[result.status] == 2
    status = json.loads((tmp_path / "e2e_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "block-unrun"


def test_per_case_counts_use_current_record_not_accumulated_log():
    # Pins the log-accumulation fix (GLM/codex review P1): assert_case_expected must count only the
    # CURRENT record, never the shared append-only log. Here log_records carries a FOREIGN complete
    # record (answered=1) plus the current one (nlb=1). Counting the log would give answered=1 and
    # mismatch; counting the current record gives nlb=1 and matches. Reverting the fix reds this.
    from tests.e2e.h2_harness import assert_case_expected

    foreign = {"complete": True, "dispositions": [{"disposition": "answered"}]}
    current = {
        "complete": True,
        "derived": ["redis:pkg/a.py:redis.from_url#1"],
        "dispositions": [{"candidate_id": "redis:pkg/a.py:redis.from_url#1", "disposition": "not_load_bearing", "valid": True}],
    }
    out = {
        "status": "enforced",
        "record_payload": current,
        "log_records": [foreign, current],
        "read_evidence": {"pkg/a.py": {"explains": ["redis:pkg/a.py:redis.from_url#1"], "sha256": "x"}},
    }
    case = {
        "expected": {
            "status": "enforced",
            "derived": ["redis:pkg/a.py:redis.from_url#1"],
            "record": {"complete": True, "dispositions": current["dispositions"]},
            "graduation": {"counted": True, "disposition_counts": {"answered": 0, "not_load_bearing": 1, "flag": 0}},
        }
    }
    assert_case_expected(case, out)  # passes only if counts come from `current`, not the polluted log
