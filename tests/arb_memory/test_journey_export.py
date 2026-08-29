import json
import os
from pathlib import Path

import pytest

from arb_memory.store import upsert_artefact, upsert_hint


def _unit_vec(index):
    vec = [0.0] * 1536
    vec[index] = 1.0
    return vec


def test_extract_refs_boundaries_self_dangling_and_binary():
    from arb_memory.journey_export import extract_refs

    known = {"art-0123456789abcdef", "art-fedcba9876543210", "wiki-alpha_page"}
    content = (
        "art-0123456789abcdef xart-fedcba9876543210 "
        "art-fedcba9876543210y `wiki-alpha_page` "
        "art-9999999999999999 `wiki-missing` art-0123456789abcdef"
    )

    refs, dangling = extract_refs(content, "art-0123456789abcdef", known)

    assert refs == {"wiki-alpha_page"}
    assert dangling == 2
    assert extract_refs(None, "self", known) == (set(), 0)
    assert extract_refs(b"art-0123456789abcdef", "self", known) == (set(), 0)


def test_ref_scanners_share_ascii_boundary_semantics_for_unicode_adjacent_ids():
    from arb_memory.journey_export import _verify_scan, extract_refs

    known = {"art-0123456789abcdef", "wiki-alpha"}
    content = "éart-0123456789abcdefé and `wiki-alpha`"

    assert extract_refs(content, "wiki-self", known) == (known, 0)
    assert _verify_scan(content, "wiki-self", known) == (known, 0)


@pytest.mark.parametrize(
    ("content", "artefact_id", "expected"),
    [
        ("# Main Heading\nbody", "id-a", "Main Heading"),
        ("\nfirst non-empty\nsecond", "id-b", "first non-empty"),
        ("x" * 140, "id-c", "x" * 120),
        (None, "id-d", "id-d"),
        (b"\x00\x01", "id-e", "id-e"),
    ],
)
def test_node_title(content, artefact_id, expected):
    from arb_memory.journey_export import node_title

    assert node_title(content, artefact_id) == expected


@pytest.mark.parametrize(
    ("artefact_id", "mime", "expected"),
    [
        ("wiki-bridge-manifest", "text/plain", "wiki-manifest"),
        ("wiki-bridge-overview", "text/plain", "wiki-page"),
        ("learn-abc", "text/plain", "learn-proposal"),
        ("howto-deploy", "text/markdown", "howto"),
        ("art-0123456789abcdef", "application/octet-stream", "note"),
    ],
)
def test_node_kind(artefact_id, mime, expected):
    from arb_memory.journey_export import node_kind

    assert node_kind(artefact_id, mime) == expected


def test_build_snapshot_nodes_edges_counts_and_free_hints(scratch):
    from arb_memory.journey_export import build_snapshot

    _, v_hub1, _ = upsert_artefact(
        scratch,
        "wiki-hub",
        content="# Old\nart-aaaaaaaaaaaaaaaa",
        source="wiki",
        author="bot",
    )
    _, v_hub, _ = upsert_artefact(
        scratch,
        "wiki-hub",
        content="# Hub\nart-aaaaaaaaaaaaaaaa `wiki-leaf` art-deadbeefdeadbeef",
        source="wiki",
        author="bot",
    )
    _, v_art, _ = upsert_artefact(
        scratch,
        "art-aaaaaaaaaaaaaaaa",
        content="Artifact body",
        source="seat",
        author="mark",
    )
    upsert_artefact(scratch, "wiki-leaf", content="Leaf links `wiki-hub`", source="wiki", author="bot")
    upsert_artefact(scratch, "learn-prop", content="solo", source="learn", author="mark")
    upsert_artefact(scratch, "bin-node", content_bytes=b"\x00\x01", mime="application/octet-stream")
    old_hint, _ = upsert_hint(
        scratch,
        "old",
        _unit_vec(1),
        artefact_id="wiki-hub",
        artefact_version=v_hub1,
        metadata={"tags": ["old"]},
    )
    upsert_hint(
        scratch,
        "live",
        _unit_vec(2),
        artefact_id="wiki-hub",
        artefact_version=v_hub,
        metadata={"tags": ["hub", "wiki"]},
    )
    deleted, _ = upsert_hint(
        scratch,
        "deleted",
        _unit_vec(3),
        artefact_id="art-aaaaaaaaaaaaaaaa",
        artefact_version=v_art,
        metadata={"tags": ["deleted"]},
    )
    free_id, _ = upsert_hint(scratch, "free secret text", _unit_vec(4), metadata={"tags": ["free", "hub"]})
    scratch.execute("UPDATE hints SET deleted_at = now() WHERE id IN (%s, %s)", (old_hint, deleted))

    snapshot = build_snapshot(scratch)

    nodes = {node["id"]: node for node in snapshot["nodes"]}
    assert nodes["wiki-hub"]["title"] == "Hub"
    assert nodes["wiki-hub"]["tags"] == ["hub", "wiki"]
    assert nodes["wiki-hub"]["version"] == 2
    assert nodes["wiki-hub"]["anchored_hint_count"] == 1
    assert nodes["wiki-hub"]["out_degree"] == 2
    assert nodes["wiki-hub"]["in_degree"] == 1
    assert nodes["learn-prop"]["kind"] == "learn-proposal"
    assert nodes["bin-node"]["title"] == "bin-node"
    assert set(nodes) == {"wiki-hub", "art-aaaaaaaaaaaaaaaa", "wiki-leaf", "learn-prop", "bin-node"}
    assert {tuple(edge) for edge in snapshot["edges"]} == {
        ("wiki-hub", "art-aaaaaaaaaaaaaaaa"),
        ("wiki-hub", "wiki-leaf"),
        ("wiki-leaf", "wiki-hub"),
    }
    assert snapshot["counts"]["nodes"] == 5
    assert snapshot["counts"]["edges"] == 3
    assert snapshot["counts"]["dangling"] == 1
    assert snapshot["counts"]["isolated"] == 2
    assert snapshot["counts"]["free_hints"] == 1
    assert snapshot["counts"]["generated_at"] == snapshot["generated_at"]
    assert snapshot["free_hints"] == [
        {"id": free_id, "created_at": snapshot["free_hints"][0]["created_at"], "tags": ["free", "hub"]}
    ]
    assert "text" not in snapshot["free_hints"][0]


