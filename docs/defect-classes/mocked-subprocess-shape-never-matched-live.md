# Mocked subprocess shape never matched live

A sub-shape of [`bug-lives-on-the-held-axis`](bug-lives-on-the-held-axis.md) where the held axis is
**the output shape of an external command**. The suite mocks the subprocess layer (`_run`,
`subprocess.run`, a CLI wrapper), so every test exercises the *assumed* shape — and the assumption
is wrong in a way only the real binary can reveal. The tests are green at 100% coverage of code
that cannot work.

## Canonical instances — four in ONE live acceptance (2026-08-07/08, seat registration)

| Assumed | Live reality | Failure surfaced as |
|---|---|---|
| `buzz-admin add-member` returns JSON | plain text `added <pubkey> as member` | provision "failed" AFTER the action succeeded; unbounded retry loop |
| `buzz users get` returns an object with `name` | returns a LIST of profiles carrying `display_name` | `AttributeError: 'list' object has no attribute 'get'`, then a compare against a key that never exists |
| a relay `users` row exists once the seat is a member | row is created only by the identity's FIRST kind-0 publication | owner bind matched 0 rows on every genuinely-new seat — impossible to hit in mocks, guaranteed live |
| `users.pubkey` / `agent_owner_pubkey` accept hex strings | columns are BYTEA; params must be `bytes.fromhex` | UPDATE silently matched nothing; tolerance SELECT read NULL |

All four shipped past a green, well-written mocked suite and were caught only by a live acceptance
run with an operator in the loop.

## Detection moves

1. **Run every external command once, for real, before writing its parser** — capture the actual
   stdout as a test fixture (`tests/.../fixtures/live-*.txt`). A parser without a live-captured
   fixture is a claim, not code.
2. **Grep the diff for subprocess call sites** (`_run(`, `subprocess`, `docker exec`, CLI names) and
   ask of each: *has this exact invocation executed against the real target at least once?* The
   answer "the mock covers it" is the defect signature, not a defense.
3. **Sequencing assumptions are shapes too** — "row exists by the time X runs" is an output-shape
   claim about the datastore. The live order (membership → first publication → row) beats any
   mocked order.
4. In review, treat *schema types* as live shapes: one `pg_typeof()` query would have caught the
   bytea bind before any deploy.

## Related

[`fake-cheaper-than-real`](fake-cheaper-than-real.md) — why the mock exists;
[`verification-inspected-the-wrong-object`](verification-inspected-the-wrong-object.md) — the
review-side twin, where the *checker* rather than the code binds to the wrong reality.
