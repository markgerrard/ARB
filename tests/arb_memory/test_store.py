from contextlib import nullcontext

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("pgvector")

from arb_memory import store as store_module
from arb_memory.store import retrieve, upsert_artefact, upsert_hint, write_artefact_and_hints


def _unit_vec(index):
    vec = [0.0] * 1536
    vec[index] = 1.0
    return vec


class _FakeResult:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _ArtefactContractConn:
    def __init__(self):
        self.rows = []
        self.statements = []  # (query, params) for allocation/INSERT assertions

    def execute(self, query, params):
        self.statements.append((query, params))
        if "SELECT version, content_hash" in query:
            return _FakeResult(self.rows[-1] if self.rows else None)
        if "SELECT COALESCE(max(version), 0) + 1" in query:
            return _FakeResult(((self.rows[-1][0] + 1) if self.rows else 1,))
        if "INSERT INTO artefacts" in query:
            self.rows.append((params[1], params[6]))
            return _FakeResult(None)
        raise AssertionError(f"unexpected query: {query}")


class _HintContractConn:
    def __init__(self):
        self.inserted = False

    def execute(self, query, params):
        if "INSERT INTO hints" in query:
            if self.inserted:
                return _FakeResult(None)
            self.inserted = True
            return _FakeResult((7,))
        if "SELECT id FROM hints" in query:
            return _FakeResult((7,))
        raise AssertionError(f"unexpected query: {query}")


class _TransactionContractConn:
    def transaction(self):
        return nullcontext()


def test_upsert_artefact_contract_without_database(monkeypatch):
    monkeypatch.setattr(store_module, "_register_vector", lambda conn: None)
    monkeypatch.setattr(store_module, "artefact_hash", lambda content, content_bytes, mime: content)
    conn = _ArtefactContractConn()

    assert upsert_artefact(conn, "doc", content="A") == ("doc", 1, "stored")
    assert upsert_artefact(conn, "doc", content="B") == ("doc", 2, "stored")
    assert upsert_artefact(conn, "doc", content="A") == ("doc", 3, "stored")
    assert upsert_artefact(conn, "doc", content="A") == ("doc", 3, "deduped")


def test_upsert_artefact_refuses_stale_expected_version_without_database(monkeypatch):
    """AC1: head at v2 + expected_version=1 → refused_version_mismatch, no INSERT."""
    monkeypatch.setattr(store_module, "_register_vector", lambda conn: None)
    monkeypatch.setattr(store_module, "artefact_hash", lambda content, content_bytes, mime: content)
    conn = _ArtefactContractConn()
    conn.rows = [(2, "B")]  # head at version 2

    result = upsert_artefact(conn, "doc", content="C", expected_version=1)

    assert result == ("doc", 2, "refused_version_mismatch")
    assert conn.rows == [(2, "B")]  # zero INSERT INTO artefacts


def test_upsert_artefact_create_only_and_absent_head_without_database(monkeypatch):
    """AC14 (store half): expected_version=0 create-only; absent head + expected>=1 → version 0."""
    monkeypatch.setattr(store_module, "_register_vector", lambda conn: None)
    monkeypatch.setattr(store_module, "artefact_hash", lambda content, content_bytes, mime: content)

    empty = _ArtefactContractConn()
    assert upsert_artefact(empty, "doc", content="new", expected_version=0) == (
        "doc", 1, "stored",
    )

    empty2 = _ArtefactContractConn()
    assert upsert_artefact(empty2, "doc", content="new", expected_version=2) == (
        "doc", 0, "refused_version_mismatch",
    )
    assert empty2.rows == []

    existing = _ArtefactContractConn()
    existing.rows = [(3, "old")]
    assert upsert_artefact(existing, "doc", content="new", expected_version=0) == (
        "doc", 3, "refused_version_mismatch",
    )
    assert existing.rows == [(3, "old")]


def test_upsert_artefact_guarded_allocates_expected_plus_one_without_max(monkeypatch):
    """R-1: guarded path INSERT version == expected+1; no max() allocation query."""
    monkeypatch.setattr(store_module, "_register_vector", lambda conn: None)
    monkeypatch.setattr(store_module, "artefact_hash", lambda content, content_bytes, mime: content)
    conn = _ArtefactContractConn()
    conn.rows = [(1, "A")]

    result = upsert_artefact(conn, "doc", content="B", expected_version=1)

    assert result == ("doc", 2, "stored")
    inserts = [p for q, p in conn.statements if "INSERT INTO artefacts" in q]
    assert len(inserts) == 1
    assert inserts[0][1] == 2  # version param == expected_version + 1
    assert not any("COALESCE(max(version)" in q for q, _ in conn.statements)


