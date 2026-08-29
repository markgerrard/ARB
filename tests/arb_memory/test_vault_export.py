from pathlib import Path

import pytest

from arb_memory.store import upsert_artefact, upsert_hint
from arb_memory.vault_export import FOOTER_MARKER, export_vault, _filename, _idhash, _slug, _stem


def _read(vault_root, artefact_id):
    path = Path(vault_root) / _filename(artefact_id)
    return path.read_text()


def test_frontmatter_carries_aliases_with_artefact_id(scratch, tmp_path):
    upsert_artefact(scratch, "spec-alias", content="body", source="seat", author="mark")

    export_vault(scratch, str(tmp_path))

    body = _read(tmp_path, "spec-alias")
    assert 'aliases: ["spec-alias"]' in body


def test_only_latest_version_is_rendered(scratch, tmp_path):
    upsert_artefact(scratch, "spec-a", content="v1 body", source="seat", author="mark")
    upsert_artefact(scratch, "spec-a", content="v2 body", source="seat", author="mark")

    export_vault(scratch, str(tmp_path))

    body = _read(tmp_path, "spec-a")
    assert "v2 body" in body
    assert "v1 body" not in body
    assert "version: 2" in body


def test_tags_from_single_linked_hint_appear_in_frontmatter(scratch, tmp_path, fake_embed):
    _, version, _ = upsert_artefact(scratch, "spec-b", content="body", source="seat", author="mark")
    upsert_hint(
        scratch, "a hint about spec-b", fake_embed("a hint about spec-b"),
        artefact_id="spec-b", artefact_version=version,
        metadata={"tags": ["review", "design"]},
    )

    export_vault(scratch, str(tmp_path))

    body = _read(tmp_path, "spec-b")
    assert 'tags: ["design", "review"]' in body


def test_tags_from_multiple_linked_hints_are_deduplicated_and_sorted(scratch, tmp_path, fake_embed):
    _, version, _ = upsert_artefact(scratch, "spec-c", content="body", source="seat", author="mark")
    upsert_hint(
        scratch, "first hint", fake_embed("first hint"),
        artefact_id="spec-c", artefact_version=version,
        metadata={"tags": ["zeta", "review"]},
    )
    upsert_hint(
        scratch, "second hint", fake_embed("second hint"),
        artefact_id="spec-c", artefact_version=version,
        metadata={"tags": ["review", "alpha"]},
    )

    export_vault(scratch, str(tmp_path))

    body = _read(tmp_path, "spec-c")
    assert 'tags: ["alpha", "review", "zeta"]' in body


def test_deleted_hint_is_excluded_from_tags(scratch, tmp_path, fake_embed):
    _, version, _ = upsert_artefact(scratch, "spec-d", content="body", source="seat", author="mark")
    hint_id, _ = upsert_hint(
        scratch, "soft-deleted hint", fake_embed("soft-deleted hint"),
        artefact_id="spec-d", artefact_version=version,
        metadata={"tags": ["should-not-appear"]},
    )
    scratch.execute("UPDATE hints SET deleted_at = now() WHERE id = %s", (hint_id,))

    export_vault(scratch, str(tmp_path))

    body = _read(tmp_path, "spec-d")
    assert "should-not-appear" not in body
    assert "tags: []" in body


def test_artefact_with_no_linked_hint_has_empty_tags(scratch, tmp_path):
    upsert_artefact(scratch, "spec-e", content="body", source="seat", author="mark")

    export_vault(scratch, str(tmp_path))

    body = _read(tmp_path, "spec-e")
    assert "tags: []" in body


def test_binary_artefact_gets_placeholder_not_decode_attempt(scratch, tmp_path):
    upsert_artefact(
        scratch, "spec-f", content_bytes=b"\x00\x01\x02",
        mime="application/octet-stream", source="seat", author="mark",
    )

    export_vault(scratch, str(tmp_path))

    body = _read(tmp_path, "spec-f")
    assert "binary artefact" in body
    assert "application/octet-stream" in body
    assert "3 bytes" in body


def test_distinct_slug_colliding_artefact_ids_get_distinct_files(scratch, tmp_path):
    upsert_artefact(scratch, "docs/foo.md", content="first", source="seat", author="mark")
    upsert_artefact(scratch, "docsfoo.md", content="second", source="seat", author="mark")

    export_vault(scratch, str(tmp_path))

    assert _slug("docs/foo.md") == _slug("docsfoo.md") == "docsfoo.md"
    assert _idhash("docs/foo.md") != _idhash("docsfoo.md")
    first_body = _read(tmp_path, "docs/foo.md")
    second_body = _read(tmp_path, "docsfoo.md")
    assert "first" in first_body
    assert "second" in second_body


