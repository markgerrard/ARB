# ARB Memory artefact-fidelity + audit e2e

Proves two paths the seat e2e did not, using a real document as the payload (much stronger than the unit
tests' one-char strings):

- **Artefact full-content fidelity** — store a document as an artefact (full content) + a hint via the bus
  write-intent; the memory consumer embeds + persists; retrieve via the bus reply (two-step hint→artefact,
  **not** transport-only) and assert the returned content == the original **byte-for-byte**. The
  "full content for interactive review" use case.
- **Audit store→retrieval** — emit a mini-panel's events (dispatch + votes + verdict) via `AuditRun`; the
  audit consumer drains them into `audit_events`; query by `run_id ORDER BY seq` and assert the run
  reconstructs (the disagreement-corpus superset).

Both consumers run as real subprocesses (`python -m arb_memory memory` and `… audit`) on an isolated prefix.
Fail-loud, isolated, cleans up (pre+post absence in `finally`).

## Run

```bash
set -a; . ./.env.arb-memory; set +a   # OPENAI_API_KEY for the memory consumer (audit needs no key)
ARB_MEMORY_DSN=postgresql://arb_memory:$ARB_LOCAL_PG_PASSWORD@127.0.0.1:5544/arb_memory \
ARB_MEMORY_REDIS_URL=redis://127.0.0.1:6379/15 \
.venv/bin/python3 scripts/arb-memory-artefact-audit-e2e            # default payload: the seat-e2e slice log
# or: --payload /path/to/any/document
```

Green output: `{"ok": true, "artefact": {"ok_full_content_byte_identical": true, …}, "audit": {…}}`.

## Finding from the first run (2026-06-21)

The audit path could not be prefix-isolated: `bus.py` PREFIX was env-configurable (`ARB_MEMORY_PREFIX`) but
**`audit.py` PREFIX was hardcoded `""`** — the seat-e2e build fixed `bus.py` and missed `audit.py`. So the
isolated `AuditRun(prefix=…)` emitted to a prefixed stream while the audit consumer drained the unprefixed
`arbmem:audit` group → never drained. Fixed: `audit.py` PREFIX is now env-configurable too (regression test
`test_audit_prefix.py`). The audit path had only ever been exercised at the default prefix in unit tests —
another instance of a path not run under the conditions the e2e creates.