def test_upsert_artefact_compare_before_dedup_identical_content_refuses(monkeypatch):
    """R-2.2: stale expected + content identical to head → refuse, not dedup."""
    monkeypatch.setattr(store_module, "_register_vector", lambda conn: None)
    monkeypatch.setattr(store_module, "artefact_hash", lambda content, content_bytes, mime: content)
    conn = _ArtefactContractConn()
    conn.rows = [(2, "same")]  # head v2, content hash "same"

    result = upsert_artefact(conn, "doc", content="same", expected_version=1)

    assert result == ("doc", 2, "refused_version_mismatch")
    assert not any("INSERT INTO artefacts" in q for q, _ in conn.statements)


def test_upsert_hint_contract_without_database(monkeypatch):
    monkeypatch.setattr(store_module, "_register_vector", lambda conn: None)
    conn = _HintContractConn()

    assert upsert_hint(conn, "same", [0.0]) == (7, True)
    assert upsert_hint(conn, "same", [0.0]) == (7, False)


def test_write_receipt_contract_without_database(monkeypatch):
    monkeypatch.setattr(store_module, "_register_vector", lambda conn: None)
    monkeypatch.setattr(store_module, "upsert_artefact", lambda *args, **kwargs: ("doc", 1, "deduped"))
    hint_results = iter(((1, False), (2, True)))
    monkeypatch.setattr(store_module, "upsert_hint", lambda *args, **kwargs: next(hint_results))

    receipt = write_artefact_and_hints(
        _TransactionContractConn(),
        artefact={"artefact_id": "doc", "content": "same"},
        hints=[{"text": "old", "embedding": [0.0]}, {"text": "new", "embedding": [0.0]}],
    )

    assert receipt == {
        "artefact_outcome": "deduped",
        "artefact_id": "doc",
        "version": 1,
        "hints_stored": 1,
    }


def test_write_refusal_receipt_never_calls_upsert_hint(monkeypatch):
    """AC3: refusal receipt is four fields; upsert_hint must never run."""
    monkeypatch.setattr(store_module, "_register_vector", lambda conn: None)
    monkeypatch.setattr(
        store_module,
        "upsert_artefact",
        lambda *args, **kwargs: ("doc", 2, "refused_version_mismatch"),
    )

    def _boom(*args, **kwargs):
        raise AssertionError("upsert_hint must not be called on refusal")

    monkeypatch.setattr(store_module, "upsert_hint", _boom)

    receipt = write_artefact_and_hints(
        _TransactionContractConn(),
        artefact={"artefact_id": "doc", "content": "C", "expected_version": 1},
        hints=[{"text": "orphan", "embedding": [0.0]}],
    )

    assert receipt == {
        "artefact_outcome": "refused_version_mismatch",
        "artefact_id": "doc",
        "version": 2,
        "hints_stored": 0,
    }


def test_version_on_resave_keeps_old(scratch):
    aid, v1, outcome1 = upsert_artefact(scratch, "doc.md", content="one")
    assert aid == "doc.md"
    assert v1 == 1
    assert outcome1 == "stored"
    _, v2, outcome2 = upsert_artefact(scratch, "doc.md", content="two")
    assert v2 == 2
    assert outcome2 == "stored"
    row = scratch.execute("SELECT content FROM artefacts WHERE artefact_id='doc.md' AND version=1").fetchone()
    assert row[0] == "one"


def test_identical_resave_is_noop(scratch):
    upsert_artefact(scratch, "doc.md", content="same")
    _, v, outcome = upsert_artefact(scratch, "doc.md", content="same")
    assert v == 1
    assert outcome == "deduped"
    n = scratch.execute("SELECT count(*) FROM artefacts WHERE artefact_id='doc.md'").fetchone()[0]
    assert n == 1


def test_upsert_artefact_reverted_content_creates_new_latest_version(scratch):
    upsert_artefact(scratch, "osc", content="content A")
    upsert_artefact(scratch, "osc", content="content B")
    aid, version, outcome = upsert_artefact(scratch, "osc", content="content A")

    assert (aid, version) == ("osc", 3)
    assert outcome == "stored"
    row = scratch.execute(
        "SELECT content FROM artefacts WHERE artefact_id = %s ORDER BY version DESC LIMIT 1",
        ("osc",),
    ).fetchone()
    assert row[0] == "content A"