def test_hostile_artefact_id_cannot_escape_vault_root(scratch, tmp_path):
    upsert_artefact(scratch, "../../etc/passwd", content="hostile", source="seat", author="mark")

    export_vault(scratch, str(tmp_path))

    # slug() strips "/" (not "."), so "../../etc/passwd" -> "....etcpasswd" -- a filename
    # made only of dots and letters, no path separator, so it cannot address a parent
    # directory. The safety property to assert is "the written file's parent is exactly
    # vault_root, nothing escaped it" -- not "no dots in the name" (dots are allowed and
    # expected here; only "/" enables traversal).
    written = list(Path(tmp_path).iterdir())
    assert len(written) == 1
    assert written[0].parent == Path(tmp_path)
    assert "/" not in written[0].name


def test_idhash_collision_fails_loud(scratch, tmp_path, monkeypatch):
    upsert_artefact(scratch, "id-one", content="a", source="seat", author="mark")
    upsert_artefact(scratch, "id-two", content="b", source="seat", author="mark")

    import arb_memory.vault_export as vault_export_module

    monkeypatch.setattr(vault_export_module, "_idhash", lambda artefact_id: "deadbeef")

    with pytest.raises(RuntimeError, match="idhash collision"):
        export_vault(scratch, str(tmp_path))


def test_rerun_is_idempotent_except_exported_at(scratch, tmp_path):
    upsert_artefact(scratch, "spec-g", content="stable body", source="seat", author="mark")

    export_vault(scratch, str(tmp_path))
    first = _read(tmp_path, "spec-g")

    export_vault(scratch, str(tmp_path))
    second = _read(tmp_path, "spec-g")

    def _strip_exported_at(text):
        return "\n".join(line for line in text.splitlines() if not line.startswith("exported_at:"))

    assert _strip_exported_at(first) == _strip_exported_at(second)
    assert "exported_at: " in first
    assert "exported_at: " in second


def test_references_footer_links_bare_art_hex_and_underscore_ids(scratch, tmp_path):
    upsert_artefact(scratch, "art-0123456789abcdef", content="t1", source="seat", author="mark")
    upsert_artefact(scratch, "project-a_overview", content="t2", source="seat", author="mark")
    upsert_artefact(
        scratch, "spec-src",
        content="See art-0123456789abcdef and project-a_overview for context.",
        source="seat", author="mark",
    )

    export_vault(scratch, str(tmp_path))

    body = _read(tmp_path, "spec-src")
    assert "## References" in body
    assert f"[[{_stem('art-0123456789abcdef')}|art-0123456789abcdef]]" in body
    assert f"[[{_stem('project-a_overview')}|project-a_overview]]" in body


def test_references_sentence_final_period_still_links(scratch, tmp_path):
    upsert_artefact(scratch, "art-0123456789abcdef", content="t", source="seat", author="mark")
    upsert_artefact(
        scratch, "spec-sentence",
        content="Recorded as art-0123456789abcdef. Next sentence.",
        source="seat", author="mark",
    )

    export_vault(scratch, str(tmp_path))

    assert "## References" in _read(tmp_path, "spec-sentence")


def test_references_hyphen_only_ids_require_backticks(scratch, tmp_path):
    upsert_artefact(scratch, "spec-a", content="t", source="seat", author="mark")
    upsert_artefact(
        scratch, "spec-bare",
        content="Plain prose mentioning spec-a without intent markers.",
        source="seat", author="mark",
    )
    upsert_artefact(
        scratch, "spec-ticked",
        content="Cited with intent: `spec-a` is the target.",
        source="seat", author="mark",
    )

    export_vault(scratch, str(tmp_path))

    assert FOOTER_MARKER not in _read(tmp_path, "spec-bare")
    assert f"[[{_stem('spec-a')}|spec-a]]" in _read(tmp_path, "spec-ticked")


