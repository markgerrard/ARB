# Fix brief — AUDIT-CLOSE-1 review remediation (codex-luna @ high)

**Worktree:** `/Users/<user>/<workspace>/.claude/worktrees/audit-close-1`, branch `feat/audit-close-1`
(continue on it, commit there). Base commit under fix: `eed964a`.

A 3-seat independent panel (cold-Opus + grok + your GLM peer) reviewed your build. **Verdict:
needs-changes / P1.** One blocker (F1) + one test-fidelity fix (F2). Everything else (subparser
migration, reconcile-before-claim, exit codes 0/2/4, prefix, transport) they rated sound — do not
churn it.

## F1 (P1, BLOCKER) — the SETNX claim can be held with no verdict ever emitted

**Confirmed independently by all three reviewers + the orchestrator.** In `run_audit_close`
(`run.py:390-400`): `SETNX claim` (`:390`) happens **before** `emit` (`:392`). Two failure paths:
1. **Emit raises** (e.g. `AuditRun.emit` does `INCR` seq then `XADD`; the `XADD` throws) → claim held,
   no verdict, process exits nonzero.
2. **Process dies** in the claim→emit window (the "retry after ssh drop" the design anticipates).

Then the standard retry (same payload) loses the `SETNX`, matches `existing_hash == payload_hash`, and
**returns 0 — reporting success while the verdict was never written.** The claim TTL is
`SEQ_TTL_SECONDS` (365 days), so it's a ~1-year silent lie plus a manual-`DEL` lockout. This is the
exact fabricated-completeness failure the audit trail exists to prevent.

**It is also DETERMINISTIC, not only a crash (GLM's catch):** `AuditRun.emit` → `audit_emit` raises
`ValueError` for any payload > `AUDIT_MAX_PAYLOAD_BYTES` (16384, `audit.py:21,54-55`). That raise is
UNCAUGHT in `run_audit_close`, and it happens *after* the `SETNX` at :390 — so an oversize verdict
payload reliably leaves a stuck claim + no emit + traceback exit 1, and every same-payload retry then
returns 0. So there is a concrete, non-crash reproduction to test against.

### Required properties of the fix (prove each with a test — TDD, write the failing test first)

1. A same-payload retry after a FAILED/absent emit must **never** return 0-without-verdict — it must
   re-emit (or refuse loudly). Success exit 0 must imply a verdict actually exists.
2. **Exactly one verdict per run** under BOTH (a) sequential retry (any number of times) AND
   (b) two concurrent same-payload closers.
3. A different-payload close *after a real prior verdict* still refuses (exit 5) — keep that guard.
4. **No permanent lockout:** an orphaned in-flight state must self-heal in bounded time, not 365 days.

### Fix mechanism — loud-fail, do NOT auto-re-emit (GLM's approach, adopted; it's the safer design)

The panel considered auto-re-emit on the same-hash path and REJECTED it: re-emitting when "no verdict
found" reintroduces the concurrent double-emit (closer A mid-`XADD`, closer B checks "exists?", sees
not-yet, B also emits → two verdicts). So the fix does NOT auto-re-emit. Two parts:

1. **Catchable emit failures self-heal (handles the deterministic oversize case + any emit exception).**
   Wrap the emit in `try/except`. On ANY exception, `DEL` the claim key (the current process owns it —
   it won the `SETNX` — so releasing it is race-free) and exit nonzero with a clean message (not a
   traceback). A retry then re-acquires cleanly and emits. This alone fixes the oversize-`ValueError`
   reproduction and every catchable emit error.

2. **Uncatchable hard-crash → loud error, never silent 0.** On the same-hash claim-exists branch
   (today's `existing_hash == payload_hash → return 0`), do NOT return 0 unconditionally. First verify a
   verdict was actually emitted; if absent, return a **distinct nonzero code (6)** with a message like
   "close-claim exists but no verdict emitted (prior crash) — DEL the claim key and re-run", NOT 0. This
   converts the silent lie into a loud, actionable error without re-introducing the double-emit race.

**Verdict-existence check — check the STREAM, not Postgres.** PG is written by the consumer
**asynchronously**; right after a real emit the row isn't in PG yet (consumer lag) → a PG check reads
"absent" spuriously. Since the fix is loud-fail (not re-emit), a spurious "absent" is only a false
alarm (operator investigates, finds it fine) rather than a double-emit — but still prefer the
synchronous Redis ground truth: check whether a `verdict` entry exists on the audit **stream** for this
run_id (or a per-run verdict marker you set at emit time). If you must use PG, gate it behind reconcile's
existing stability-poll so lag is drained first, and document why.

Keep exit codes 0/2/4/5 as-is; add **6** = "stuck claim, no verdict, operator intervention".

### F1 regression tests (required)

- **Deterministic oversize case:** a payload > 16384 bytes → assert the FIRST close exits nonzero-clean
  (not a traceback) AND leaves NO stuck claim (self-healed via the `try/except` `DEL`), so a retry with a
  valid payload still works.
- **Hard-crash simulation:** plant a claim WITHOUT a corresponding emit, then assert a same-payload retry
  returns **6** (loud), NOT 0, and does NOT emit.
- **Concurrency preserved:** the existing "exactly one emit" property still holds (no auto-re-emit path).
  Reuse the FakeRedis; extend it to model whatever verdict-existence signal you check.

## F2 (P2, ship with F1) — exit-5 test uses an impossible scenario

`tests/test_run_audit_close.py` exit-5 test uses a stubbed always-ok `reconcile` with
`approve` vs `block` stances — but **real** `reconcile` would reject that at exit 4 (stances must match
vote rows), so it never reaches exit 5. Change the exit-5 test to two **reconcile-legal** payloads that
differ only in a free-form field (e.g. `rationale`) or roster **list order** (set-equal, JSON-unequal) —
so the test stays valid if the stub is later tightened.

## Explicitly OUT of scope (document as residuals in the design doc §8, do NOT build now)

- Postgres partial-unique `UNIQUE (run_id) WHERE kind='verdict'` as a durable structural backstop
  (cold-Opus P2). It's a schema + consumer-behavior change needing its own review — note it as a
  follow-up, don't bundle it.
- conn/redis handles not closed (cold-Opus P2 nit) — you MAY add `with`/`close()` for consistency if
  trivial, but it's not required.

## Verify + report

Run `uv run --extra arb-memory python -m pytest tests/test_run_audit_close.py tests/arb_memory/test_run_entrypoints.py -q`
from the worktree; paste the real summary line. Do NOT run the prod live gate (orchestrator's, post-merge).

Report inline: the new commit SHA on `feat/audit-close-1`, the verbatim pytest summary, which mechanism
you chose (and why, if not the recommended two-phase), and any property you could not satisfy. End with:

```vote
{"stance":"approve|needs-changes|block|abstain","severity":"none|P2|P1|P0","refs":["src/arb_memory/run.py"],"note":"<one line>"}
```
