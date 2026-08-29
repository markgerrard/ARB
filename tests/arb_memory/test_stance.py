import pytest
from arb_memory.stance import parse_stance, StanceError, STANCES, SEVERITIES

VALID = '''Here is my review.
```vote
{"stance":"block","severity":"P0","refs":["protocol.py:9"],"note":"bad"}
```'''

_BARE = 'blah blah\n{"stance":"approve","severity":"none"}'

def test_parses_vote_fenced_block():
    out = parse_stance(VALID)
    assert out == {"stance":"block","severity":"P0","refs":["protocol.py:9"],"note":"bad"}

def test_accepts_json_tagged_fence():
    out = parse_stance('```json\n{"stance":"approve","severity":"none","refs":[],"note":""}\n```')
    assert out["stance"] == "approve"

def test_accepts_bare_trailing_object():
    out = parse_stance('prose...\n{"stance":"approve","severity":"none","refs":[],"note":"ok"}')
    assert out["stance"] == "approve"

def test_uses_last_block_when_multiple():
    txt = '```vote\n{"stance":"approve","severity":"none","refs":[],"note":"a"}\n```\n' \
          '```vote\n{"stance":"block","severity":"P1","refs":[],"note":"b"}\n```'
    assert parse_stance(txt)["stance"] == "block"

def test_note_with_closing_brace_parses():
    txt = '```vote\n{"stance":"approve","severity":"none","refs":[],"note":"fix foo() }"}\n```'
    out = parse_stance(txt)
    assert out["stance"] == "approve"
    assert out["note"] == "fix foo() }"

def test_malformed_last_fence_falls_back_to_earlier_valid():
    txt = '```vote\n{"stance":"approve","severity":"none","refs":[],"note":"ok"}\n```\n' \
          '```vote\n{"stance":"block", oops}\n```'
    assert parse_stance(txt)["stance"] == "approve"

def test_timed_out_is_valid_stance():
    assert "timed-out" in STANCES
    assert parse_stance('```vote\n{"stance":"timed-out","severity":"none","refs":[],"note":""}\n```')["stance"] == "timed-out"

def test_absent_block_raises():
    with pytest.raises(StanceError):
        parse_stance("no vote here at all")

def test_malformed_json_raises():
    with pytest.raises(StanceError):
        parse_stance('```vote\n{"stance":"approve", oops}\n```')

def test_wrong_enum_raises():
    with pytest.raises(StanceError):
        parse_stance('```vote\n{"stance":"yolo","severity":"none","refs":[],"note":""}\n```')

def test_absent_severity_defaults_to_none_instead_of_dropping_the_vote():
    # #12 (2026-07-08): a valid stance fence with NO severity field used to raise,
    # so the bridge auto-emit fail-soft SILENTLY DROPPED the vote — cost real
    # re-fires when agy/GLM/cold-Opus ended with {"stance":"approve"} only. The
    # stance is what drives the merge decision; absent severity means "unrated".
    out = parse_stance('```vote\n{"stance":"approve","refs":[],"note":""}\n```')
    assert out["stance"] == "approve"
    assert out["severity"] == "none"


def test_present_but_invalid_severity_still_raises():
    # Absent is defaulted; a genuine typo (P3, "critical") is still a hard error
    # so malformed fences don't pass silently.
    with pytest.raises(StanceError):
        parse_stance('```vote\n{"stance":"block","severity":"P3","refs":[],"note":""}\n```')

def test_require_fence_rejects_bare_trailing_json():
    # default (loose) still parses the bare object — unchanged behavior
    assert parse_stance(_BARE)["stance"] == "approve"
    # strict bridge path rejects it (guard b)
    with pytest.raises(StanceError):
        parse_stance(_BARE, require_fence=True)

def test_require_fence_accepts_fenced():
    fenced = 'text\n```vote\n{"stance":"block","severity":"P0"}\n```'
    assert parse_stance(fenced, require_fence=True)["stance"] == "block"


def test_require_fence_rejects_unlabeled_fence():
    with pytest.raises(StanceError):
        parse_stance('```\n{"stance":"approve","severity":"none"}\n```', require_fence=True)


def test_require_fence_accepts_vote_and_json_labels():
    vote = '```vote\n{"stance":"approve","severity":"none"}\n```'
    json = '```json\n{"stance":"needs-changes","severity":"P2"}\n```'
    assert parse_stance(vote, require_fence=True)["stance"] == "approve"
    assert parse_stance(json, require_fence=True)["stance"] == "needs-changes"
