# ARB Wiki v1.1 — zero-touch repo onboarding (design)

> Status: design, **round 1 panel-reviewed 2026-07-06** (codex `needs-changes`/P1, agy
> `needs-changes`/P1, cold-Opus `needs-changes`/P2; run
> `panel-wiki-onboard-spec-20260706T204623Z-ffaf00`). Two distinct P1s, both real: agy — the
> CLI's `all_ids` is computed once before the onboard write, so a virgin repo's own sibling
> cross-references fail first-refresh validation; codex — the default-on review gate must fire
> *inside* `refresh_repo` (before store), contradicting the draft's "refresh_repo unchanged".
> Both fixed, plus the convergent findings (reserved-`manifest`-slug ulid-collision — agy +
> cold-Opus, sharpened to a silent batch-marker drop; config write must run under v1's lock —
> all three; `parse_discovery` takes `existing_ids`, not a `load_config` round-trip — codex +
> cold-Opus; 2–8 page bound — all three; seat/reviewer config strategy — agy). Round 2 pending.
> Extends the shipped, prod-deployed v1 generation
> loop (`src/agent_redis_bridge/wiki_refresh.py`, merged `5e9aa35`, live-proven). Implements
> `docs/BACKLOG.md` § "ARB Wiki v1.1 — zero-touch repo onboarding" — Mark's steer: onboarding a
> new repo must need no human step beyond naming the repo path.

## Problem

v1's only human step is authoring the `configs/arb-wiki.json` block for a new repo — its name,
seat, and curated page list. Everything else is automatic. This design removes that step: a
seat proposes the page set from the repo itself, the loop writes the config, and normal v1
refresh runs. An optional reviewer gate replaces the "eyeball the first generation" caveat with
a decorrelated seat.

## What exists and is reused (grounded in source, 2026-07-06)

- `wiki_refresh.py::load_config` / `all_page_ids` — config schema + validation to reuse for
  proposal validation (a proposed repo block must pass the SAME `load_config` shape checks).
- `refresh_repo(...)` — the per-repo loop, extended with one new injected hook: a
  `review_fn=None` parameter (round-1 codex + cold-Opus P1). When set, it is called after
  `validate_pages` passes and BEFORE `build_intents`/temp-dir cleanup/store, receiving
  `(repo, output_dir)` (the generated files still on disk) and returning `(ok: bool, reasons:
  str)`; `ok is False` raises before any pending-state write or store — same all-or-nothing
  posture as a validation failure. `review_fn=None` (v1 default) preserves v1 behavior
  exactly. This is the ONLY change to the shipped v1 function.
- `render_brief` / `validate_pages` / `build_intents` / `_run_store` — untouched.
- `main(argv)` — the CLI; v1.1 adds `--add <path>` alongside the existing
  `--config/--state-dir/--repo/--force`. **Review-gate flag contract (round-2 codex P1 — the
  round-1 draft contradicted itself with a plain opt-in `--review`):** review defaults **ON
  for `--add`** (disable with `--no-review`) and **OFF for steady-state refresh** (enable with
  `--review`) — a single mutually-exclusive `--review/--no-review` pair whose default is
  chosen by whether `--add` is present. Also `--seat-{engine,target-id,timeout}` (onboarding
  seat override) and `--reviewer-{engine,target-id,timeout}` (reviewer override, round-2 agy).
- `configs/arb-wiki.json` — the storage format stays; it stops being hand-authored for new
  repos. It is a git-tracked file (warm-orchestrator commits it, per protocol — the loop writes
  it but does not commit; the CLI prints that a config change was made).

## Architecture

**All additions in `wiki_refresh.py` + the CLI; no new module.** Two new pure functions plus
`--add` wiring.

### Discovery — `parse_discovery(reply_text, repo_name, existing_ids) -> list[dict]`

