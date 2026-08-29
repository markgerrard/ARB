# ARB Wiki generation loop (design)

> Status: design, **round 1 panel-reviewed 2026-07-06** (codex `needs-changes`/P2, agy
> `needs-changes`/**P1**, cold-Opus `approve`/P2, run
> `panel-wiki-loop-spec-20260706T191620Z-2cdba0`). Headline: agy found a real **pre-existing
> P1 in shipped `store.py`** (dedup-against-any-historical-version breaks content rollback —
> now a prerequisite fix, § below); codex proved the store step could half-publish despite the
> spec's own all-or-nothing claim (batch protocol rewritten); all three seats independently
> converged on strict sibling-id validation. **Round 2 (targeted, run
> `panel-wiki-loop-spec-r2-20260706T192601Z-25dba0`): codex `needs-changes`/P2, agy
> `needs-changes`/P2 (verdict text approve-with-notes), cold-Opus `approve`/P2 — all three
> independently confirmed the prerequisite fix complete and the constraint drop safe; the
> remaining convergent P2s (resume-from-persisted-batch not regeneration; state-file
> atomicity + run lock; cross-repo/bare citation validation; 2-page-repo See-also edge) are
> folded in, marked with round-2 callouts. Ready for plan.**
> Builds the "generation loop" half of
> `docs/BACKLOG.md` § "ARB Wiki" — the pilot (2026-07-06, same day) proved the whole pipeline
> end-to-end manually: codex generated 5 pages from <workspace> source → stored as artefacts via
> the write path → vault graph export linked them fully (96 files, 351 wikilinks, wiki cluster
> connected internally at distance 0.17–0.22 AND bidirectionally to the pre-existing corpus).
> This design automates exactly what the pilot did by hand, and nothing more.

## Problem

The pilot's steps were manual: compose a brief, dispatch codex, eyeball the output, store each
page via the MCP door, rerun the export. A repo's wiki goes stale the moment the repo moves.
The loop must: detect that a configured repo changed, regenerate its page set through a bridge
seat, validate the output against the format rules the graph export depends on, store pages
through the single-writer path, and record what was generated from which commit — idempotently,
so a scheduled run on an unchanged repo is a cheap no-op.

## What exists and is reused (grounded in source, 2026-07-06)

- **Write path:** `src/arb_memory/bus.py::memory_write(redis, artefact=..., hints=...)`
  enqueues an intent on `arbmem:writes`; the prod memory consumer owns embedding + transactional
  insert (`handle_write_intent`, idempotent by ulid — the ulid is the caller-controllable
  idempotency key). `src/arb_memory/mcp/tools.py::memory_store` shows the artefact+index-hint
  shape to mirror. **Load-bearing invariant (round-1 cold-Opus catch): the hint→artefact link
  is POSITIONAL, not metadata-driven** — `store.write_artefact_and_hints` links a hint to the
  artefact only because both ride in the *same* intent; the hint's
  `metadata: {"kind": "artefact_index", ...}` is descriptive, not the linking mechanism.
  Therefore **each page is exactly one intent carrying its artefact AND its index hint
  together** — batching artefacts and hints separately would store hints unlinked (pages
  unsearchable, no graph edges). Re-storing content unchanged from the *latest* version is
  version-stable via content-hash dedup (after the prerequisite fix below).
- **Dispatch:** `scripts/dispatch-dev` (auto run-id, canonical env overrides), codex seat
  proven for this task class in the pilot. Generation is READ-ONLY — no worktree needed.
- **Format rules the exporter depends on** (from the pilot brief, now load-bearing): first line
  `# <title>`; sibling cross-references as **backticked artefact ids**; no `[[...]]` anywhere;
  no frontmatter; ends with a `See also:` line of backticked sibling ids.
- **Export/cron:** the nightly vault export (03:30 UTC on the droplet) picks up new versions
  automatically; the loop does not need to trigger it.

## Prerequisite fix — `store.upsert_artefact` latest-version dedup (round-1 agy, P1)

`src/arb_memory/store.py::upsert_artefact` dedups by checking whether `(artefact_id,
content_hash)` exists at **any** historical version. If content oscillates (A → B → A: a repo
reverted, a page regenerated back to prior text), the third write matches version 1's hash,
returns `(id, 1)`, and writes nothing — but the **latest** version is still 2 (content B), so
the vault (latest-version-only) stays stale on B forever, silently. This is a pre-existing bug
in shipped code whose blast radius exceeds the wiki (any re-stored artefact that reverts —
revised specs, handoffs); the wiki loop would merely trigger it routinely. Fix (Task 0 of the
plan, red-first with an A→B→A test): dedup against the **latest** version only —

```sql
SELECT version, content_hash FROM artefacts
WHERE artefact_id = %s ORDER BY version DESC LIMIT 1
```

— return the existing version iff its hash matches; otherwise insert the next version even if
the hash matches an older one. Blast radius checked: hints pin `(artefact_id, version)` FKs
(old pins unaffected); the common re-store-unchanged case still no-ops; the vault export
becomes *correct* for reverts. The `UNIQUE (artefact_id, content_hash)` table constraint also
blocks re-inserting a historical hash — the fix must drop that constraint (a migration note
for the plan; uniqueness of `(artefact_id, version)` is the real key). Cold-Opus reviewed the
same behavior and would have accepted it as a documented non-goal; agy's fix-it position wins
adjudication because the bug is latent in every artefact class, not just wiki pages.

## Architecture

**One new module `src/agent_redis_bridge/wiki_refresh.py`** (pure logic, testable without
network) **plus thin CLI `scripts/arb-wiki-refresh`** (this repo's thin-wrapper convention).

### Config — `configs/arb-wiki.json` (committed, curated)

```json
{
  "repos": [
    {
      "name": "workspace-dev",
      "path": "/Users/<user>/<workspace>",
      "seat": {"engine": "codex", "target_id": "codex-bridge-dev", "timeout": 3600},
      "pages": [
        {"id": "wiki-workspace-dev-overview", "title": "<workspace> Overview",
         "scope": "what the bridge is: dispatch bus, ARB planes, warm/cold orchestration, two-clone topology"},
        {"id": "wiki-workspace-dev-dispatch-protocol", "title": "<workspace> Dispatch Protocol",
         "scope": "envelope, inbox/BLPOP, agent-dispatch vs dispatch-dev, run-id discipline, worktree dispatch, notify split"},
        {"id": "wiki-workspace-dev-engines", "title": "<workspace> Engines",
         "scope": "engine adapters, role profiles, engine pool/parallelism, completion gates"},
        {"id": "wiki-workspace-dev-arb-memory-plane", "title": "<workspace> ARB Memory Plane",
         "scope": "artefacts/hints schema, single-writer, doors, read tools, vault export + graph"},
        {"id": "wiki-workspace-dev-observability", "title": "<workspace> Observability",
         "scope": "events:live/eval/trace/transcript tees, arb-watch/visibility, audit/vote plane"}
      ]
    }
  ]
}
```

Page sets are **operator-curated** (v1 non-goal: auto-discovery). Ids follow the pilot's
`wiki-<repo>-<page>` scheme — hyphen-only, so per the graph spec they cross-link via backticks.

### The loop (per configured repo, sequential)

1. **Change detection:** `git -C <path> rev-parse HEAD` → current SHA. Compare against the
   state file `~/.arb-wiki/state.json` (`{"<repo>": {"head_sha": ..., "generated_at": ...}}`).
   Unchanged and not `--force` → log and skip. The state file is the operative gate (single
   orchestration host runs the loop); the manifest artefact below is visibility, not the gate.
2. **Generate:** render the generation brief from config (template = the pilot brief: page
   list with titles+scopes, format rules verbatim, output dir), write it to a temp dir,
   dispatch the configured seat via `scripts/dispatch-dev` with `--adhoc` and the standard
   env overrides. Seat writes `<page-id>.md` files to the temp output dir (out-of-repo).
3. **Validate (fail loud per page, refuse partial stores):** every configured page file
   exists and is non-empty; first line starts with `# `; contains no `[[`; length within sane
   bounds (500–6000 chars); **the final non-empty line is the `See also:` line, every
   backticked token on it is a configured page id of this repo and not the page itself, with
   `min(2, sibling count)` entries (a 2-page repo can only cite one sibling — round-2 agy)
   and no bare (unbackticked) or unknown ids on that line**; and body-wide, any backticked
   token matching `wiki-*` must be a page id configured for ANY repo in the config (cross-repo
   citations validate against the whole config — round-2 agy + cold-Opus), and any BARE token
   matching `wiki-*` fails outright (hyphen-only ids never link unbackticked, so a bare
   mention is always a silent orphan — round-2 agy). Any failure → store NOTHING for that
   repo, exit nonzero with per-page reasons. Named residual: `art-<hex>` citations are NOT
   validated against anything (they may legitimately cite prod artefacts the config doesn't
   know; they link bare per the exporter's rules, and a typo'd one simply doesn't resolve —
   the exporter drops unknown ids rather than emitting dead links).
   > **Round 1, addressed (all three seats independently):** the draft's "contains a See-also
   > line with ≥1 backticked id" was weaker than the pilot contract the exporter depends on —
   > a typo'd id, a mid-file See-also, or bare hyphen-only ids all passed while producing
   > silent graph orphans. Tightened to the exact final-line contract above (codex's
   > formulation, extended with agy's and cold-Opus's config cross-check).
4. **Store (batch-safe protocol — rewritten after round 1):**
   - Build ALL intents up front, one intent per page (artefact + its index hint together, per
     the positional-linking invariant), each mirroring `memory_store`'s shape (explicit
     `artefact_id`, `content`, `mime="text/markdown"`, `source="wiki"`,
     `author="<seat target_id>"`), plus the `wiki-<repo>-manifest` artefact intent (JSON:
     repo, head_sha, generated_at, page ids) — and validate/serialize every one BEFORE any
     enqueue.
   - **Retry-stable idempotency — resume from the persisted batch, never regenerate (round-2
     codex, corroborated by cold-Opus and agy):** when generation + validation complete, mint
     a batch nonce and atomically write `~/.arb-wiki/pending-<repo>.json` containing the FULL
     serialized, validated intent batch (all page intents + the manifest intent, in order)
     plus nonce and head_sha — BEFORE any enqueue. Each intent's ulid is derived
     deterministically from `(nonce, artefact_id)`. A rerun that finds a pending file resumes
     by enqueueing FROM THAT FILE — it does not re-dispatch the seat, so a resumed batch is
     byte-identical to the interrupted one (LLM regeneration would otherwise produce a
     mixed-generation set: consumed ulids keep run-1 prose, unconsumed accept run-2).
     Already-consumed intents dedupe at the consumer's idempotency key; missing ones fill in —
     resume, never double-publish, never mix. `--force` deletes any pending file and starts a
     fresh batch (prior partial versions become history, which the prerequisite fix renders
     correctly). Pending files are per-repo, so a multi-repo run interrupted mid-sequence
     resumes exactly the repos that need it (round-2 agy).
   - **State hygiene (round-2 agy + cold-Opus):** the state file and pending files are written
     atomically (temp + rename), and the CLI holds an exclusive `fcntl` lock on
     `~/.arb-wiki/lock` for the whole run — a concurrent cron + manual invocation blocks
     rather than interleaving nonces.
   - **Ordering:** page intents first, the manifest LAST — the manifest's presence at a given
     `head_sha` is the batch-complete marker.
   - **Transport:** one `ssh arb-prod docker compose exec -T memory python3 -` invocation;
     the remote script is generated host-side with the base64 payload embedded as a string
     literal (a `python3 -` script cannot also read data from the stdin that delivers the
     script — round-1 agy catch), calling `arb_memory.bus.memory_write` against the
     container's `ARB_MEMORY_REDIS_URL`. Note the automation path is plain scripted ssh —
     the operator's interactive mosh/tmux resilience doesn't apply here, which is exactly why
     the protocol above is interruption-proof by construction rather than relying on the
     transport (likelier interruption: the orchestration host sleeping mid-run).
   - Rationale for the hop (unchanged, panel-endorsed): bus membership is the sanctioned
     seat-side write door (single-writer preserved — the consumer still owns embedding +
     insert); the MCP door is not scriptable (interactive OAuth); a direct DB write would
     break the single-embedding-owner property.
5. **Record:** mark the repo's state `complete` (head_sha, generated_at, clear `pending`)
   only after ALL enqueues succeed. **Named residual (cold-Opus): enqueue ≠ persisted** — a
   deadlettered intent (malformed after our validation: near-impossible for valid markdown)
   would leave a page missing with state already advanced; v1 documents this rather than
   building reconciliation (the deadletter table is the existing monitored fail-loud surface,
   and the manifest artefact records the intended page set for manual comparison).

### Cadence

v1 ships manual invocation (`scripts/arb-wiki-refresh [--repo NAME] [--force]`) plus a
documented (not installed) launchd/cron recipe for a daily run. Rationale: generation costs a
real seat turn per changed repo; the operator decides when that becomes automatic. The nightly
vault export is already automatic, so stored refreshes surface within a day regardless.

## Non-goals (v1)

- Page-set auto-discovery / page retirement. **Sharpened per round 1 (agy):** removing a page
  from config stops refreshing it but the artefact — and its vault file, and its graph edges —
  linger indefinitely (artefacts are immutable; the exporter renders every latest version). A
  tombstone mechanism is named future work, not silently absent.
- Parallel multi-repo generation (sequential; page sets are small).
- Per-refresh quality panels (validation is structural; the seat is trusted for prose — same
  trust as the pilot; a bad page is repaired by the next refresh or a manual re-run).
- Deadletter reconciliation (see step 5's named residual).
- Any change to the exporter, grants, or the droplet's cron. (The prerequisite `store.py` fix
  + its constraint migration is the one deliberate exception, scoped and red-first-tested.)
- **Footer redundancy accepted (agy P3):** pages carry a human-readable `See also:` line AND
  the exporter appends its generated `## References` — the See-also line is the in-body
  citation carrier E1 scans; the exporter's footer is the materialized graph. Two views of
  the same edges, by design.

## Testing plan

**Task 0 (`tests/arb_memory/test_store.py` extension):** red-first A→B→A oscillation test —
store content A, then B, then A again; assert the latest version now carries content A (fails
on current code with the stale-B behavior, and would fail with a unique-violation once the
dedup query is fixed but the `(artefact_id, content_hash)` constraint remains — the test
gates both halves of the fix). Plus: re-store-unchanged-from-latest still returns the same
version (no version churn).

**Loop (`tests/test_wiki_refresh.py`):** pure-logic, TDD, no network: config
parsing/validation (missing fields fail loud); change-detection against a fake state file +
injected `git_head` callable (unchanged→skip, changed→run, `--force`, pending-batch→resume
with the SAME nonce); brief rendering; output validation — each rule red-first: missing file,
empty, no `# ` title, `[[` present, See-also missing / not the final non-empty line / with a
typo'd sibling / with a bare unbackticked sibling / with self / with <2 entries, typo'd
inline `wiki-<repo>-*` backticked token, too short/long, plus a fully passing set; intent
construction (one intent per page carrying artefact + index hint together; manifest intent
last in the batch; deterministic ulids stable across a rerun with the same nonce, different
across nonces); state-machine ordering (pending written before store, complete only after
all enqueues, pending preserved on store failure). Dispatch and store are injected callables
(`dispatch_fn`, `store_fn`); the thin CLI wires real ones.

## Self-review notes

- Every reused surface was read this session (`bus.py` write intent + consumer idempotency,
  `mcp/tools.py` store shape, `store.upsert_artefact` content-hash dedup, pilot brief format
  rules, dispatch-dev behavior); the one operational claim not re-verifiable from this repo
  (droplet container has `ARB_MEMORY_REDIS_URL`) was verified live on prod earlier today.
- The riskiest design choice is the ssh-to-droplet store hop; the alternatives (OAuth MCP from
  a script; direct DB write; new writer-token distribution to the mac-mini) are each worse on
  a named axis (scriptability; single-writer violation; credential sprawl). The round-1 panel
  challenged it as asked and endorsed the hop itself (codex: "defensible against the named
  alternatives"; cold-Opus: "justified with correctly-dismissed alternatives") while rejecting
  the original batch mechanics — now rewritten above.

## Round 1 panel record

Independent 3-seat panel, run `panel-wiki-loop-spec-20260706T191620Z-2cdba0`. Stances: codex
`needs-changes`/P2 (partial-publish under mid-batch store failure — traced against
`bus.py`'s per-intent enqueue/ack; strict final-line sibling contract), agy
`needs-changes`/**P1** (the shipped `upsert_artefact` any-historical-version dedup bug, proven
by query trace — promoted to the prerequisite fix section; `python3 -` stdin conflation;
sibling-id typo validation; retirement lingering; footer redundancy), cold-Opus `approve`/P2
(positional hint-linking invariant — a real implementer trap the draft's prose invited;
sibling cross-check; enqueue≠persisted state residual; pages-before-manifest ordering;
rollback behavior — adjudicated in favor of agy's fix over cold-Opus's accept-as-non-goal,
since the bug is latent in every artefact class). Three-seat convergence on sibling-id
validation; two-seat on batch ordering/manifest-last and on the rollback behavior. All
findings addressed inline.

## Round 2 panel record

Same three seats, targeted, run `panel-wiki-loop-spec-r2-20260706T192601Z-25dba0`,
`supersedes` round 1. **All three independently confirmed the prerequisite fix is complete
and correct** — each separately verified (a) the fixed dedup query alone would hit the
`UNIQUE (artefact_id, content_hash)` constraint on the A→B→A re-insert, so the constraint
drop is required, and (b) nothing in `src/` relies on that constraint (agy grepped callers;
codex checked hint dedup is separate; cold-Opus confirmed no `ON CONFLICT` usage). Remaining
convergent P2s, all folded in with round-2 callouts: **resume must enqueue from a persisted
validated batch, never regenerate** (codex's mixed-generation trace, corroborated by
cold-Opus's reuse-vs-regenerate-undefined and agy's per-repo pending) → per-repo
`pending-<repo>.json` carrying the full intent batch; **state-file atomicity + exclusive run
lock** (agy + cold-Opus); **citation validation widened** to all configured repos' page ids,
bare `wiki-*` tokens fail, See-also minimum becomes `min(2, siblings)` (agy + cold-Opus);
`art-<hex>` citations named as an unvalidated residual with rationale. No P0/P1 in either
round; design is ready for the implementation plan.
