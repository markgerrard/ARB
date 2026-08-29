# Design spec — wiring `arb-audit-emit` into real ARB panel dispatch

**Date:** 2026-06-23 · **Status:** design v2 (pre-plan) · **Branch target:** `dev` (ARB Memory)
**Related memory:** [[arb-audit-emit-unwired]], [[arb-eval-trace-capture]], [[warm-seat-synthesis-laundering]],
[[vacuously-green-guard-fail-loud]], [[graduation-criterion-measures-what-it-claims]],
[[bridge-seat-role-bound-at-launch]], [[arb-threat-model-recalibration]], [[cross-mode-decorrelation-empirical-check]]
**Decided in design panel (3-seat: codex contributor + 2 cold-Opus certifying), 2026-06-23.**

**v2 (folds 3-seat design-REVIEW panel, unanimous NEEDS-CHANGES, 2026-06-23):** P0 — bridge
`--expect-structured` cannot carry the stance block (fixed `status` schema + field whitelist +
the ```` ```vote ```` fence misses the bridge extraction regex; verified from source): votes are now
parsed from the **raw reply text** by one shared `stance.py` parser for both transports (drops the
bridge-structured dependence). P1 — manifest is now **seq-1 + precedes-all-votes** enforced (closes
late/trimmed-manifest laundering). P1 — verdict prose-bypass named as the residual with the
done-criterion SQL as the compensating control. P2 — bounded-poll bounded + `incomplete→refuse`;
`timed-out` added to the stance enum; `roles/reviewer.md` vocabulary reconciled.

## Problem

ARB Memory's audit path (`audit` stream → `AuditConsumer` → Postgres `audit_events`, `kind` first-class)
is built, reviewed, e2e-canaried, and merged to `dev` — but **dormant**: nothing in the real
orchestration flow emits audit events. We want **real review/design panels** (the kind the orchestrator
runs by dispatching seats and synthesizing a verdict) to record an audit trail — who was in the roster,
how each seat voted, and the final verdict — correlation-keyed by one `run_id` so the audit ("what was
decided") joins the eval trace ("how each seat behaved getting there").

The audit log's *purpose* is to catch **doneness-laundering**: a lone-seat severity laundered into false
"consensus", or a silently-absent certifying seat. So the wiring is not just "call the CLI" — the emit
path must make those failure modes **fail loud**, structurally.

## Constraints that shape the design (established facts, not choices)

1. **No automated panel-runner exists.** A panel is the warm orchestrator (a Claude Code session)
   dispatching seats and synthesizing by judgment — heterogeneous (mixed engines, worktrees, hygiene
   phases, mid-panel steering). We are not building a runner that owns that flow.
2. **Bridge seats are credential-wiped by design** (the decorrelation guarantee). A dispatched seat has
   no `ARB_MEMORY_REDIS_URL` and **cannot emit its own vote**. The working model is
   **orchestrator-writes-all-three** (`source=orchestrator`, `actor=seat:x`). The orchestrator host has
   the audit-bus env (DO Valkey **db3**).
3. **Panels mix two seat transports.** Bridge seats go through `scripts/agent-dispatch` (support
   `--expect-structured`). In-session cold-Opus seats are subagents that return a freeform final message
   (no `agent-dispatch`, no `--expect-structured`). The design must record laundering-proof votes for
   **both**.
4. **The three kinds sit at different points on the mechanical↔judgment axis** — `dispatch` mechanical,
   `vote` should be machine-derived (not paraphrased), `verdict` is synthesis and the locus of laundering.

## Decisions (from the design panel + brainstorm)

- **Approach: B + structured-vote + verifier** (panel-selected over C-narrow and A+B-minimal).
  Bake the mechanical emit into `agent-dispatch` behind an explicit gate; derive votes from a structured
  stance block (not orchestrator paraphrase); gate the verdict behind a fail-loud verifier.
- **Uniform structured-stance contract for ALL seats** (bridge + in-session). Every seat ends its reply
  with a machine-parseable vote block; the orchestrator extracts it **verbatim** and never authors a vote.
- **One spec, staged plan.** Ships as one coherent unit (the done-criterion needs all parts); the
  implementation plan stages it into checkpoints. No "wired-but-unverifiable" half-state.

## Architecture — three audit kinds, one `run_id`

### `run_id` — the correlation spine
- **Minted once per panel, before the first dispatch, by the warm orchestrator.** Never re-minted per
  seat. New panel → new id; a re-panel mints a fresh id and references the prior via
  `supersedes:<prior-run-id>` in its verdict payload (never reuses — reuse collides the per-run seq INCR
  and dead-letters on `content_hash`).
- **Format:** `panel-<topic-slug>-<YYYYMMDDTHHMMSSZ>-<6hex>` — human-greppable + collision-safe on the
  shared multi-session bus.
- Threaded **identically** into every `agent-dispatch --run-id $RID` (envelope `run_id` → bridge eval
  tee) and every `arb-audit-emit --run-id $RID`. This single value is the audit↔eval join key.

### `dispatch` — the roster manifest (one per panel, orchestrator-emitted at t0)
`arb-audit-emit --run-id R --kind dispatch --payload '{"roster":["seat:codex-bridge-dev","seat:cold-opus-a",...],"task":"...","branch":"..."}'`
- Emitted **before fan-out**, declaring the **full intended roster** (bridge AND in-session seats).
- This is the **independent ground truth** the verifier reconciles against. **(v2/P1 — what makes "t0
  ground truth" actually true, not just claimed):** the manifest MUST be **seq 1** for the run, and the
  verifier asserts `manifest.seq < every vote.seq` (the manifest precedes every vote). Without this, an
  orchestrator could collect votes first and then emit a *trimmed* manifest matching only the seats that
  voted — the verifier would reconcile against that late manifest and pass, which is precisely the
  silent absent-seat laundering this design exists to catch. Seq-1 + precedence makes the roster
  un-trimmable after the fact. As a best-effort early tripwire, `agent-dispatch --audit-panel` warns
  loudly if no committed manifest for the run already contains its actor (the seq-precedence assertion at
  verdict time is the hard gate).
- Manifest roster entries MUST equal the `seat:<target-id>` string `agent-dispatch` emits (`seat:$TO`);
  short names like `seat:codex` will refuse.

### `vote` — one per seat, machine-derived from the stance block in the **raw reply text**
**The stance block** (uniform contract): every seat ends its reply with
````
```vote
{"stance":"approve|needs-changes|block|abstain|timed-out","severity":"none|P2|P1|P0","refs":["file:line",...],"note":"<=200 chars"}
```
````
The vote is parsed from the **raw reply text by one shared parser** (`src/arb_memory/stance.py`) for
**both** transports — NOT from the bridge's `--expect-structured` field. (v2/P0: the bridge's structured
contract is a fixed `status ∈ {DONE,DONE_WITH_CONCERNS,BLOCKED,NEEDS_CONTEXT}` schema that whitelists its
own fields and whose extraction regex `` ```(?:json)?\s*(\{…\})``` `` does not even match a `` ```vote ``
fence — so `payload.structured` is `null` for a stance block. Parsing the fence out of the raw reply text
sidesteps this entirely and unifies the two transports under one code path.)
- **Bridge seats:** `agent-dispatch --audit-panel` (gated, see below) parses the fence out of
  `reply.payload.result` with `stance.py` and emits the `vote` — automatic, un-skippable, never
  paraphrased. No `--expect-structured` dependence.
