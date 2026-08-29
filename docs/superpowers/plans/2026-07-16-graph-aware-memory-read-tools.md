# Graph-aware ARB Memory read tools — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two read-only MCP tools — `memory_related` (E2 pgvector similarity edges) and `memory_references` (E1 citation edges, both directions) — on BOTH MCP doors, backed by one shared edge-logic module the vault exporter also uses.

**Architecture:** New `src/arb_memory/graph.py` owns both edge definitions (hoisted from `vault_export.py`, which becomes an importer). Thin per-door methods on `ReadMemoryTools` (local stdio) and `MemoryTools` (connector OAuth) delegate to `graph.*` — the doors do NOT share a class. Subject-hint mode is a pure function of caller intent: `version=None` → `'live'`, explicit version → `'as_written'`; no DB-derived mode decision exists (TOCTOU class eliminated by construction).

**Tech Stack:** Python 3.12, psycopg 3, pgvector (`<=>` cosine distance), FastMCP, pytest (+anyio).

**Spec:** `docs/superpowers/specs/2026-07-16-graph-aware-memory-read-tools-design.md` (r4, `d4b80de` — panel-approved, 0 P0/P1). The spec is authoritative on every semantic question; this plan mechanizes it.

## Global Constraints

- `memory_related(artefact_id, version=None, k=5, threshold=0.35)`; `k` capped 1..20; `threshold` in `0 < t <= 2.0`; returns `[{"artefact_id", "version", "distance"}]` nearest-first, tie-break artefact_id ASC.
- `memory_references(artefact_id, version=None)` returns `{"references": [...], "referenced_by": [...]}`, sorted ids; `version` affects the OUTGOING direction only (docstrings on both doors must say so).
- **`None` sentinel preservation:** `memory_related` passes the caller's `version` verbatim to `graph.related_artefacts` — nothing between the wrapper and the SQL may resolve `None` to a number. `latest_version` is used by `memory_references` ONLY.
- Missing artefact / missing explicit version → `ValueError("artefact not found")`. Bad `k`/`threshold` → `ValueError`. Rate limit exceeded → `ValueError("graph rate limit exceeded")`.
- Corpus side (`others` CTE) always filters `deleted_at IS NULL`; `'as_written'` mode drops that filter on the SUBJECT side only.
- Backlink prefilter is `strpos(content, %s) > 0` (never `LIKE`) applied AFTER latest-version resolution.
- Exporter rendered output must be byte-identical to current behavior.
- No new tables, no new grants, no new OAuth scope, no write surface.
- Commit after every task; run the named test command before each commit.
- **Worktree:** all work happens in the dispatch worktree the orchestrator provides; never touch the base checkout.

## Test gates

SQL-backed tests live in their OWN file, `tests/arb_memory/test_graph_pg.py`, with
module-level `pytestmark = pytest.mark.pg` (marker registered in `pyproject.toml`, Task 2).
Pure tests live in `tests/arb_memory/test_graph.py`. This file-level split is what makes
both gates honest — `-k` CANNOT deselect tests by fixture name (panel r0 finding).

- **No-DB unit gate (hermetic, always runs):**
  `.venv/bin/python -m pytest -q tests/arb_memory/test_graph.py tests/arb_memory/test_read_tools.py tests/arb_memory/test_mcp_graph_tools.py tests/arb_memory/test_local_server.py -m "not pg"`
  — deny-proof: this command must pass with `ARB_MEMORY_DSN` unset and report ZERO skips.
  (`test_mcp_tools.py` is deliberately NOT in this list — it contains six pre-existing
  `scratch`-fixture tests that skip without a DSN, which would falsify the ZERO-skips
  signal; the NEW connector graph tests therefore live in their own hermetic file,
  `test_mcp_graph_tools.py` — panel r1 finding.)
- **PostgreSQL gate (needs `ARB_MEMORY_DSN`; MUST NOT skip, MUST count):** `scripts/graph-sql-gate`
  (Task 8) — runs the SQL files, exits 1 on any skip AND on a passed-count below the
  pinned expectation (a silently missing test is the same failure as a skipped one).
- `pytest` is NOT on PATH on this host — every command in this plan uses
  `.venv/bin/python -m pytest` (or `$REPO/.venv/bin/python` from scripts).

---

### Task 1: `graph.py` — pure E1 logic + mode helper

**Files:**
- Create: `src/arb_memory/graph.py`
- Test: `tests/arb_memory/test_graph.py` (new)

**Interfaces:**
- Produces: `ART_ID_RE`, `REF_LEAD`, `REF_TRAIL` (module constants); `reference_targets(body: str, artefact_id: str, export_ids: set[str]) -> list[str]`; `subject_mode(version: int | None) -> str` returning `'live'` for `None`, `'as_written'` otherwise.
- Consumed by: Tasks 2–7 and `vault_export.py` (Task 4).

- [ ] **Step 1: Write the failing tests**

```python
# tests/arb_memory/test_graph.py
from arb_memory.graph import reference_targets, subject_mode


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
```

(add `import pytest` and `validate_related_params` to the imports at the top of the test file)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest -q tests/arb_memory/test_graph.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'arb_memory.graph'`

- [ ] **Step 3: Create `graph.py` with the moved logic**

Move (do not copy-edit) the constants and function bodies from `vault_export.py:57-80` verbatim; add `subject_mode`:

```python
# src/arb_memory/graph.py
"""Shared graph-edge logic for ARB Memory.

Single implementation consumed by BOTH the vault exporter (vault_export.py) and the
MCP read tools on both doors (mcp/read_tools.py local, mcp/tools.py connector).
E1 = explicit textual references; E2 = pgvector min-pairwise similarity.
"""
from __future__ import annotations

