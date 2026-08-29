# Fix brief — AUDIT-CLOSE-2 panel remediation (codex-luna @ high)

**Worktree:** `/Users/<user>/<workspace>/.claude/worktrees/audit-close-2`, branch `feat/audit-close-2`
(continue; commit there). Base under fix: `efd9ac8`.

A 4-seat panel (cold-Opus + sol@high + grok + GLM) reviewed your build: **needs-changes**. Core
`close_core` extraction is faithful (keep it). Fix the items below. **Do NOT churn** the reconcile→
SETNX→emit logic or `test_run_audit_close.py` (must stay 0-lines-changed + green — faithfulness anchor).
TDD: write the failing test first for each P1.

## P1-1 (load-bearing) — transient failures strand in the consumer PEL, never retry

`AuditCloseConsumer.step()` reads `{stream: ">"}` (new only), and `run()` calls `drain_pending()` **once
at startup** then loops `step()`. So when `_handle_entry` returns `None` on a transient redis/pg error
(the correct "don't ack" path), the entry sits in this consumer's PEL and is **never re-read** until the
process restarts — the "no-ack → retry" the design promises does not happen in steady state. (sol@high
proved it: two `step()` calls → one `close_core`, request still pending, no result.)

**Fix.** Make the running loop reprocess this consumer's own PEL, bounded, so a transient failure retries
and eventually acks. Options: (a) each loop cycle, after the `>` read, also do a pending read
(`xreadgroup(GROUP, consumer, {stream: "0"}, ...)` / `XAUTOCLAIM`) with a bounded backoff; or (b) an
idle-triggered re-drain. Don't busy-spin. **Test (required):** a transient error on first delivery
(inject a `conn_factory`/emit throw once), then assert a later loop cycle retries the SAME entry and, on
success, acks it (pending → 0) and writes the result. This is the fix the panel blocks on.

## P1-2 — helper default timeout equals the reconcile poll window

`scripts/arb-audit-close-request` default `--timeout` 30s == `reconcile(poll_timeout_s=30.0)`. Under the
audit lag the poll exists for, the helper `BLPOP` times out and returns exit 7 ("no consumer response")
against a live, working consumer — and the LPUSHed result is then stranded (BLPOP doesn't re-poll; the
600s TTL is moot for that caller). **Fix:** default the helper timeout comfortably above the poll window
(e.g. 90s), and/or document that `--timeout` must exceed reconcile's poll. Add/adjust the helper test.

## P1-3 — break-glass CLI no longer returns exit 1 on an infra error during emit

`close_core` now re-raises infra (redis/pg) errors during emit (correct — the consumer needs them to
retry), but the thin CLI wrapper (`run_audit_close`) doesn't catch them, so `python -m arb_memory
audit-close` **tracebacks instead of returning 1** on an infra failure — breaking the AUDIT-CLOSE-1
0/1/4/5/6 contract. The unchanged tests miss it because their emit-failure case uses a `ValueError`, not
an infra error. **Fix:** in the CLI wrapper only, catch infra exceptions from `close_core`, print an
`emit_failed` message, and `return 1` (the consumer path keeps re-raising for retry). **Test:** a CLI
invocation whose emit raises `redis.ConnectionError` returns exit 1 (not a traceback).

## P2-4 — prove the deadletter table's grant + storage (deny-proof + real INSERT)

`audit_close_deadletter` is correctly REVOKED from the local reader in `grants.py`, but it's **not in the
deny-proof tuples**, so nothing enforces it. Add `audit_close_deadletter` to `SENSITIVE_TABLES` in BOTH
`tests/arb_memory/test_local_reader_grants.py` AND `tests/arb_memory/test_vault_export_grants.py` (+
`INJECTED_PRIVILEGES` if that pattern applies). Also: `test_audit_close_consumer.py` monkeypatches
`deadletter_malformed_close_request`, so the real INSERT + `ON CONFLICT (stream_entry_id) DO NOTHING`
never runs against a DB — add one scratch-DB test (follow `tests/arb_memory/conftest.py`) that drives a
malformed request through the real deadletter INSERT and asserts the row + idempotent redelivery.

## P2-5 — deterministic non-infra handler errors should deadletter+ack, not strand

The consumer special-cases only redis/pg errors; a deterministic non-infra bug in the handler would
`return None` → no ack → poison-pill (same class as P1-1). `AuditConsumer` deadletters other `Exception`s.
Mirror that: an unexpected non-infra exception in the close handler → deadletter (recoverable) + ack.

## Out of scope (orchestrator handles at deploy)

The `audit-close-consumer` **compose service** (all seats flagged its absence) — the orchestrator wires
it into `deploy/docker-compose.yml` with the writer DSN at deploy. Do NOT add it.

## Verify + report

`uv run --extra arb-memory python -m pytest tests/test_run_audit_close.py tests/test_close_core.py
tests/test_audit_close_consumer.py tests/test_arb_audit_close_request.py tests/arb_memory/test_local_reader_grants.py -q`
— paste the REAL summary; confirm `test_run_audit_close.py` still 0-changed + green. Report the new SHA,
which P1 tests you added, and anything you couldn't run (no DSN). End with the vote fence.