def test_references_negative_cases(scratch, tmp_path):
    upsert_artefact(scratch, "art-0123456789abcdef", content="t", source="seat", author="mark")
    upsert_artefact(scratch, "wiki_page", content="t", source="seat", author="mark")
    upsert_artefact(scratch, "wiki_page_two", content="t", source="seat", author="mark")
    upsert_artefact(
        scratch, "spec-neg",
        content=(
            "URL https://x.com/art-0123456789abcdef and path docs/art-0123456789abcdef.md "
            "and adjacent xart-0123456789abcdef and wiki_page_two only."
        ),
        source="seat", author="mark",
    )

    export_vault(scratch, str(tmp_path))

    body = _read(tmp_path, "spec-neg")
    # URL-, path-, and adjacent-embedded ids never link:
    assert f"[[{_stem('art-0123456789abcdef')}|" not in body
    # wiki_page must NOT match inside wiki_page_two (prefix collision):
    assert f"[[{_stem('wiki_page')}|wiki_page]]" not in body
    assert f"[[{_stem('wiki_page_two')}|wiki_page_two]]" in body


def test_references_self_mention_and_dedup(scratch, tmp_path):
    upsert_artefact(scratch, "art-aaaaaaaaaaaaaaaa", content="t", source="seat", author="mark")
    upsert_artefact(
        scratch, "spec-dup",
        content="art-aaaaaaaaaaaaaaaa twice: art-aaaaaaaaaaaaaaaa",
        source="seat", author="mark",
    )
    upsert_artefact(
        scratch, "art-bbbbbbbbbbbbbbbb",
        content="I am art-bbbbbbbbbbbbbbbb and mention only myself.",
        source="seat", author="mark",
    )

    export_vault(scratch, str(tmp_path))

    assert _read(tmp_path, "spec-dup").count("[[") == 1
    assert FOOTER_MARKER not in _read(tmp_path, "art-bbbbbbbbbbbbbbbb")


def test_footer_blank_line_separation_regardless_of_body_ending(scratch, tmp_path):
    upsert_artefact(scratch, "art-cccccccccccccccc", content="t", source="seat", author="mark")
    upsert_artefact(
        scratch, "spec-nonl", content="ends without newline art-cccccccccccccccc",
        source="seat", author="mark",
    )
    upsert_artefact(
        scratch, "spec-withnl", content="ends with newline art-cccccccccccccccc\n",
        source="seat", author="mark",
    )

    export_vault(scratch, str(tmp_path))

    # A true BLANK line (\n\n) must precede the marker in both cases -- a single line
    # break is not enough (markdown block parsing; round-1 panel finding, all 3 seats).
    assert f"art-cccccccccccccccc\n\n{FOOTER_MARKER}" in _read(tmp_path, "spec-nonl")
    assert f"art-cccccccccccccccc\n\n{FOOTER_MARKER}" in _read(tmp_path, "spec-withnl")


def test_body_above_footer_marker_is_verbatim_content(scratch, tmp_path):
    content = "line one art-dddddddddddddddd\n\nsome `code` and text\n"
    upsert_artefact(scratch, "art-dddddddddddddddd", content="t", source="seat", author="mark")
    upsert_artefact(scratch, "spec-verbatim", content=content, source="seat", author="mark")

    export_vault(scratch, str(tmp_path))

    text = _read(tmp_path, "spec-verbatim")
    above_marker = text.split(FOOTER_MARKER)[0]
    after_frontmatter = above_marker.split("---\n", 2)[2]
    # body bytes stay verbatim; the only addition above the marker is the footer's
    # single leading newline (content already ends with \n, so no extra is prepended)
    assert after_frontmatter == content + "\n"


def _unit_vec(index, weight=1.0, second_index=None):
    vec = [0.0] * 1536
    if second_index is None:
        vec[index] = 1.0
    else:
        import math
        vec[index] = weight
        vec[second_index] = math.sqrt(1.0 - weight * weight)
    return vec


def test_related_links_near_pair_excludes_far_and_deleted(scratch, tmp_path):
    # a and b: cosine distance 1 - 0.98 = 0.02 (near). c: orthogonal, distance 1.0 (far).
    # d: identical embedding to a, but soft-deleted -- must not appear.
    _, va, _ = upsert_artefact(scratch, "near-a", content="a", source="seat", author="mark")
    upsert_hint(scratch, "hint a", _unit_vec(0), artefact_id="near-a", artefact_version=va)
    _, vb, _ = upsert_artefact(scratch, "near-b", content="b", source="seat", author="mark")
    upsert_hint(scratch, "hint b", _unit_vec(0, weight=0.98, second_index=1),
                artefact_id="near-b", artefact_version=vb)
    _, vc, _ = upsert_artefact(scratch, "far-c", content="c", source="seat", author="mark")
    upsert_hint(scratch, "hint c", _unit_vec(2), artefact_id="far-c", artefact_version=vc)
    _, vd, _ = upsert_artefact(scratch, "del-d", content="d", source="seat", author="mark")
    hid, _ = upsert_hint(scratch, "hint d", _unit_vec(0), artefact_id="del-d", artefact_version=vd)
    scratch.execute("UPDATE hints SET deleted_at = now() WHERE id = %s", (hid,))

    export_vault(scratch, str(tmp_path))

    body_a = _read(tmp_path, "near-a")
    assert "## Related" in body_a
    assert f"[[{_stem('near-b')}|near-b]]" in body_a
    assert "far-c" not in body_a
    assert "del-d" not in body_a
    # mutuality:
    assert f"[[{_stem('near-a')}|near-a]]" in _read(tmp_path, "near-b")


