# ARB Memory Phase 0 — Store + Write-Library Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or
> superpowers:executing-plans to implement this plan task-by-task. Steps use `- [ ]` checkboxes.

**Goal:** Build the two-lane Postgres store (versioned faithful artefacts + fuzzy hints) and the scoped
write-library (single embedding owner; dual-write-one-txn; version-on-resave; two-step retrieval; content
idempotency), proven against local psql.

**Architecture:** `docs/decisions/arb-memory-architecture.md`. **Spec:**
`docs/superpowers/specs/2026-06-20-arb-memory-phase0-store-design.md` (spec-panel folded).

**Tech Stack:** Python 3.11+, `psycopg` (v3), `pgvector` (the psycopg3 adapter), `openai`. Dev DB via
`ARB_MEMORY_DSN` (`envs/arb-memory-dev.env`, local `pgvector/pgvector:pg16` on :5544).

> **Plan-panel folded (2026-06-20, 4/4 PLAN-HOLES — cold-Opus+agy+M3 certifiers + codex implementor):**
> (P0) `test_dual_write_atomic` rewritten as a DB-trigger deny-proof (Task 6) — the `embedding=None` version
> couldn't discriminate atomic from non-atomic under psycopg3's implicit transaction; (P0) Task 1
> table-existence uses `.fetchone()` not cursor-truthiness; (P0) every `arb_memory` test module
> `pytest.importorskip`s `psycopg`/`openai`/`pgvector` so it can't break core-suite *collection* on hosts
> without the extra; (P0) the pgvector psycopg3 adapter is registered (`register_vector`) — a Python list
> won't adapt to `vector` otherwise; (P0) `embed` is a **required** kwarg on `search_hints`/`retrieve` (no
> `fake_embed` default — that's a test fixture, unimportable from `src/`).

## Global Constraints
- New package `src/arb_memory/` — imported only by the (future) MCP-host services. NO seat imports it.
- **`content_hash` is canonicalized exactly per spec §2a** (versioned domain tags; hint hash includes
  `(artefact_id, version)`; metadata excluded; text exact-as-stored). This is load-bearing — do not improvise.
- Deps added to `pyproject.toml` as an **optional extra**
  `arb-memory = ["psycopg[binary]>=3.1", "pgvector>=0.3", "openai>=1.0"]` (mirrors the lazy `agent-sdk`
  extra — the bridge core must not require these).
- **Every `arb_memory` test module begins with `pytest.importorskip("psycopg")` (and `openai`/`pgvector`
  where used)** — plan-panel P0 (agy): a bare `import psycopg` at module top crashes `pytest tests/`
  *collection* on a host without the extra, contaminating the whole suite. `importorskip` makes the module
  skip, not error, during discovery.
- **The pgvector psycopg3 adapter is registered on every connection** — plan-panel P0 (codex): after
  connect, `from pgvector.psycopg import register_vector; register_vector(conn)`, else a Python list won't
  adapt to a `vector` param. The `scratch` fixture does this; production connections do too.
