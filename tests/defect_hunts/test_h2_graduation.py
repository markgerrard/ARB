from __future__ import annotations

import skills.defect_hunts.h2_graduation as G


def _rec(derived: list[str], dispositions: list[dict], *, complete: bool = True) -> dict:
    return {"derived": derived, "dispositions": dispositions, "complete": complete}


def _complete_rec(n_answered: int = 0, n_nlb: int = 0, n_flag: int = 0) -> dict:
    derived = [f"redis:pkg/a.py:redis.from_url#{i + 1}" for i in range(n_answered + n_nlb + n_flag)]
    dispositions = (
        [{"candidate_id": candidate_id, "disposition": "answered"} for candidate_id in derived[:n_answered]]
        + [
            {"candidate_id": candidate_id, "disposition": "not_load_bearing"}
            for candidate_id in derived[n_answered : n_answered + n_nlb]
        ]
        + [{"candidate_id": candidate_id, "disposition": "flag"} for candidate_id in derived[n_answered + n_nlb :]]
    )
    return _rec(derived, dispositions, complete=True)


def test_guard_names_are_explicit_and_stable():
    assert G.GUARDS == {"min_runs", "min_disposed", "discrimination", "fp_threshold", "complete_only"}


def test_fp_rate_uses_disposed_denominator_not_derived_denominator():
    records = [
        _rec(
            ["redis:pkg/a.py:redis.from_url#1", "redis:pkg/a.py:redis.from_url#2", "redis:pkg/a.py:redis.from_url#3"],
            [{"candidate_id": "redis:pkg/a.py:redis.from_url#1", "disposition": "not_load_bearing"}],
            complete=True,
        ),
        _rec(
            ["redis:pkg/b.py:redis.from_url#1", "redis:pkg/b.py:redis.from_url#2"],
            [{"candidate_id": "redis:pkg/b.py:redis.from_url#1", "disposition": "answered"}],
            complete=True,
        ),
    ]

    assert G.fp_rate(records) == 0.5


def test_fp_rate_ignores_incomplete_records():
    records = [
        _complete_rec(n_answered=19, n_nlb=1),
        _rec(
            ["redis:pkg/incomplete.py:redis.from_url#1"],
            [{"candidate_id": "redis:pkg/incomplete.py:redis.from_url#1", "disposition": "not_load_bearing"}],
            complete=False,
        ),
    ]

    assert G.fp_rate(records) == 0.05


def test_fp_rate_is_none_when_no_complete_disposed_candidates_exist():
    assert G.fp_rate([]) is None
    assert G.fp_rate([_rec([], [], complete=True)]) is None
    assert G.fp_rate([_complete_rec(n_answered=2, n_nlb=1) | {"complete": False}]) is None


def test_good_window_graduates_when_all_guards_pass():
    records = [_complete_rec(n_answered=19, n_nlb=1) for _ in range(11)]

    assert G.fp_rate(records) == 11 / 220
    assert G.is_graduation_ready(records) is True


def test_graduation_query_filters_incomplete_records_without_penalizing_the_window():
    records = [_complete_rec(n_answered=19, n_nlb=1) for _ in range(11)]
    records.append(_complete_rec(n_answered=1, n_nlb=99) | {"complete": False})

    assert G.fp_rate(records) == 11 / 220
    assert G.is_graduation_ready(records) is True
