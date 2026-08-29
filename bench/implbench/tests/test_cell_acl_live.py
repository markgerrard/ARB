from __future__ import annotations

import pytest


@pytest.mark.live_bakeoff
def test_live_managed_valkey_acl_is_retired() -> None:
    pytest.fail("Task 14 live readiness owns managed Valkey ACL proof")
