import pytest

from tests.e2e.corpus import iter_corpus
from tests.e2e.h2_harness import assert_case_expected, run_case


pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("case_id,case", list(iter_corpus()))
def test_h2_surface_matches_expected(case_id, case, tmp_path):
    out = run_case(case, tmp_path)
    assert_case_expected(case, out)