- Tests run against `ARB_MEMORY_DSN`; **each test gets an isolated scratch schema** (`CREATE SCHEMA arb_test_
  <uuid>`; `SET search_path TO arb_test_<uuid>, public` — `public` kept so the `vector` extension/type
  resolves; create the extension once in `public`). Drop on teardown — no cross-test state (the #11 lesson).
  If `ARB_MEMORY_DSN` is unset, DB tests `skip`.
- Store tests use the **deterministic fake embedder**; the single real-OpenAI test is `@pytest.mark.smoke`,
  gated on `OPENAI_API_KEY`.
- TDD: failing test → run-to-fail → minimal impl → run-to-pass → commit, per task.

## File Structure
- `src/arb_memory/__init__.py` · `schema.sql` · `hash.py` · `embed.py` · `store.py`
- `tests/arb_memory/__init__.py` · `conftest.py` (scratch-schema fixture + fake embedder) ·
  `test_schema.py` · `test_hash.py` · `test_store.py` · `test_embed_owner.py` · `test_embed_smoke.py`

---

### Task 1: Package + deps + schema.sql (DDL runs from scratch)

**Files:** Create `src/arb_memory/__init__.py`, `src/arb_memory/schema.sql`; modify `pyproject.toml`;
Create `tests/arb_memory/__init__.py`, `tests/arb_memory/conftest.py`, `tests/arb_memory/test_schema.py`.

- [ ] **Step 1: failing test** — `test_schema.py`:
```python
import os, pytest
psycopg = pytest.importorskip("psycopg")   # P0 (agy): skip-not-error on hosts without the extra
pytest.importorskip("pgvector")
pytestmark = pytest.mark.skipif(not os.environ.get("ARB_MEMORY_DSN"), reason="no ARB_MEMORY_DSN")

def test_schema_applies_and_constraints(scratch):  # `scratch`: isolated schema, DDL applied, register_vector'd
    assert scratch.execute("SELECT extname FROM pg_extension WHERE extname='vector'").fetchone(), \
        "vector extension missing — DDL must CREATE EXTENSION first"
    # P0 (cold-Opus/agy/M3): fetch the OID, don't test cursor-truthiness (always truthy)
    for t in ("artefacts", "hints", "audit_events", "idempotency_keys"):
        oid = scratch.execute("SELECT to_regclass(%s)", (t,)).fetchone()[0]
        assert oid is not None, f"table {t} missing"
    # CHECK rejects a partial-NULL hint link (artefact_id set, version NULL)
    with pytest.raises(psycopg.errors.CheckViolation):
        scratch.execute("INSERT INTO hints (text, embedding, artefact_id, content_hash) "
                        "VALUES ('x', %s, 'a', 'h')", ([0.0]*1536,))
```
- [ ] **Step 2: run-to-fail** — `pytest tests/arb_memory/test_schema.py -q` → fails (no package/DDL).
- [ ] **Step 3: implement** — `conftest.py` provides `scratch`: connect via `ARB_MEMORY_DSN`, `CREATE SCHEMA
  arb_test_<uuid>`, `SET search_path`, apply `schema.sql`, yield the connection, `DROP SCHEMA … CASCADE` on
  teardown. Write `schema.sql` exactly per spec §2 (begins `CREATE EXTENSION IF NOT EXISTS vector;`, then
  `artefacts`, `hints` with the `hints_artefact_pairing` CHECK + composite FK, `audit_events`,
  `idempotency_keys`, all indexes). Add the `arb-memory` extra to `pyproject.toml`.
- [ ] **Step 4: run-to-pass** → PASS.  - [ ] **Step 5: commit** — `feat(arb-memory): schema + scratch fixture [P0]`

---

### Task 2: `hash.py` — content_hash canonicalization (spec §2a, load-bearing)

**Files:** Create `src/arb_memory/hash.py`, `tests/arb_memory/test_hash.py`.

**Interfaces — Produces:**
- `artefact_hash(content: str|None, content_bytes: bytes|None, content_mime: str) -> str`
- `hint_hash(text: str, artefact_id: str|None, artefact_version: int|None, repo_pointer: str|None) -> str`

- [ ] **Step 1: failing tests** (pure, no DB):
```python
from arb_memory.hash import artefact_hash, hint_hash

def test_artefact_hash_mime_and_kind_change_the_hash():
    a = artefact_hash("hello", None, "text/plain")
    assert a != artefact_hash("hello", None, "text/markdown")        # mime in the domain
    assert a != artefact_hash(None, b"hello", "application/octet-stream")  # text vs binary
    assert a == artefact_hash("hello", None, "text/plain")           # stable

def test_hint_hash_is_version_aware():
    # identical text, different artefact version → DIFFERENT hash (v2 discoverable)
    assert hint_hash("desc", "doc.md", 1, None) != hint_hash("desc", "doc.md", 2, None)
    # identical text + same link → SAME hash (genuine duplicate dedups)
    assert hint_hash("desc", "doc.md", 1, None) == hint_hash("desc", "doc.md", 1, None)
    # standalone (no link) is hashed over text
    assert hint_hash("lead", None, None, None) == hint_hash("lead", None, None, None)

def test_hint_hash_exact_text_no_normalization():
    assert hint_hash("a b", None, None, None) != hint_hash("a  b", None, None, None)  # whitespace matters
```
- [ ] **Step 2: run-to-fail.**
- [ ] **Step 3: implement** exactly per spec §2a:
```python
import hashlib
def artefact_hash(content, content_bytes, content_mime):
    if content_bytes is not None:
        kind, payload = b"binary", content_bytes
    else:
        kind, payload = b"text", (content or "").encode("utf-8")
    return hashlib.sha256(b"arbmem:artefact:v1\0" + content_mime.encode("utf-8") + b"\0"
                          + kind + b"\0" + payload).hexdigest()
def hint_hash(text, artefact_id, artefact_version, repo_pointer):
    return hashlib.sha256(
        b"arbmem:hint:v1\0" + text.encode("utf-8") + b"\0"
        + (artefact_id or "").encode("utf-8") + b"\0"
        + str(artefact_version or 0).encode("utf-8") + b"\0"
        + (repo_pointer or "").encode("utf-8")).hexdigest()
```
- [ ] **Step 4: run-to-pass** → PASS.  - [ ] **Step 5: commit** — `feat(arb-memory): versioned content_hash canonicalization [P0]`

---

### Task 3: `embed.py` — single embedding owner + fake embedder

**Files:** Create `src/arb_memory/embed.py`; add the fake embedder to `tests/arb_memory/conftest.py`.

**Interfaces — Produces:** `embed(text: str) -> list[float]` (len 1536). Module-level `EMBED_MODEL =
"text-embedding-3-small"`, `EMBED_DIM = 1536`. Reads `OPENAI_API_KEY`.

- [ ] **Step 1: failing test** — `test_embed_owner.py::test_embed_dim` using the fake embedder asserts a
  1536-float vector; and a fake-embedder helper `fake_embed(text)` returns a deterministic L2-normalized
  1536-vector (`sha256(text)`-seeded). (Real embed exercised in Task 9 smoke.)
- [ ] **Step 2-4:** implement `embed()` (the ONLY function importing `openai`), and `fake_embed` in conftest.
- [ ] **Step 5: commit** — `feat(arb-memory): embed() single owner + deterministic fake embedder [P0]`

---

### Task 4: `store.upsert_artefact` — version on changed content, idempotent on identical

**Files:** Create `src/arb_memory/store.py`; `tests/arb_memory/test_store.py`.

**Interfaces — Produces:** `upsert_artefact(conn, artefact_id, *, content=None, content_bytes=None,
mime="text/plain", repo_pointer=None) -> tuple[str, int]` (artefact_id, version).

- [ ] **Step 1: failing tests:**
```python
def test_version_on_resave_keeps_old(scratch):
    aid, v1 = upsert_artefact(scratch, "doc.md", content="one")
    assert v1 == 1
    _, v2 = upsert_artefact(scratch, "doc.md", content="two")   # changed content
    assert v2 == 2
    # v1 row intact, byte-for-byte
    row = scratch.execute("SELECT content FROM artefacts WHERE artefact_id='doc.md' AND version=1").fetchone()
    assert row[0] == "one"

def test_identical_resave_is_noop(scratch):
    upsert_artefact(scratch, "doc.md", content="same")
    _, v = upsert_artefact(scratch, "doc.md", content="same")     # identical → no new version
    assert v == 1
    n = scratch.execute("SELECT count(*) FROM artefacts WHERE artefact_id='doc.md'").fetchone()[0]
    assert n == 1
```
- [ ] **Steps 2-4:** implement: compute `artefact_hash`; `SELECT version WHERE (artefact_id, content_hash)`
  → if exists return it (no-op); else `version = COALESCE(max(version),0)+1`, INSERT, return.
- [ ] **Step 5: commit** — `feat(arb-memory): upsert_artefact version-on-change, idempotent [P0]`

---

### Task 5: `store.upsert_hint` — idempotent on version-aware hash

**Interfaces — Produces:** `upsert_hint(conn, text, embedding, *, artefact_id=None, artefact_version=None,
repo_pointer=None, metadata=None, source="seat", author="unknown") -> int` (hint id).

- [ ] **Step 1: failing tests:** `test_content_idempotent_hint` (same hint twice → one row);
  `test_hint_hash_is_version_aware` end-to-end (a v1 hint and a same-text v2 hint both persist → 2 rows).
- [ ] **Steps 2-4:** implement: compute `hint_hash`; `INSERT … ON CONFLICT (content_hash) DO NOTHING
  RETURNING id` (on conflict, SELECT the existing id).
- [ ] **Step 5: commit** — `feat(arb-memory): upsert_hint version-aware idempotent [P0]`

---

### Task 6: `store.write_artefact_and_hints` — one transaction, atomic

**Interfaces — Produces:** `write_artefact_and_hints(conn, *, artefact=None, hints=()) -> dict` —
`artefact` = `{artefact_id, content?, content_bytes?, mime?, repo_pointer?}`; `hints` = list of
`{text, embedding, repo_pointer?, metadata?}` (each links to the artefact's resolved `(id, version)`).

- [ ] **Step 1: failing test (deny-proof, DB-trigger version — plan-panel P0 cold-Opus+codex).**
  `embedding=None` couldn't discriminate atomic from non-atomic (it can fail before the artefact insert, or
  abort psycopg3's implicit transaction so everything rolls back regardless). Instead force the failure
  **inside the hint INSERT, after the artefact upsert has happened**, via a DB-side trigger — so only a
  *non-atomic* impl (which committed/left the artefact) is caught:
```python
def test_dual_write_atomic(scratch):
    # trigger raises when a hint with text='boom' is inserted — i.e. AFTER the artefact write
    scratch.execute("""
        CREATE FUNCTION boom() RETURNS trigger AS $$
        BEGIN IF NEW.text = 'boom' THEN RAISE EXCEPTION 'boom'; END IF; RETURN NEW; END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER hints_boom BEFORE INSERT ON hints
            FOR EACH ROW EXECUTE FUNCTION boom();
    """)
    good_vec = [0.0]*1536
    with pytest.raises(psycopg.errors.RaiseException):
        write_artefact_and_hints(scratch,
            artefact={"artefact_id": "doc.md", "content": "c"},
            hints=[{"text": "boom", "embedding": good_vec}])   # valid vector; fails at INSERT, post-artefact
    scratch.rollback()   # clear the aborted tx before reading
    # NEITHER landed — a non-atomic impl that wrote the artefact before the failing hint would leave it
    assert scratch.execute("SELECT count(*) FROM artefacts WHERE artefact_id='doc.md'").fetchone()[0] == 0
    assert scratch.execute("SELECT count(*) FROM hints").fetchone()[0] == 0
```
- [ ] **Steps 2-4:** implement inside an explicit transaction (`with conn.transaction():`): upsert artefact →
  get `(id, version)` → upsert each hint linking to it. Any exception rolls back BOTH. Deny-proof: a
  non-atomic impl that doesn't wrap both (so the artefact write is committed/visible before the trigger
  fires on the hint) leaves the artefact → fails the count. (Note: the scratch fixture must `rollback()` in
  teardown too, since the trigger leaves the connection in an aborted-tx state.)
- [ ] **Step 5: commit** — `feat(arb-memory): write_artefact_and_hints atomic dual-write [P0]`

---

### Task 7: retrieval — `search_hints` + `fetch_artefact` + `retrieve` (two-step, version-pinned)

**Interfaces — Produces:** `search_hints(conn, query_text, k=8, *, embed) -> list[Row]` (vector + lexical
fusion; live only); `fetch_artefact(conn, artefact_id, version) -> Row` (PK, exact version);
`retrieve(conn, query_text, k=8, *, embed) -> list[dict]` (two-step). **`embed` is a REQUIRED kwarg, NO
default** (plan-panel P0 M3 — `fake_embed` is a test fixture, unimportable from `src/`; a default would
`NameError` at import). Tests pass `embed=fake_embed`; production callers pass `embed=embed` (from
`.embed`). The convenience prod wrappers (if any) default to the real `embed` explicitly.

- [ ] **Step 1: failing test (the un-hollowed deny-proof, spec §6):**
```python
def test_two_step_faithful(scratch):
    # v1 + hint pinned to v1
    write_artefact_and_hints(scratch,
        artefact={"artefact_id": "doc.md", "content": "ONE"},
        hints=[{"text": "the doc about one", "embedding": fake_embed("the doc about one")}])
    # NOW advance to v2 (this is what makes the test prove pinning, not MAX(version))
    upsert_artefact(scratch, "doc.md", content="TWO")
    out = retrieve(scratch, "the doc about one", k=1, embed=fake_embed)
    assert out[0]["hint"]["artefact_version"] == 1
    assert out[0]["artefact"]["content"] == "ONE"     # v1 bytes, NOT "TWO"

def test_repo_pointer_hint_returns_pointer(scratch):
    upsert_hint(scratch, "see the gate", fake_embed("see the gate"),
                repo_pointer="skills/bridge-protocol/gate/gate.py@HEAD")
    out = retrieve(scratch, "see the gate", k=1, embed=fake_embed)
    assert out[0]["artefact"] is None and out[0]["repo_pointer"].startswith("skills/")
```
- [ ] **Steps 2-4:** implement. `search_hints`: `embed(query)`, ORDER BY a fusion of `embedding <=> q`
  (cosine) and `ts_rank(search_tsv, …)` (port ai-brain's RRF from `/tmp/ai-brain-study/app/db.py`), WHERE
  `deleted_at IS NULL`. `fetch_artefact`: `SELECT … WHERE artefact_id=%s AND version=%s`. `retrieve`: for
  each hit, if `artefact_id` → `fetch_artefact(id, version)` (the PINNED version), elif `repo_pointer` →
  return pointer, else standalone fragment. Deny-proof: a "fetch latest" impl returns v2 → fails.
- [ ] **Step 5: commit** — `feat(arb-memory): two-step version-pinned retrieve [P0]`

---

### Task 8: single-embedding-owner drift guard (AST + URL grep)

**Files:** `tests/arb_memory/test_embed_owner.py`.

- [ ] **Step 1: failing/▶ test:** AST-parse every `*.py` under `src/` except `src/arb_memory/embed.py`;
  assert none has an `import openai` / `from openai …` node; AND grep all of `src/` (except `embed.py`) for
  `api.openai.com` and the `/embeddings` path. Assert clean. (Drift guard for committed code — docstring
  states it is NOT a deployment single-writer proof.)
- [ ] **Steps 2-4:** the impl is the test itself; it passes once Task 3 confines `openai` to `embed.py`.
  Deny-proof: add a throwaway `import openai` in `src/arb_memory/store.py` → test fails → remove.
- [ ] **Step 5: commit** — `test(arb-memory): embedding-owner drift guard (AST+grep) [P0]`

---

### Task 9: real-embedding smoke (gated) + Phase 0 wrap

**Files:** `tests/arb_memory/test_embed_smoke.py`.

- [ ] **Step 1:** `@pytest.mark.smoke`, `skipif not OPENAI_API_KEY`: `embed("hello")` returns len-1536; two
  semantically-related strings are nearer (smaller cosine) than two unrelated strings — proves real semantic
  retrieval the fake embedder can't.
- [ ] **Step 2: run the full Phase 0 suite** against `ARB_MEMORY_DSN`: `pytest tests/arb_memory -q` → green
  (smoke skipped without key). Confirm `git diff --name-only` touches only `src/arb_memory/**`,
  `tests/arb_memory/**`, `pyproject.toml` — **no bridge-core or gate files**.
- [ ] **Step 3: commit** — `test(arb-memory): real-embedding smoke + Phase 0 green [P0]`

---

## Self-Review
- **Spec coverage:** §2 schema → T1; §2a hash → T2; §3 write-library → T4/T5/T6/T7; §5 fake/real split →
  T3/T9; §6 deny-proofs → T4-T8. ✓
- **Deny-proofs real:** T6 (non-atomic leaves artefact), T7 (`test_two_step_faithful` advances to v2 so a
  `MAX(version)` impl fails — the un-hollowed version), T8 (inject `import openai` → fails). ✓
- **Load-bearing pinned, not improvised:** content_hash is copied verbatim from §2a; the version-pinning
  test puts a v2 on the path. ✓
- **No bridge-core contamination:** new package + optional extra; structural guard + the T9 diff check. ✓
