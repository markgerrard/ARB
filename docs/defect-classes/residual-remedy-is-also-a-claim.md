# A named residual's remedy is also a claim

**Naming a gap honestly does not make the fix you prescribe for it true.** The disciplined move — "this
mechanism does not establish X; to get X, do Y" — produces two claims, not one. The residual (X is not
established) is usually well-evidenced, because you just found it. The remedy (Y establishes X) is
typically *asserted*, because at the moment you write it you are documenting a limit, not testing a fix.

A wrong remedy is worse than an unnamed gap. An unnamed gap leaves a reader uncertain, which is a state
that invites checking. A named gap with a confident remedy leaves a reader *certain and wrong*, and they
stop looking — the honesty of the disclosure is exactly what buys the false conclusion its credibility.
Note the shape: the code can be entirely correct and this defect still present. What is wrong is the
document's account of its own limits.

## Detection move

**For every residual you name, ask: has the prescribed remedy been executed, or only reasoned about? Then
deny-proof the REJECTED remedy — write a test asserting the insufficient thing is insufficient.**

That test is the odd one in the suite: it does not guard the code, it guards a sentence. It fails if the
rejected remedy ever becomes sufficient — a dependency upgrade, a platform change, a later refactor — at
which point you reinstate the simpler remedy *deliberately*, rather than by assumption. Cheap to write,
and it converts a claim that can only rot into one that fails loudly.

A residual you cannot test at all is still worth naming — but say which of the two claims is evidenced and
which is not, rather than presenting both in the same voice.

## Canonical instance

Bus-side gate Slice 1b (panel `panel-gate-slice1b-r2-20260726T153801Z-ded470`). `apply_gate_reader_grants`
revoked PUBLIC and direct ACLs so a reader role could not mint gate state. Its docstring named the honest
residual — role membership and ownership are invisible to relation ACLs — and prescribed: *"Provision it
NOINHERIT, or assert membership at deploy time."*

The first half was wrong. Measured on PostgreSQL 17.10:

- with the role `INHERIT`, the automatic write succeeded;
- with the role `NOINHERIT`, the automatic write was `REFUSED 42501` — **and** `SET ROLE <parent>` followed
  by `INSERT` still minted a row. `NOINHERIT` blocks only *automatic* inheritance; membership continues to
  authorise `SET ROLE`.
- separately, a role *owning* a gate relation re-granted itself write access after the helper's revoke.

An operator following the docstring would have provisioned `NOINHERIT`, believed the property held, and
been wrong — with no test anywhere able to say so, because no test targets a docstring. The remedy was
replaced with `assert_gate_role_isolation()` (refuses any membership or ownership, called by the grant
helper so it fails closed), and the rejected remedy was pinned by
`test_noinherit_alone_does_not_establish_isolation`, which asserts that `SET ROLE` through a membership
still mints. Its docstring says what it is for: *"Deny-proof for a REJECTED REMEDY, not for the code."*

Found by a panel seat that attacked the residual rather than accepting it as adequately named — two other
seats read the same docstring, treated the gap as disclosed, and voted approve.

Related: [`deny-proofs-need-adversarial-verification`](deny-proofs-need-adversarial-verification.md) (the
same discipline applied to the tests rather than the prose),
[`prediction-written-as-result`](prediction-written-as-result.md) (the general case of an untested claim
recorded as an established one), and
[`refusal-is-ambient-assert-the-code`](refusal-is-ambient-assert-the-code.md).
