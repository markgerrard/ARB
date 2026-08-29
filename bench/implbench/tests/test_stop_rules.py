from __future__ import annotations

import pytest

from implbench.harness.phases import STOP_RULES, StopRuleError, evaluate_stop_rules


def test_stop_rules_are_closed_and_pair_scoped() -> None:
    assert len(STOP_RULES) == 10
    reasons = evaluate_stop_rules({"wrong_pin": True, "pair": "GLM"})
    assert reasons == ("wrong-pin",)


@pytest.mark.parametrize(
    ("key", "reason"),
    [
        ("context_reuse", "context-reuse"),
        ("hidden_key_exposure", "hidden-key-exposure"),
        ("fixture_sha_mismatch", "fixture-sha-mismatch"),
        ("write_outside_worktree", "write-outside-worktree"),
        ("source_drift", "source-drift"),
        ("malformed_ndjson", "malformed-evidence"),
        ("discarded_provider_error", "discarded-provider-error"),
        ("unknown_reasoning", "unknown-reasoning"),
    ],
)
def test_each_frozen_stop_rule_is_detected(key: str, reason: str) -> None:
    assert evaluate_stop_rules({key: True}) == (reason,)


def test_three_same_cause_infrastructure_failures_stop_before_dispatch() -> None:
    assert evaluate_stop_rules({"infrastructure_failures": ["bridge", "bridge", "bridge"]}) == ("three-infrastructure-failures",)


def test_unknown_stop_observation_is_rejected() -> None:
    with pytest.raises(StopRuleError):
        evaluate_stop_rules({"not-a-rule": True})
