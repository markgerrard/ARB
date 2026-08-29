# Deny-proofs themselves need adversarial verification

**A green deny-proof is just another green test.** The technique of proving a control by a test that *should
fail without it* is the right discipline — but the deny-proof can be fixture-masked or hollow exactly like
any other test. If it was never confirmed to fail against the wrong implementation, "it passes" tells you
nothing about whether it's load-bearing.

This is the corollary that closes the loop on the gate itself: the verification step is the *last* place a
blind spot can hide, because once you "verified the fix," scrutiny stops.

## Detection move

**Inject-revert every load-bearing deny-proof: remove the mechanism it guards, and confirm the test goes
RED.** If it stays green with the control deleted, the proof is hollow — rewrite it (usually by seeding the
adversarial precondition the happy-path test never creates). The red-first run in TDD *is* this verification;
for a fix added to existing code, do the inject-revert explicitly.

Two operational cautions learned the hard way:
- **Don't inject-revert via `git checkout` on uncommitted work** — it reverts the whole file to HEAD and
  silently discards your unstaged fix. Inject by editing the condition (e.g. `if False:`) and restore by
  re-editing, or commit first.
- A deny-proof that "didn't reproduce red before the fix" is a signal, not a pass — either the bug wasn't
  real, or the test doesn't exercise it. Investigate which.

## Canonical instance

ARB Memory Phase 3: the `/token` RFC 8707 `resource` enforcement had a deny-proof that **passed even when the
check was deleted** — the existing tests only seeded codes whose resource already equalled `public_base_url`,
so the line rejecting a *foreign-resource* code was never exercised. Caught during warm-seat verification
(inject-revert), not by the test suite. A real deny-proof was added that seeds a code minted for a foreign
resource and confirmed RED without the check. The gate's own verification step had exhibited the exact defect
the gate exists to catch.

Related: [`fake-cheaper-than-real`](fake-cheaper-than-real.md), and `vacuously-green-guard-fail-loud` (a guard
that's green because the mechanism it guards is stubbed).
