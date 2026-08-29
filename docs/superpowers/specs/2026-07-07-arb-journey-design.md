# ARB `/journey` — memory-graph view: design (v3)

**Status:** design v3 after two panel rounds (R1: 4×reject of v1's live-endpoint architecture;
R2: 4×needs-changes, precompute architecture unanimously endorsed, findings converged on
deploy mechanics + egress scope). Author: Fable (warm, inline).
**Brief:** `art-777cbc6a8be142f9`. **Panel record:** R1 killed the live endpoint (visibility
role deny-proven against memory tables — deliberate containment; verified `grants.py`,
`test_visibility_grants.py:136-139`). R2 accepted precompute and found the mechanics gaps
fixed below.

## Architecture (v3 = v2 precompute + R2 fixes)

**`journey-export`** (new `src/arb_memory/journey_export.py` + console script) runs in the
**memory container** (privileged read, like the vault export) and writes one snapshot,
atomically (temp + rename), to a **new shared volume** — the R2 P0 fix (found independently
by codex, agy, cold-Opus with mechanics: memory's only mount is the vault volume; visibility
has NO mounts and serves in-image files):

- `deploy/docker-compose.yml`: named volume `arb_journey` mounted into `memory` (rw) and
  `visibility` (ro); path via `ARB_JOURNEY_SNAPSHOT_DIR` env in both services. Survives
  container recreation by construction.
- The visibility app gains **two explicit always-auth routes** (R2: there is no StaticFiles
  mount, and the index-route pattern is only gated when login is enabled — the snapshot must
  use the data-route pattern that authenticates unconditionally): `GET /journey` (page) and
  `GET /journey/graph.json` (reads the mounted snapshot). No DB access added; the containment
  deny-proof and its tests stay untouched.

**Snapshot content — egress scope tightened (R2 cold-Opus P1-c, GLM P1-β):**
- Nodes (latest artefact versions): id, **title ≤120 chars**, **tags**, kind, source, author,
  created_at, version, in/out degree, anchored-hint count. Titles + tags are retained as the
  approved bounded egress (labels, not bodies) — **flagged for Mark's sign-off as the one
  open fork**.
- Free-standing hints (table lens only): **id, created_at, tags — NO text preview** (R2: an
  un-flagged sensitive surface; dropped).
- Full content never leaves the store; the detail panel shows a copyable
  `memory_get(id, version)` pointer.
- `counts`: nodes, edges, dangling (measured open-world), isolated, free hints, generated_at.

**Edge extraction (`extract_refs`, R2 agy P1 + P2 fixes):** open-world scan of latest content
for `art-[0-9a-f]{16}` and backticked `wiki-*` ids with **word-boundary guards** (the wiki
validator's boundary approach — no substring leaks); artefacts with NULL/binary content skip
body parsing and take `artefact_id` as title (explicit test). Self-refs dropped; dangling
counted, not silently dropped. `kind` derivation is an explicit precedence table:
`wiki-*-manifest → wiki-manifest`, `wiki-* → wiki-page`, `learn-* → learn-proposal`,
`howto-* → howto`, else `note`.

**Verification (R2 GLM P1-γ + agy verify-race fix):**
- Golden edge-SET fixture test (unchanged from v2).
- `journey-export --verify`: runs in the SAME transaction snapshot as the export (REPEATABLE
  READ) and checks the written file against **an independent checker** — count SQL plus a
  second, separately-implemented reference scanner (find + boundary check, not a call into
  `extract_refs`) — nonzero exit on mismatch. Standalone prod runs compare `generated_at`
  first and skip if the store moved since export (no false race alarms).
- Playwright live-render check: **declared** as a dev-dependency, skipped when unavailable
  locally, required in CI (R2 GLM P2).

**Cadence:** piggyback the nightly vault-export cron + on-demand command (R2: fork accepted).

## Non-goals (unchanged)

Writes; similarity edges; version-history nodes; live SSE; inline content; any change to
`vault_export.py`, the visibility grants, or their tests.

## Egress fork — RESOLVED (Mark, 2026-07-07)

Titles (≤120 chars) + tags cross the token-gated web pane as bounded label-egress; artefact
bodies and free-hint text never do. Basis: the store's actual titles/tags are descriptive
labels (census-sampled), the pane sits behind the bearer gate (+ CF Access on prod), and the
detail panel hands out `memory_get` pointers rather than content. If a sensitive-titled
artefact class emerges later, the export gains a per-class id-only override — noted as a
future knob, not built now.
