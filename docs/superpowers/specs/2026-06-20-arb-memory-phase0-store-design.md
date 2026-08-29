# ARB Memory — Phase 0: store + write-library [spec]

**Status:** SPEC (spec-panel folded; ready for the plan). First pipeline artifact for ARB Memory (Workflow B).

**Spec-panel verdict (2026-06-20):** cold-Opus + agy + M3 (certifiers) + codex (implementor contributor) —
**4/4 SPEC-HOLES**, converged. Schema judged *sound for version-pinning* (PK/FK/UNIQUE deliver faithfulness).
Folded: **(P0)** `content_hash` canonicalization pinned (§2a) — hint hash includes `(artefact_id, version)`
so a v2 hint doesn't dedup against the v1 hint (undiscoverable-version bug) yet same-fragment+same-version
still dedups; **(P0)** schema now `CREATE EXTENSION vector` first (codex caught the from-scratch build-block);
**(P0)** `test_two_step_faithful` now creates v2 *after* the hint and asserts the v1-pinned fetch returns v1
(was hollow — `MAX(version)` passed it); **(P1)** explicit `CHECK (artefact_id ⇒ version)`; **(P1)**
single-embedding-owner test = AST + embeddings-URL grep, framed as a structural drift-guard. Deferred with
named limits: FK-on-prune/soft-delete seam, fusion-weight retune.
**Architecture of record:** `docs/decisions/arb-memory-architecture.md`.
**Dev substrate:** local `pgvector/pgvector:pg16` container (`arb-memory-pg-dev`, port 5544, db/role
`arb_memory`, pgvector 0.8.3); DSN in gitignored `envs/arb-memory-dev.env` (`ARB_MEMORY_DSN`).

## 0. Scope — what Phase 0 IS and is NOT

**IS:** the two-lane Postgres schema + the **scoped write-library** (the single embedding owner), proven
against local psql. The faithfulness contracts the whole design rests on — dual-write atomicity, artefact
versioning, two-step retrieval, content idempotency, single-embedding-owner — built and deny-proven here so
later phases inherit them.

**IS NOT:** no Valkey transport (Phase 1), no audit consumer (Phase 2 — audit table schema is a *stub* only),
no containers/MCP/CF (Phase 3). No code path from any seat into this library yet; it is exercised only by its
own tests.

## 1. Module placement (§7 — scoped write-library)

- New package `src/arb_memory/` — the write-library imported **only** by the three MCP-host services
  (memory consumer, audit consumer, MCP host) in later phases. The rest of ARB has **no import path into it**
  (enforced by a structural test, §6). This is the in-repo expression of single-writer (§4): the embedding
  path is not a free function call from any seat.
- `src/arb_memory/schema.sql` — DDL (idempotent `CREATE … IF NOT EXISTS`).
- `src/arb_memory/embed.py` — `embed()` (the ONLY embedding path).
- `src/arb_memory/store.py` — the dual-write / versioning / retrieval functions.
- `tests/arb_memory/` — the deny-proof suite.

## 2. Schema (two lanes + idempotency + audit stub)

**The DDL MUST run from scratch** on a fresh `arb_memory` DB — so it begins with the extension (codex P0;
`vector(1536)` and HNSW fail without it):
```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

### `artefacts` — faithful lane (retrieved by identity, versioned)
```sql
CREATE TABLE IF NOT EXISTS artefacts (
    artefact_id   text    NOT NULL,           -- caller-supplied id (repo-relative path, or draft slug) — §7
    version       int     NOT NULL,           -- monotonic per artefact_id, starts at 1
    content       text,                       -- faithful bytes (text); binary via content_bytes
    content_bytes bytea,                       -- optional (images/binary), nullable
    content_mime  text    NOT NULL DEFAULT 'text/plain',
    repo_pointer  text,                       -- 'path@commit' when this mirrors repo content; else NULL
    content_hash  text    NOT NULL,           -- versioned canonical hash — see §2a
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (artefact_id, version),
    UNIQUE (artefact_id, content_hash)        -- identical re-save is a no-op, not a new version
);
CREATE INDEX IF NOT EXISTS artefacts_id_idx ON artefacts (artefact_id);
```

### `hints` — fuzzy lane (retrieved by meaning, lossy)
```sql
CREATE TABLE IF NOT EXISTS hints (
    id             bigserial PRIMARY KEY,
    created_at     timestamptz NOT NULL DEFAULT now(),
    text           text NOT NULL,                 -- the fragment that is embedded
    embedding      vector(1536) NOT NULL,         -- OpenAI text-embedding-3-small
    metadata       jsonb NOT NULL DEFAULT '{}',
    source         text NOT NULL DEFAULT 'seat',
    author         text NOT NULL DEFAULT 'unknown',
    -- linkage: a hint is the semantic index over an artefact, OR a repo pointer, OR standalone (§7)
    artefact_id      text,
    artefact_version int,
    repo_pointer     text,                         -- 'path@commit' for repo-resident, seat-readable content
    content_hash   text NOT NULL,                  -- idempotency, VERSION-AWARE (see §2a)
    deleted_at     timestamptz,                    -- soft delete (NULL = live)
    UNIQUE (content_hash),
    -- the link pairing is enforced in DDL, not prose: artefact_id set <=> artefact_version set
    -- (the composite FK alone won't catch a partial-NULL tuple under MATCH SIMPLE).
    CONSTRAINT hints_artefact_pairing CHECK ((artefact_id IS NULL) = (artefact_version IS NULL)),
    -- ON DELETE NO ACTION (default): a pinned hint blocks hard-deleting its artefact version. The
    -- prune/soft-delete-on-artefacts contract is a NAMED DEFERRAL to Phase 1+ (§7); Phase 0 never
    -- hard-deletes artefacts, so NO ACTION is correct here.
    FOREIGN KEY (artefact_id, artefact_version) REFERENCES artefacts (artefact_id, version)
);
CREATE INDEX IF NOT EXISTS hints_embedding_idx
    ON hints USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 256);
