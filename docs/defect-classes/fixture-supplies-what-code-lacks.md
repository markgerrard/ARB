# Fixture supplies what the code lacks (the fixture face)

A specific, high-stakes face of [`fake-cheaper-than-real`](fake-cheaper-than-real.md): the **test fixture is
configured to provide a property the production code fails to provide**, so the test passes because the
*fixture* does the code's job, not the code. It fails green by a different mechanism than the framework face —
not "the test couldn't be adversarial," but "the scaffold quietly did the work."

## Detection move

**Ask of every fixture: "is this fixture PROVIDING a property the production code is supposed to provide
itself?"** Common culprits: autocommit/commit, transaction rollback between tests, connection lifecycle/close,
cleanup, deterministic ordering, a pre-seeded cache, an env var the prod path must set.

Then either:
- run at least one test against a fixture configured **like production** along that axis (a non-autocommit
  connection; a real connection lifecycle), **or**
- **assert the production property directly** so the fixture can't stand in for it —
  e.g. `assert mcp_connect().autocommit is True`.

## Canonical instance (the worst place it could hide)

ARB Memory Phase 3, the public MCP-OAuth door — new auth code on the sole public boundary. The test `scratch`
connection was **autocommit**, so every OAuth write (DCR, login, code, token) persisted *in the test*.
Production `mcp_connect()` used psycopg's **non-autocommit default and never committed** — a **store that
doesn't store**. 90+ green tests over a persistence path that loses every write across requests. Caught only
by the decorrelated code-review panel; fixed by making `mcp_connect()` autocommit + a regression test that
fails against `autocommit=False`. (Then the multi-write `exchange`/`rotate` paths were wrapped in explicit
transactions so autocommit didn't trade durability for lost atomicity — the resting state is "autocommit
default + explicit transactions on the genuinely-multi-write paths.")

## Variant: the test NAME supplies the proof the ASSERTION lacks

A close cousin: a test *named* for a behaviour it never exercises. The name carries the claim; the body
doesn't back it; the doc/reader trusts the name. Same mechanism — something other than the code-under-test
supplies the appearance of proof — but the "fixture" here is the **identifier**.

**Instance (ARB Memory, found in our *own* doctrine-claiming architecture doc):** `arb-memory-architecture`
§3 called `timeout→grep` a load-bearing safety valve to "build first." The test
`test_read_timeout_returns_none_then_grep` asserted **only** `out is None` — the `_then_grep` half was never
exercised, and (verified) there is no grep anywhere in `src/arb_memory/`. A misnamed test let a load-bearing
architecture claim read as covered for three phases. Fix: rename to `test_read_timeout_returns_none` (name it
for what it proves), and correct the doc — `timeout→grep` is **caller-side discipline** (the seat greps its
own repo on `None`), not a memory-layer mechanism.

**Detection move:** for any test whose name asserts a behaviour (`_then_X`, `_rejects_Y`, `_is_atomic`),
confirm the **body actually exercises X/Y/atomicity** — and if the name references a mechanism, confirm that
mechanism **exists in the code** (grep for it). An over-claiming name is a flagged defect.

See the sibling [`test-behind-framework-drive-directly`](test-behind-framework-drive-directly.md), the
corollary [`deny-proofs-need-adversarial-verification`](deny-proofs-need-adversarial-verification.md), and
the process class [`primary-path-was-the-unreviewed-path`](primary-path-was-the-unreviewed-path.md).
