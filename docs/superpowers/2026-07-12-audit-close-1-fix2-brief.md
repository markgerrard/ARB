# Fix-2 brief — AUDIT-CLOSE-1: restore the durable one-verdict guard (codex-luna @ high)

**Worktree:** `/Users/<user>/<workspace>/.claude/worktrees/audit-close-1`, branch `feat/audit-close-1`
(continue; commit there). Base under fix: `bc37206`.

Your fix for the crash-window is correct (loud exit 6, try/except claim-release, stream verdict check).
But it introduced ONE regression by shortening the claim TTL. This brief fixes only that. Do not churn
the rest.

## The regression (P1)

`AUDIT_CLOSE_CLAIM_TTL_SECONDS = 10*60` (`run.py:11`) makes the claim expire in 10 minutes, but the
durable stream-verdict check (`_audit_stream_has_verdict`) runs ONLY on the same-hash existing-claim
branch (`run.py:423`). The **acquire path** (`run.py:408-417`, `claimed` is truthy) emits
**unconditionally**. So after 10 minutes the claim is gone and:

- a **different-verdict** close → `SETNX` succeeds → emits → **two conflicting verdicts for one run**.
  The exit-5 anti-laundering guard (the whole point) only holds for 10 minutes.
- a **same-payload** retry >10 min later → re-acquires → **emits a duplicate verdict**.

The one-verdict / anti-laundering invariant MUST be durable (indefinitely), not 10-minute-bounded.

## The fix — revert to a long claim TTL (one-line-ish); keep everything else

The earlier brief's "self-heal, not 365 days" property was WRONG and caused this — disregard it. Because
an orphaned claim now surfaces as a **loud exit 6** (operator DELs the key + re-runs), the long-lived
claim is no longer a silent lie; it is the durable different-verdict guard. So:

1. **Set the claim TTL back to the long value: reuse `SEQ_TTL_SECONDS`** from `arb_memory.audit` (the
   365-day value the per-run seq key already uses) instead of `AUDIT_CLOSE_CLAIM_TTL_SECONDS = 10*60`.
   Drop the custom 10-minute constant. With a long TTL the claim persists across any realistic re-close
   window, so exit 5 (different verdict) and same-hash idempotency hold indefinitely.
2. Keep everything else exactly as-is — do NOT add new scan logic:
   - loud **exit 6** for claim-exists-but-no-verdict;
   - the `try/except` that `DEL`s the claim and exits nonzero on emit failure (this still self-heals the
     catchable oversize case: acquire → emit raises → DEL → a retry re-acquires cleanly);
   - the stream-verdict check ONLY on the same-hash branch (it's a full `xrange` scan — keep it on that
     rare retry path, do NOT move/copy it onto the common acquire path, which would scan the whole
     stream on every close).
   - exit 5 on different hash.
3. Update design §8: recovery from a true hard-crash (SIGKILL between claim and emit) is a **loud exit 6
   → operator DELs the claim key and re-runs** — a documented manual step, the accepted price of a
   durable invariant. Remove any "auto-heal / short TTL / self-heal" language. Note the residual: a claim
   that is manually DEL'd or evicted *while a verdict exists* could allow a re-emit — bounded by the long
   TTL + the operator only DELing on an exit-6 (which means no verdict exists); acceptable, documented.

## Required regression test — pin the TTL long (this is why the bug shipped)

The existing test asserts only `0 < ttl < 365*24*60*60` (`test_run_audit_close.py:103`), which passes for
the 10-minute value — that looseness let the regression through. **Tighten it: assert the claim TTL is the
long value** (e.g. `ttl == SEQ_TTL_SECONDS`, or `ttl >= 30*24*60*60`), so a short TTL can never regress in
silently again. Keep the existing different-hash → exit 5 test (it already proves the durable guard holds
while the claim is present, which — with the long TTL — is the real operating window).

## Verify + report

`uv run --extra arb-memory python -m pytest tests/test_run_audit_close.py tests/arb_memory/test_run_entrypoints.py -q`
— paste the real summary. Report the new commit SHA and confirm the acquire-path stream guard + long TTL
are both in. End with the vote fence.
