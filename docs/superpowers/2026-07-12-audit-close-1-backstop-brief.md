# Build brief — AUDIT-CLOSE-1 backstop: structural one-verdict guard (codex-luna @ high)

**Worktree:** `/Users/<user>/<workspace>/.claude/worktrees/audit-close-1-backstop`, branch
`feat/audit-close-1-backstop` (off dev, which already has the merged `audit-close`). `cd` there,
commit there.

## Why

`audit-close` (merged to dev, prod deploy HELD) enforces one-verdict-per-run via a Redis `SETNX`
claim. But the prod audit bus (db/5) runs `maxmemory-policy: allkeys-lru` — under memory pressure the
claim key can be **evicted** while a verdict row exists, after which a re-close hits the acquire path
and emits a **second verdict silently** (a fresh seq dodges `ON CONFLICT (run_id, seq)`). This slice
makes the invariant **structural in Postgres**, so it no longer depends on an evictable Redis key.
Shipping this is what unblocks the prod deploy of audit-close. (Named as the deferred backstop in
`docs/audit-close-1-design.md` §8 Residuals; now required.)

## Deliverables

### 1. Schema — partial unique index (`src/arb_memory/schema.sql`)

Add, near the `audit_events` definition:
```sql
CREATE UNIQUE INDEX IF NOT EXISTS audit_events_one_verdict
    ON audit_events (run_id) WHERE kind = 'verdict';
```
This enforces at most one `kind='verdict'` row per `run_id` (non-verdict rows unconstrained; different
`run_id`s unconstrained). Add a comment explaining it's the structural one-verdict backstop for
audit-close. Note in the comment: on a DB that already holds duplicate verdicts the index creation
fails loudly — that is correct (surfaces real duplicates for manual resolution); prod `arbmemory`
currently has zero verdicts so creation is clean. `setup-schema` applies this via schema.sql — follow
the existing `CREATE INDEX IF NOT EXISTS` pattern in that file; do NOT restructure the file.

### 2. Consumer — deadletter the second verdict, never crash-loop (`src/arb_memory/audit.py`)

The insert method (~`audit.py:111-142`) does `INSERT … ON CONFLICT (run_id, seq) DO NOTHING` then, on a
(run_id,seq) conflict, either returns `"duplicate"` (same content_hash) or deadletters (different hash).
A second verdict has a **different seq**, so it passes that ON CONFLICT and then violates the new
partial-unique → `psycopg.errors.UniqueViolation` raised inside `with conn.transaction()`.

Handle it: catch the partial-unique violation specifically (by constraint name
`audit_events_one_verdict`, or by catching `UniqueViolation` and confirming a verdict already exists for
the run) and route the rejected event to `audit_deadletter` with a clear reason (e.g.
`"duplicate verdict for run_id"`). Requirements (per [[evidence-store-no-silent-drop]]):
- **Never silent-drop** — the rejected second verdict MUST land in `audit_deadletter` (recoverable).
- **Never crash-loop / poison-pill** — a `UniqueViolation` is deterministic; it must be deadlettered +
  the stream entry **acked**, not retried forever. Distinguish it from *transient* `psycopg.Error`
  (connection blips), which should still retry, not deadletter.
- The transaction aborts on the violation — deadletter in a **fresh** transaction/connection (mirror how
  the existing handler deadletters after a failed insert).
- The FIRST verdict must remain intact; only the duplicate is rejected.

Return a distinct status string (e.g. `"duplicate_verdict"`) so it's observable/testable.

### 3. Tests (`tests/arb_memory/…`) — real Postgres, follow the existing scratch-schema harness

Use the existing integration pattern in `tests/arb_memory/conftest.py` (scratch schema per test). Add:
- **Schema enforcement:** inserting two `kind='verdict'` rows for one `run_id` violates
  `audit_events_one_verdict`; a verdict for a *different* run_id and a *non-verdict* second row for the
  same run_id both succeed.
- **Consumer behavior:** feed the consumer two verdict events for one run_id (different seq); assert the
  second lands in `audit_deadletter` with the duplicate reason, the consumer does NOT raise / wedge (it
  acks), and exactly one verdict row remains in `audit_events`.
- A transient `psycopg.Error` (mock/inject) is still retried, NOT deadlettered (guard the discrimination).

If you cannot reach a Postgres from your dispatch environment, still WRITE the tests (TDD) and say so in
your report — the orchestrator will run the integration suite. Do not fake a green result.

### 4. Docs

- `docs/audit-close-1-design.md` §8 Residuals: mark the partial-unique backstop **DONE** (this slice);
  note prod deploy is unblocked pending this slice's review + the live gate.
- `CHANGELOG.md`: one entry (what + why: structural one-verdict guard; removes the allkeys-lru eviction
  double-emit risk).

## Verify + report

Run whatever tests you can (`uv run --extra arb-memory python -m pytest tests/arb_memory/ -q` or the
specific new files) and paste the REAL summary. Report the commit SHA on `feat/audit-close-1-backstop`,
which tests you could/couldn't run and why, and any place the deadletter discrimination was ambiguous.
End with the vote fence:

```vote
{"stance":"approve|needs-changes|block|abstain","severity":"none|P2|P1|P0","refs":["src/arb_memory/schema.sql","src/arb_memory/audit.py"],"note":"<one line>"}
```
