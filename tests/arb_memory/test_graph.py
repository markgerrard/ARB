import pytest

from arb_memory.graph import reference_targets, subject_mode, validate_related_params


def test_reference_targets_backtick_match():
    ids = {"art-0123456789abcdef", "other-id"}
    body = "see `other-id` for details"
    assert reference_targets(body, "art-0123456789abcdef", ids) == ["other-id"]


def test_reference_targets_bare_token_art_id():
    ids = {"art-0123456789abcdef", "me"}
    body = "derived from art-0123456789abcdef earlier"
    assert reference_targets(body, "me", ids) == ["art-0123456789abcdef"]


def test_reference_targets_bare_token_guards_reject_embedded():
    ids = {"snake_case_id", "me"}
    body = "path/snake_case_id.py is not a citation"
    assert reference_targets(body, "me", ids) == []


def test_reference_targets_excludes_self():
    ids = {"me"}
    assert reference_targets("about `me` indeed", "me", ids) == []


def test_reference_targets_empty_body():
    assert reference_targets("", "me", {"me", "you"}) == []


def test_subject_mode_is_pure_caller_intent():
    assert subject_mode(None) == "live"
    assert subject_mode(3) == "as_written"
    assert subject_mode(0) == "as_written"


def test_validate_related_params_ranges():
    validate_related_params(1, 0.01)
    validate_related_params(20, 2.0)
    for k, t in ((0, 0.35), (21, 0.35), (5, 0.0), (5, 2.1)):
        with pytest.raises(ValueError):
            validate_related_params(k, t)
