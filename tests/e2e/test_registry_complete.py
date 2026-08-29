import json
from pathlib import Path

import pytest

from skills.defect_hunts import h2_graduation


pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "tests/e2e/h2_guard_registry.json"
IS_COMPLETE_GUARDS = {
    "coverage_ack",
    "derived_nonempty",
    "validity",
    "rows_subset",
    "uniqueness",
    "every_derived_has_row",
}
# The e2e's OWN load-bearing guards (the deny-proof guards in the harness itself), pinned into the
# mutation registry so they're continuously verified rather than only out-of-band (#32) — the
# slice's own deny-proof doctrine applied to the slice.
E2E_OWN_GUARDS = {
    "runner_canary",  # run_suite invokes the boundary-honesty canary (deny-proof #1)
    "spine_zero_case",  # zero-case -> BLOCK_UNRUN, never a vacuous pass (deny-proof #2)
    "runner_block_fail",  # detected surface breakage classifies BLOCK_FAIL, not BLOCK_UNRUN (deny-proof #3)
}


def test_registry_covers_all_h2_guards():
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    expected = IS_COMPLETE_GUARDS | h2_graduation.GUARDS | E2E_OWN_GUARDS
    assert set(registry) == expected


def test_registry_locators_exist_and_are_unique():
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    for guard_id, entry in registry.items():
        source = (ROOT / entry["file"]).read_text(encoding="utf-8")
        locator = entry["locator"]
        assert source.count(locator) == 1, guard_id
        assert entry["recipe"] in {"delete-line", "replace"}
        assert entry["paired_case_id"]
