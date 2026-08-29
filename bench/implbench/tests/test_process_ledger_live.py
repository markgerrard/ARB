from __future__ import annotations

import pytest


@pytest.mark.live_bakeoff
def test_live_double_fork_session_is_empty_after_close() -> None:
    pytest.fail("Task 14 live readiness owns real double-fork census proof")
