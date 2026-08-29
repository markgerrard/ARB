import pytest

from skills.defect_hunts.h2_graduation import is_graduation_ready
from tests.e2e.h2_graduation_fixtures import green_records, n_minus_one_records


pytestmark = pytest.mark.e2e


def test_green_multi_record_fixture_graduates():
    assert is_graduation_ready(green_records()) is True


@pytest.mark.parametrize("guard_id", ["min_runs", "min_disposed", "discrimination", "fp_threshold", "complete_only"])
def test_each_graduation_guard_bites_at_boundary(guard_id):
    records = n_minus_one_records(guard_id)
    assert is_graduation_ready(records) is False
    assert is_graduation_ready(records, _disabled_guards=frozenset({guard_id})) is True