- **In-session seats:** the orchestrator feeds the subagent's final message to `arb-panel-vote`, which uses
  the *same* `stance.py` and emits the `vote`. Identical block, identical parser.
- **`stance.py` accepts** a fenced block tagged `vote` or `json`, or a bare trailing `{…}` object; it
  validates the enum/shape and **raises** on malformed/absent/wrong-enum.
- **Malformed / absent block → fail loud.** The parser raises; the orchestrator/`agent-dispatch` must NOT
  invent a vote. An unresolved seat has no terminal `vote` row → the verifier blocks the verdict.
- **Timeout / no-reply → an explicit `vote` with `stance:"timed-out"`** (a first-class enum member, so
  validators accept it) — emitted by `agent-dispatch` on exit 124 for bridge seats; by the orchestrator
  for in-session. Absence is the laundering vector; timeout-as-recorded-stance closes it.

### `verdict` — orchestrator synthesis, verifier-gated (one per panel)
`arb-audit-emit --run-id R --kind verdict --payload '{"decision":"...","roster":["seat:codex-bridge-dev",...],"stances":{"seat:codex-bridge-dev":"...",...},"rationale":"...","refs":{...}}'`
- The verdict-emit path is **structurally incapable of recording a verdict over an incomplete/inconsistent
  roster.** Before any `XADD`, it runs the **verifier** (below). On mismatch it exits nonzero with the
  specific gap and **emits nothing**.
- Payload = stances + short rationale + pointers (eval `run_id`, commit SHAs), **not** full reply text
  (16KB `audit_emit` cap; bulky text lives in the eval trace, joined by `run_id`).