CREATE INDEX IF NOT EXISTS hints_tags_idx ON hints USING GIN ((metadata->'tags'));
CREATE INDEX IF NOT EXISTS hints_created_idx ON hints (created_at DESC);
-- hybrid lexical lane (ported from ai-brain fusion); 'simple' regconfig so tool names/IDs aren't stemmed
ALTER TABLE hints ADD COLUMN IF NOT EXISTS search_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('simple',
        coalesce(text,'') || ' ' || coalesce(metadata->>'tags',''))) STORED;
CREATE INDEX IF NOT EXISTS hints_tsv_idx ON hints USING GIN (search_tsv);
```
The FK pins a hint to a **specific artefact version** — the staleness guard (§2 of the decision record).
`artefact_version` is nullable (repo-pointer or standalone hints), but if `artefact_id` is set,
`artefact_version` MUST be set (a CHECK constraint enforces the pairing).

### `audit` — STUB ONLY (schema lands now so Phase 2 is additive; no consumer in Phase 0)
```sql
CREATE TABLE IF NOT EXISTS audit_events (
    id              bigserial PRIMARY KEY,
    run_id          text NOT NULL,
    seq             bigint NOT NULL,               -- monotonic, orchestrator POV
    source          text NOT NULL,                -- 'orchestrator' | '<seat-id>'
    ts              timestamptz NOT NULL DEFAULT now(),
    payload         jsonb NOT NULL DEFAULT '{}',
    stream_entry_id text,                          -- record-shaped (§8): raw bus provenance
    content_hash    text,
    raw_entry       jsonb,
    UNIQUE (run_id, seq)
);
```

### `idempotency_keys` — bus-level dedup (ported; exercised in Phase 1, table lands now)
```sql
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key        text PRIMARY KEY,                   -- client-supplied ULID on a write-intent
    created_at timestamptz NOT NULL DEFAULT now()
);
```

## 2a. `content_hash` canonicalization — PINNED (spec-panel P0, load-bearing #1, all 4 seats)

Idempotency correctness — both "identical re-save is a no-op, not a new version" and "same fragment not
stored twice" — depends entirely on what bytes are hashed. Left open it is either over-eager (collapses
distinct content) or vacuous (won't dedup equivalent content), and for hints it can make a new artefact
version **undiscoverable**. Pinned, versioned (the `arbmem:…:v1\0` tag lets the canonical form evolve
without silent collisions):

- **Artefact hash** (drives `UNIQUE(artefact_id, content_hash)` → new-version-on-changed-content):
  ```
  content_hash = sha256(b"arbmem:artefact:v1\0"
                        + content_mime.encode("utf-8") + b"\0"
                        + kind + b"\0"                       # b"text" or b"binary"
                        + payload)                            # text: content.encode("utf-8") exactly as stored
                                                              # binary: content_bytes exactly
  ```
  A changed mime or text→binary switch yields a different hash (→ a new version), as it must.

- **Hint hash** (drives `UNIQUE(content_hash)` on hints) — **version-aware** so a v2 hint with the SAME
  description text as the v1 hint is still distinct and stored (else v2 is undiscoverable), while a genuine
  duplicate fragment (same text, same link) still dedups:
  ```
  hint_hash = sha256(b"arbmem:hint:v1\0"
                     + text.encode("utf-8") + b"\0"
                     + (artefact_id or "").encode("utf-8") + b"\0"
                     + str(artefact_version or 0).encode("utf-8") + b"\0"
                     + (repo_pointer or "").encode("utf-8"))
  ```
  Metadata is **excluded** from the hash (tags are mutable annotation, not identity). Text is hashed
  **exactly as stored** (no whitespace/case normalization) — normalization is a deliberate non-goal for
  Phase 0; "look identical but differ in whitespace" intentionally stores twice (named limit).

This is the reconciliation the panel forced: cold-Opus/agy required the link fields for discoverability;
codex/M3 required exact-text for storage idempotency — `(text + artefact_id + version)` satisfies both.

## 3. Write-library contract (`src/arb_memory/store.py` + `embed.py`)

- `embed(text: str) -> list[float]` — OpenAI `text-embedding-3-small` (1536-dim). The **single embedding
  owner**. Reads `OPENAI_API_KEY`. No other module in the repo calls the embedding API (§6 structural test).
- `upsert_artefact(conn, artefact_id, content, *, mime='text/plain', content_bytes=None, repo_pointer=None)
  -> (artefact_id, version)` — compute `content_hash`; if `(artefact_id, content_hash)` exists, **return its
  existing version (no-op)**; else insert `version = COALESCE(max(version),0)+1`. Idempotent on identical
  content; a *new* version only on *changed* content.
- `upsert_hint(conn, text, embedding, *, artefact_id=None, artefact_version=None, repo_pointer=None,
  metadata=None) -> hint_id` — idempotent on `content_hash` (`ON CONFLICT (content_hash) DO NOTHING`,
  returning the existing id).
- `write_artefact_and_hints(conn, *, artefact=None, hints=[]) -> {artefact_ref, hint_ids}` — **one
  transaction**: upsert the artefact (obtain `(id, version)`), then upsert each hint linking to that exact
  `(id, version)`. Atomic — a failure in any step rolls back BOTH the artefact and the hints. This is the
  single consumer's write primitive (§4).
- `search_hints(conn, query_text, k=8, *, alpha=…) -> [hint_row]` — `embed(query_text)`, then **hybrid**
  retrieval: vector cosine on `embedding` fused with lexical rank on `search_tsv` (ai-brain's fusion). Returns
  live hints (`deleted_at IS NULL`) with their `(artefact_id, artefact_version, repo_pointer)`.
- `fetch_artefact(conn, artefact_id, version) -> artefact_row` — PK lookup, **byte-for-byte**, the exact
  version (never "latest").
- `retrieve(conn, query_text, k=8) -> [{hint, artefact|None, repo_pointer|None}]` — the **two-step** read:
  `search_hints` → for each hit with an `artefact_id`, `fetch_artefact(id, version)` (the *pinned* version);
  hits with only a `repo_pointer` return the pointer (seat resolves it); standalone hits return the fragment.

## 4. Load-bearing invariants (what the panel must hold to account)

1. **Single embedding owner** — `embed()` is the only embedding path; nothing else imports the OpenAI
   embedding client (structural, like the #9 no-seam guard). Drift prevention (§4).
2. **Dual-write atomicity** — artefact + its hints land together or not at all (one txn). A hint never points
   at an artefact that didn't land; an artefact is never undiscoverable.
3. **Version pinning + faithfulness** — a hint stores `(artefact_id, version)`; `retrieve` fetches **that
   exact version**, byte-for-byte. Re-save with changed content = new version; old version and the hints
   pinned to it are **intact and still fetch the old bytes**.
4. **Content idempotency** — identical content/fragment is not duplicated (`content_hash` unique); identical
   artefact re-save is a no-op, not a new version.
5. **Never-the-only-copy discipline (advisory, not schema-enforced)** — repo-resident, seat-readable content
   SHOULD be a `repo_pointer` hint, not a copied artefact; full-content artefacts are for drafts +
   interactive (no-repo) review. The schema permits all three hint shapes; the discipline lives in the
   write-side callers + docs.

## 5. Embedding in tests — honest fake vs real (anti cheap-fake)

The store mechanics (dual-write, versioning, two-step, idempotency) do **not** depend on embedding
*semantics* — only on a real 1536-vector that pgvector indexes and searches. So:
- **Store/version/two-step tests** use a **deterministic fake embedder** (`text -> sha256 -> 1536 floats`,
  L2-normalized) — it preserves the costly dimension (a real `vector(1536)` exercising HNSW + cosine), and
  same-text→same-vector makes exact-match retrieval deterministic. This is NOT a cheap-fake: the fake is a
  *real vector of the right shape through the real index*, only the semantic quality is synthetic.
- **Semantic quality** (fuzzy match on related-but-different text) needs real embeddings → a separate
  `@pytest.mark.smoke` test gated on `OPENAI_API_KEY`, skipped in the default suite, run on demand.

The panel should confirm this split is honest (the mechanics-fake preserves vector ops + dimension; semantic
correctness is genuinely covered by the real-embedding smoke test, not silently dropped).

## 6. Tests — deny-proof style (each FAILS if its invariant is violated)

- `test_dual_write_atomic` — `write_artefact_and_hints` with a hint that violates a constraint → assert
  **neither** the artefact **nor** any hint persisted (rollback). Deny-proof: a non-transactional impl leaves
  the artefact orphaned.
- `test_version_on_resave_keeps_old` — save artefact v1, re-save changed content → v2 exists, **v1 intact**;
  a hint pinned to v1 still `retrieve`s v1's bytes (not v2's). Deny-proof: an overwrite impl loses v1 / the
  hint fetches v2.
- `test_identical_resave_is_noop` — save same content twice → still v1, no v2. Deny-proof: naive impl makes v2.
- `test_two_step_faithful` — **must put a v2 on the path or it proves nothing** (spec-panel P0: with one
  version, "pinned v1" and "fetch latest" return the same row, so a `MAX(version)` impl passes). Sequence:
  write artefact v1 + a hint pinned to v1; **then re-save changed content → v2 exists**; `retrieve(query)`
  on the v1 hint returns the hint's `(id, version=1)` and the fetched artefact is **byte-identical to v1**,
  NOT v2. Deny-proof: a "fetch latest" / `MAX(version)` impl returns v2 and FAILS. Query semantics pinned to
  **identical-text exact match** (mechanics only, deterministic with the fake embedder); semantic-similarity
  retrieval is the separate `OPENAI_API_KEY` smoke test (§5), so the cheap-fake axis is explicit.
- `test_content_idempotent_hint` — write the same hint twice → one row.
- `test_hint_hash_is_version_aware` — a hint for v1 and a hint with **identical text** for v2 are BOTH
  stored (distinct `content_hash` via §2a) → v2 is discoverable. Deny-proof: a text-only hash dedups them →
  v2 undiscoverable (the central panel P0).
- `test_content_hash_canonicalization` (§2a edge cases) — changed `content_mime` → new artefact version;
  text↔binary switch → different hash; whitespace-differing text → stored twice (named limit, exact-text);
  byte-identical binary → deduped.
- `test_single_embedding_owner` — structural **drift guard** (NOT a deployment single-writer proof — agy +
  codex): **AST-scan** every module under `src/` outside `src/arb_memory/embed.py` for imports of `openai`
  (import + `from`-import nodes), AND **grep** for the embeddings endpoint string
  (`https://api.openai.com/.../embeddings`) to catch a raw-`requests`/`httpx` bypass. Asserts `embed()` is
  the sole embedding path *in committed code*. Stated limit: it cannot prove the deployed single-writer
  (that's §4/§7 deploy-scope) nor a fully dynamic call — it is a committed-code drift guard.
- `test_repo_pointer_hint_no_artefact` — a `repo_pointer` hint (no artefact) stores + `retrieve` returns the
  pointer; vs a full-content artefact returns bytes. Boundary discipline (§4.5).

All against local psql via `ARB_MEMORY_DSN`; the suite creates a scratch schema/rolls back per test (no
cross-test state — the #11 isolation lesson).

## 7. Opens — resolved by the panel (load-bearing closed; rest deferred-with-name)

- **`content_hash` canonicalization — CLOSED pre-build (load-bearing).** Pinned in §2a (versioned domains;
  hint hash includes `(artefact_id, version)`; metadata excluded; text exact-as-stored).
- **`artefact_id` minting — PINNED.** **Caller-supplied string id.** `upsert_artefact` takes an explicit
  `artefact_id`: for repo-mirror artefacts it is the **repo-relative path** (e.g. `docs/foo.md`), so the same
  doc re-captured at a later commit versions under the same id; for born-in-conversation drafts the caller
  supplies a meaningful slug. No server ULID in Phase 0 (the id is meant to be human-stable across
  re-captures, which an opaque ULID defeats). Collision = intended identity (that *is* the versioning key).
- **Standalone hints — ALLOWED by design (named).** The schema permits a hint with no artefact and no
  `repo_pointer` (a pure recollection / lead). No CHECK forbids it; the §2a hint hash over `text` dedups
  duplicates. The "never the only copy" discipline (§1) is advisory on the write side — a standalone hint is
  a *fragment/lead*, not a faithful artefact; original faithful content belongs in the artefact table.
- **Hybrid fusion weighting — port ai-brain's RRF as-is (named deferral).** Re-tune only if a measured
  retrieval-quality signal shows the default is unsuited to ARB topics. Not load-bearing for Phase 0.
- **FK-on-prune / soft-delete-on-artefacts — DEFERRED with named limit (M3 ruled deferrable).** Phase 0
  never hard-deletes artefacts; the hints FK is `ON DELETE NO ACTION` (a pinned hint blocks deleting its
  version). The artefact prune / soft-delete contract is a Phase 1+ decision; named here, not built now.
