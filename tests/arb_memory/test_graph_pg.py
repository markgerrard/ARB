import pytest

from arb_memory.graph import artefact_exists, latest_version, references, related_artefacts
from arb_memory.store import upsert_artefact, upsert_hint

pytestmark = pytest.mark.pg


def _vec(x: float) -> list[float]:
    # unit-ish vectors at controlled angles from [1,0,0,...]
    v = [0.0] * 1536
    v[0] = 1.0 - x
    v[1] = x
    return v


def _seed_subject_and_candidates(scratch):
    _, sv, _ = upsert_artefact(scratch, "subject", content="s", source="t", author="t")
    upsert_hint(scratch, "s-hint", _vec(0.0), artefact_id="subject", artefact_version=sv)
    # near candidate gets MANY versions (higher version number, smaller distance)
    for body in ("n1", "n2", "n3"):
        _, nv, _ = upsert_artefact(scratch, "near", content=body, source="t", author="t")
    upsert_hint(scratch, "near-hint", _vec(0.05), artefact_id="near", artefact_version=nv)
    _, fv, _ = upsert_artefact(scratch, "far", content="f", source="t", author="t")
    upsert_hint(scratch, "far-hint", _vec(0.3), artefact_id="far", artefact_version=fv)
    return sv


def test_related_orders_by_distance_not_version(scratch):
    sv = _seed_subject_and_candidates(scratch)
    rows = related_artefacts(scratch, "subject", sv, k=5, threshold=2.0)
    ids = [r[0] for r in rows]
    assert ids == ["near", "far"]           # distance order; version order is far(1) < near(3)
    assert rows[0][1] == 3 and rows[1][1] == 1   # latest version carried per candidate
    assert rows[0][2] < rows[1][2]


def test_related_equal_distance_ties_break_by_id(scratch):
    _, sv, _ = upsert_artefact(scratch, "subject", content="s", source="t", author="t")
    upsert_hint(scratch, "s", _vec(0.0), artefact_id="subject", artefact_version=sv)
    for aid in ("bbb", "aaa"):
        _, v, _ = upsert_artefact(scratch, aid, content=aid, source="t", author="t")
        upsert_hint(scratch, aid, _vec(0.1), artefact_id=aid, artefact_version=v)
    rows = related_artefacts(scratch, "subject", sv, k=5, threshold=2.0)
    assert [r[0] for r in rows] == ["aaa", "bbb"]


def test_related_default_none_resolves_latest_in_statement(scratch):
    _seed_subject_and_candidates(scratch)
    rows = related_artefacts(scratch, "subject", None, k=5, threshold=2.0)
    assert [r[0] for r in rows] == ["near", "far"]


def test_related_live_mode_ignores_deleted_latest_hint(scratch):
    _seed_subject_and_candidates(scratch)
    scratch.execute("UPDATE hints SET deleted_at = now() WHERE artefact_id = 'subject'")
    assert related_artefacts(scratch, "subject", None, k=5, threshold=2.0) == []


def test_related_as_written_uses_retired_historical_hint(scratch):
    # Fixture creates the retired STATE directly (manual UPDATE mirrors the predicate
    # store.write_artefact_and_hints applies at store.py:151-168; this test does NOT
    # exercise that write path — it pins graph-query behavior over retired state)
    _, v1, _ = upsert_artefact(scratch, "hist", content="v1", source="t", author="t")
    upsert_hint(scratch, "h1", _vec(0.0), artefact_id="hist", artefact_version=v1,
                metadata={"kind": "artefact_index"})
    _, v2, _ = upsert_artefact(scratch, "hist", content="v2", source="t", author="t")
    scratch.execute(
        "UPDATE hints SET deleted_at = now() "
        "WHERE artefact_id = 'hist' AND artefact_version < %s AND deleted_at IS NULL "
        "AND metadata->>'kind' = 'artefact_index'",
        (v2,),
    )
    _, cv, _ = upsert_artefact(scratch, "cand", content="c", source="t", author="t")
    upsert_hint(scratch, "c", _vec(0.05), artefact_id="cand", artefact_version=cv)
    live = related_artefacts(scratch, "hist", v1, k=5, threshold=2.0, subject_hints="live")
    assert live == []
    rows = related_artefacts(scratch, "hist", v1, k=5, threshold=2.0, subject_hints="as_written")
    assert [r[0] for r in rows] == ["cand"]


def test_related_as_written_applies_at_latest_version_too(scratch):
    # mode boundary (spec test c): explicit version == latest with soft-deleted hint
    _, sv, _ = upsert_artefact(scratch, "subject", content="s", source="t", author="t")
    upsert_hint(scratch, "s", _vec(0.0), artefact_id="subject", artefact_version=sv)
    scratch.execute("UPDATE hints SET deleted_at = now() WHERE artefact_id = 'subject'")
    _, cv, _ = upsert_artefact(scratch, "cand", content="c", source="t", author="t")
    upsert_hint(scratch, "c", _vec(0.05), artefact_id="cand", artefact_version=cv)
    rows = related_artefacts(scratch, "subject", sv, k=5, threshold=2.0, subject_hints="as_written")
    assert [r[0] for r in rows] == ["cand"]


def test_related_corpus_side_stays_live_in_both_modes(scratch):
    _, sv, _ = upsert_artefact(scratch, "subject", content="s", source="t", author="t")
    upsert_hint(scratch, "s", _vec(0.0), artefact_id="subject", artefact_version=sv)
    _, cv, _ = upsert_artefact(scratch, "cand", content="c", source="t", author="t")
    upsert_hint(scratch, "c", _vec(0.05), artefact_id="cand", artefact_version=cv)
    scratch.execute("UPDATE hints SET deleted_at = now() WHERE artefact_id = 'cand'")
    assert related_artefacts(scratch, "subject", sv, k=5, threshold=2.0) == []
    assert related_artefacts(scratch, "subject", sv, k=5, threshold=2.0,
                             subject_hints="as_written") == []


