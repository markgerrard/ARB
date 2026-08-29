# Build brief — AUDIT-CLOSE-1 (codex-luna @ high)

**Worktree:** `/Users/<user>/<workspace>/.claude/worktrees/audit-close-1` (branch `feat/audit-close-1`).
`cd` there first; do ALL work there; commit there. Do NOT touch the base checkout.

**Spec:** `docs/audit-close-1-design.md` — read **§8 "Build-ready resolution"** end to end. It is the
authoritative spec (exit-code table, the SETNX close-claim sequence, stdin transport, deliverables).
§1–§7 are context/rationale; §8 is what you build. If anything in §8 is ambiguous or you believe a
decision is wrong, STOP and say so in your reply — do not silently deviate.

## What you're building

A one-shot `audit-close` subcommand in `arb_memory` so a panel verdict can be closed into the audit
trail from inside the audit container, with the reconcile-refusal and one-verdict invariants as
committed, tested image code. Reuse `arb_memory.panel_audit.reconcile`, `arb_memory.audit.AuditRun`,
and `audit._canonical_payload` — do NOT reimplement reconcile or seq/hash logic.

## Method: TDD, non-negotiable (this is the luna@high regime)

1. **Write the failing tests FIRST** — `tests/test_run_audit_close.py` encoding the full exit-code
   contract from §8 (0 happy-emit, 2 malformed JSON, 4 reconcile-refused no-emit, 5 different-verdict
   no-emit, 0 idempotent same-payload re-close, concurrent-close = exactly one emit, stdin transport
   with `"` + `'` + newline in the payload). Drive them through `main(["audit-close", …])` — NOT the
   helper in isolation (P1-a: prove the exit code propagates through `main`/`SystemExit`). Reuse the
   fake redis/conn factories used by `scripts/arb-audit-emit` / `tests/test_panel_run_cli.py`.
2. Run them RED. Then implement `run.py` (subparser migration + verb + `run_audit_close()`) until GREEN.
3. Add the subparser-regression guard: a parametrized test that every pre-existing service verb
   (`memory`, `audit`, `eval`, `eval-purge`, `transcript`, `transcript-purge`, `setup-schema`, `mcp`,
   `local-read-mcp`, `writer`, `grants`, `visibility`) still dispatches after the migration.

## Load-bearing details (get these exactly right)

- **P1-a:** migrate `run.py:main()`'s fixed-`choices` `service` positional to argparse **subparsers**;
  every existing verb keeps its exact no-arg invocation; `main()` **returns the handler's exit code**
  (today it unconditionally `return 0` — that's the bug). Existing services return `None` → treat as 0.
- **P1-b (concurrency — the reviewer will hammer this):** the SETNX close-claim happens **after**
  reconcile succeeds, **before** emit. Key `arbmem:audit:run:{prefix}{run_id}:verdict_close`, value =
  `sha256` of the canonical payload, TTL `SEQ_TTL_SECONDS`. Acquired → emit + exit 0. Exists & same hash
  → exit 0 no second emit. Exists & different hash → exit 5 no emit. Claim on the audit-bus Redis
  (`ARB_MEMORY_REDIS_URL`). A reconcile FAILURE must take NO claim (so a legitimate retry after fixing
  the roster still works). Test the concurrent path deterministically (e.g. pre-set the claim to
  simulate the loser).
- **P1-c:** payload from **stdin** (`--payload-file -`) or a real path; NO inline `--payload` arg.
  Malformed JSON → exit 2 (caught, not a traceback). Honor `ARB_MEMORY_PREFIX` exactly as
  `scripts/arb-audit-emit` does.
- **Do NOT run the live/prod gate.** §8 P1-d (closing a fresh run against prod `arbmemory`) is the
  orchestrator's post-merge gate, not yours. You have no prod DSN and must not reach for one.

## Also update

- `deploy/README.md` — replace close guidance with
  `docker compose exec -T audit python -m arb_memory audit-close --run-id <id> --payload-file - < verdict.json`
  and the `build memory` (not `build audit`) rebuild gotcha (see §2 Option B con).
- `CHANGELOG.md` — one entry (what + why).

## Verify before you report

Run the targeted suite and paste real output:
`cd <worktree> && uv run --extra arb-memory python -m pytest tests/test_run_audit_close.py tests/test_run.py -q`
(create/adjust paths as needed). Also run the existing `tests/` that touch `run.py` to prove the
subparser migration didn't regress. Do NOT run the full e2e suite.

## Report (inline in your reply)

- The commit SHA(s) on `feat/audit-close-1`.
- The pytest summary line (N passed) — verbatim, from your actual run.
- Any §8 point you deviated from or found underspecified, and why.
- End with the machine-readable fence:

```vote
{"stance":"approve|needs-changes|block|abstain","severity":"none|P2|P1|P0","refs":["src/arb_memory/run.py"],"note":"<one line: what shipped>"}
```
(For a build dispatch this reports YOUR confidence in the delivered code; the independent review is separate.)