def test_golden_edge_set_fixture(scratch):
    from arb_memory.journey_export import build_snapshot

    upsert_artefact(
        scratch,
        "wiki-root",
        content="See art-1111111111111111 and `wiki-child` and art-ffffffffffffffff",
    )
    upsert_artefact(scratch, "wiki-child", content="Back to `wiki-root` and `wiki-grandchild`")
    upsert_artefact(scratch, "wiki-grandchild", content="Leaf")
    upsert_artefact(scratch, "art-1111111111111111", content="Evidence")

    expected = json.loads(
        (Path(__file__).parent / "fixtures" / "journey" / "golden_edges.json").read_text(encoding="utf-8")
    )
    snapshot = build_snapshot(scratch)

    assert sorted(snapshot["edges"]) == expected["edges"]
    assert snapshot["counts"]["dangling"] == expected["dangling"]


def test_verify_refs_independent_and_detects_corruption(scratch, tmp_path, monkeypatch):
    import arb_memory.journey_export as journey_export
    from arb_memory.journey_export import _verify_refs, build_snapshot, extract_refs, write_snapshot

    upsert_artefact(scratch, "wiki-a", content="See `wiki-b`")
    upsert_artefact(scratch, "wiki-b", content="Back `wiki-a`")
    snapshot = build_snapshot(scratch)
    path = write_snapshot(snapshot, tmp_path)
    corrupted = json.loads(path.read_text(encoding="utf-8"))
    corrupted["edges"] = [["wiki-a", "wiki-missing"]]
    path.write_text(json.dumps(corrupted), encoding="utf-8")

    assert _verify_refs.__code__ is not extract_refs.__code__
    monkeypatch.setattr(journey_export, "extract_refs", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError))
    assert _verify_refs(scratch, json.loads(path.read_text(encoding="utf-8"))).ok is False


def test_main_builds_atomically_and_verify_only_detects_corruption(scratch, tmp_path, monkeypatch, capsys):
    import arb_memory.journey_export as journey_export

    upsert_artefact(scratch, "wiki-a", content="See `wiki-b`")
    upsert_artefact(scratch, "wiki-b", content="Back `wiki-a`")
    schema = scratch.execute("SELECT current_schema()").fetchone()[0]
    dsn = os.environ["ARB_MEMORY_DSN"]
    monkeypatch.setenv("ARB_MEMORY_DSN", dsn)
    monkeypatch.setenv("PGOPTIONS", f"-c search_path={schema},public")

    assert journey_export.main(["--snapshot-dir", str(tmp_path)]) == 0
    path = tmp_path / "graph.json"
    assert path.exists()
    assert not (tmp_path / "graph.json.tmp").exists()
    assert journey_export.main(["--verify-only", str(path)]) == 0

    data = json.loads(path.read_text(encoding="utf-8"))
    data["edges"] = [["wiki-a", "wiki-missing"]]
    path.write_text(json.dumps(data), encoding="utf-8")

    assert journey_export.main(["--verify-only", str(path)]) == 1
    assert "verification failed" in capsys.readouterr().err


def test_main_empty_store_exports_and_verify_only_handles_empty_snapshot(scratch, tmp_path, monkeypatch):
    import arb_memory.journey_export as journey_export

    schema = scratch.execute("SELECT current_schema()").fetchone()[0]
    monkeypatch.setenv("ARB_MEMORY_DSN", os.environ["ARB_MEMORY_DSN"])
    monkeypatch.setenv("PGOPTIONS", f"-c search_path={schema},public")

    assert journey_export.main(["--snapshot-dir", str(tmp_path)]) == 0
    path = tmp_path / "graph.json"
    snapshot = json.loads(path.read_text(encoding="utf-8"))

    assert snapshot["nodes"] == []
    assert snapshot["edges"] == []
    assert snapshot["free_hints"] == []
    assert snapshot["counts"]["nodes"] == 0
    assert snapshot["counts"]["edges"] == 0
    assert snapshot["counts"]["dangling"] == 0
    assert snapshot["counts"]["isolated"] == 0
    assert snapshot["counts"]["free_hints"] == 0
    assert journey_export.main(["--verify-only", str(path)]) == 0


def test_verify_only_stale_snapshot_is_notice_not_failure(scratch, tmp_path, monkeypatch, capsys):
    import arb_memory.journey_export as journey_export

    upsert_artefact(scratch, "wiki-a", content="initial")
    schema = scratch.execute("SELECT current_schema()").fetchone()[0]
    monkeypatch.setenv("ARB_MEMORY_DSN", os.environ["ARB_MEMORY_DSN"])
    monkeypatch.setenv("PGOPTIONS", f"-c search_path={schema},public")
    assert journey_export.main(["--snapshot-dir", str(tmp_path)]) == 0

    upsert_artefact(scratch, "wiki-new", content="newer")

    assert journey_export.main(["--verify-only", str(tmp_path / "graph.json")]) == 0
    assert "snapshot is stale" in capsys.readouterr().err
