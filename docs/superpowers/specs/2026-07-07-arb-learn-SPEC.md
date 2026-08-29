# ARB `/learn` — SPEC (v3 — configurable evaluation panel)

**Design:** `2026-07-07-arb-learn-design.md` (3-round certified). Carried R3 items are
resolved inline and marked ⟨R3⟩.

## Modules & files

- `src/agent_redis_bridge/learn_intake.py` — pure helpers + subprocess glue + `main()`
  (wiki-pipeline structure; `main()`-level tests mandatory).
- `scripts/arb-learn` — thin entrypoint (wiki-style).
- `skills/using-arb-learn/SKILL.md` — orchestrator workflow; requires a structured
  model-selection question before every interactive evaluation.
- Tests: `tests/test_learn_intake.py`.
- No changes to `arb_memory` core, `stance.py`, `panel_run.py`.

## Identity & lifecycle

- Id: `learn-<slug>-<hash8>`; slug = sanitized first-line ≤40 chars; hash8 = sha256(source
  bytes)[:8]. `--force` re-proposal mints `…-r<N>` with `supersedes` recorded.
- Statuses (forward-only per id): `proposed → rejected | eval-approved | needs-mark |
  eval-error`; `eval-error → (re-evaluate) → any`; `eval-approved → promoted`;
  `needs-mark → (arb-learn resolve, human) → eval-approved | rejected` — the one exit from
  needs-mark (spec-panel P1: it was a stuck state). `resolve` requires `--reason`, records it.
- **Write-visibility barrier (spec-panel BLOCK, cold-Opus):** every artefact write goes async
  (bus stream → WriteLoop → Postgres) while `get_status` reads Postgres directly — so after
  EVERY write, the CLI polls `get_status` until the new version is visible (10 tries × 2s,
  then fail-loud naming the WriteLoop) BEFORE reporting success or proceeding. The live gate
  MUST re-attack a freshly-rejected id (propose+evaluate+promote) — faked-dispatch unit
  tests cannot see this race by construction.
- **Terminal guard is CLI-enforced** ⟨R3 cold-Opus P2-1⟩: `propose`, `evaluate`, `promote`
  each call `get_status(id)` first and refuse invalid transitions. The resurrection pin test
  attacks ALL THREE verbs on a `rejected` id (not just propose) — the store itself remains a
  bare version-append and that is accepted, documented residual.
- **Metadata split** ⟨R3 cold-Opus P2-2⟩: artefact rows carry only content (the proposal doc:
  first line is a single JSON object — status, target, proposer, supersedes, referent — then
  markdown body; JSON not YAML, so no new dependency). Hint rows carry
  `metadata = {kind: artefact_index, learn_proposal: true, status, target}`. `get_status`
  parses the LATEST artefact version's first-line JSON header (single source of truth); hint metadata
  is search convenience only.

## Query primitives (new SQL over the proven ssh→memory-container transport)

- `list_proposals()` / `get_status(id)` run
  `SELECT DISTINCT ON (artefact_id) artefact_id, version, content, created_at FROM artefacts
  WHERE artefact_id LIKE 'learn-%' ORDER BY artefact_id, version DESC` (± id filter) via
  `ssh arb-prod … docker compose exec -T memory python3 -`, **capturing stdout**: the inline script prints the JSON payload between literal sentinel
  lines `ARB_JSON_BEGIN` / `ARB_JSON_END`, and the helper parses ONLY between them
  (ssh/docker banners and warnings cannot break the parse). ⟨R3 GLM P2-δ resolved⟩.

## `evaluate` — panel selection and verdict plumbing

- `--panel core` selects Codex Sol + agy + GLM and remains the automation default.
  `--panel full` adds Fable 5 through Agent SDK and Grok 4.5. Repeated `--seat NAME`
  selects an exact custom subset. `--panel` and `--seat` are mutually exclusive, preventing
  an explicit preset from being silently discarded. Valid aliases are
  `codex-sol`, `agy`, `glm`, `fable`, and `grok`; duplicates are removed without
  reordering the selection.
  The mutually-exclusive actions use suppressed defaults; `main()` applies `core`/`[]`
  after parsing. This prevents Python `argparse` from treating an explicit
  `--panel core` as absent and accepting a simultaneous `--seat`.
- Interactive orchestrators MUST load `skills/using-arb-learn/SKILL.md` and use their
  structured question tool to offer Full/Core/Custom before every evaluation or retry.
  The CLI default supports non-interactive automation; it is not permission to skip the
  interactive selection.
