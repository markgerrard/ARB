# AUDIT-CLOSE-1 — orchestrator verdict-close path to the prod audit trail

**Status:** DESIGN DRAFT — reviewed by codex-sol@high (needs-changes/P1), revised §7 below. Not build-ready until §7 folds are settled + a re-review.
**Author:** warm Opus (orchestrator).
**Date:** 2026-07-12.
**Workflow:** A (lightweight single-reviewer) — chosen by Mark; the surface reuses
already-tri-reviewed `reconcile`/`AuditRun` code and is mostly an entrypoint + deploy question.

---

## 1. Problem statement

As of 2026-07-12 the **prod audit trail is LIVE** (this is new — prior memory
[[arb-audit-emit-unwired]] said dormant). Verified on the `arb-prod` droplet:

- `deploy-audit-1` container up ~4 days, consuming the shared DO Valkey `arbmem:audit`
  stream on **db/5** → prod Postgres `arbmemory.audit_events`. Group `arbmem-audit`,
  2 consumers, lag 0, deadletter 0.
- **First real events landed today** (~10:34–10:57Z): `panel-arb-learn-config` runs
  r1/r2/r3, each `dispatch` + 4 seat `vote`s (codex-luna / asdk-opus48 / grok45 / agy),
  emitted by the codex orchestration session on the MBP.
- 16 stream entries → 15 rows: idempotent redelivery dedupe via
  `ON CONFLICT (run_id, seq) DO NOTHING`, by design.

So **emit** works end-to-end against prod. What has no clean path is the **verdict-close**:
the final `verdict` event that `panel_audit.reconcile` gates — it emits the verdict *only
if* the committed vote roster reconciles against the verdict payload (the anti-laundering
refusal, [[warm-seat-synthesis-laundering]]).

### Why the close is blocked today

Two independent facts, both verified against current code:

1. **Reconcile needs `SELECT audit_events`; the laptop can't.** `reconcile(conn, run_id, …)`
   (`src/arb_memory/panel_audit.py:81`) counts and loads `audit_events` rows. The only prod
   role this orchestrator host holds is the read-only local reader, and
   `tests/arb_memory/test_local_reader_grants.py:24` lists `audit_events` in
   `SENSITIVE_TABLES` — the reader role is **deny-proven** against it, by design (seats and
   orchestrators must not browse the audit trail). So a prod close cannot run from the MBP,
   even read-only.