A discovery dispatch asks a seat to read the repo and return a **strict JSON page proposal**.
`parse_discovery` (pure, testable) extracts the JSON block and enforces every guardrail — it is
the single source of proposal-validity truth, NOT a `load_config` round-trip (round-1 codex +
cold-Opus: the real `load_config` checks only key presence — no id uniqueness, format, or
non-empty values — so it cannot be leaned on for onboarding's invariants). `existing_ids` (the
union of every already-configured page id) is passed in so the disjointness check lives here:

- **Page count 2–8** (settled — round-1 all three seats caught the draft's 3-vs-2
  contradiction). v1's See-also floor is `min(2, siblings)`, so a 2-page repo is valid (each
  cites the one sibling) and is v1-tested; 2 is the floor, 8 the curated ceiling.
- Every `id` matches `^wiki-<repo_name>-[a-z0-9-]+$` exactly — `<repo_name>` fixed to the
  onboarding repo (a proposal cannot mint ids for other repos), slug hyphen-only (links via
  backticks per the graph exporter).
- **Slug is not `manifest` and does not end in `-manifest`** (round-1 agy + cold-Opus,
  sharpened): `build_intents` mints a `wiki-<repo_name>-manifest` artefact whose deterministic
  ulid `sha256(nonce:artefact_id)` would then *equal* a proposed manifest page's ulid,
  silently dropping the batch-complete marker at the consumer's idempotency key. The
  `load_config` round-trip and the disjointness check both miss this (manifest ids never live
  in config `pages`), so it must be an explicit slug reservation.
- Ids unique within the proposal AND disjoint from `existing_ids`.
- `title` and `scope` present, non-empty strings.

Parse failure raises `WikiDiscoveryError` with the specific reason; the onboarding aborts,
writing nothing (a repo half-onboarded is worse than not).

### Discovery brief — `render_discovery_brief(repo_name, repo_path) -> str`