def test_upsert_artefact_unchanged_from_latest_still_noops(scratch):
    upsert_artefact(scratch, "stable", content="same")
    aid, version, outcome = upsert_artefact(scratch, "stable", content="same")
    assert (aid, version) == ("stable", 1)
    assert outcome == "deduped"
    count = scratch.execute(
        "SELECT count(*) FROM artefacts WHERE artefact_id = %s", ("stable",)
    ).fetchone()[0]
    assert count == 1


def test_content_idempotent_hint(scratch, fake_embed):
    first_id, first_inserted = upsert_hint(scratch, "same hint", fake_embed("same hint"))
    second_id, second_inserted = upsert_hint(scratch, "same hint", fake_embed("same hint"))
    assert first_inserted is True
    assert second_id == first_id
    assert second_inserted is False
    n = scratch.execute("SELECT count(*) FROM hints").fetchone()[0]
    assert n == 1


def test_hint_hash_is_version_aware(scratch, fake_embed):
    _, v1, _ = upsert_artefact(scratch, "doc.md", content="one")
    _, v2, _ = upsert_artefact(scratch, "doc.md", content="two")
    upsert_hint(
        scratch,
        "same text",
        fake_embed("same text"),
        artefact_id="doc.md",
        artefact_version=v1,
    )
    upsert_hint(
        scratch,
        "same text",
        fake_embed("same text"),
        artefact_id="doc.md",
        artefact_version=v2,
    )
    n = scratch.execute("SELECT count(*) FROM hints WHERE text='same text'").fetchone()[0]
    assert n == 2


def test_dual_write_atomic(scratch):
    scratch.execute(
        """
        CREATE FUNCTION boom() RETURNS trigger AS $$
        BEGIN IF NEW.text = 'boom' THEN RAISE EXCEPTION 'boom'; END IF; RETURN NEW; END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER hints_boom BEFORE INSERT ON hints
            FOR EACH ROW EXECUTE FUNCTION boom();
        """
    )
    good_vec = [0.0] * 1536
    with pytest.raises(psycopg.errors.RaiseException):
        write_artefact_and_hints(
            scratch,
            artefact={"artefact_id": "doc.md", "content": "c"},
            hints=[{"text": "boom", "embedding": good_vec}],
        )
    scratch.rollback()
    assert scratch.execute("SELECT count(*) FROM artefacts WHERE artefact_id='doc.md'").fetchone()[0] == 0
    assert scratch.execute("SELECT count(*) FROM hints").fetchone()[0] == 0


def test_two_step_faithful(scratch, fake_embed):
    write_artefact_and_hints(
        scratch,
        artefact={"artefact_id": "doc.md", "content": "ONE"},
        hints=[{"text": "the doc about one", "embedding": fake_embed("the doc about one")}],
    )
    upsert_artefact(scratch, "doc.md", content="TWO")
    out = retrieve(scratch, "the doc about one", k=1, embed=fake_embed)
    assert out[0]["hint"]["artefact_version"] == 1
    assert out[0]["artefact"]["content"] == "ONE"


def test_repo_pointer_hint_returns_pointer(scratch, fake_embed):
    upsert_hint(
        scratch,
        "see the gate",
        fake_embed("see the gate"),
        repo_pointer="skills/bridge-protocol/gate/gate.py@HEAD",
    )
    out = retrieve(scratch, "see the gate", k=1, embed=fake_embed)
    assert out[0]["artefact"] is None and out[0]["repo_pointer"].startswith("skills/")


def test_two_step_hybrid_fuses_vector_and_lexical(scratch):
    strong_vector, _ = upsert_hint(
        scratch,
        "semantic neighbor without the exact term",
        _unit_vec(0),
    )
    strong_lexical, _ = upsert_hint(
        scratch,
        "needle exact lexical match",
        _unit_vec(1),
    )

    out = retrieve(scratch, "needle", k=2, embed=lambda _: _unit_vec(0))
    ids = [row["hint"]["id"] for row in out]

    assert ids.index(strong_lexical) <= ids.index(strong_vector)


