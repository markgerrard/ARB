"""Classification tests. Pure rows in, findings out — no database, no clock.

Every branch is exercised, including the ones that have never fired in
production, because the first time a branch fires is exactly when nobody is in a
position to debug the checker.
"""

from __future__ import annotations

import json

import sys as _sys
import pytest as _pytest
if _sys.version_info >= (3, 14):
    # coincurve publishes no wheels for Python >= 3.14 and pyproject.toml scopes the
    # dependency to < 3.14. On those interpreters this module is a stated platform gap,
    # not a silent skip; on <= 3.13 the hard import below still fails loudly.
    _pytest.importorskip("coincurve", reason="coincurve has no wheel for Python >= 3.14 (see pyproject.toml)")

from buzz_ops.seat_auth_tag_check import check_rows, check_seat

AGENT = "ae925ba349c7055b65cf88575a790c414636d0c1a4c35ee123a342e72d6bfbe6"
OWNER = "ae0954e4582ac686025837180479a4248add49016f1769d15d0af5bf2eb67cdd"
SIG = ("56ce136df2414239fa57bf7b2b520540ce599b101dc90de47905fc515632687937f95f76bf714622"
       "ea1ae097bb3442037764a1a7512ff02109695581a3ff3bf5")
GOOD_TAG = ["auth", OWNER, "kind=0", SIG]


def row(**overrides):
    base = {
        "agent_pubkey": AGENT,
        "relay_owner": OWNER,
        "kind": 0,
        "created_at": 1786000000,
        "tags": json.dumps([GOOD_TAG]),
        "content": json.dumps({"display_name": "db-prod-1", "about": "x"}),
    }
    base.update(overrides)
    return base


def test_a_healthy_seat_passes():
    finding = check_seat(row())
    assert finding.ok, f"{finding.status}: {finding.detail}"
    assert finding.display_name == "db-prod-1"
    assert not finding.blocking


def test_the_real_regression_a_rename_that_dropped_the_tag():
    """The real regression: a rename left the profile intact but tags=[]."""
    finding = check_seat(row(tags="[]"))
    assert finding.status == "no_auth_tag"
    assert finding.blocking


def test_two_auth_tags_fail_exactly_like_zero():
    """server.rs destructures `let [auth_tag] = ...`, so a well-meant 'add the tag
    back alongside the old one' repair produces the same blank panel."""
    finding = check_seat(row(tags=json.dumps([GOOD_TAG, GOOD_TAG])))
    assert finding.status == "multiple_auth_tags"
    assert finding.blocking


def test_a_seat_with_no_profile_at_all_is_a_finding_not_a_skip():
    finding = check_seat(row(kind=None, tags=None, content=None))
    assert finding.status == "no_profile"
    assert finding.blocking


def test_a_forged_signature_is_caught():
    bad = ["auth", OWNER, "kind=0", "ff" + SIG[2:]]
    finding = check_seat(row(tags=json.dumps([bad])))
    assert finding.status == "bad_signature"
    assert finding.blocking


def test_a_tag_lifted_from_another_seat_is_caught():
    """Tag reuse is legitimate WITHIN a seat and forgery ACROSS seats; the
    preimage binds the agent pubkey, so this must not pass."""
    other = "11" * 32
    finding = check_seat(row(agent_pubkey=other, relay_owner=OWNER))
    assert finding.status == "bad_signature"


def test_conditions_that_do_not_apply_to_a_profile_event():
    tag = ["auth", OWNER, "kind=24200", SIG]
    finding = check_seat(row(tags=json.dumps([tag])))
    assert finding.status in {"conditions_not_applicable", "bad_signature"}
    assert finding.blocking


def test_owner_mismatch_is_a_warning_not_a_block():
    """The proxy checks the tag; the relay checks its own column. A disagreement
    does not blank the panel today, so calling it blocking would cry wolf — but
    it is one write away from a confusing half-failure, so it is not silent."""
    finding = check_seat(row(relay_owner="cc" * 32))
    assert finding.status == "owner_mismatch_warning"
    assert not finding.blocking
    assert not finding.ok


def test_malformed_tags_column_does_not_crash_the_run():
    """One unparseable row must not take the whole fleet check down — a checker
    that dies on bad data reports nothing about the other seats."""
    finding = check_seat(row(tags="{not json"))
    assert finding.status == "no_auth_tag"


def test_report_separates_blocking_from_warnings_and_serialises():
    # The third row keeps the real agent pubkey deliberately: the tag is bound to
    # it, so changing the pubkey would make the signature fail and the row would
    # classify as bad_signature instead of the mismatch this test is about. (It
    # did, on the first run — the checker was right and the fixture was wrong.)
    report = check_rows([
        row(),
        row(agent_pubkey="bb" * 32, tags="[]"),
        row(relay_owner="dd" * 32),
    ])
    assert len(report.findings) == 3
    assert len(report.blocking) == 1
    assert len(report.warnings) == 1
    payload = json.loads(report.as_json())
    assert payload["checked"] == 3 and payload["blocking"] == 1
    assert {s["status"] for s in payload["seats"]} == {
        "ok", "no_auth_tag", "owner_mismatch_warning"}
