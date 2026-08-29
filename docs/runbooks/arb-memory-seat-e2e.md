# ARB Memory Seat E2E

This runbook exercises the cross-seat ARB Memory path:

1. start one local `python -m arb_memory memory` consumer on an isolated `ARB_MEMORY_PREFIX`;
2. prove readiness with a sentinel write that is persisted by the consumer;
3. dispatch a real writer seat through `scripts/agent-dispatch`;
4. dispatch a different real reader seat through `scripts/agent-dispatch`;
5. require the reader to query with `transport_only=True`, so grep fallback cannot mask a broken bus.

The script does not simulate seats. If a seat cannot import `arb_memory.client` or reach Redis, the run fails.

## Prerequisites

- Postgres is reachable via `ARB_MEMORY_DSN`.
- Redis is reachable via `ARB_MEMORY_REDIS_URL`.
- The bridge seats `codex-bridge-dev-example` and `agy-bridge-dev` are registered on the bridge Redis bus.
- `/Users/<user>/<workspace>/envs/agent-redis-bridge-dev.env` is readable by `scripts/agent-dispatch`.
- The checkout is importable by the seats with `PYTHONPATH=src`.
- The consumer environment has the real embedding configuration needed by `arb_memory.embed`.

## Run

```bash
ARB_MEMORY_DSN=postgresql://arb_memory:$ARB_LOCAL_PG_PASSWORD@127.0.0.1:5544/arb_memory \
ARB_MEMORY_REDIS_URL=redis://127.0.0.1:6379/15 \
PYTHONPATH=src \
scripts/arb-memory-seat-e2e
```

Useful overrides:

```bash
scripts/arb-memory-seat-e2e \
  --writer-target-id codex-bridge-dev-example \
  --reader-target-id agy-bridge-dev \
  --agent-env-file /Users/<user>/<workspace>/envs/agent-redis-bridge-dev.env \
  --from-agent-id claude-bridge-dev
```

## Cleanup

Each run uses a unique `arb-memory-seat-e2e-<uuid>` tag as source, author, artefact prefix, and Redis prefix.
The script asserts pre-test absence, deletes its sentinel rows before dispatching the real seats, and deletes
run-tagged hints, artefacts, idempotency keys, and Redis keys in a `finally` block.

If the script exits non-zero, rerun it after fixing the reported failure; cleanup is idempotent for the run tag
printed in the exception context or visible in the isolated Redis prefix.

## First live-run findings (2026-06-21) — what the real run surfaced that no unit test could

The first live runs were green only on the third attempt; each failure was a real cross-seat/real-env gate.
Captured here because this is how the path actually behaves, not how the components test:

1. **The CONSUMER needs the embed key; seats do NOT.** The consumer embeds on write (`arb_memory.embed` →
   OpenAI), so its env needs `OPENAI_API_KEY` — source `.env.arb-memory` before running. The seats run with
   `OPENAI_API_KEY` *unset* (the script unsets it); a seat that tries to embed locally is a bug. So:
   ```bash
   set -a; . ./.env.arb-memory; set +a   # OPENAI_API_KEY for the consumer
   ARB_MEMORY_DSN=… ARB_MEMORY_REDIS_URL=redis://127.0.0.1:6379/15 .venv/bin/python3 scripts/arb-memory-seat-e2e
   ```
2. **A seat's python needs `redis`.** The import-light client's *one* dependency is `redis`; a bare system
   `python3` in a seat env lacks it (`ModuleNotFoundError: No module named 'redis'` at `bus.py` import). The
   script's `--seat-python` defaults to the repo venv (`.venv/bin/python3`), which has it. **For a real
   deployment, each seat's environment must provide `redis`** — the seat-integration prerequisite the build
   had not addressed (the package was never importable in a lean seat env).
3. **Readiness races the consumer's group creation.** The consumer creates its group at `id="$"` (new-only,
   `mkstream`) on first boot; a sentinel written *before* the group exists is never delivered. The readiness
   probe therefore **retries** the sentinel write until one lands after the group is up. *Production
   corollary:* start the consumer (which creates the group) **before** seats write to a brand-new prefix —
   writes added before the group ever exists are lost (standard consumer-group semantics; a deploy-ordering
   rule, not a code bug).

**Verified green:** a real `codex-bridge-dev-example` seat wrote a unique marker via the bus and a different real
`agy-bridge-dev` seat recalled it `--transport-only` (through the reply lane, grep disabled) →
`{"ok": true, …}`. First proof of cross-seat ingest→recall.

## The INTERNAL canary (go-live) — this is a GATE, the twin of the external connector canary

**Green here is "the path works once, proven," NOT "the internal door is live."** The e2e proves the path
under a *controlled* harness; the test-scoped fixes have real-deployment twins still open:

- `--seat-python` defaulting to the repo venv is a **test** fix — the **deployment** requirement is **`redis`
  provisioned in every seat's python env** (the import-light client's one dependency). Documented, not done.
- "Start the consumer before first writes" is a documented **corollary**, not an enforced one.

So seat-memory is no more "live" than Phase 3 is "done" until its canary runs. The **internal canary** is:
*provision `redis` per seat + bring up the consumer first, then run real seat dispatch under production
conditions* (not this harness's venv/isolated-prefix). It is the internal twin of the connector canary in
`deploy/README.md`, and it is the one thing local validation structurally cannot prove. **Treat a failure as a
BLOCK on "seat-memory is live," not a post-hoc note.**

### Internal-canary observations (fill in at go-live — the only source of real seat-dispatch behaviour)

The gap between "proven once in the e2e harness" and "running under real seat dispatch in production" is
exactly where this slice found its three findings; assume production has its own. Capture them here **while
live**, the same way:

- **Seat env reality** — did each seat env actually have `redis`? any other missing dep / PYTHONPATH / bus
  reachability surprise once seats run from their real workdirs, not the venv? _(fill in)_
- **Consumer-first ordering** — any write lost to the boot-race because a seat wrote before the consumer's
  group existed? _(fill in)_
- **Concurrency** — multiple seats reading/writing at once: any per-seat reply mis-routing (the §3 "breaks
  only under concurrency" risk single-seat tests can't catch)? _(fill in)_
- **Anything that required a code/config change to make a real seat work** — fold it back through the gate, not
  as a hotfix.
