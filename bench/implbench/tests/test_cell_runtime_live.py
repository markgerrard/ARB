from __future__ import annotations

import pytest


@pytest.mark.live_bakeoff
def test_live_ephemeral_uid_is_retired() -> None:
    pytest.fail("Task 14 live readiness owns ephemeral UID proof")
