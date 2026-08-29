# Build brief — AUDIT-CLOSE-2: bus-driven verdict close (codex-luna @ high)

**Worktree:** `/Users/<user>/<workspace>/.claude/worktrees/audit-close-2`, branch `feat/audit-close-2`
(off dev). `cd` there, commit there. **Spec:** `docs/audit-close-2-design.md` — read it; §2 (decided
substrate/auth) and §3 (components) are authoritative. TDD, effort high.

## Goal

Let an orchestrator close a panel verdict over the **bus** (which it already uses for votes) instead of
`ssh`-ing to arb-prod. A close-consumer on arb-prod (writer DSN) does the privileged reconcile+emit and
returns the result on the bus. The AUDIT-CLOSE-1 close logic is already built, reviewed, and
prod-live-gated — you are REFACTORING it into a reusable core and adding a bus transport, NOT rewriting
the close semantics.

## Resolved design choices (build to these; don't re-litigate)

- **Result channel = a per-request Redis LIST** `arbmem:audit:close_result:<request_id>`: the consumer
  `LPUSH`es the result JSON then sets a TTL (e.g. 600s); the orchestrator `BLPOP`s it with a timeout.
  (Same blocking-await shape the dispatch bus uses — not a polled key.)
- **Separate consumer service** (`command: ["audit-close-consumer"]` in `run.py`), not folded into the
  audit consumer. (You add the run.py entry + consumer class + tests; the orchestrator will wire the
  compose service + deploy — do NOT edit deploy/docker-compose.yml unless trivial and additive.)
- **Auth: reconcile-gated, bus-open** — the consumer processes any `close_request`; `reconcile` is the
  only gate. No identity/allowlist check.
- **request_id** is orchestrator-minted (uuid). Idempotency is already handled by close_core's SETNX
  one-verdict claim (a double-consume / retry with the same verdict → exit 0 no re-emit), so the consumer
  need not dedupe requests itself.

## Deliverables

### 1. Refactor `run_audit_close` → `close_core` (`src/arb_memory/run.py`)

Extract the reviewed logic into a pure function:
`close_core(conn, redis, run_id, payload, *, source="orchestrator") -> CloseResult` where `CloseResult`
carries `{outcome, exit_code, gaps}` (outcome ∈ `emitted|refused_reconcile|different_verdict|orphaned|
emit_failed`; exit_code ∈ 0/1/4/5/6 exactly as today). Move the reconcile → SETNX claim (long TTL,
`SEQ_TTL_SECONDS`) → emit, the stream verdict check, the try/except claim-release, and the exit-5/6
branches into it — **verbatim behavior**. `run_audit_close(run_id, payload_file, source)` becomes a thin
wrapper: read/parse the payload-file (exit 2 on malformed) → `close_core` → print gaps to stderr →
`return result.exit_code`. **The existing `tests/test_run_audit_close.py` exit-code contract MUST stay
green unchanged — that is the proof the refactor is faithful.** Do not touch the backstop
(`_is_duplicate_verdict_violation` / `deadletter_duplicate_verdict`) — it's on the persist path.

### 2. Close-consumer (`src/arb_memory/` + `run.py` `audit-close-consumer` command)

A consumer on stream `arbmem:audit:close_request`, group `arbmem-audit-close`. For each entry
(`{request_id, run_id, verdict, requested_by}`): call `close_core(conn, redis, run_id, verdict, ...)`,
then `LPUSH arbmem:audit:close_result:<request_id>` the result JSON `{outcome, exit_code, gaps}` +
`EXPIRE` it (600s). Discipline (mirror the audit consumer):
- **No silent-drop / no poison-pill:** a malformed request (bad JSON, missing `request_id`/`run_id`/
  `verdict`) → write a `{outcome:"malformed", exit_code:2}` result AND deadletter the raw entry
  (reuse/extend the deadletter pattern) AND ack. A transient infra error (redis/pg) → no ack, retry.
- close_core's own refusals (exit 4/5/6) are NORMAL results, not errors — LPUSH them and ack.

### 3. Orchestrator helper (`scripts/arb-audit-close-request`)

`--run-id`, `--payload-file -` (stdin), `--timeout` (default 30). Mints a `request_id`, `XADD`s the
close_request to `arbmem:audit:close_request`, `BLPOP`s `close_result:<request_id>` up to timeout, prints
the outcome/gaps, and **exits with the returned exit_code** (so callers get the same 0/1/4/5/6 contract
as the CLI, over the bus). Timeout with no result → distinct exit (e.g. 7, "no close-consumer response").

### 4. Tests (real where it matters)

- `close_core` unit tests: reuse the fake redis/conn factories; assert each outcome/exit_code (0/1/4/5/6)
  — and confirm `test_run_audit_close.py` still passes unchanged.
- Consumer tests: happy request → close_result has `emitted`/0 + one verdict emitted; wrong-roster
  request → `refused_reconcile`/4, no emit; malformed request → `malformed`/2 result + deadletter + ack
  (no poison-pill); transient error → no ack.
- Helper test: publish + BLPOP round-trip returns the consumer's result and the right exit code; timeout
  path → exit 7.
- Integration (real PG) where the consumer touches the DB — note which you can't run (no DSN in your env)
  and the orchestrator will run them.

## Verify + report

Run the suite you can (`uv run --extra arb-memory python -m pytest tests/test_run_audit_close.py <new
files> -q`) and paste the REAL summary. Report the commit SHA on `feat/audit-close-2`, confirm
`test_run_audit_close.py` is unchanged + green (faithfulness), and flag anything underspecified. End with
the vote fence:

```vote
{"stance":"approve|needs-changes|block|abstain","severity":"none|P2|P1|P0","refs":["src/arb_memory/run.py"],"note":"<one line>"}
```
