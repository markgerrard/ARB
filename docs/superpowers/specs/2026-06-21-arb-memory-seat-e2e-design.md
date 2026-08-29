# ARB Memory — cross-seat ingest/recall e2e + seat-side handle [design]

**Status:** DESIGN (for the panel, then build). A follow-on to ARB Memory Phases 0–3 (merged to `dev`).
**Architecture of record:** `docs/decisions/arb-memory-architecture.md` §3 (transport), §5 (seats are the
high-frequency primary clients; auth = bus membership), §7 (no code path into memory except the bus).

## 0. Why this exists
Phases 0–3 built the store, the bus transport, the audit, and the external MCP door — but **never exercised
the seat path across real seats**, and there is **no seat-facing handle**: a dispatched codex/agy/M3 seat has
only the raw `arb_memory.bus.memory_write`/`memory_query` Python functions, no CLI/skill, and **no
`MemoryConsumer` runs on the bus**. So today no seat can actually ingest or recall a memory, even though the
transport is merged. This slice delivers (a) the minimal seat-side handle and (b) a **reproducible, committed**
cross-seat e2e that proves *seat A writes → consumer drains/embeds/inserts → seat B recalls* — the internal
twin of the external connector canary.

## 1. Scope — IS / IS NOT
**IS:** a thin **seat CLI** (`python -m arb_memory write` / `query`) that a dispatched seat invokes; a
**committed runbook + scripted e2e** that stands up the consumer and drives two real seats end-to-end;
proof the cross-seat path works on local infra (no DO/CF/providers).
**IS NOT:** no change to the store/bus/audit internals; no external MCP door (that's the canary); no
production deploy of the consumer (that's Phase 3 go-live); no orchestrator auto-wiring beyond the CLI + a
documented dispatch recipe.

## 2. The seat-side handle — a CLI (the missing primary surface)
`python -m arb_memory write` and `python -m arb_memory query`, added to the existing `run.py` arg dispatch
(which already has `memory|audit|mcp`):
- **`write`** — `--text <hint text>` (required), optional `--artefact-id` / `--artefact-content` /
  `--repo-pointer` / `--source` / `--author`. Emits a **write-intent onto the bus** via `bus.memory_write`
  (fire-and-forget) and prints the `ulid`. **Single-writer preserved:** the CLI is a write-intent *producer*,
  never a direct store writer — exactly like §5's rule; the one `MemoryConsumer` embeds + inserts.
- **`query`** — `--q <text>` (required), `--k <int>` (default 8), `--timeout-s <int>` (default 10). Calls
  `bus.memory_query` (XADD + BLPOP reply, **timeout→grep** fallback) and prints the hits as JSON.
- Reads bus connection from env (`ARB_MEMORY_REDIS_URL`), same as the consumer. **No embedding key needed on
  the seat** — the seat only touches the bus; the *consumer* owns the embed (single embedding owner, §7).

Rationale for CLI over a skill: a CLI is the smallest reproducible handle a dispatched seat (any engine) can
invoke in one shell line, and it's directly scriptable in the e2e. A skill (richer, orchestrator-loaded) is a
later affordance, not the minimal proof-of-path.

## 3. The reproducible e2e (committed, not /tmp)
`scripts/arb-memory-seat-e2e` (or `tests/arb_memory/test_seat_e2e.py` marked `@pytest.mark.e2e`) +
`docs/runbooks/arb-memory-seat-e2e.md`. Steps:
1. **Consumer up:** start `python -m arb_memory memory` against local PG (`ARB_MEMORY_DSN`) + a bus on a
   **dedicated prefix** so the e2e never collides with live fleet memory traffic. (Open: db 12 real keyspace
   vs an isolated db/prefix — §6.)
2. **Readiness gate:** wait until the consumer actually answers (a real round-trip), not just "process up"
   (§6 "readiness ≠ port-open").
3. **Seat A writes:** dispatch a **real bridge seat** (e.g. `codex-bridge-dev`) with a one-line task:
   `python -m arb_memory write --text "<unique marker>" --artefact-id <id> --artefact-content "<body>"`.
   Verify it returned a `ulid` (the seat actually produced the intent).
4. **Recall:** dispatch a **second, different seat** (e.g. `agy-bridge-dev`) with
   `python -m arb_memory query --q "<the unique marker>"` and assert the marker comes back in the hits — i.e.
   seat B recalled what seat A ingested, through the bus + consumer + store, with a real embedding.
5. **Teardown:** stop the consumer; clean the e2e's PG rows + bus keys (by the unique marker/prefix).
The script asserts each step and exits non-zero on any failure, so it's a real gate, re-runnable any time.

## 4. What it proves (vs what it doesn't)
**Proves:** the real distributed path — a real seat emits a write-intent over the bus, the single consumer
drains+embeds+inserts, a *different* real seat recalls it; the single-writer property (only the consumer
writes); timeout→grep is reachable; the seat CLI is a working handle.
**Does not prove:** the external MCP/OAuth door (the connector canary), nor production-deployed consumer
durability/reboot-survival (Phase 3 go-live).

## 5. Tests + review
- A unit test for the `write`/`query` CLI arg parsing + that `write` calls `bus.memory_write` (not a direct
  store write — the single-writer guard, mirrored from the MCP read-only guard).
- The scripted cross-seat e2e (§3) is the integration gate.
- **Review:** the 5-reviewer panel on this design, then on the build, before it's called done — same Workflow B
  discipline as Phases 0–3.

## 6. Opens for the panel
- **O1 (bus isolation):** run the e2e on db 12's real `agent_scratch:arbmem:*` keyspace (the true path, but
  pollutes live memory) with a unique marker + cleanup, OR an isolated db/prefix (clean, but one step removed
  from the real keyspace)? Which is the honest "cross-seat" proof?
- **O2 (real seats vs simulated):** must steps 3–4 dispatch *actual bridge seats* (true cross-seat, but needs
  the seats to have `arb_memory` importable + bus access in their env), or is a two-distinct-process client
  sufficient? If real seats can't import `arb_memory`, that itself is a finding (the seat env needs the
  package) — surface it, don't paper over it.