Mirrors the generation brief's shape but asks for the *proposal*, not the pages: read the repo
at `<path>`, propose 2–8 wiki pages that would let a stranger understand it, reply with ONLY a
fenced ```json block: `{"pages": [{"id": "wiki-<repo>-<slug>", "title": ..., "scope": ...}]}`.
States the id rules verbatim so the seat produces parseable output first-try.

### `add_repo(...)` orchestration

```
add_repo(config_path, repo_path, *, dispatch_capture_fn, seat) -> dict   # returns the new repo block
```

**Lock contract (round-2 agy):** `add_repo` assumes the **caller holds** the exclusive
`<state-dir>/lock` — the CLI takes it once, then runs `add_repo` and the subsequent
`refresh_repo` under that single top-level lock, and neither re-acquires it (no self-deadlock).
`add_repo` itself does not touch the lock. **The whole `--add` sequence runs under that lock,
and the config is read AFTER the lock is taken** (round-1 all three seats: `_atomic_write`/`os.replace` makes each
write atomic but does NOT serialize read-modify-write — two concurrent `--add` runs would each
read the pre-existing config and the later rename would lose the earlier repo). Steps:

1. Derive `repo_name` = `Path(repo_path).name` on the normalized path (NOT `os.path.basename`,
   which returns `''` for a trailing-slash path — round-2 agy), lowercased, non-`[a-z0-9-]` →
   `-`; reject if
   it collides with an existing repo `name` (checked against the freshly-read config).
2. Dispatch the discovery brief to `seat` via `dispatch_capture_fn(brief) -> reply_text` — the
   CLI wires this to a `dispatch-dev` call that CAPTURES the reply payload (the discovery
   reply IS the data, unlike generation where the seat writes files); injected for tests.
3. `parse_discovery(reply, repo_name, all_page_ids(config))` → page list, every guardrail
   enforced here (id format, manifest reservation, 2–8, disjointness).
4. Build the repo block `{name, path, seat, pages}` (`seat` supplied by the CLI — see below).
5. Atomically merge the block into `config_path` (read-under-lock already done; append to
   `repos`, `_atomic_write`).
6. Return the block. The CLI then runs the normal `refresh_repo` for it, **recomputing
   `all_ids = all_page_ids(reloaded_config)` first** (round-1 agy P1: the CLI's startup
   `all_ids` predates the new repo, so the new pages' own sibling cross-references would be
   flagged as unknown ids on the very first refresh — the onboarded repo's `all_ids` must
   include its own just-written pages).

### Seat strategy (round-1 agy P2)

The CLI resolves the onboarding seat, in precedence order: explicit
`--seat-engine/--seat-target-id/--seat-timeout` flags; else, if the config already has repos,
reuse the seat of the first configured repo (the deployment's proven seat); else a documented
default (`codex` / `codex-bridge-dev` / `3600`). The resolved seat is stored in the repo block,
so subsequent refreshes are self-describing.

### Review gate (`--review`/`--no-review`, default-on for `--add`) — the ARB pattern applied to itself

The gate is the `review_fn` hook now added to `refresh_repo` (see reuse list): called after
`validate_pages` passes, before `build_intents`/cleanup/store. `--review` dispatches ONE
**decorrelated** reviewer seat with the generated pages and a factual-sanity brief: "these
pages document <repo>; flag any statement factually wrong about the code, or any page that is
generic filler. Reply APPROVE or REQUEST-CHANGES with reasons." REQUEST-CHANGES → `(False,
reasons)` → `refresh_repo` raises before pending-write/store (stores nothing, exits nonzero);
the operator re-runs. Structural validation's factual counterpart, **default-on for `--add`**
(virgin repos, unproven prose), default-off for steady-state refreshes.

**Reviewer resolution (round-1 agy P3):** the reviewer engine is chosen to differ from the
generator engine via a fixed decorrelation map (`codex ↔ agy-print` as the primary pair, with
its `<engine>-bridge-dev` target), overridable by the `--reviewer-*` flags. If no decorrelated
seat is configured/available, `--review` fails loud (a same-engine "review" is not decorrelated
and must not silently degrade). **Consequence, stated explicitly (round-2 cold-Opus P2):** on a
single-engine deployment, `--add` (review default-on) fails by default — so it is not literally
zero-touch there. The intended fallback is documented, not silent: the operator runs
`--add --no-review` (accepting the pilot-level trust the v1 pilot already ran under) or supplies
a `--reviewer-*` seat. The "no human step" goal holds on any multi-engine deployment (the real
one — this host has codex + agy-print + more); single-engine is the degenerate case, handled
loudly rather than by quietly reviewing with the same engine. `review_fn(repo,
output_dir) -> (ok, reasons)` is injected, so the loop is testable without any dispatch; the
CLI wires the real reviewer dispatch (capturing its reply, like discovery).

## Non-goals (v1.1)

- Page-set *evolution* for already-onboarded repos (re-discovery cadence) — `--add` is
  first-onboarding; changing an existing repo's page set stays manual config editing.
- Auto-committing the config change (config is git-tracked; the warm orchestrator commits it —
  the loop writes + reports, matching v1's "loop writes state, doesn't commit" boundary).
- Multi-repo `--add` in one invocation (one repo per `--add`; page sets are small).
- Changing the generator seat's role or the store path (both v1, unchanged).

## Testing plan (`tests/test_wiki_refresh.py`, extended)

Pure-logic, injected callables. **`parse_discovery`**: valid 2-page and 8-page proposals
parse; each guardrail red-first (non-JSON reply; prose-wrapped JSON still extracted; <2 or >8
pages; missing/empty title or scope; id not matching `^wiki-<repo_name>-[a-z0-9-]+$`; slug
`manifest` and slug ending `-manifest` rejected; id colliding with `existing_ids`; duplicate
id within proposal). **`render_discovery_brief`**: contains path, id-format rule, 2–8 count,
JSON-only instruction. **`add_repo`**: happy path writes a valid mergeable block, returns it,
and the merged config passes `load_config`; parse failure writes NOTHING; repo-name collision
rejected; **concurrent-merge test** — simulate a config that changed on disk between the
startup read and the merge, assert the under-lock re-read prevents lost updates. **`all_ids`
recompute**: a first refresh of a freshly-onboarded 2-page repo passes `validate_pages` (its
own siblings resolve — the regression test for agy's P1). **`refresh_repo` review hook**:
`review_fn` returning `(False, reasons)` raises before any pending file / store (reuse the v1
Recorder; assert `stored_batches == []` and no pending file); `(True, "")` proceeds; `None`
(v1 default) is byte-identical to current v1 behavior (the existing v1 tests still pass
unchanged). **Reviewer resolution**: same-engine-only config with `--review` fails loud.

## Self-review notes

- `parse_discovery` (not `load_config`) is the single source of proposal-validity truth —
  `load_config` checks only key presence, so onboarding's real invariants (id format/
  uniqueness/disjointness, non-empty values, count, manifest reservation) all live in
  `parse_discovery`; the merged config is additionally re-checked with `load_config` as a
  cheap shape backstop, not the truth-source (round-2 codex doc-cleanup).
- The one irreducible human input is the repo path (`--add <path>`); page curation and
  first-generation quality both become seat dispatches. The reviewer seat is decorrelated from
  the generator by construction (different engine).
- Riskiest choices for the panel: (a) parsing free-form seat JSON — mitigated by strict
  `parse_discovery` (fail-loud, write-nothing-before-parse); (b) the 2–8 page bound and
  default-on review gate are judgment calls, not derived.

## Round 1 panel record

Independent 3-seat panel, run `panel-wiki-onboard-spec-20260706T204623Z-ffaf00`. Stances:
codex `needs-changes`/**P1** (the review gate needs a `refresh_repo` hook — contradicts
"unchanged"; `parse_discovery` signature/`load_config`-round-trip too weak; config write must
run under the lock), agy `needs-changes`/**P1** (stale `all_ids` fails first-refresh
validation of a new repo's own siblings; config-write race; 3-vs-2 page contradiction;
`manifest`-slug hijack; seat/reviewer strategy unspecified), cold-Opus `needs-changes`/P2
(`manifest`-slug ulid-collision sharpened to a *silent batch-marker drop*; the same
page-count, review-hook, round-trip, and lock findings). Two distinct P1s (agy's stale-ids,
codex's review-hook) both fixed; every convergent P2 folded in — see the round-1 callouts
throughout. The `manifest`-slug reservation was found by two seats and its severity upgraded
by the third (deterministic ulid ⇒ marker drop, not a mere id clash).

## Round 2 panel record

Same three seats, targeted, run `panel-wiki-onboard-spec-r2-20260706T205319Z-f7aff5`. Stances:
agy `approve`/none, cold-Opus `approve`/P2 (all four round-1 fixes confirmed correct against
source, no new defects — its two P2 notes, the add_repo no-re-lock contract and the
single-engine `--add` fallback, are folded in), codex
`needs-changes`/**P1** — a real self-contradiction I introduced in round 1's fix: the spec
said both "opt-in `--review`" and "default-on for `--add`". Resolved: `--review`/`--no-review`
is one mutually-exclusive pair whose default flips on `--add` presence (on for add, off for
refresh). codex also caught the discovery brief still saying "3–8" while the parser/tests say
2–8 (fixed), and two doc-cleanup drifts (the self-review `load_config`-truth-source note; the
top-level `{"pages": [...]}` JSON wrapper to pin in tests — both corrected). codex independently
**confirmed** all four round-1 mechanics against source (review_fn insertion point,
`all_ids` recompute preserving multi-repo cross-citation, `parse_discovery` guardrail
completeness, single-lock consistency). agy's notes are plan-stage refinements folded into the
reuse/architecture sections: `add_repo`'s caller-holds-the-lock contract (no re-acquire inside),
`Path(repo_path).name` on a normalized path (not `basename`, which is `''` for a trailing
slash), and the `--reviewer-*` CLI flags. Ready for plan.
