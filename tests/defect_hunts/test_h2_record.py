from __future__ import annotations

from skills.defect_hunts.h2_assumptions import is_complete


def test_complete_requires_every_candidate_disposed_and_valid_and_ack_and_nonempty():
    derived = ["redis:pkg/a.py:redis.from_url#1"]
    section = {
        "coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []},
        "rows": [
            {
                "candidate_id": derived[0],
                "disposition": "not_load_bearing",
                "reason": "x",
                "evidence": "skills/defect_hunts/h2_assumptions.py",
            }
        ],
    }
    assert is_complete(derived, section, repo_root=".") is True


def test_one_undisposed_candidate_makes_incomplete():
    derived = ["redis:pkg/a.py:redis.from_url#1", "postgres:pkg/a.py:psycopg.connect#1"]
    section = {
        "coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []},
        "rows": [
            {
                "candidate_id": derived[0],
                "disposition": "answered",
                "violating_run": "r",
                "evidence": "skills/defect_hunts/h2_assumptions.py",
            }
        ],
    }
    assert is_complete(derived, section, repo_root=".") is False


def test_unknown_disposed_candidate_makes_incomplete():
    derived = ["redis:pkg/a.py:redis.from_url#1"]
    section = {
        "coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []},
        "rows": [
            {
                "candidate_id": derived[0],
                "disposition": "answered",
                "violating_run": "r",
                "evidence": "skills/defect_hunts/h2_assumptions.py",
            },
            {
                "candidate_id": "unknown:pkg/a.py:unknown#1",
                "disposition": "not_load_bearing",
                "reason": "x",
                "evidence": "skills/defect_hunts/h2_assumptions.py",
            },
        ],
    }
    assert is_complete(derived, section, repo_root=".") is False


def test_duplicate_disposed_candidate_makes_incomplete():
    # A duplicate disposition row (two rows for the SAME derived candidate) is
    # malformed: it double-counts in the graduation denominator and skews the FP
    # rate toward easier graduation (false-pass). is_complete must reject it.
    derived = ["redis:pkg/a.py:redis.from_url#1"]

    def _row():
        return {
            "candidate_id": derived[0],
            "disposition": "answered",
            "violating_run": "r",
            "evidence": "skills/defect_hunts/h2_assumptions.py",
        }

    # Control: a single valid row for the derived candidate IS complete — this
    # proves the duplicate (not some unrelated section defect) is what flips the
    # verdict below. Delete the uniqueness clause in is_complete and the duplicate
    # assertion goes green while this control stays green → the proof goes red.
    single = {
        "coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []},
        "rows": [_row()],
    }
    assert is_complete(derived, single, repo_root=".") is True

    duplicate = {
        "coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []},
        "rows": [_row(), _row()],
    }
    assert is_complete(derived, duplicate, repo_root=".") is False


def test_zero_derived_candidates_is_not_complete():
    assert (
        is_complete(
            [],
            {"coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []}, "rows": []},
            repo_root=".",
        )
        is False
    )


def test_invalid_disposed_row_makes_incomplete():
    derived = ["redis:pkg/a.py:redis.from_url#1"]
    section = {
        "coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []},
        "rows": [
            {
                "candidate_id": derived[0],
                "disposition": "answered",
                "violating_run": "",
                "evidence": "x",
            }
        ],
    }
    assert is_complete(derived, section, repo_root=".") is False


def test_unacknowledged_is_not_complete():
    derived = ["redis:pkg/a.py:redis.from_url#1"]
    section = {"coverage_acknowledgment": {"acknowledged": False, "additional_assumptions": []}, "rows": []}
    assert is_complete(derived, section, repo_root=".") is False