- **(v2/P1 — named residual hole):** the verifier only constrains verdicts that go *through* the CLI. An
  orchestrator can announce a verdict in prose and never call `arb-audit-emit --kind verdict`. This is
  **not** closed structurally and is **acceptable under the mistakes-not-malice threat model**
  ([[arb-threat-model-recalibration]]): a skipped verdict-emit leaves **zero** audit rows for the run —
  detectable *absence*, not a laundered false-green. The **compensating control** is the done-criterion
  SQL (below), which the orchestrator MUST run as a real post-hoc gate — citing the `run_id` and the row
  proof — *before* announcing any verdict in prose. "Verdict announced without a passing audit-close" is
  treated as un-audited, the same status as "no manifest".

## The verifier (`src/arb_memory/panel_audit.py`, invoked inside `arb-audit-emit --kind verdict`)

`reconcile(conn, run_id, verdict_payload) -> Ok | Gaps`. Reads back the run's committed rows from
**Postgres `audit_events`** (durable truth) and asserts:
1. Exactly one `dispatch` (manifest) row exists for `run_id`, and it is **seq 1**. (Missing → "no
   manifest; run un-auditable".)
2. **`manifest.seq < every vote.seq`** — the manifest precedes all votes (v2/P1; defeats a late/trimmed
   manifest). (Violation → "manifest not committed before votes; roster not trustworthy".)
3. Every seat in `manifest.roster` has exactly one terminal `vote` row (a real stance OR `timed-out`).
   (Missing → "seat X declared but never voted".)
4. No `vote` row from an actor **not** in `manifest.roster`. (Extra → "unrostered vote from X".)
5. `verdict.stances` keys == the set of vote actors == `manifest.roster`, and each `verdict.stances[x]`
   matches that seat's `vote.stance`. (Mismatch → "verdict claims X=approve but X voted block".)

**Bounded-poll (v2/P2 — concrete):** before asserting, `reconcile` drains consumer lag by polling
`audit_events` for `run_id` until the row count is **stable across two consecutive reads** AND
`audit_lag`/`drain_pending` (audit.py) reports the audit group caught up — capped at **30s wall-clock,
250ms interval**. If the cap is hit without stability → return **`incomplete` ("audit-consumer-incomplete",
include stream lag/pending)**, which the verdict-emit path treats **exactly like a gap: nonzero exit, no
XADD**. `incomplete` is a refuse state and MUST NOT degrade to a pass — it means "can't prove the roster",
which is not "the roster is fine".

Any failure → nonzero exit, no emit, specific message. This is the
[[vacuously-green-guard-fail-loud]] discipline at the audit boundary: the only path that can record a
final verdict refuses to do so over a roster that doesn't reconcile against independently-committed
ground truth.

## `agent-dispatch` changes (the "B" part)

New flag `--audit-panel`. When set **and** `--run-id` present:
- After the seat's reply lands: parse the stance fence out of **`reply.payload.result` (the raw reply
  text)** with `src/arb_memory/stance.py` (v2/P0 — NOT the bridge's `payload.structured` field, which
  can't carry a stance block) and emit `vote --actor seat:<derived-from-target-id>`. On exit 124: emit
  `vote stance:timed-out`. This is **net-new emit logic inside the dispatch loop** (today the loop only
  `jq '.payload'`-prints and exits on `.payload.ok`; the exit-124 path just prints) — a new stage with its
  own fail-soft wrapper, not a one-line tweak.
- **Fail-soft-but-loud:** the emit is wrapped (`set +e` around it; `agent-dispatch` runs `set -euo
  pipefail`, so an unwrapped emit failure would break the dispatch). A failed emit prints a visible
  stderr warning and proceeds — the dispatch is primary, audit is observation. The *missing row* is
  itself loud: the verdict verifier sees a roster seat with no vote and blocks. (Never silent — a
  fail-soft-**silent** spine would make the verifier vacuously green.)
- If `--audit-panel` is set but `ARB_MEMORY_REDIS_URL` is unreachable: **loud warning** "panel audit
  requested but bus unreachable; run un-auditable" — don't silently no-op (the operator explicitly asked
  for audit via the flag).
- **Without `--audit-panel`: byte-identical to today** (no emit). The gate is explicit; `--run-id` alone
  must NOT trigger audit emit (eval-trace wants `run_id` on every dispatch — gating on it would flood
  `audit_events` with non-panel impl dispatches). This is the #1 over-capture trap; the explicit flag is
  the fix all three seats demanded.

## Components / files

