# Agent-local memory seeds (canonical corpus)

Version-controlled source of truth for the GLOBAL (machine-wide) memory topics we seed into
each agent family's local auto-memory store. One corpus, per-agent adapters.

## Contents
Topic files in codex auto-memory format (YAML frontmatter: name/description/metadata{type,
origin_session_id, last_write_session_id, source_project_key} + markdown body). Curated for an
agent audience on the operator's main workstation: ARB overview/repo map, dispatch recipe, seat roster +
calibration, panel protocol + round-convergence doctrine, verify-don't-trust epistemics
(authored organically BY codex, 2026-07-21), FABA bounded-rounds, operator rules, machine
quirks, codex-fork state.

## Seeding targets
- **codex** (`~/.codex/auto-memory/global/`): SEEDED 2026-07-21 — files copy verbatim
  (this IS the codex format); the store's staleness signature rebuilds MEMORY.md on the next
  injection read. chmod 600, dirs 700.
- **pi** (`~/.pi/agent/memory/` global tier, pi-memory extension in a private pi-extensions
  repo — the DONOR codex auto-memory was built from): SEEDED 2026-07-22 via `./seed-pi.py`, which
  translates the frontmatter (metadata.node_type: memory; camelCase
  originSessionId/lastWriteSessionId/sourceProjectKey; JSON-quoted scalars) and applies pi's
  sanitizer limits (desc ≤240, name ≤64 kebab). Body markdown carries over unchanged;
  [[links]] work in both.
  **`seed-pi.py` is initial-seed-only — it SKIPS any topic that already exists, by design, so it
  never clobbers organic pi saves. It therefore cannot deliver an update to an
  already-seeded topic; those are hand-merges.** pi's copies have also been adapted in place
  (seat table, local `/tmp/arb-memory-refresh/...` cache pointers, tighter per-seat bullets), so
  a corpus file is never a drop-in replacement for its pi counterpart.
- Keep entries agent-agnostic: no "you are codex/pi" phrasing; "you" = whichever agent reads it.

## Update discipline
Edit HERE first, then re-seed changed topics to each store (agents' own organic saves live only
in their stores and are NOT mirrored here unless promoted deliberately — promotion = copy back
+ commit). Provenance rule: seeded files carry origin_session_id "seeded-by-*" so stores never
misattribute authorship; organically-authored entries promoted here keep their real session ids.

**The fan-out is the weak link, not the corpus.** A corpus edit changes nothing any agent reads
until it reaches each live store, and nothing warns you that it hasn't. Measured 2026-07-24: the
GLM read-only correction (`c46ec1ca`, 2026-07-23) was correct in git while the codex store still
served the inverted prior, and both the codex and pi copies still cited `arb-seat-scorecard` v1
against a store at v3. So after editing here, verify each target store directly —
`diff` for codex (verbatim copies), a read-and-merge for pi and claude-harness (adapted copies).
`scripts/check-seed-canon` gates the corpus's canonical pointers; it does NOT inspect live
stores, so store drift is still caught only by looking.

## Panel-seat exclusion rule (decorrelation)
Seeded memory is for INTERACTIVE/ORCHESTRATOR agents only. PANEL REVIEWER seats stay COLD:
shared doctrine/context injected into a reviewer anchors the very seat whose decorrelation the
panel edifice rests on, and makes "you have no prior context" briefs quietly false.
- pi-sdk seats: set `PI_AUTO_MEMORY_DISABLED=1` in the seat's plist EnvironmentVariables
  (applied to pi-sdk-arb-codex-dev-glm 2026-07-21).
- codex seats: leave `features.auto_memory` OFF in any seat-specific codex config (seat
  app-servers currently inherit ~/.codex/config.toml — if a codex seat is used as a PANEL
  reviewer with memory flags on, override per-seat before trusting its cold-review framing).
- Generalizes to any future agent family: wire the memory-disable switch into the seat's spawn
  env BEFORE first panel use; an informed reviewer is a correlated reviewer.
