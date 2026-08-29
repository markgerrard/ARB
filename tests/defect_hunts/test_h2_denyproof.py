from __future__ import annotations

from collections.abc import Callable

from skills.defect_hunts.h2_assumptions import is_complete
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


def _computed_rec(
    n_answered: int = 0,
    n_nlb: int = 0,
    n_flag: int = 0,
    *,
    mutate: Callable[[list[dict]], None] | None = None,
) -> dict:
    derived = [f"redis:pkg/a.py:redis.from_url#{i + 1}" for i in range(n_answered + n_nlb + n_flag)]
    rows = (
        [
            {
                "candidate_id": candidate_id,
                "disposition": "answered",
                "violating_run": "run-1",
                "evidence": "skills/defect_hunts/h2_assumptions.py",
            }
            for candidate_id in derived[:n_answered]
        ]
        + [
            {
                "candidate_id": candidate_id,
                "disposition": "not_load_bearing",
                "reason": "test-only path",
                "evidence": "skills/defect_hunts/h2_assumptions.py",
            }
            for candidate_id in derived[n_answered : n_answered + n_nlb]
        ]
        + [
            {
                "candidate_id": candidate_id,
                "disposition": "flag",
                "assumption": "needs review",
                "evidence": "skills/defect_hunts/h2_assumptions.py",
            }
            for candidate_id in derived[n_answered + n_nlb :]
        ]
    )
    if mutate is not None:
        mutate(rows)
    section = {"coverage_acknowledgment": {"acknowledged": True, "additional_assumptions": []}, "rows": rows}
    return _rec(derived, rows, complete=is_complete(derived, section, repo_root="."))


def _incomplete_rec(n_answered: int = 0, n_nlb: int = 0, n_flag: int = 0, *, derived: list[str] | None = None) -> dict:
    rec = _complete_rec(n_answered=n_answered, n_nlb=n_nlb, n_flag=n_flag)
    return _rec(rec["derived"] if derived is None else derived, rec["dispositions"], complete=False)


def test_a_measurable_fp_token_does_not_graduate_AND_fp_threshold_is_load_bearing():
    bad = [_complete_rec(n_answered=1, n_nlb=2) for _ in range(10)]

    assert G.fp_rate(bad) == 20 / 30
    assert G.is_graduation_ready(bad) is False
    assert G.is_graduation_ready(bad, _disabled_guards={"fp_threshold"}) is True


def test_b_silence_does_not_graduate_AND_complete_only_is_load_bearing():
    bad = [_incomplete_rec(n_answered=19, n_nlb=1) for _ in range(10)]

    assert G.is_graduation_ready(bad) is False
    assert G.is_graduation_ready(bad, _disabled_guards={"complete_only"}) is True


def test_c_silence_boundary_does_not_graduate_AND_complete_only_is_load_bearing():
    bad = [_complete_rec(n_answered=2) for _ in range(9)]
    bad.append(_incomplete_rec(n_answered=1, n_nlb=1))

    assert G.is_graduation_ready(bad) is False
    assert G.is_graduation_ready(bad, _disabled_guards={"complete_only"}) is True


def test_d_achievable_k3_window_does_not_graduate_AND_min_runs_is_load_bearing():
    bad = [_complete_rec(n_answered=3) for _ in range(8)]
    bad.append(_complete_rec(n_answered=2, n_nlb=1))

    assert len(bad) == 9
    assert G.fp_rate(bad) == 1 / 27
    assert G.is_graduation_ready(bad) is False
    assert G.is_graduation_ready(bad, _disabled_guards={"min_runs"}) is True


def test_min_disposed_floor_does_not_graduate_AND_min_disposed_is_load_bearing():
    bad = [_complete_rec(n_answered=2) for _ in range(9)]
    bad.append(_complete_rec(n_nlb=1))

    assert G.fp_rate(bad) == 1 / 19
    assert G.is_graduation_ready(bad) is False
    assert G.is_graduation_ready(bad, _disabled_guards={"min_disposed"}) is True


def test_e_empty_run_does_not_graduate_AND_complete_only_is_load_bearing():
    bad = [_incomplete_rec(n_answered=1, n_nlb=1, derived=[])]
    bad.extend(_incomplete_rec(n_answered=2, derived=[]) for _ in range(9))

    assert all(record["derived"] == [] for record in bad)
    assert G.is_graduation_ready(bad) is False
    assert G.is_graduation_ready(bad, _disabled_guards={"min_disposed"}) is False
    assert G.is_graduation_ready(bad, _disabled_guards={"complete_only"}) is True


def test_f_validity_not_presence_does_not_graduate_AND_complete_only_is_load_bearing():
    def invalidate_answered(rows: list[dict]) -> None:
        rows[0]["violating_run"] = ""

    bad = [_computed_rec(n_answered=19, n_nlb=1, mutate=invalidate_answered) for _ in range(10)]

    assert all(record["complete"] is False for record in bad)
    assert G.is_graduation_ready(bad) is False
    assert G.is_graduation_ready(bad, _disabled_guards={"complete_only"}) is True


def test_g_uniform_window_does_not_graduate_AND_discrimination_is_load_bearing():
    bad = [_complete_rec(n_answered=2) for _ in range(15)]

    assert G.fp_rate(bad) == 0
    assert G.is_graduation_ready(bad) is False
    assert G.is_graduation_ready(bad, _disabled_guards={"discrimination"}) is True


def test_h_anchored_fp_token_does_not_graduate_AND_complete_only_is_load_bearing():
    def unanchor_fp_token(rows: list[dict]) -> None:
        fp_row = next(row for row in rows if row["disposition"] == "not_load_bearing")
        fp_row["reason"] = "test-only path"
        fp_row["evidence"] = "free text, not an anchor"

    bad = [_computed_rec(n_answered=19, n_nlb=1, mutate=unanchor_fp_token) for _ in range(10)]

    assert all(record["complete"] is False for record in bad)
    assert G.is_graduation_ready(bad) is False
    assert G.is_graduation_ready(bad, _disabled_guards={"complete_only"}) is True