import re

ART_ID_RE = re.compile(r"art-[0-9a-f]{16}")
REF_LEAD = r"(?<![A-Za-z0-9._/-])"
REF_TRAIL = r"(?![A-Za-z0-9_/-]|\.[A-Za-z0-9._/-])"


def subject_mode(version: int | None) -> str:
    """'live' vs 'as_written' — a pure function of caller intent, NEVER of DB state."""
    return "live" if version is None else "as_written"


def validate_related_params(k: int, threshold: float) -> None:
    """Shared k/threshold range checks — the ONE implementation both doors call
    (spec fold 14: validation semantics must not drift between doors)."""
    if not 1 <= k <= 20:
        raise ValueError("k must be between 1 and 20")
    if not 0.0 < threshold <= 2.0:
        raise ValueError("threshold must be in (0, 2]")


def reference_targets(body: str, artefact_id: str, export_ids: set[str]) -> list[str]:
    targets = []
    for candidate in export_ids:
        if candidate == artefact_id or candidate not in body:
            continue
        escaped = re.escape(candidate)
        patterns = [f"`{escaped}`"]
        if ART_ID_RE.fullmatch(candidate) or "_" in candidate:
            patterns.append(REF_LEAD + escaped + REF_TRAIL)
        if any(re.search(pattern, body) for pattern in patterns):
            targets.append(candidate)
    return sorted(targets)