- Pinned dispatch targets are `codex-bridge-dev-example`, `agy-bridge-dev`,
  `pi-sdk-bridge-dev-glm`, `asdk-agentredisbridge-dev-fable5`, and
  `grok-agentredisbridge-dev-grok45`. Codex uses per-dispatch `--effort high`; its seat is
  pinned to `gpt-5.6-sol` at launch rather than through `--model` because explicit model
  forwarding was live-proven to fail. Fable receives a run-scoped isolated worktree with
  `--worktree-cleanup auto`, required by trusted Agent SDK dispatches.
- Every selected seat shares one minted run-id and uses `--audit-panel`. **Decision source
  = the selected synchronous dispatch replies** (the
  captured stdout envelopes) — never the audit stream ⟨R3 GLM P2-γ: kills the
  consumer-flush race⟩. Audit votes are trail only; no `panel_run.finalize`/`reconcile`
  dependency; unrostered/duplicate handling therefore N/A in v1 and stated as such
  ⟨R3 GLM P2-β⟩.
- Per-seat domain verdict parsed from reply prose with word-boundary regexes
  (`\bREJECT\b` etc., uppercase only — "rejected/rejection" prose cannot false-fire) and
  fixed severity precedence on co-occurrence: `REJECT` > `NEEDS-MARK` > `WORTH-BUILDING`
  (position never matters); none found ⇒ that seat is `eval-error`. Fence cross-check with
  `require_fence=True`: mapped stance must agree (`REJECT→block`, `WORTH-BUILDING→approve`,
  `NEEDS-MARK→abstain`, severity field required); disagreement ⇒ `eval-error` for the seat.
- **Outcome precedence** ⟨R3 GLM P1-α + cold-Opus P2-3, the one open ruling — resolved
  strict⟩: (1) any substantive `REJECT` ⇒ `rejected`, regardless of other seats' errors;
  (2) else ANY seat `eval-error`/timeout ⇒ eval outcome `eval-error` (retryable) — approval
  NEVER proceeds with a silent seat; the "≥2 clean seats" floor is deleted as unreachable;
  (3) else any `NEEDS-MARK` ⇒ `needs-mark`; (4) else (all selected seats return clean
  `WORTH-BUILDING`) ⇒ `eval-approved`.
- Outcome written as next artefact version, ALWAYS accompanied by a replacement
  `artefact_index` hint (agy P1: without it, retirement never fires and search serves the
  stale status forever). Header + per-seat reasons verbatim.

## `propose` / `promote` (unchanged from design v3)

Propose: terminal-guard → dedupe → store artefact+index hint (then the visibility barrier).
Dedupe is PINNED (agy P1): `difflib.SequenceMatcher(None, a, b).ratio() ≥ 0.6` over
normalized (lowercased, whitespace-collapsed) title + first 500 chars, against every
`list_proposals()` row; on match, print the matched id + its status and require `--force`.
`memory_search` top-5 is printed as advisory prior-art (never blocks). Eval-brief citations
are PINNED: top-3 `wiki-*` hits from `memory_search` on the proposal title, embedded by id.
`--from-workflow <handoff> <sha> --repo <path>`: handoff file must exist; SHA verified with
`git -C <path> cat-file -e` (codex P1: cat-file is repo-local — the repo is now explicit);
all three recorded in the referent. Promote: refuses unless `eval-approved`;
gate-drift refuse (last 10 substantive evals; display-only below 5 samples; warn ≥40%,
refuse ≥60% without `--override`); `--target project` needs `--i-am-mark`; emits inert
build-brief file only.

## Verification obligations

1. Unit: id/slug/hash/ordinal minting; three-verb resurrection pin; JSON-header status
   parse; verdict precedence table (every branch incl. REJECT+error, error+approve,
   fence/prose disagreement); drift math with low-N floor; promote refusals; referent checks;
   `resolve` exits from needs-mark (and refuses from any other state); dedupe ratio
   threshold; sentinel-framed payload parse with noisy stdout; visibility-barrier poll
   (fail-loud on never-visible); core/full/custom selection; Sol high-effort dispatch;
   Fable isolated-worktree dispatch; exact selected-seat execution.
   The `main()` contract must execute `--panel full`, reject `--panel` + `--seat`, and
   bind each seat-specific flag to its exact value rather than testing token presence.
   Mutual-exclusion tests cover both presets and both flag orders.
2. `main()`-level with faked `subprocess.run`: propose→evaluate(REJECT)→propose-again-refused;
   propose→evaluate(all WORTH-BUILDING)→promote emits brief; evaluate with one timeout ⇒
   eval-error and `--retry` path.
3. Live gate (required): one real external proposal end-to-end (expected `rejected` with
   reasons); one `--from-workflow` proposal promoted only with Mark's nod. CHANGELOG entry.
