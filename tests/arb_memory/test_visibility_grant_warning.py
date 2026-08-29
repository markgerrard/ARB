from arb_memory.run import visibility_grant_warning


def test_warns_when_visibility_dsn_set_but_grant_role_unset():
    # OPS-V1 (audit 2026-07-08): an unset ARB_VISIBILITY_GATEWAY_ROLE skips
    # apply_visibility_grants silently (fails closed, but invisibly), so a
    # future schema/role change strands the reader with no signal. The skip
    # must be LOUD when a visibility service is actually configured.
    warning = visibility_grant_warning(visibility_role=None, visibility_dsn_set=True)
    assert warning is not None
    assert "ARB_VISIBILITY_GATEWAY_ROLE" in warning


def test_no_warning_when_role_present():
    assert visibility_grant_warning(visibility_role="arb_vis_reader", visibility_dsn_set=True) is None


def test_no_warning_when_no_visibility_service_configured():
    # No visibility DSN → not running visibility → skipping its grants is expected.
    assert visibility_grant_warning(visibility_role=None, visibility_dsn_set=False) is None
