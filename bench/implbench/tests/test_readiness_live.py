from __future__ import annotations

import pytest

from implbench.harness.readiness import LIVE_BOUNDARY_GATES, bind_live_boundaries


@pytest.mark.live_bakeoff
def test_live_task4_task5_boundaries_bind_to_gates_9_to_14() -> None:
    bindings = bind_live_boundaries()
    assert bindings == LIVE_BOUNDARY_GATES