def test_related_corpus_excludes_live_old_version_hints(scratch):
    # regression guard: dropping the latest-version JOIN while keeping deleted_at IS NULL
    # would match this still-live OLD hint (near) instead of the latest hint (far)
    _, sv, _ = upsert_artefact(scratch, "subject", content="s", source="t", author="t")
    upsert_hint(scratch, "s", _vec(0.0), artefact_id="subject", artefact_version=sv)
    _, c1, _ = upsert_artefact(scratch, "cand", content="v1", source="t", author="t")
    upsert_hint(scratch, "old-near", _vec(0.01), artefact_id="cand", artefact_version=c1)  # live, old, NEAR
    _, c2, _ = upsert_artefact(scratch, "cand", content="v2", source="t", author="t")
    upsert_hint(scratch, "new-far", _vec(0.3), artefact_id="cand", artefact_version=c2)    # live, latest, FAR
    for mode, ver in (("live", None), ("as_written", sv)):
        rows = related_artefacts(scratch, "subject", ver, k=5, threshold=2.0, subject_hints=mode)
        assert [(r[0], r[1]) for r in rows] == [("cand", 2)]
        # correct impl (latest FAR hint [0.7,0.3]) → distance ≈ 0.081;
        # broken impl (old NEAR hint [0.99,0.01]) → distance ≈ 0.00005.
        # 0.05 separates the two decisively (panel r1: >0.1 rejected correct code).
        assert rows[0][2] > 0.05


def test_related_threshold_and_k_apply(scratch):
    sv = _seed_subject_and_candidates(scratch)
    near_only = related_artefacts(scratch, "subject", sv, k=5, threshold=0.02)
    assert [r[0] for r in near_only] == ["near"]
    limited = related_artefacts(scratch, "subject", sv, k=1, threshold=2.0)
    assert [r[0] for r in limited] == ["near"]


def test_artefact_exists_probe(scratch):
    upsert_artefact(scratch, "probe", content="x", source="t", author="t")
    assert artefact_exists(scratch, "probe") is True
    assert artefact_exists(scratch, "probe", 1) is True
    assert artefact_exists(scratch, "probe", 9) is False
    assert artefact_exists(scratch, "nope") is False


def test_latest_version(scratch):
    upsert_artefact(scratch, "lv", content="a", source="t", author="t")
    upsert_artefact(scratch, "lv", content="b", source="t", author="t")
    assert latest_version(scratch, "lv") == 2
    assert latest_version(scratch, "nope") is None


def test_references_outgoing_and_backlinks(scratch):
    upsert_artefact(scratch, "target", content="plain", source="t", author="t")
    upsert_artefact(scratch, "citer", content="see `target` here", source="t", author="t")
    upsert_artefact(scratch, "subject", content="mentions `target` too", source="t", author="t")
    result = references(scratch, "subject", 1)
    assert result["references"] == ["target"]
    assert result["referenced_by"] == []
    back = references(scratch, "target", 1)
    assert back["referenced_by"] == ["citer", "subject"]


def test_references_backlinks_use_latest_bodies_only(scratch):
    upsert_artefact(scratch, "subject", content="s", source="t", author="t")
    upsert_artefact(scratch, "flip", content="cites `subject`", source="t", author="t")
    upsert_artefact(scratch, "flip", content="no longer cites", source="t", author="t")
    assert references(scratch, "subject", 1)["referenced_by"] == []


def test_references_outgoing_uses_requested_version_body(scratch):
    upsert_artefact(scratch, "target", content="x", source="t", author="t")
    upsert_artefact(scratch, "subject", content="cites `target`", source="t", author="t")
    upsert_artefact(scratch, "subject", content="cites nothing now", source="t", author="t")
    assert references(scratch, "subject", 1)["references"] == ["target"]
    assert references(scratch, "subject", 2)["references"] == []


def test_references_null_content_subject_yields_empty_outgoing(scratch):
    scratch.execute(
        "INSERT INTO artefacts (artefact_id, version, content, content_bytes, content_mime,"
        " content_hash, source, author) VALUES ('bin', 1, NULL, %s, 'image/png', 'h', 't', 't')",
        (b"\x89PNG",),
    )
    upsert_artefact(scratch, "other", content="x", source="t", author="t")
    assert references(scratch, "bin", 1) == {"references": [], "referenced_by": []}


def test_references_backslash_id_backlink_found(scratch):
    # strpos soundness: LIKE would treat the backslash as its escape char and under-match
    scratch.execute(
        "INSERT INTO artefacts (artefact_id, version, content, content_hash, source, author)"
        " VALUES ('wiki\\page', 1, 'body', 'h', 't', 't')"
    )
    upsert_artefact(scratch, "citer", content="see `wiki\\page` here", source="t", author="t")
    assert references(scratch, "wiki\\page", 1)["referenced_by"] == ["citer"]


def test_references_wildcard_ids_do_not_overmatch(scratch):
    scratch.execute(
        "INSERT INTO artefacts (artefact_id, version, content, content_hash, source, author)"
        " VALUES ('a_b', 1, 'body', 'h', 't', 't')"
    )
    upsert_artefact(scratch, "noise", content="axb is not a citation of anything", source="t", author="t")
    upsert_artefact(scratch, "citer", content="uses a_b for real", source="t", author="t")
    assert references(scratch, "a_b", 1)["referenced_by"] == ["citer"]