- **O3 (single-writer):** is the CLI `write` definitely a bus-intent producer with no direct-store path, and
  is that guarded by a test (like the MCP read-only role guard)?
- **O4 (consumer lifecycle in the script):** start/stop/readiness robustly so the e2e is reproducible and
  leaves no orphan consumer or polluted keyspace.
- **O5 (scope creep):** is the CLI the right minimal handle, or does proving the path pull in a skill/
  orchestrator-wiring that should be deferred?

---

## 7. v2 — folded the 5-reviewer panel (4/5 DESIGN-HOLES; cold-Opus+agy+codex+M3; GLM pending)

The panel turned "run a script" into a real build and caught two merged-code corrections. **The e2e's job is
to make simulation IMPOSSIBLE, not optional — build it so the easy way to write it is the real way.**

- **Import-light seat client (P0, all 4).** `run.py` imports `psycopg`+`mcp` at top level (optional extras,
  package `agent-redis-bridge`) → `python -m arb_memory write` crashes at import in a lean seat env. Build a
  **separate client module** (`src/arb_memory/client.py` + a thin `python -m arb_memory.client write|query`)
  that imports **only the bus (redis)** — no `psycopg`, no `mcp`, no embed. Move `run.py`'s server imports
  *inside* the runner functions. A seat needs only `redis` + `ARB_MEMORY_REDIS_URL`.
- **Bus isolation, env-configurable (P0, cold-Opus+agy+codex).** `bus.py` `PREFIX` becomes
  `os.environ.get("ARB_MEMORY_PREFIX", "")`. The e2e runs on an **isolated db/prefix passed to the consumer
  AND both seats** — NEVER the live `arbmem-memory` group on db 12 (a test consumer there *load-balances and
  steals production writes* — agy, verified mechanism). A separate, named, manual "real-keyspace canary" run
  is the only db-12 path, and it is opt-in.
- **Real 3-process e2e, fail-loud, dispatch pinned (P0, all 4).** Steps 3–4 dispatch **actual bridge seats**
  via `scripts/agent-dispatch --target-id <codex|agy>-bridge-dev …` with the seat env carrying `PYTHONPATH=src`
  + `ARB_MEMORY_REDIS_URL` + `ARB_MEMORY_PREFIX` (and **`OPENAI_API_KEY` UNSET** — seats must not embed). If a
  seat **can't import the client or reach the bus, the e2e FAILS LOUD** — that is the finding, never a silent
  fallback to an in-process/subprocess simulation. Writer seat ≠ consumer ≠ reader seat = three real OS
  processes; the consumer uses the **real embed** (`ARB_MEMORY_EMBED_MOCK` is for *offline unit* runs, not the
  cross-seat gate).
- **`timeout→grep` = CALLER-side discipline (Mark's ruling; merged-code correction).** It is NOT a mechanism
  in `arb_memory` (the layer doesn't hold what to grep — the repo is the seat's). `memory_query` correctly
  returns `None`; the **seat client owns the `None`→grep** behaviour (greps its own source), with a real test
  (query → `None` → demonstrably greps + finds). §3 corrected; `test_..._then_grep` renamed to
  `test_read_timeout_returns_none`.
- **MODE-EXCLUSIVITY (Mark — the sharpest constraint).** The grep fallback I'm adding is in *direct tension*
  with the e2e I'm hardening: grep can mask a broken bus. So the seat client `query` has **two structurally
  distinct modes** — production (`None`→grep) and **`--transport-only`** (grep DISABLED; asserts the marker
  returned **through the bus reply lane**, not grep, not a pre-existing store row). **The cross-seat e2e runs
  `--transport-only`** so a broken bus *cannot be rescued by grep*. Mutual exclusion is structural, not
  conventional (e.g. transport-only path has no grep code reachable).
- **Structural single-writer guard (P1, all).** An AST/import guard (mirror `test_embed_owner.py`): the seat
  client `write` path imports no `store`/`psycopg`/embed and only calls `bus.memory_write` (+ a monkeypatched-
  `bus.memory_write` test proving zero store writes).
- **Readiness via sentinel write (P1, codex).** The e2e injects a unique sentinel write on its prefix, waits
  for the consumer to persist it, cleans it, *then* runs the cross-seat step — not a port/process check, not a
  stale query.
- **Cleanup all related rows (P1, codex).** Run-specific `artefact_id`/`source`/`author` tag; a `finally`
  deletes artefacts + hints (+ idempotency) for that tag; assert **pre-test absence AND post-test absence** so
  residue can't pass a later run.

**Build order:** (1) env-configurable `bus.py` PREFIX; (2) import-light `client.py` + the entry; (3) seat
client `None`→grep + `--transport-only` mode-exclusivity + the structural write-guard test; (4) the renamed
timeout test + §3 correction (done); (5) the committed 3-process e2e (`scripts/arb-memory-seat-e2e` +
`docs/runbooks/`) with sentinel-readiness + fail-loud + cleanup; (6) run it for real. Then the build-panel.
