# ARB `/journey` — SPEC (v2 — spec-panel round 1 absorbed)

**Design:** `2026-07-07-arb-journey-design.md` (3-round certified). This spec is the
implementation contract; carried P2s from round 3 are resolved inline and marked ⟨R3⟩.

## Modules & files

- `src/arb_memory/journey_export.py` — pure graph assembly + export job.
- Console script: pyproject `[project.scripts]` gains
  `arb-journey-export = "arb_memory.journey_export:main"` — runs in the memory container
  under its existing env (`ARB_MEMORY_DSN`, same as the vault export; no new role).
- `src/arb_memory/visibility.py` — two new routes (below). No other changes.
- `src/arb_memory/static/journey.html` — SELF-CONTAINED (inline JS + CSS, no CDN,
  no separate journey.js: the panel's convergent P1 was an unroutable second file).
- `deploy/docker-compose.yml` — top-level `volumes: arb_journey:` declared; mounts: `memory` rw at
  `/journey-snapshot`, `visibility` ro at same path; both services get
  `ARB_JOURNEY_SNAPSHOT_DIR=/journey-snapshot`.
- Tests: `tests/arb_memory/test_journey_export.py`, route tests in the visibility test
  module, golden fixture under `tests/arb_memory/fixtures/journey/`.

## `journey_export` contract

- `extract_refs(content: str, artefact_id: str, known_ids: set) -> tuple[set[str], int]`
  (own id passed in — required for the minus-self contract) — open-world scan for
  `art-[0-9a-f]{16}` and backticked `wiki-[A-Za-z0-9._-]+` with word-boundary guards
  (`(?<![A-Za-z0-9_-]) … (?![A-Za-z0-9_-])` for bare art-ids; backtick delimiters for wiki
  ids); returns (resolved refs ∩ known_ids minus self, dangling count). NULL/None/binary
  content ⇒ `(set(), 0)`.
- `node_title(content, artefact_id)` — first `# ` heading else first non-empty line, ≤120
  chars; NULL/binary ⇒ artefact_id.
- `node_kind(artefact_id, mime)` — precedence: `wiki-*-manifest`→wiki-manifest,
  `wiki-*`→wiki-page, `learn-*`→learn-proposal, `howto-*`→howto, else note.
- `build_snapshot(conn) -> dict` — pure assembly; `main()` OWNS the single REPEATABLE
  READ transaction (build and same-run verify share it; build_snapshot never commits); nodes = latest artefact
  versions (id, title, tags [from live anchored hints], kind, source, author, created_at,
  version, degrees, anchored_hint_count); edges (src,dst); `free_hints` = live unanchored
  hints as (id, created_at, tags) — **no text** (Mark's egress ruling: titles+tags only);
  `counts` = {nodes, edges, dangling, isolated, free_hints, generated_at}.
- `main()`: build → write `graph.json.tmp` in `ARB_JOURNEY_SNAPSHOT_DIR` → `os.replace`.
  Two verify modes (cold-Opus P2 — one flag, two behaviors, now split):
  default run = build + verify within the same transaction, using an independent checker
  (`_verify_refs`: separately-implemented find+boundary-check, MUST NOT call `extract_refs`),
  nonzero exit on mismatch. `--verify-only [path]` = no build: open a fresh transaction,
  verify the EXISTING file; if `generated_at` predates the store's latest write, exit
  0-with-notice (stale-not-wrong). Verification obligation 3 uses `--verify-only`.

## Visibility routes ⟨R3 cold-Opus P2s resolved⟩

- Both routes authenticate via `request_authenticated` (session OR token — cold-Opus P2:
  literal bearer-only would 401 the session-authed prod browser).
- `GET /journey/graph.json`: never redirects; unauthenticated ⇒ 401 JSON. Missing snapshot ⇒
  **503** `{"error": "no snapshot yet — run arb-journey-export"}` (pinned test).
- `GET /journey`: serves the self-contained journey.html. Unauthenticated browser
  (Accept: text/html) with login enabled ⇒ **302 /login**; otherwise 401. Pinned both ways.
- Grants/containment: zero DB access added; `test_visibility_grants` untouched and must
  still pass (regression guard in CI).

## Client (journey.html/js)

Force layout (radius ∝ in-degree, colour by kind), table lens (artefacts + free hints;
filters kind/source/tag/isolated-only), shared detail panel (metadata, neighbours, copyable
`memory_get(id, version)` — no content). `generated_at` rendered prominently.

## Verification obligations

1. Unit: extract_refs (boundaries, self, dangling, NULL), title/kind tables, degree math.
2. Golden edge-SET fixture test (hand-curated expected set, exact match).
3. `--verify` reality-contact test: fixture store, corrupt the written file, verify fails.
4. Route tests: 503-no-snapshot, 302-vs-401, auth-required-always for graph.json.
5. Playwright (declared dev-dep, CI-required, local-skippable): rendered node count ==
   snapshot count; a known hub clickable; page renders behind real token.
6. Live gate: run export on prod, `--verify` green, pane loads, spot-check one hub against
   `memory_get`.

## Cadence

Nightly with the vault-export cron + on-demand `arb-journey-export`. CHANGELOG entry.
