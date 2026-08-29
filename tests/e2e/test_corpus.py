import pytest

from tests.e2e.corpus import assert_changed_paths_match_diff, iter_corpus, validate_case
from tests.e2e.h2_harness import run_case


pytestmark = pytest.mark.e2e


def test_all_cases_valid():
    cases = dict(iter_corpus())
    assert cases
    for case in cases.values():
        validate_case(case)
        assert_changed_paths_match_diff(case)


def test_duplicate_id_excluded_from_window(tmp_path):
    case = dict(iter_corpus())["discovered/duplicate-id"]
    out = run_case(case, tmp_path)
    assert out["record_payload"]["complete"] is False
    assert out["record_payload"]["dispositions"] == case["expected"]["record"]["dispositions"]


def test_duplicate_id_control_is_complete(tmp_path):
    case = dict(iter_corpus())["discovered/duplicate-id"]
    control = dict(case)
    control["phase_input"] = {
        "h2_section": {
            **case["phase_input"]["h2_section"],
            "rows": [case["phase_input"]["h2_section"]["rows"][0]],
        }
    }
    out = run_case(control, tmp_path)
    assert out["record_payload"]["complete"] is True