```

(`vault_export.py` keeps its own copies until Task 4 — the two coexist briefly; Task 4 deletes the originals.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest -q tests/arb_memory/test_graph.py`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/arb_memory/graph.py tests/arb_memory/test_graph.py
git commit -m "feat(graph): shared E1 reference logic + caller-intent subject mode"
```

---

### Task 2: `graph.related_artefacts` — E2 SQL with modes and 3-tuple

**Files:**
- Create: `tests/arb_memory/test_graph_pg.py` (SQL tests — `scratch` fixture, module-marked `pg`)
- Modify: `src/arb_memory/graph.py` (append), `pyproject.toml` (register `pg` marker)

**Interfaces:**
- Produces: `related_artefacts(conn, artefact_id, version, *, k, threshold, subject_hints="live") -> list[tuple[str, int, float]]` — `(artefact_id, version, distance)` nearest-first. `version=None` allowed ONLY with `subject_hints="live"` (latest resolved in-statement).
- Consumes: Task 1 module.

- [ ] **Step 0: Register the `pg` marker**

In `pyproject.toml`, append to the existing `[tool.pytest.ini_options]` `markers = [` list (after the `e2e:` entry):

```toml
    "pg: PostgreSQL-backed graph tests — run via scripts/graph-sql-gate; must never skip-green",
```

- [ ] **Step 1: Write the failing tests**

Create `tests/arb_memory/test_graph_pg.py`. Embeddings are literal 1536-dim vectors so distances are controlled; version order is deliberately the REVERSE of distance order (spec: adversarial ordering).

```python
# tests/arb_memory/test_graph_pg.py — PostgreSQL-backed graph tests (see scripts/graph-sql-gate)
import pytest

from arb_memory.graph import related_artefacts
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
    sv = _seed_subject_and_candidates(scratch)
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `ARB_MEMORY_DSN=<dev-dsn> .venv/bin/python -m pytest -q tests/arb_memory/test_graph_pg.py`
Expected: new tests FAIL — `ImportError: cannot import name 'related_artefacts'`

- [ ] **Step 3: Implement**

Append to `src/arb_memory/graph.py`:

```python
def related_artefacts(
    conn,
    artefact_id: str,
    version: int | None,
    *,
    k: int,
    threshold: float,
    subject_hints: str = "live",
) -> list[tuple[str, int, float]]:
    """E2 edges: (artefact_id, latest_version, distance), nearest-first, id tie-break.

    subject_hints is set from caller intent ONLY (subject_mode()); 'live' keeps
    deleted_at IS NULL on the subject and resolves version=None to latest INSIDE this
    statement (single snapshot). 'as_written' filters only on (artefact_id, version)
    and requires an explicit version. The corpus side is always live-latest-only.
    """
    if subject_hints not in ("live", "as_written"):
        raise ValueError(f"invalid subject_hints {subject_hints!r}")
    if subject_hints == "as_written" and version is None:
        raise ValueError("as_written mode requires an explicit version")
    subject_predicate = (
        "AND deleted_at IS NULL" if subject_hints == "live" else ""
    )
    rows = conn.execute(
        f"""
        WITH latest AS (
            SELECT DISTINCT ON (artefact_id) artefact_id, version
            FROM artefacts ORDER BY artefact_id, version DESC
        ),
        mine AS (
            SELECT embedding FROM hints
            WHERE artefact_id = %(aid)s
              AND artefact_version = COALESCE(
                  %(version)s,
                  (SELECT version FROM latest WHERE artefact_id = %(aid)s)
              )
              {subject_predicate}
        ),
        others AS (
            SELECT h.artefact_id AS aid, l.version AS version, h.embedding
            FROM hints h JOIN latest l
              ON h.artefact_id = l.artefact_id AND h.artefact_version = l.version
            WHERE h.deleted_at IS NULL AND h.artefact_id <> %(aid)s
        )
        SELECT o.aid, o.version, MIN(o.embedding <=> m.embedding) AS dist
        FROM others o CROSS JOIN mine m
        GROUP BY o.aid, o.version
        HAVING MIN(o.embedding <=> m.embedding) <= %(threshold)s
        ORDER BY dist ASC, o.aid ASC
        LIMIT %(k)s
        """,
        {"aid": artefact_id, "version": version, "threshold": threshold, "k": k},
    ).fetchall()
    return [(row[0], int(row[1]), float(row[2])) for row in rows]
```

Note the f-string interpolates ONLY the fixed two-branch predicate string (validated above) — never caller data; all values are bound parameters.

- [ ] **Step 4: Run tests to verify they pass**

Run: `ARB_MEMORY_DSN=<dev-dsn> .venv/bin/python -m pytest -q tests/arb_memory/test_graph_pg.py`
Expected: all 9 pass

- [ ] **Step 5: Commit**

```bash
git add src/arb_memory/graph.py tests/arb_memory/test_graph_pg.py pyproject.toml
git commit -m "feat(graph): related_artefacts with caller-intent modes, in-statement latest, named ordering"
```

---

### Task 3: `graph.references`, `artefact_exists`, `latest_version`

**Files:**
- Modify: `src/arb_memory/graph.py` (append)
- Test: `tests/arb_memory/test_graph_pg.py` (append)

**Interfaces:**
- Produces:
  - `artefact_exists(conn, artefact_id, version=None) -> bool` — never resolves `None`.
  - `latest_version(conn, artefact_id) -> int | None` — used by `references` / `memory_references` ONLY.
  - `references(conn, artefact_id, version) -> dict` — `{"references": list[str], "referenced_by": list[str]}`; `version` is a CONCRETE int here (the tool wrapper resolves `None` via `latest_version` — allowed for references because the artefacts table is append-only, so a fetched version's body row cannot disappear).
- Consumes: `reference_targets` (Task 1).

- [ ] **Step 1: Write the failing tests**

```python
from arb_memory.graph import artefact_exists, latest_version, references


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `ARB_MEMORY_DSN=<dev-dsn> .venv/bin/python -m pytest -q tests/arb_memory/test_graph_pg.py -k "exists or latest_version or references"`
Expected: FAIL — ImportError

- [ ] **Step 3: Implement**

Append to `src/arb_memory/graph.py`:

```python
def artefact_exists(conn, artefact_id: str, version: int | None = None) -> bool:
    """Fail-loud probe ONLY. Never resolves None; gates raise-vs-proceed, never mode."""
    if version is None:
        row = conn.execute(
            "SELECT 1 FROM artefacts WHERE artefact_id = %s LIMIT 1", (artefact_id,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM artefacts WHERE artefact_id = %s AND version = %s",
            (artefact_id, version),
        ).fetchone()
    return row is not None


def latest_version(conn, artefact_id: str) -> int | None:
    """Used by memory_references ONLY (see spec: memory_related passes None verbatim)."""
    row = conn.execute(
        "SELECT MAX(version) FROM artefacts WHERE artefact_id = %s", (artefact_id,)
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def references(conn, artefact_id: str, version: int) -> dict:
    """E1 edges, both directions. version is concrete here (wrapper resolves None).

    Backlinks: latest bodies resolved FIRST (DISTINCT ON), then exact-substring
    strpos prefilter (no pattern semantics -> pure over-approximation), then the
    authoritative reference_targets regex confirmation in Python.
    """
    row = conn.execute(
        "SELECT content FROM artefacts WHERE artefact_id = %s AND version = %s",
        (artefact_id, version),
    ).fetchone()
    body = (row[0] if row else None) or ""

    latest_ids = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT artefact_id FROM artefacts"
        ).fetchall()
    }
    outgoing = reference_targets(body, artefact_id, latest_ids)

    candidates = conn.execute(
        """
        WITH latest AS (
            SELECT DISTINCT ON (artefact_id) artefact_id, content
            FROM artefacts ORDER BY artefact_id, version DESC
        )
        SELECT artefact_id, content FROM latest
        WHERE artefact_id <> %(aid)s
          AND content IS NOT NULL
          AND strpos(content, %(aid)s) > 0
        """,
        {"aid": artefact_id},
    ).fetchall()
    referenced_by = sorted(
        cand_id
        for cand_id, cand_body in candidates
        if artefact_id in reference_targets(cand_body, cand_id, {artefact_id})
    )
    return {"references": outgoing, "referenced_by": referenced_by}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `ARB_MEMORY_DSN=<dev-dsn> .venv/bin/python -m pytest -q tests/arb_memory/test_graph_pg.py`
Expected: all 17 pass

- [ ] **Step 5: Commit**

```bash
git add src/arb_memory/graph.py tests/arb_memory/test_graph_pg.py
git commit -m "feat(graph): references (strpos backlinks, latest-first), artefact_exists, latest_version"
```

---

### Task 4: Exporter hoist — `vault_export.py` imports from `graph`

**Files:**
- Modify: `src/arb_memory/vault_export.py`
- Test: `tests/arb_memory/test_vault_export.py` (append one byte-identity test; the rest must pass UNCHANGED)

**Interfaces:**
- Consumes: `reference_targets`, `related_artefacts` from Task 1/2.
- Produces: exporter behavior byte-identical to pre-hoist.

- [ ] **Step 1: Write the failing byte-identity test**

Append to `tests/arb_memory/test_vault_export.py`:

```python
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
```

- [ ] **Step 2: Run to verify it passes on CURRENT code (pins the oracle), and record the full-suite baseline**

Run: `ARB_MEMORY_DSN=<dev-dsn> .venv/bin/python -m pytest -q tests/arb_memory/test_vault_export.py`
Expected: all pass (this test is the byte-shape oracle; it must pass BEFORE and AFTER the hoist)

- [ ] **Step 3: Perform the hoist**

In `src/arb_memory/vault_export.py`:
1. Delete `ART_ID_RE`, `_REF_LEAD`, `_REF_TRAIL` (lines 57-59), `_reference_targets` (69-80), `_related_artefacts` (128-154).
2. Add import: `from arb_memory.graph import reference_targets, related_artefacts`.
3. In `export_vault`, replace the two call sites: `references = reference_targets(body, artefact_id, export_ids)` and `related = related_artefacts(conn, artefact_id, artefact["version"], k=related_k, threshold=similarity_threshold, subject_hints="live")` (explicit `'live'` — spec requirement).
4. In `_footer`, change the unpack `for t, d in related` → `for t, _v, d in related` (signature/docstring: `related: list[tuple[str, int, float]]`).
5. `re` import stays only if still used elsewhere in the file (it is — `SLUG_DISALLOWED_RE`).

- [ ] **Step 4: Run the full exporter suite + graph suite**

Run: `ARB_MEMORY_DSN=<dev-dsn> .venv/bin/python -m pytest -q tests/arb_memory/test_vault_export.py tests/arb_memory/test_graph.py tests/arb_memory/test_graph_pg.py tests/arb_memory/test_vault_export_grants.py`
Expected: all pass, zero test-file edits beyond the one appended test

- [ ] **Step 5: Commit**

```bash
git add src/arb_memory/vault_export.py tests/arb_memory/test_vault_export.py
git commit -m "refactor(vault-export): consume shared graph.py edge logic; byte-identical output"
```

---

### Task 5: `ReadMemoryTools.memory_related` / `.memory_references` (local door)

**Files:**
- Modify: `src/arb_memory/mcp/read_tools.py`
- Test: `tests/arb_memory/test_read_tools.py` (append)

**Interfaces:**
- Consumes: `graph.subject_mode`, `graph.related_artefacts`, `graph.references`, `graph.artefact_exists`, `graph.latest_version`.
- Produces: `async memory_related(artefact_id, version=None, k=5, threshold=0.35) -> list[dict]`; `async memory_references(artefact_id, version=None) -> dict`; `LocalReadSettings.graph_rate_per_min: int = 30`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/arb_memory/test_read_tools.py`:

```python
from arb_memory.mcp import read_tools as read_tools_mod


def _rt(monkeypatch, **graph_stubs):
    rt = ReadMemoryTools(LocalReadSettings(dsn="postgresql://ignored"),
                         conn_factory=FakeConn, embed=fake_embed)
    for name, fn in graph_stubs.items():
        monkeypatch.setattr(read_tools_mod.graph, name, fn)
    return rt


@pytest.mark.anyio
async def test_memory_related_preserves_none_sentinel(monkeypatch):
    seen = {}

    def fake_related(conn, artefact_id, version, *, k, threshold, subject_hints):
        seen.update(version=version, subject_hints=subject_hints)
        return [("other", 2, 0.1)]

    rt = _rt(monkeypatch, artefact_exists=lambda c, a, v=None: True,
             related_artefacts=fake_related)
    out = await rt.memory_related("subject")
    assert seen["version"] is None                    # sentinel reached graph verbatim
    assert seen["subject_hints"] == "live"
    assert out == [{"artefact_id": "other", "version": 2, "distance": 0.1}]


@pytest.mark.anyio
async def test_memory_related_explicit_version_is_as_written(monkeypatch):
    seen = {}

    def fake_related(conn, artefact_id, version, *, k, threshold, subject_hints):
        seen.update(version=version, subject_hints=subject_hints)
        return []

    rt = _rt(monkeypatch, artefact_exists=lambda c, a, v=None: True,
             related_artefacts=fake_related)
    await rt.memory_related("subject", version=1)
    assert seen["version"] == 1 and seen["subject_hints"] == "as_written"


@pytest.mark.anyio
async def test_memory_related_param_validation(monkeypatch):
    rt = _rt(monkeypatch, artefact_exists=lambda c, a, v=None: True,
             related_artefacts=lambda *a, **k: [])
    for bad in ({"k": 0}, {"k": 21}, {"threshold": 0.0}, {"threshold": 2.1}):
        with pytest.raises(ValueError):
            await rt.memory_related("subject", **bad)


@pytest.mark.anyio
async def test_memory_related_unknown_artefact_raises(monkeypatch):
    rt = _rt(monkeypatch, artefact_exists=lambda c, a, v=None: False)
    with pytest.raises(ValueError, match="artefact not found"):
        await rt.memory_related("ghost")
    with pytest.raises(ValueError, match="artefact not found"):
        await rt.memory_references("ghost")


@pytest.mark.anyio
async def test_graph_rate_limit_bucket_is_separate_from_search(monkeypatch):
    rt = _rt(monkeypatch, artefact_exists=lambda c, a, v=None: True,
             related_artefacts=lambda *a, **k: [],
             references=lambda *a, **k: {"references": [], "referenced_by": []},
             latest_version=lambda c, a: 1)
    rt.settings = LocalReadSettings(dsn="postgresql://ignored", graph_rate_per_min=2)
    await rt.memory_related("s")
    await rt.memory_references("s")
    with pytest.raises(ValueError, match="graph rate limit"):
        await rt.memory_related("s")
    assert rt._search_hits == []                      # search bucket untouched


@pytest.mark.anyio
async def test_memory_references_resolves_none_via_latest_version(monkeypatch):
    seen = {}

    def fake_refs(conn, artefact_id, version):
        seen["version"] = version
        return {"references": [], "referenced_by": []}

    rt = _rt(monkeypatch, artefact_exists=lambda c, a, v=None: True,
             references=fake_refs, latest_version=lambda c, a: 7)
    await rt.memory_references("subject")
    assert seen["version"] == 7
    await rt.memory_references("subject", version=3)
    assert seen["version"] == 3
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest -q tests/arb_memory/test_read_tools.py`
Expected: new tests FAIL — `AttributeError: ... no attribute 'memory_related'`

- [ ] **Step 3: Implement**

In `src/arb_memory/mcp/read_tools.py`: add `import` of the graph module (`from arb_memory import graph`), add `graph_rate_per_min: int = 30` to `LocalReadSettings`, add `self._graph_hits: list[float] = []` in `__init__`, then:

```python
    def _check_graph_allowed(self) -> None:
        now = time.monotonic()
        window_start = now - 60.0
        self._graph_hits = [stamp for stamp in self._graph_hits if stamp >= window_start]
        if len(self._graph_hits) >= self.settings.graph_rate_per_min:
            raise ValueError("graph rate limit exceeded")
        self._graph_hits.append(now)

    async def memory_related(
        self, artefact_id: str, version: int | None = None, k: int = 5, threshold: float = 0.35
    ) -> list[dict]:
        """Artefacts similar to `artefact_id` (pgvector min-pairwise hint distance),
        nearest-first. Default reads the latest version's live hints; passing an explicit
        `version` switches to the as-written view of that version's hints (including
        hints retired by later versions)."""
        graph.validate_related_params(k, threshold)
        conn = self._conn()
        if not graph.artefact_exists(conn, artefact_id, version):
            raise ValueError("artefact not found")
        self._check_graph_allowed()
        rows = graph.related_artefacts(
            conn, artefact_id, version, k=k, threshold=threshold,
            subject_hints=graph.subject_mode(version),
        )
        return [
            {"artefact_id": aid, "version": ver, "distance": dist}
            for aid, ver, dist in rows
        ]

    async def memory_references(self, artefact_id: str, version: int | None = None) -> dict:
        """Explicit citations, both directions: `references` = ids this artefact's body
        cites (the given version's body; latest if omitted); `referenced_by` = latest
        artefacts citing this id. `version` affects ONLY the outgoing direction —
        backlinks always scan the latest corpus."""
        conn = self._conn()
        if not graph.artefact_exists(conn, artefact_id, version):
            raise ValueError("artefact not found")
        self._check_graph_allowed()
        resolved = version if version is not None else graph.latest_version(conn, artefact_id)
        return graph.references(conn, artefact_id, resolved)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest -q tests/arb_memory/test_read_tools.py`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add src/arb_memory/mcp/read_tools.py tests/arb_memory/test_read_tools.py
git commit -m "feat(read-tools): memory_related/memory_references on the local door, graph bucket, None sentinel"
```

---

### Task 6: `MemoryTools` methods (connector door, per-token bucket)

**Files:**
- Modify: `src/arb_memory/mcp/tools.py`, `src/arb_memory/mcp/config.py`
- Test: `tests/arb_memory/test_mcp_graph_tools.py` (NEW hermetic file)

**Interfaces:**
- Consumes: same `graph.*` functions.
- Produces: `MemoryTools.memory_related(..., access_token=None)` / `.memory_references(..., access_token=None)` (same outward shape as Task 5); `Settings.graph_rate_per_min: int = 30`.

- [ ] **Step 1: Write the failing tests**

Create `tests/arb_memory/test_mcp_graph_tools.py` — a NEW hermetic file (panel r1: the new
tests must not live in `test_mcp_tools.py`, whose six pre-existing `scratch` tests skip
without a DSN and would falsify the no-DB gate's ZERO-skips deny-proof). It reuses that
file's module-local `_settings()` helper by direct import (the repo's pyproject sets
`pythonpath = ["."]` specifically so `from tests....` helper imports work):

```python
# tests/arb_memory/test_mcp_graph_tools.py — hermetic connector-door graph-tool tests
from dataclasses import replace

import pytest

from arb_memory.mcp import tools as tools_mod
from arb_memory.mcp.tools import MemoryTools
from tests.arb_memory.test_mcp_tools import _settings


def _graph_tools(monkeypatch, settings=None, **graph_stubs):
    mt = MemoryTools(settings or _settings(), conn_factory=lambda: object(),
                     embed=lambda t: [0.0] * 1536)
    for name, fn in graph_stubs.items():
        monkeypatch.setattr(tools_mod.graph, name, fn)
    return mt


@pytest.mark.anyio
async def test_connector_memory_related_preserves_none_sentinel(monkeypatch):
    seen = {}

    def fake_related(conn, artefact_id, version, *, k, threshold, subject_hints):
        seen.update(version=version, subject_hints=subject_hints)
        return [("other", 2, 0.1)]

    mt = _graph_tools(monkeypatch, artefact_exists=lambda c, a, v=None: True,
                      related_artefacts=fake_related)
    out = await mt.memory_related("subject", access_token="tok-a")
    assert seen["version"] is None and seen["subject_hints"] == "live"
    assert out == [{"artefact_id": "other", "version": 2, "distance": 0.1}]


@pytest.mark.anyio
async def test_connector_memory_related_explicit_version_as_written(monkeypatch):
    seen = {}

    def fake_related(conn, artefact_id, version, *, k, threshold, subject_hints):
        seen.update(version=version, subject_hints=subject_hints)
        return []

    mt = _graph_tools(monkeypatch, artefact_exists=lambda c, a, v=None: True,
                      related_artefacts=fake_related)
    await mt.memory_related("subject", version=1, access_token="tok-a")
    assert seen["version"] == 1 and seen["subject_hints"] == "as_written"


@pytest.mark.anyio
async def test_connector_param_and_not_found_contracts(monkeypatch):
    mt = _graph_tools(monkeypatch, artefact_exists=lambda c, a, v=None: False,
                      related_artefacts=lambda *a, **k: [])
    with pytest.raises(ValueError):
        await mt.memory_related("s", k=0, access_token="tok-a")
    with pytest.raises(ValueError, match="artefact not found"):
        await mt.memory_related("ghost", access_token="tok-a")
    with pytest.raises(ValueError, match="artefact not found"):
        await mt.memory_references("ghost", access_token="tok-a")


@pytest.mark.anyio
async def test_connector_memory_references_resolves_none_via_latest(monkeypatch):
    seen = {}

    def fake_refs(conn, artefact_id, version):
        seen["version"] = version
        return {"references": [], "referenced_by": []}

    mt = _graph_tools(monkeypatch, artefact_exists=lambda c, a, v=None: True,
                      references=fake_refs, latest_version=lambda c, a: 7)
    await mt.memory_references("subject", access_token="tok-a")
    assert seen["version"] == 7
    await mt.memory_references("subject", version=3, access_token="tok-a")
    assert seen["version"] == 3


@pytest.mark.anyio
async def test_connector_graph_bucket_per_token_shared_across_tools_and_not_search(monkeypatch):
    settings = replace(_settings(), graph_rate_per_min=2)
    mt = _graph_tools(monkeypatch, settings,
                      artefact_exists=lambda c, a, v=None: True,
                      related_artefacts=lambda *a, **k: [],
                      references=lambda c, a, v: {"references": [], "referenced_by": []},
                      latest_version=lambda c, a: 1)
    await mt.memory_related("s", access_token="tok-a")
    await mt.memory_references("s", access_token="tok-a")     # SAME bucket as related
    with pytest.raises(ValueError, match="graph rate limit"):
        await mt.memory_related("s", access_token="tok-a")
    await mt.memory_related("s", access_token="tok-b")        # different token unaffected
    assert mt._search_hits == {}                              # search bucket never touched
```

(If `_settings()` in `test_mcp_tools.py` has a different name or signature, mirror the construction used by its existing search-rate-limit test verbatim — but do NOT invent fixtures. If the direct import proves impossible, copy the helper's body into the new file with a comment naming its origin.)

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest -q tests/arb_memory/test_mcp_graph_tools.py`
Expected: FAIL — no attribute / no field

- [ ] **Step 3: Implement**

1. `config.py`: add `graph_rate_per_min: int = 30` to `Settings` (after `write_rate_per_min`).
2. `tools.py`: `from arb_memory import graph`; in `__init__` add `self._graph_hits: dict[str, list[float]] = {}`; add:

```python
    def _check_graph_allowed(self, access_token: str) -> None:
        now = time.monotonic()
        window_start = now - 60.0
        hits = [stamp for stamp in self._graph_hits.get(access_token, []) if stamp >= window_start]
        if len(hits) >= self.settings.graph_rate_per_min:
            self._graph_hits[access_token] = hits
            raise ValueError("graph rate limit exceeded")
        hits.append(now)
        self._graph_hits[access_token] = hits

    async def memory_related(
        self, artefact_id: str, version: int | None = None, k: int = 5,
        threshold: float = 0.35, *, access_token: str | None = None,
    ) -> list[dict]:
        token = access_token or _current_access_token()
        graph.validate_related_params(k, threshold)
        conn = self.conn_factory()
        if not graph.artefact_exists(conn, artefact_id, version):
            raise ValueError("artefact not found")
        self._check_graph_allowed(token)
        rows = graph.related_artefacts(
            conn, artefact_id, version, k=k, threshold=threshold,
            subject_hints=graph.subject_mode(version),
        )
        return [
            {"artefact_id": aid, "version": ver, "distance": dist}
            for aid, ver, dist in rows
        ]

    async def memory_references(
        self, artefact_id: str, version: int | None = None, *, access_token: str | None = None,
    ) -> dict:
        token = access_token or _current_access_token()
        conn = self.conn_factory()
        if not graph.artefact_exists(conn, artefact_id, version):
            raise ValueError("artefact not found")
        self._check_graph_allowed(token)
        resolved = version if version is not None else graph.latest_version(conn, artefact_id)
        return graph.references(conn, artefact_id, resolved)
```

Match the file's existing conn-usage idiom: if the surrounding read methods obtain the connection differently (e.g. context-managed), copy that idiom instead of `self.conn_factory()` bare — read the neighboring `memory_search` body first and mirror it exactly.

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest -q tests/arb_memory/test_mcp_graph_tools.py tests/arb_memory/test_mcp_tools.py`
Expected: all pass (the latter file skips its scratch tests without a DSN — that is pre-existing and fine outside the gate)

- [ ] **Step 5: Commit**

```bash
git add src/arb_memory/mcp/tools.py src/arb_memory/mcp/config.py tests/arb_memory/test_mcp_graph_tools.py
git commit -m "feat(mcp-tools): connector-door graph tools with per-token rate bucket"
```

---

### Task 7: Register on both doors

**Files:**
- Modify: `src/arb_memory/mcp/local_server.py`, `src/arb_memory/mcp/server.py` (the wrapper block ending at the `add_tool` lines, `server.py:404-408` region)
- Test: `tests/arb_memory/test_local_server.py` (append), `tests/arb_memory/test_mcp_sdk_contract.py` or the nearest connector-registration test file (append — find the existing test that asserts the connector's tool list and extend it)

**Interfaces:**
- Consumes: Tasks 5 and 6 methods.
- Produces: tools named `memory_related` and `memory_references` registered on both doors.

- [ ] **Step 1: Write the failing tests**

`tests/arb_memory/test_local_server.py` — TWO changes (panel r0 P1: the existing test uses
strict set equality and WILL go red when the tools register; updating it is part of this
task's test-first step, and the updated version is the failing test):

1. **Update the existing** `test_local_server_registers_only_read_tools` (line ~71-83) —
   change its equality assertion to the new exact set, keeping the write-tool exclusions:

```python
    assert names == {
        "memory_search", "memory_get", "memory_recent",
        "memory_related", "memory_references",
    }
    assert "memory_store" not in names
    assert "memory_remember" not in names
```

2. **Append** the subset check for the connector-door symmetry:

```python
@pytest.mark.anyio
async def test_local_server_registers_graph_tools():
    server = build_local_server(LocalReadSettings(dsn="postgresql://ignored"),
                                conn_factory=FakeConn, embed=fake_embed)
    names = {t.name for t in await server.list_tools()}
    assert {"memory_related", "memory_references"} <= names
```

For the connector door: the existing tool-enumeration test is in
`tests/arb_memory/test_mcp_tools.py` (~line 147). Extend its expected tool set with
`memory_related` and `memory_references`, using that test's existing server-construction
code verbatim (if it also asserts strict equality, update the full set the same way).

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest -q tests/arb_memory/test_local_server.py`
Expected: FAIL — missing names

- [ ] **Step 3: Implement**

`local_server.py` — two lines after the existing three:

```python
    server.add_tool(tools.memory_related, name="memory_related")
    server.add_tool(tools.memory_references, name="memory_references")
```

`server.py` — thin wrappers next to the existing `memory_search` wrapper (docstrings become the tool descriptions; copy the two docstrings from Task 5 verbatim so both doors describe identical semantics), then two `add_tool` lines next to the existing five:

```python
    async def memory_related(
        artefact_id: str, version: int | None = None, k: int = 5, threshold: float = 0.35
    ) -> list[dict]:
        """Artefacts similar to `artefact_id` (pgvector min-pairwise hint distance),
        nearest-first. Default reads the latest version's live hints; passing an explicit
        `version` switches to the as-written view of that version's hints (including
        hints retired by later versions)."""
        return await tools.memory_related(artefact_id, version, k, threshold)

    async def memory_references(artefact_id: str, version: int | None = None) -> dict:
        """Explicit citations, both directions: `references` = ids this artefact's body
        cites (the given version's body; latest if omitted); `referenced_by` = latest
        artefacts citing this id. `version` affects ONLY the outgoing direction —
        backlinks always scan the latest corpus."""
        return await tools.memory_references(artefact_id, version)

    server.add_tool(memory_related, name="memory_related")
    server.add_tool(memory_references, name="memory_references")
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest -q tests/arb_memory/test_local_server.py tests/arb_memory/test_mcp_tools.py tests/arb_memory/test_mcp_sdk_contract.py`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add src/arb_memory/mcp/local_server.py src/arb_memory/mcp/server.py tests/arb_memory/test_local_server.py tests/arb_memory/test_mcp_tools.py
git commit -m "feat(mcp): register memory_related/memory_references on both doors"
```

---

### Task 8: PostgreSQL gate script + changelog

**Files:**
- Create: `scripts/graph-sql-gate`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Produces: `scripts/graph-sql-gate` — exits 0 only if every SQL-backed graph/exporter test RAN and PASSED; exits 1 on any skip (the vacuously-green guard).

- [ ] **Step 1: Write the gate script**

```bash
#!/usr/bin/env bash
# graph-sql-gate — PostgreSQL-backed test gate for the graph read tools.
# The scratch fixture SKIPS silently without ARB_MEMORY_DSN; a green run with every
# SQL assertion skipped is the vacuously-green shape this gate refuses. It ALSO pins
# the passed count: a silently missing/deselected test is the same failure as a skip.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO/.venv/bin/python"          # pytest is NOT on PATH on this host
cd "$REPO"                           # relative test paths below require repo-root CWD
EXPECTED_MIN_PASSED=40               # pin to the count observed in Step 2; update when tests are added
if [ -z "${ARB_MEMORY_DSN:-}" ]; then
  echo "graph-sql-gate: ARB_MEMORY_DSN is not set — refusing to run a skippable gate" >&2
  exit 1
fi
out=$("$PY" -m pytest -q -rs \
  tests/arb_memory/test_graph_pg.py tests/arb_memory/test_vault_export.py 2>&1) || {
  echo "$out"; exit 1; }
echo "$out"
if echo "$out" | grep -qE "[0-9]+ skipped"; then
  echo "graph-sql-gate: FAIL — SQL tests were skipped; gate requires them to RUN" >&2
  exit 1
fi
passed=$(echo "$out" | grep -oE "[0-9]+ passed" | grep -oE "[0-9]+" | head -1)
if [ "${passed:-0}" -lt "$EXPECTED_MIN_PASSED" ]; then
  echo "graph-sql-gate: FAIL — only ${passed:-0} passed; expected >= $EXPECTED_MIN_PASSED (missing tests?)" >&2
  exit 1
fi
```

- [ ] **Step 2: Make executable, pin the count, and verify ALL THREE deny-proofs**

Run: `chmod +x scripts/graph-sql-gate && ARB_MEMORY_DSN=<dev-dsn> scripts/graph-sql-gate`
Expected: passes; note the `N passed` count and set `EXPECTED_MIN_PASSED=N` in the script, then re-run to confirm still green.
Run: `env -u ARB_MEMORY_DSN scripts/graph-sql-gate; echo "exit=$?"`
Expected: `exit=1` with the refusing message (missing-DSN deny-proof)
Run: `ARB_MEMORY_DSN=<dev-dsn> bash -c 'sed "s/EXPECTED_MIN_PASSED=[0-9]*/EXPECTED_MIN_PASSED=999/" scripts/graph-sql-gate > /tmp/gate-tamper && chmod +x /tmp/gate-tamper && /tmp/gate-tamper; echo "exit=$?"'`
Expected: `exit=1` with the count-shortfall message (missing-test deny-proof — proves the count check can go red)

- [ ] **Step 3: Changelog entry**

Append to `CHANGELOG.md` under a new dated heading, following the file's existing entry format (what AND why):

```markdown
## 2026-07-16 — graph-aware memory read tools
- Added `memory_related` / `memory_references` MCP read tools on BOTH doors (local stdio
  + connector OAuth), exposing the vault exporter's E1/E2 graph edges on demand.
  Why: MCP consumers were blind to the graph; recall now walks edges instead of
  composing searches. Shared logic lives in `src/arb_memory/graph.py` (exporter now
  imports it — one implementation, byte-identical footers). Subject-hint mode is pure
  caller intent ('live'/'as_written') — eliminates the TOCTOU class two panel rounds
  caught. Backlink prefilter is strpos (LIKE under-matches on backslash ids).
  `scripts/graph-sql-gate` refuses skip-green SQL runs.
```

- [ ] **Step 4: Full-suite regression run**

Run: `ARB_MEMORY_DSN=<dev-dsn> .venv/bin/python -m pytest -q tests/arb_memory/ --ignore=tests/arb_memory/e2e_local_read_mcp.py -x -q`
Expected: everything passes (pre-existing skips unrelated to graph/vault_export are acceptable; zero failures)

- [ ] **Step 5: Commit**

```bash
git add scripts/graph-sql-gate CHANGELOG.md
git commit -m "feat(gate): graph-sql-gate refuses skip-green SQL runs; changelog"
```

---

## Out of scope for the implementer (orchestrator-owned, arc-final)

- e2e live gate (both doors against the real dev DB) — runs after the implementation-review panel.
- `tools/pi-sdk-host/install.sh` re-run and deployment.
- Merging to `dev` — workers commit on their branch in the worktree; ONLY the orchestrator integrates.

## Fold changelog — plan r0 → r1

Panel run `panel-graph-read-tools-plan-r0-20260716T121703Z-7ba29b` (agy block/P1,
grok needs-changes/P1, codex-sol block/P1, cold-opus approve/P2). Dispositions:

| # | Finding (seats) | Disposition in r1 |
|---|---|---|
| 1 | Existing `test_local_server_registers_only_read_tools` asserts strict set equality — Task 7 as written leaves the suite red (agy F1, grok P1-1, codex P1-1) | Task 7 now updates that test's expected set as part of its test-first step |
| 2 | Task 6 tests referenced nonexistent `settings`/`settings_factory` fixtures (agy F3, grok P2-1, codex P1-2, cold-opus P2) | Rewritten against the file's real `_settings()` + `dataclasses.replace` idiom |
| 3 | Shared k/threshold validation helper missing — plan duplicated range checks in both doors, violating spec fold 14 (grok P1-2, codex P1-3) | `graph.validate_related_params` added in Task 1; both doors call it; unit-tested once |
| 4 | Gate mechanics: bare `pytest` not on PATH (agy F2); `-k "not scratch"` cannot deselect fixture-based tests (agy F4, codex P1-4, cold-opus P2); SQL gate lacked a count assertion so a missing test passes green (codex P1-4) | SQL tests split into `test_graph_pg.py` with registered `pg` marker; all commands use `.venv/bin/python -m pytest`; gate pins `EXPECTED_MIN_PASSED` and has three deny-proofs (no-DSN, skip, count-shortfall) |
| 5 | Byte-identity oracle asserted substrings only (codex P1-5.1) | Task 4 test now compares the exact post-marker footer bytes against a literal |
| 6 | Corpus-side test couldn't detect a dropped latest-version join (codex P1-5.2) | `test_related_corpus_excludes_live_old_version_hints` added (old live NEAR hint vs latest FAR hint, both modes) |
| 7 | Connector coverage gaps: no memory_references, no explicit-version, no param/not-found, no search-bucket independence (codex P1-5.3) | Task 6 test set expanded to cover all of these incl. shared-bucket-across-tools |
| 8 | Historical-state test comment overstated (claimed to exercise the write path; it seeds state manually) (codex P2) | Comment rewritten as fixture-state pinning |

## Fold changelog — plan r1 → r2

Panel run `panel-graph-read-tools-plan-r1-20260716T122946Z-946629` (agy approve/none,
grok needs-changes/P1, codex-sol block/P1, cold-opus needs-changes/P1). Folds 1–3 and 5–8
confirmed resolved by all seats. Dispositions:

| # | Finding (seats) | Disposition in r2 |
|---|---|---|
| 9 | Hermetic no-DB gate's ZERO-skips deny-proof unsatisfiable — `test_mcp_tools.py` carries six pre-existing `scratch` tests that skip without a DSN and are not `pg`-marked (grok P1 empirical `15 passed, 6 skipped`; codex P1; cold-opus P2) | Task 6's NEW connector tests moved to their own hermetic file `tests/arb_memory/test_mcp_graph_tools.py` (imports `_settings` from the sibling test module — pyproject `pythonpath=["."]` supports this); the gate lists that file instead; `test_mcp_tools.py` deliberately excluded, with the reason documented at the gate |
| 10 | Corpus-regression test miscalibrated: far-hint cosine distance is ≈0.081, so `> 0.1` fails on CORRECT code, inverting the TDD loop (codex P1, cold-opus P1 — independent computation) | Assertion recalibrated to `> 0.05` with both computed distances documented in the test (correct ≈0.081 vs broken ≈0.00005) |
| 11 | `graph-sql-gate` runs relative paths without `cd "$REPO"` (grok P2) | `cd "$REPO"` added after interpreter resolution |
| 12 | Task 7 commit staged all of `tests/arb_memory/` (grok P2) | Surgical `git add` of the two touched test files |
