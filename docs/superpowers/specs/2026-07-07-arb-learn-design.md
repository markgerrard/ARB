# ARB `/learn` — technique-intake pipeline: design (v3)

**Status:** design v3 after two panel rounds (R1: 4×needs-changes — identity model, query
paths, stance conflict, verdict rule; R2: 4×needs-changes — the v2 id scheme re-opened the
resurrection bug, query helpers were unbuildable as gestured, verdict plumbing under-specified).
Author: Fable (warm, inline). **Brief:** `art-764315ac2c7ca83f`. The reframe is unchallenged
across both rounds: content → reject-biased proposal; rejections are the point; auto-evaluate,
gated-build; promote emits an inert brief file only.

## Identity + the terminal guarantee (R2 cold-Opus P0-1, GLM P1-1 — the load-bearing fix)

Ids are `learn-<slug>-<hash8>` (hash of source content). The id scheme is for ADDRESSING, not
for the guarantee — deterministic hashing means an identical re-propose computes the SAME id,
so the terminal guarantee is enforced as an explicit gate in `propose`:

1. `propose` computes the id, then calls `get_status(id)` FIRST. If the id exists in a
   terminal state (`rejected`, `eval-approved`, `promoted`), it **refuses and prints the
   prior verdict + reasons** — the store is never written.
2. `--force` never rewrites an existing id: it mints `learn-<slug>-<hash8>-r<N>` (next free
   ordinal) with `metadata.supersedes=<prior-id>` — the prior's terminal record stays intact
   and linked (R2 fork: force-names-prior, accepted).
3. Statuses only move forward per id: `proposed → rejected | eval-approved | needs-mark |
   eval-error`; `eval-error` is the only re-evaluable state; `promoted` is written by
   `promote`. A pinning test proves a rejected id cannot be resurrected via `propose`,
   `evaluate`, or a raw re-write through the CLI.

## Query primitives — specified as new SQL (R2 cold-Opus P0-2)

`learn_intake.py` ships two read helpers executed over the wiki pipeline's established
ssh→memory-container mechanism, carrying their own SQL (no existing read API covers this;
`arb_memory` core is untouched):

- `list_proposals()`: `SELECT DISTINCT ON (artefact_id) artefact_id, version, content,
  created_at FROM artefacts WHERE artefact_id LIKE 'learn-%' ORDER BY artefact_id, version
  DESC` — parsed into (id, status, slug, summary) rows.
- `get_status(id)`: same shape filtered to one id; returns None if absent.

Dedupe in `propose` = similarity against `list_proposals()` locally + `memory_search` broad
prior-art check. Proposal index hints use **`metadata.kind=artefact_index`** (inheriting the
2026-07-07 stale-hint retirement fix) plus `metadata.learn_proposal=true` as the marker —
NOT a new kind (R2 codex P1: a new kind would reintroduce the stale-version search bug).

## Eval verdict plumbing (R2 GLM P1-3/P1-4/P2-1/P2-2, cold-Opus P1)

- Per-seat DOMAIN verdict (`REJECT`/`WORTH-BUILDING`/`NEEDS-MARK`) parsed by `arb-learn` from
  reply prose. The audit-plane ```vote``` fence maps `REJECT→block`, `WORTH-BUILDING→approve`,
  `NEEDS-MARK→abstain`, **with the mandatory `severity` field** (seat's top finding severity,
  default P2). `arb-learn` cross-checks prose verdict against fence stance via the mapping;
  disagreement ⇒ that seat is `eval-error` (fail-closed toward not-counting it).
- An unparseable reply is `eval-error` for that seat — **never a substantive REJECT** (a
  technical failure must not suppress an idea) and never counts toward approval.
- Precedence: any substantive `REJECT` ⇒ `rejected` (beats errors). Else any seat error ⇒
  `eval-error` (retry via `evaluate --retry`). Else any `NEEDS-MARK` ⇒ `needs-mark`. Else
  `eval-approved` requires ALL responding seats `WORTH-BUILDING` with **≥2 clean seats
  minimum** — fail-closed toward not-approving, retryable toward not-rejecting.
- `evaluate` does NOT route through `panel_run.finalize`/`reconcile` in v1 — the audit votes
  are trail, `arb-learn`'s own parse is the decider; stated explicitly so the anti-laundering
  question has a definite answer. Roster strings are real `seat:<agent-id>` values.
- Panel = codex + agy-print + pi-GLM (trio; no cold-Opus — the gate is the cheap pass;
  cold-Opus stays on design/build panels where its depth pays. R2 GLM's composition concern
  noted; the eval task is proposal-text + wiki-page grounded, within the trio's reach).

## Gate-drift tripwire (R2 cold-Opus P1, fork ruling)

Denominator = last 10 **substantive** evals (errors excluded). Below 5 samples the tripwire
is display-only (no refusal — the first approval cannot self-refuse). At ≥5 samples: warn at
≥40% approval rate, `promote` refuses at ≥60% without `--override`.

## Unchanged from v2 (accepted by R2)

Single-REJECT veto; retryable errors; `--from-workflow` requires a verifiable referent
(handoff path + commit SHA, checked); promote refusals incl. `--target project --i-am-mark`
friction with the inert-brief file as the real guard; eval briefs embed wiki-page citations
for stack grounding (R2 fork: accepted); no URL fetching / auto-evals / auto-build / web UI.

## Verification (unchanged shape + new pins)

Unit: terminal-guard resurrection test (the R2 P0 pin), ordinal minting + supersedes links,
verdict precedence table (every branch), mapping cross-check, drift math incl. low-N floor,
promote refusals. `main()`-level: reject and approve round trips, faked dispatch. Live proof:
one real external proposal (expected REJECT), one `--from-workflow` promotion with Mark's nod.