| File | Change |
|---|---|
| `src/arb_memory/stance.py` (new) | `parse_stance(text) -> dict \| raise` — extract & validate the stance block from raw reply text; accepts a fence tagged `vote` or `json`, or a bare trailing `{…}`; raises on malformed/absent/wrong-enum. The uniform contract's single parser, shared by both transports. |
| `src/arb_memory/panel_audit.py` (new) | `reconcile(conn, run_id, verdict_payload)` — the verifier; bounded-poll read-back (drain-barrier, 30s/250ms cap, `incomplete→refuse`) + the 5 assertions incl. manifest seq-1 + precedence. |
| `scripts/arb-audit-emit` | `--kind verdict` runs `reconcile` first; refuse + nonzero on gap **or `incomplete`**, no XADD. `--kind dispatch` remains a plain manifest XADD. |
| `scripts/arb-panel-vote` | The only vote-writing CLI; parses raw reply text through `stance.py` (or emits `timed-out`) and refuses malformed/absent stance blocks. |
| `scripts/agent-dispatch` | `--audit-panel` flag; gated, fail-soft-but-loud `vote` emit parsed from `payload.result` via `stance.py` + timeout-vote (exit 124); byte-identical when off. No `--expect-structured` dependence. |
| `skills/using-agent-bridge/SKILL.md` | Document the panel-emit discipline: mint run_id at t0, emit the manifest **first (seq 1)**, dispatch bridge seats `--audit-panel`, enforce the stance fence for in-session seats, and **run the done-criterion audit-close (verdict-emit self-verifies) and cite the row proof before announcing any verdict in prose**. |
| `roles/reviewer.md` | **Reconcile the existing verdict vocabulary** (today `SHIP/SHIP_WITH_NITS/FIX_BEFORE_MERGE/BLOCK_MERGE`) with the stance enum: either map it onto `approve/needs-changes/block/abstain` or supersede it, and require the trailing stance fence — so a seat emits ONE consistent label, not a prose label contradicting/duplicating the fence. (v2/P2 — three-vocabulary collision.) |

## Error handling (summary)

- **Audit bus down:** dispatch/`arb-panel-vote` emit fail-soft-but-loud (warn, dispatch proceeds); verdict emit
  fail-loud (can't verify → refuse). Asymmetry is deliberate: soften the observation, harden the
  integrity record.
- **Malformed/absent stance block:** parser raises; no invented vote; verifier blocks verdict.
- **Partial panel (timeout):** explicit `timed-out` vote; roster satisfied; verdict may proceed and
  records the timeout faithfully.
- **run_id collision:** format makes it negligible; all reads scoped by exact `run_id`, never prefix.
- **Verdict payload > 16KB:** `audit_emit` raises (fail-loud) — keep payload to stances + pointers.

## Testing

- **Unit:** `parse_stance` (valid / malformed / absent / wrong-enum → raise); `run_id` format; gate-off →
  envelope byte-identical (golden test).
- **Verifier (deny-proofs, inject-revert each):** clean panel → pass; delete one vote row → verdict
  refuses; inject unrostered vote → refuses; flip a `verdict.stances` value → refuses; `timed-out` vote →
  satisfies roster. Acceptance: delete the guard → these red, both directions.
- **`agent-dispatch` baked emit:** audit bus down → dispatch still exits 0 with loud warning (fail-soft);
  `--audit-panel` off → unchanged behaviour.
- **e2e (mirror `scripts/arb-memory-eval-e2e`: run-tag isolation, drain barrier):** a real/simulated panel
  → manifest + N votes + verdict; verifier gates; Postgres reconciliation both directions; audit↔eval
  `run_id` join holds.

## Done-criterion (proves "wired", not "can emit" — [[graduation-criterion-measures-what-it-claims]])

Run a **real panel** (e.g. the next review the orchestrator runs), then an executable check over the
*disposed* run:
```sql
SELECT kind, count(*), array_agg(DISTINCT payload->>'actor')
FROM audit_events WHERE run_id = :rid GROUP BY kind;
```
Passes iff: exactly 1 `dispatch` (manifest listing N seats); N `vote` rows (actors == manifest roster,
each a real or `timed-out` stance); exactly 1 `verdict` (its `roster`==manifest roster, `stances`
keys==vote actors); and eval rows exist for every dispatched `task_id` under the same `run_id`. The
verdict-emit succeeding *is* most of the proof (it ran the verifier). "The CLI can emit" is explicitly NOT
done.

## Out of scope (this slice)

- `arb-panel` runner / C-narrow chokepoint (kept as the upgrade path if the flag-gated approach proves
  leaky in practice).
- Seats emitting their own votes (blocked by cred-wipe; productization-era — [[arb-threat-model-recalibration]]).
- Consumer-side / post-hoc roster reconstruction from bridge `task:<id>:events` (a v-next that would make
  the roster ground-truth independent of emitter behaviour; noted, not built).
- Minter-role auth on `run_id` (decided out: mistake-prevention scope, not adversarial).