def test_related_min_aggregation_over_multiple_hints(scratch, tmp_path):
    # multi's hints: one orthogonal, one near anchor -- min-linkage must connect them.
    _, va, _ = upsert_artefact(scratch, "anchor", content="a", source="seat", author="mark")
    upsert_hint(scratch, "anchor hint", _unit_vec(0), artefact_id="anchor", artefact_version=va)
    _, vm, _ = upsert_artefact(scratch, "multi", content="m", source="seat", author="mark")
    upsert_hint(scratch, "multi far hint", _unit_vec(3), artefact_id="multi", artefact_version=vm)
    upsert_hint(scratch, "multi near hint", _unit_vec(0, weight=0.95, second_index=1),
                artefact_id="multi", artefact_version=vm)

    export_vault(scratch, str(tmp_path))

    assert f"[[{_stem('multi')}|multi]]" in _read(tmp_path, "anchor")


def test_similarity_threshold_env_resolution():
    from arb_memory.vault_export import resolve_settings

    env = {"ARB_VAULT_EXPORT_DSN": "d", "ARB_VAULT_EXPORT_ROOT": "r"}
    assert resolve_settings(env).similarity_threshold == 0.35
    env["ARB_VAULT_EXPORT_SIMILARITY_THRESHOLD"] = "0.5"
    assert resolve_settings(env).similarity_threshold == 0.5


def test_footer_renders_reference_and_related_with_distance(scratch, tmp_path):
    _, sv, _ = upsert_artefact(scratch, "spec-x", content="see `spec-y`\n", source="t", author="t")
    upsert_hint(scratch, "x", [1.0] + [0.0] * 1535, artefact_id="spec-x", artefact_version=sv)
    _, yv, _ = upsert_artefact(scratch, "spec-y", content="plain", source="t", author="t")
    # cosine distance to [1,0,...] = 1 - 1/sqrt(1.01) ≈ 0.005 → renders "(distance 0.00)"
    upsert_hint(scratch, "y", [1.0, 0.1] + [0.0] * 1534, artefact_id="spec-y", artefact_version=yv)

    export_vault(scratch, str(tmp_path))

    body = _read(tmp_path, "spec-x")
    # BYTE-EXACT footer oracle (spec obligation): everything after the marker must equal
    # this literal — extra bytes, reordered sections, or changed spacing all fail.
    stem = _stem("spec-y")
    expected_footer = (
        "\n\n## References\n"
        f"- [[{stem}|spec-y]]\n"
        "\n## Related\n"
        f"- [[{stem}|spec-y]] (distance 0.00)\n"
    )
    marker_idx = body.index(FOOTER_MARKER)
    assert body[marker_idx + len(FOOTER_MARKER):] == expected_footer


def test_exporter_deleted_latest_hint_yields_no_related_footer(scratch, tmp_path):
    # spec counter-case (b): exporter runs 'live' mode; a soft-deleted latest-version
    # hint must produce NO Related entry (guards the mode's False path end-to-end)
    _, sv, _ = upsert_artefact(scratch, "spec-del", content="plain", source="t", author="t")
    upsert_hint(scratch, "d", [1.0] + [0.0] * 1535, artefact_id="spec-del", artefact_version=sv)
    scratch.execute("UPDATE hints SET deleted_at = now() WHERE artefact_id = 'spec-del'")
    _, cv, _ = upsert_artefact(scratch, "spec-near", content="c", source="t", author="t")
    upsert_hint(scratch, "n", [0.999, 0.04] + [0.0] * 1534, artefact_id="spec-near", artefact_version=cv)

    export_vault(scratch, str(tmp_path))

    body = _read(tmp_path, "spec-del")
    assert "## Related" not in body
