import pytest

from tests.e2e.spine import E2EResult, E2EStatus


pytestmark = pytest.mark.e2e


def test_only_pass_merges():
    assert E2EResult(E2EStatus.PASS, "ok", 1, 1, 0, 0).merges() is True
    assert E2EResult(E2EStatus.BLOCK_FAIL, "broke", 1, 0, 1, 0).merges() is False
    assert E2EResult(E2EStatus.BLOCK_UNRUN, "vacuous", 0, 0, 0, 0).merges() is False


def test_zero_case_is_block_unrun():
    assert (
        E2EResult.from_counts(case_count=0, passed=0, block_fail=0, block_unrun=0).status
        is E2EStatus.BLOCK_UNRUN
    )


def test_block_unrun_precedes_block_fail():
    result = E2EResult.from_counts(case_count=2, passed=0, block_fail=1, block_unrun=1)
    assert result.status is E2EStatus.BLOCK_UNRUN
