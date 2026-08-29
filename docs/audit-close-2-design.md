# AUDIT-CLOSE-2 — bus-driven verdict close (no per-orchestrator ssh)

**Status:** DESIGN (warm-Opus authored). Substrate + auth decided by Mark 2026-07-12.
**Builds on:** AUDIT-CLOSE-1 (`audit-close` verb + structural backstop, shipped + prod-live-gated).

## 1. Problem

AUDIT-CLOSE-1's close runs via `ssh arb-prod && docker compose exec audit python -m arb_memory
audit-close …`. That was a correct bootstrap (proved reconcile+emit against prod) but a bad product:
**every orchestrator host would need droplet ssh + knowledge, coupling orchestrators to the box.** The
close should travel the **bus** and be **consumed by arb-prod** — exactly like emit already does
(multi-emitter, single-writer). The orchestrator should need only bus access (which it already has for
votes) — no DSN, no ssh.

## 2. Decided architecture

- **Substrate:** a dedicated close stream, NOT ARB Messages. (Messages carries NaCl-sealed results +
  provider tokens + TOFU keys for *secret* delivery — irrelevant to a public, reconcile-gated close.)
- **Auth:** reconcile-gated, bus-open. Any fleet participant may *request* a close; `reconcile` refuses
  anything whose roster/stances don't match the recorded votes, so a spoofed/mistaken request cannot
  launder a verdict. Fits the mistakes-not-malice threat model; zero new auth surface.

### Flow

```
orchestrator (bus only)                         arb-prod close-consumer (writer DSN)
  publish arbmem:audit:close_request  ───────▶  XREADGROUP close_request
    {request_id, run_id, verdict, requested_by}   → close_core(run_id, verdict)   [= AUDIT-CLOSE-1 logic]
  BLPOP/poll arbmem:audit:close_result:<req_id> ◀─  write close_result:<req_id>
    {outcome, exit_code, gaps}                      {emitted|refused|different|orphaned|emit_failed, gaps}
```

## 3. Components

1. **Refactor `run_audit_close` → `close_core(conn, redis, run_id, payload, source) -> Result`.** Extract
   the reviewed+live-gated logic (reconcile → SETNX one-verdict claim → emit; exit 0/1/4/5/6; the stream
   verdict check) into a pure function returning a structured `Result {outcome, exit_code, gaps}`. The
   existing CLI becomes a thin wrapper: read `--payload-file` → `close_core` → map to exit code + stderr.
   **No logic change** — the CLI's committed exit-code tests must stay green, proving the refactor is
   faithful. The backstop (PG partial-unique + consumer deadletter) is untouched (it's on the persist
   path, orthogonal).

2. **Close-consumer** (arb-prod). A new one-shot-per-message consumer on `arbmem:audit:close_request`
   (consumer group `arbmem-audit-close`), running with the writer DSN. For each request: `close_core(...)`,
   then write `arbmem:audit:close_result:<request_id>` = `{outcome, exit_code, gaps}` (a key with a short
   TTL, or a 1-entry stream). No-silent-drop: infra errors (redis/pg blips) retry (no ack); a malformed
   request (bad JSON / missing fields) deadletters + acks (never poison-pill). Deployment: either a new
   compose service (`command: ["audit-close-consumer"]`, reuses the shared image) or fold into the audit
   consumer loop — prefer a separate service for isolation + independent restart.

3. **Orchestrator helper** (`scripts/arb-audit-close-request` or a client fn). Publishes the close_request
   with a minted `request_id`, then blocking-reads `close_result:<request_id>` with a timeout, and exits
   with the returned exit_code (so callers get the same 0/4/5/6 contract they'd get from the CLI — just
   over the bus). Timeout → distinct exit (consumer down / not deployed).

4. **Result channel** — `arbmem:audit:close_result:<request_id>`. Design the shape generically; the
   memory write **stored-vs-deduped** follow-up (BACKLOG) needs the same request_id→result pattern —
   build it once, reuse.

## 4. Keep as break-glass

The AUDIT-CLOSE-1 `ssh + docker exec audit-close` path stays as a manual fallback (e.g. consumer down).
Not removed.

## 5. Review + gate (same rigor as AUDIT-CLOSE-1)

- Panel focus: the refactor's faithfulness (CLI exit-code tests unchanged + green), the consumer's
  no-silent-drop / no-poison-pill on malformed requests, the result-channel race (result written before
  the orchestrator reads; TTL long enough), and the reconcile-gated/bus-open threat model.
- **Live gate (bus, against prod):** publish a close_request for a fresh disposable run → assert the
  close_result reports `emitted` AND a verdict row lands in prod `arbmemory`; publish a wrong-roster
  request → close_result reports `refused`/exit 4, no verdict. All **without ssh-ing to run the close** —
  that's the whole point.

## 6. Open (non-blocking) build questions for the panel

1. Result channel as a TTL'd key vs a short stream (stream gives redelivery/history; key is simpler).
2. Separate compose service vs folding into the audit consumer (isolation vs one-less-service).
3. `request_id` scheme + how the orchestrator dedupes/retries a lost request (idempotent by run_id +
   payload hash — the SETNX claim already makes a double-consume safe).