2. **The image ships `src/` but not `scripts/`.** `deploy/Dockerfile:13` is `COPY src ./src`
   — there is no `COPY scripts`. So `scripts/arb-audit-emit` (the current documented close CLI)
   **is not in the container**. The close code it depends on — `arb_memory.audit.AuditRun`,
   `arb_memory.panel_audit.reconcile` — *is* in the image (it's under `src/`).

The one place that *can* close correctly is **inside `deploy-audit-1`**: it already connects
with the owner/writer `ARB_MEMORY_DSN` (can `SELECT` + `INSERT` `audit_events`) and already
has the reconcile code. It just has no entrypoint to invoke it: `python -m arb_memory` only
exposes long-running services (`audit`, `memory`, …), no one-shot close verb
(`src/arb_memory/run.py:351`).

### Scope

In scope: a supported, repeatable way for the orchestrator to close a panel run's verdict
into the **prod** audit trail, with the reconcile refusal intact, without handing prod writer
creds to the laptop. Out of scope: changing the emit path (works), the dev substrate
(untouched), or the reconcile logic itself (already tri-reviewed).

---

## 2. Options considered

### Option A — ssh + `docker compose exec` inline python (wrapper script)

`scripts/arb-audit-close` on the orchestrator host runs, effectively:

```
ssh arb-prod 'cd ~/AgentRedisBridge/deploy && docker compose exec -T audit \
  python -c "<inline reconcile+emit>"' --run-id … --payload …
```

- **Pro:** no image rebuild/redeploy; ships today; DSN never leaves the droplet.
- **Con:** inline-python-over-ssh is exactly the untested-CLI-glue class that
  [[live-verification-catches-cli-glue]] warns about — quoting, exit-code propagation, and
  the reconcile-refusal path all live in a shell heredoc that no unit test covers. Payload
  with embedded quotes/newlines is a footgun. The refusal exit code (4) must survive two
  hops (container → ssh → local) or a failed close reads as success.

### Option B — first-class `audit-close` subcommand in the image (RECOMMENDED)

Add a one-shot verb to `arb_memory` so the close is real code in the image, unit-tested,
invoked as:

```
ssh arb-prod 'cd ~/AgentRedisBridge/deploy && docker compose exec -T audit \
  python -m arb_memory audit-close --run-id <id> --payload <json>'
```

- Implementation: `run.py` currently parses a single positional `service` from a fixed
  `choices` list (`run.py:351`). Adding a verb that takes its *own* `--run-id`/`--payload`
  needs argparse **subparsers** (or a `parse_known_args` remainder re-parse) — a real but
  small refactor. `run_audit_close()` calls the existing `reconcile` + `AuditRun.emit`; the
  refusal is a nonzero exit that `docker compose exec` propagates.
- **Pro:** the close logic is in `src/` → in the image → unit-testable with the existing
  `conn_factory`/`redis_factory` seams that `scripts/arb-audit-emit` already uses; no inline
  shell; exit-code contract locked by a committed test the way `test_panel_run_cli.py` locks
  the reconcile-refusal (exit 4). ssh is then a dumb transport, not logic.
- **Con:** requires an image rebuild + `up -d --force-recreate` on the droplet. Per
  `deploy/README.md:49`, only the `memory` service carries the `build:` block, so the rebuild
  is `docker compose build memory` (NOT `build audit` — silent no-op) then force-recreate.
  One-time deploy cost.

### Option C — a thin close *service* / HTTP endpoint on the droplet

Expose close as an authenticated endpoint (like the MCP publish-proxy) so the orchestrator
calls it without ssh.

- **Pro:** no ssh; symmetric with how prod *writes* already go through the publish-proxy.
- **Con:** materially more surface — an auth story, a new listener, a new deploy unit — for a
  low-frequency operation (one close per panel). Over-built for the need. Defer unless close
  frequency or non-droplet-reachable orchestrators make ssh untenable.

---

## 3. Recommendation

**Option B.** It converts the highest-risk part of the close — the reconcile-refusal
exit-code contract — from untested shell glue into committed, unit-tested image code, at the
cost of one image rebuild. ssh degrades to transport. It reuses the exact
`reconcile`-before-`emit` ordering and the `conn_factory`/`redis_factory` test seams already
proven in `scripts/arb-audit-emit` and `test_panel_run_cli.py`, so the net new surface is an
argparse subparser + a `run_audit_close()` wrapper + a CLI exit-code test.

Rejecting A because the anti-laundering refusal is the single most important property of the
close, and A puts it in an untested heredoc across two transport hops. Rejecting C as
premature infrastructure.

---

## 4. Build sketch (Option B)

1. `run.py`: migrate the `service` positional to argparse **subparsers** (preserving every
   existing service verb + its no-arg invocation, so `python -m arb_memory audit` etc. are
   byte-identical). Add `audit-close` with `--run-id` (required), `--payload` (JSON, required),
   `--source` (default `orchestrator`), `--actor`.
2. `run_audit_close()`: build writer conn from `ARB_MEMORY_DSN` + redis from
   `ARB_MEMORY_REDIS_URL`; call `reconcile(conn, run_id, payload, redis=r)`; on `ok=False`
   print gaps to stderr and return **4** (match `arb-audit-emit`'s refusal code); on ok emit
   the `verdict` via `AuditRun(...).emit(source, "verdict", payload)` and return 0.
   Honour `ARB_MEMORY_PREFIX` exactly as `arb-audit-emit` does.
3. Tests: `test_run_audit_close.py` — happy path emits; not-reconciled → exit 4, no emit;
   malformed `--payload` → exit 2. Reuse fake redis/conn factories. This is the load-bearing
   coverage.
4. Docs: replace the "close = `scripts/arb-audit-emit` on the droplet" guidance in
   `deploy/README.md` with the `docker compose exec audit python -m arb_memory audit-close`
   recipe, and note the `build memory` (not `build audit`) rebuild gotcha.
5. Keep `scripts/arb-audit-emit` as-is for the dev substrate; optionally have it shell to the
   new subcommand later. Not in this slice.

---

## 5. Live gate (REQUIRED before claiming done)

Per [[live-verification-catches-cli-glue]] and the project's merge-gate rule
([[e2e-mutation-tier-run-policy]]), static review + unit tests do not close this. The gate is:

- Rebuild + redeploy the audit image on `arb-prod` (`build memory` → `up -d --force-recreate`;
  confirm `deploy-audit-1` returns healthy and lag returns to 0).
- **Close the three real open runs** that emitted votes today but have no verdict:
  `panel-arb-learn-config` r1/r2/r3. For each, run `audit-close` with the true roster/stances
  and confirm a `verdict` row appears in prod `audit_events` for that `run_id`.
- **Deny-proof the refusal live:** attempt a close with a deliberately wrong roster (drop one
  seat) and confirm exit 4 + NO verdict row written — the refusal must fire against prod, not
  just in unit tests ([[deny-proofs-need-adversarial-verification]]).

Only when the three verdicts are present and the wrong-roster close is proven-refused is
AUDIT-CLOSE-1 done.

---

## 6. Open questions for the reviewer

1. Is the subparser migration of `run.py:main` low-risk enough to do in this slice, or should
   the existing service verbs be frozen and `audit-close` bolted on via `parse_known_args`?
2. Should the three already-open learn-config runs be closed as part of this gate, or left
   open as historical (votes-without-verdict) and the gate use a fresh throwaway run instead?
3. Does the prod close need `--run-id` allow-listing / any minter-role auth, or is
   droplet-shell access (already privileged) a sufficient authorization boundary?

---

## 7. Review outcome (codex-sol @ high, 2026-07-12) + required revisions

Run `panel-audit-close-1-design-20260712T113944Z-c51c00`. Verdict **needs-changes / P1**.
sol independently verified the §1 prod premises read-only (deploy-audit-1 up; the three runs each
carry 1 dispatch + 4 votes + no verdict; `scripts/arb-audit-emit` absent from the image). Option B
affirmed as the right architecture; four P1s must be resolved before build. All four hinge-claims were
reality-checked against the code by the orchestrator and CONFIRMED.

- **P1-a — exit code 4 is not actually guaranteed.** `run.py:main()` invokes each service handler and
  then `return 0` **unconditionally** (`run.py:372-396`); handlers return `None`. A close handler that
  follows the same pattern would have its refusal (4) discarded, so `docker compose exec` reports success
  on a *refused* close — the exact laundering this slice prevents. **Fix:** the subparser dispatch must
  `return` the handler's exit code so it reaches `SystemExit`; the test asserts
  `main(["audit-close", …]) == 4`, not the helper in isolation.

- **P1-b — re-closing a run emits a SECOND verdict (idempotency / back-door laundering).** `reconcile()`
  does not check for an existing verdict; `AuditRun.emit`→`next_seq` always `INCR`s; the schema is
  `UNIQUE (run_id, seq)` only. So a retry after an ssh drop — or a *deliberate* second close with a
  DIFFERENT stance map — reconciles and emits again. This defeats the one-verdict invariant post-hoc.
  **This is the most important finding.** **Fix (open mechanism decision):** either (i) a Redis
  `SETNX` per-run close-claim taken *before* emit so a second close refuses at the CLI, or (ii) a
  partial unique index `(run_id) WHERE kind='verdict'` so the *consumer* rejects the second verdict to
  deadletter — but (ii) leaves the CLI returning 0 while the consumer drops it, so (i) is preferred for a
  truthful exit code. Same-payload re-close → 0 no-emit; different-payload → nonzero; concurrent → exactly
  one. Needs concurrent + retry tests + a live double-close gate.

- **P1-c — Option B still ships raw JSON through two shells.** My "ssh becomes dumb transport" claim was
  wrong: `ssh … docker compose exec … --payload <json>` is interpreted by BOTH the local and the remote
  shell, so an apostrophe or newline in `rationale`/`note` breaks the command or mutates the payload
  before argparse. **Fix:** read the payload from **stdin** (`--payload-file -`) and pipe exact bytes
  through `ssh … docker compose exec -T …`; keep malformed-JSON → exit 2 (not a traceback). Transport
  test must include a double-quote, an apostrophe, and a newline.

- **P1-d — historical runs are the wrong correctness gate.** reconcile proves roster + stances but NOT
  `decision`/`rationale`/refs; the repo has no durable artifact tying the three `panel-arb-learn-config`
  run-ids to their original synthesized verdict. Closing them means **inventing** those fields into an
  **irreversible** prod write. **Fix:** use a **fresh, explicitly disposable** run for the live gate
  (refusal + successful close + retry-idempotency + one-verdict). Backfill the three historical runs only
  as a separate operator decision IF authoritative payloads are recoverable; label them backfilled and
  keep original decision-time distinct from emission-time. → supersedes §5's gate and §6-Q2.

### P0 discovered during review verification — the emit bus is a two-bus split

Not in sol's report; found while confirming the vote persisted. **This design's premise "emit-to-prod
works, only close is gapped" is false for the orchestration clone.** There are two audit buses:

- **db/5** → `deploy-audit-1` → prod `arbmemory.audit_events`. Fed by **fleet-clone** seats only. Live.
- **db/3** → what THIS orchestration clone emits to (`envs/agent-redis-bridge-dev.env`). Its `audit`
  consumer has been **idle ~11h (dead)**. Panels audited from the orchestration session **strand on db/3**
  and never reach any Postgres. (`panel-astrev-r5`'s verdict and this review's dispatch+vote are sitting
  there now.)

**Consequence:** AUDIT-CLOSE-1 is necessary but not sufficient. For orchestration-session panels to land
in the **prod** trail, a prerequisite decision is needed: **(P) point this clone's
`ARB_MEMORY_REDIS_URL` at db/5**, OR stand up a live consumer on db/3 and treat db/3 as the durable dev
trail. This is a routing/intent fork (dev panels may be deliberately kept off the prod trail) — Mark's
call, and it gates whether the close path even targets prod. Until (P) is settled, building the close verb
is fine but it can only be exercised against whichever bus has a live consumer.

### Net

Option B stands. Both blockers now resolved (see §8). Build authorized by Mark 2026-07-12 with
codex-luna @ high; the concurrency mechanism (P1-b) gets a hard adversarial pass in the post-build
review (per review-depth doctrine).

---

## 8. Build-ready resolution (Mark authorized build 2026-07-12, codex-luna@high)

Decisions pinned — this section is the build spec; the brief points luna here.

- **(P) bus routing — DONE.** Orchestration clone repointed to db/5 (prod audit bus); verified probe →
  prod `arbmemory`. Not part of this build; the close verb targets whatever `ARB_MEMORY_REDIS_URL`
  names (now db/5).

- **P1-a exit-code propagation.** `run.py:main()` migrates the fixed-`choices` `service` positional to
  argparse **subparsers**. Every existing service verb stays behaviorally identical (same no-arg
  invocation: `python -m arb_memory audit`, `… memory`, etc. — tests parametrize across all verbs).
  The dispatch **returns the handler's exit code** (not unconditional `return 0`); `main(["audit-close",
  …])` must return 4 on refusal and that reaches `SystemExit`.

- **P1-b one-verdict idempotency — Redis `SETNX` close-claim (mechanism i, pinned).** Sequence in
  `run_audit_close()`, in order:
  1. `reconcile(conn, run_id, payload, redis=r)` FIRST. Not ok → print gaps to stderr, **exit 4**, no
     claim, no emit.
  2. Reconcile ok → compute `h = sha256(canonical_json(payload))`. Atomically
     `SETNX arbmem:audit:run:{prefix}{run_id}:verdict_close = h` (TTL = `SEQ_TTL_SECONDS`, the 365-day
     sequence-key TTL, so the one-verdict guard remains durable across realistic re-close windows).
     - claim ACQUIRED → `AuditRun(...).emit(source, "verdict", payload)` inside a catchable failure boundary;
       an emit exception deletes the claim and returns a clean nonzero error so a retry can acquire it.
       Successful emit → **exit 0**.
     - claim EXISTS and stored hash == h → scan the audit stream for a `verdict` event for this `run_id`.
       If present, the run already closed with this exact verdict; **exit 0, no second emit** (idempotent
       retry). If absent, return **exit 6** with an actionable message; do not auto-re-emit into a possible
       concurrent in-flight `XADD`. Recovery from a true hard crash (SIGKILL between claim and emit)
       is a loud exit 6; the operator must DEL the claim key and re-run.
     - claim EXISTS and stored hash != h → a *different* verdict already closed this run; print refusal
       to stderr, **exit 5**, no emit (this is the back-door-laundering guard).
  3. Two concurrent closers both pass reconcile → exactly one wins `SETNX` and emits; the other takes the
     same-hash (exit 0 or loud exit 6 if it observes the in-flight window) or different-hash (exit 5) branch.
     Net: **exactly one verdict row per run**.
  Canonical-json + hash reuse `audit._canonical_payload`. The claim lives on the audit-bus Redis
  (`ARB_MEMORY_REDIS_URL`), same connection as the emit.

- **P1-c quote-safe transport.** Payload is read from **stdin** via `--payload-file -` (also accept a
  real path). No `--payload <json>` inline arg. Malformed JSON → **exit 2** (caught, not a traceback).
  Invocation: `… docker compose exec -T audit python -m arb_memory audit-close --run-id <id>
  --payload-file - < verdict.json`.

- **P1-d live gate = fresh disposable run.** Do NOT close the three historical `panel-arb-learn-config`
  runs (no authoritative verdict payloads → would invent fields into an irreversible prod write). The
  live gate mints a throwaway run: emit a manifest + votes to db/5, then (a) close it → verdict row
  appears in prod `arbmemory`; (b) re-close same payload → exit 0, still one verdict row; (c) re-close
  a different roster → exit 5, no new row; (d) a wrong-roster close pre-reconcile → exit 4. Deny-proof:
  the exit-4 and exit-5 refusals must be shown live against prod, not only in unit tests.

### Exit-code contract (locked by tests)

| code | meaning |
|---|---|
| 0 | verdict emitted, OR idempotent same-payload re-close (no second emit) |
| 1 | verdict emit raised an exception; the claim is released so a retry can re-acquire |
| 2 | malformed `--payload-file` JSON |
| 4 | reconcile refused (roster/stances gap) — no emit |
| 5 | run already closed with a *different* verdict — no emit |
| 6 | close claim exists but no verdict is present in the audit stream — operator must remove the claim and re-run |

### Residuals

- **DONE in this slice — structural one-verdict backstop:** `audit_events_one_verdict` is a Postgres
  partial unique index on `(run_id) WHERE kind = 'verdict'`, and the audit consumer deadletters and
  acks a rejected second verdict in a fresh transaction. This removes the allkeys-lru eviction
  double-emit risk from the prod audit bus (db/5). Prod deploy is unblocked pending this slice's
  review and the live gate.
- The long claim TTL (`SEQ_TTL_SECONDS`) + the operator procedure of DELing only after exit 6 continue
  to bound the *manual*-DEL variant; the Postgres backstop covers the separate claim-eviction variant.
- Redis and Postgres connection-handle cleanup remains a follow-up consistency improvement; it is not
  required for the close-path correctness fix.

### Deliverables

1. `src/arb_memory/run.py` — subparser migration + `audit-close` verb + `run_audit_close()`.
2. `tests/test_run_audit_close.py` — the full exit-code contract above, via `main([...])` (not the helper
   in isolation): happy-emit, exit-4 no-emit, exit-2 malformed, exit-0 idempotent re-close, exit-5
   different-verdict, concurrent-close (exactly one emit), stdin transport with `"`/`'`/newline in the
   payload. Reuse the fake redis/conn factories from `test_panel_run_cli.py` / `arb-audit-emit`.
3. `tests/` — a parametrized check that every pre-existing service verb still dispatches (subparser
   migration regression guard).
4. `deploy/README.md` — replace close guidance with the `docker compose exec -T audit … --payload-file -`
   recipe + the `build memory` (not `build audit`) rebuild gotcha.
5. `CHANGELOG.md` entry.

TDD: write the failing exit-code-contract tests first. Live gate (§8 P1-d, against prod) is a REQUIRED
post-merge gate, run by the orchestrator — not luna's to execute.
