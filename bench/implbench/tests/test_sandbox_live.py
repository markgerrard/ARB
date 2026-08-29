from __future__ import annotations

import pytest

pytestmark = pytest.mark.live_bakeoff


def test_real_seatbelt_profile_launch_and_mach_deny_proofs_are_task14_only() -> None:
    """Task 14 owns real Seatbelt, UID, Mach, network, and filesystem evidence."""
    pytest.fail("live_bakeoff is reserved for Task 14")