def test_new_version_retires_superseded_index_hints(scratch, fake_embed):
    # live incident 2026-07-07: a refreshed wiki page's v1 index hint stayed searchable and
    # OUTRANKED the v2 hint (near-identical text; lexical tie-break is by ascending hint id),
    # and retrieve() pins the artefact at the hint's version -- so search served the stale
    # page indefinitely. A replacement index hint must retire its predecessors.
    write_artefact_and_hints(
        scratch,
        artefact={"artefact_id": "page.md", "content": "the page about widgets, version one"},
        hints=[{"text": "the page about widgets", "embedding": fake_embed("the page about widgets"),
                "metadata": {"kind": "artefact_index", "artefact_id": "page.md"}}],
    )
    write_artefact_and_hints(
        scratch,
        artefact={"artefact_id": "page.md", "content": "the page about widgets, version two"},
        hints=[{"text": "the page about widgets (updated)",
                "embedding": fake_embed("the page about widgets (updated)"),
                "metadata": {"kind": "artefact_index", "artefact_id": "page.md"}}],
    )
    out = retrieve(scratch, "the page about widgets", k=5, embed=fake_embed)
    versions = [row["hint"]["artefact_version"] for row in out
                if row["hint"]["artefact_id"] == "page.md"]
    assert versions == [2]  # v1 index hint retired, not still competing
    assert out[0]["artefact"]["content"].endswith("version two")
    retired = scratch.execute(
        "SELECT deleted_at FROM hints WHERE artefact_id='page.md' AND artefact_version=1"
    ).fetchone()[0]
    assert retired is not None  # soft-deleted, not dropped


def test_new_version_keeps_non_index_hints_faithful(scratch, fake_embed):
    # pinned-version faithfulness (test_two_step_faithful) must survive: only artefact_index
    # projection hints retire; evidence/audit hints pinned to an old version stay searchable.
    write_artefact_and_hints(
        scratch,
        artefact={"artefact_id": "page.md", "content": "original"},
        hints=[{"text": "audit evidence about the original",
                "embedding": fake_embed("audit evidence about the original")}],
    )
    write_artefact_and_hints(
        scratch,
        artefact={"artefact_id": "page.md", "content": "revised"},
        hints=[{"text": "index for revised", "embedding": fake_embed("index for revised"),
                "metadata": {"kind": "artefact_index", "artefact_id": "page.md"}}],
    )
    out = retrieve(scratch, "audit evidence about the original", k=1, embed=fake_embed)
    assert out[0]["hint"]["artefact_version"] == 1
    assert out[0]["artefact"]["content"] == "original"  # faithful history intact


def test_no_replacement_index_hint_no_retirement(scratch, fake_embed):
    # a new artefact version WITHOUT a replacement index hint must not retire the old one --
    # retiring it would make the artefact invisible to search entirely
    write_artefact_and_hints(
        scratch,
        artefact={"artefact_id": "page.md", "content": "one"},
        hints=[{"text": "findable page", "embedding": fake_embed("findable page"),
                "metadata": {"kind": "artefact_index", "artefact_id": "page.md"}}],
    )
    write_artefact_and_hints(
        scratch,
        artefact={"artefact_id": "page.md", "content": "two"},
        hints=[],
    )
    out = retrieve(scratch, "findable page", k=1, embed=fake_embed)
    assert out and out[0]["hint"]["artefact_version"] == 1  # still searchable


def test_retrieve_withholds_learn_proposal_artefact_attachment(scratch, fake_embed):
    # completion of the learn hint-summary fix (2026-07-07): the hint text stopped carrying
    # external body text, but retrieve() attached the full artefact to every search hit --
    # ambiently re-delivering the untrusted body to any searching client. Learn-proposal
    # hits return hint-only (the hint carries the memory_get pointer); explicit fetch stays.
    write_artefact_and_hints(
        scratch,
        artefact={"artefact_id": "learn-x-aaaa1111", "content": "EXTERNAL BODY"},
        hints=[{"text": "learn proposal: x", "embedding": fake_embed("learn proposal: x"),
                "metadata": {"kind": "artefact_index", "learn_proposal": True}}],
    )
    write_artefact_and_hints(
        scratch,
        artefact={"artefact_id": "note-y", "content": "ordinary note"},
        hints=[{"text": "ordinary searchable note", "embedding": fake_embed("ordinary searchable note"),
                "metadata": {"kind": "artefact_index"}}],
    )
    hits = {r["hint"]["artefact_id"]: r for r in retrieve(scratch, "learn proposal ordinary", k=5, embed=fake_embed)}
    assert hits["learn-x-aaaa1111"]["artefact"] is None      # no ambient body delivery
    assert hits["note-y"]["artefact"]["content"] == "ordinary note"  # everything else unchanged
    # D-3 / F-09: withheld is on the outer retrieve element (not inferred from artefact is None)
    assert hits["learn-x-aaaa1111"]["withheld"] is True
    assert hits["note-y"]["withheld"] is False
