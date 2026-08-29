# Persistent-connection refactor (shell tooling) — DESIGN, execution DEFERRED

**Status:** DESIGN ONLY (2026-06-01). Execution deliberately deferred — see "When to
execute" / "Deferred acceptance test". Filed by Claude (project-g-laravel-dev) as bridge-repo
improvement #2 from an external review, after determining the benefit is **unmeasurable on
the current single-host non-TLS bus** and the change is **bootstrapping-risky** (it rewrites
the dispatcher used to run the review panel). The design is captured now, while the
dispatcher architecture is fresh; the rewrite happens against a TLS bus where its purpose is
observable.

## The gap

Every shell-side Redis op spawns a fresh `redis-cli` subprocess, each opening (and tearing
down) its own connection. On a **managed / TLS bus** every op therefore pays a full TLS
handshake. The bridge *daemon* already uses a persistent `redis-py` connection
(`src/agent_redis_bridge/redis_io.py`), but the shell tooling does not:

- `scripts/agent-dispatch` — LPUSH the envelope, then a **BLPOP-loop** (5s blocks) until the
  matching reply, RPUSH-ing non-matching messages back. **Worst offender:** one `redis-cli`
  (one handshake) PER POLL, for the whole `--timeout` window — a 90-minute impl dispatch can
  pay dozens-to-hundreds of handshakes for a single logical "dispatch and wait".
- `scripts/agent-bridge-ping` — `HGET` + `TTL` (2 subprocesses/handshakes per ping).
- `scripts/verify-bridge-supervision` — `GET` per poll.
- `scripts/agent-inbox-watcher*` and the peer-coordination `R()`/raw-`redis-cli` patterns
  (`docs/claude-peer-coordination.md`) — BLPOP loops, same shape.

The acute instance (per-token streaming writes) was fixed in `9da7761`; this is the
**generic** version of that bug class: "network I/O via subprocess-spawn in a path that runs
often." The acute fix removed the worst single case; the pattern is still everywhere.

## Why it isn't a cheap fix

Bash cannot hold a persistent connection across `redis-cli` invocations. Eliminating the
per-op handshake needs a **process that holds one connection for the duration of a logical
operation**. The fix is therefore a (small) Python helper, not a shell tweak.

## Design — a thin `redis-py` helper, one connection per logical op

Add a small client entrypoint (e.g. `python -m agent_redis_bridge.client` or
`scripts/agent-redis`) that:

1. Opens **one** `redis-py` connection, reusing the daemon's existing env-driven config
   (`RedisConfig` / `RedisCli` in `redis_io.py`) so TLS/ACL/prefix handling is identical to
   the daemon and there is one place that knows how to connect.
2. Performs a **whole logical operation** inside that one process, holding the connection for
   its duration. The shell scripts become thin wrappers that resolve env + shell out **once**:
   - `dispatch` — LPUSH envelope + the entire BLPOP-loop-until-matching-reply (with RPUSH of
     non-matching messages) in ONE process. **This is ~all the win**: one handshake per
     dispatch instead of one per 5s poll.
   - `ping` — registry HGET + status TTL in one process.
   - `get` / `hget` / `ttl` — generic reads for `verify-bridge-supervision`.
   - watch/stream ops for the inbox watchers.
3. Keeps output contracts byte-identical to today (the dispatcher's `task-id:` stderr line,
   the reply payload on stdout, exit codes) so callers and the harness don't change.
4. **Survives a long hold (a new failure mode the per-call path didn't have).** A dispatch
   can block up to the `--timeout` window (default 1800s, impl up to 5400s) on one connection
   — vulnerable to silent close by firewalls/NAT/managed-Redis idle timeouts, where the
   current `redis-cli`-per-poll path is inherently robust (a fresh connection each poll). The
   helper MUST set `socket_keepalive=True` AND reconnect-on-socket-error, **recomputing the
   remaining BLPOP timeout** across the reconnect so the total wait is preserved. This is the
   one place the refactor can *regress* reliability; treat it as a first-class requirement.
5. **Signal-safe message handling.** In the BLPOP-and-RPUSH loop there's a window where a
   non-matching message has been popped but not yet pushed back; a SIGTERM/SIGINT there loses
   it. The helper traps termination and RPUSHes any held envelope before exiting.

A long-lived client-side connection *daemon* is **out of scope** — per-invocation
one-connection already collapses the BLPOP-loop's N handshakes to 1, which is the dominant
cost. Don't build a pool for the client.

## INVARIANT — dual-path until verified on a TLS bus (hard requirement, not intention)

The conversion MUST ship able to run **both** paths, selected by a flag/env (e.g.
`AGENT_REDIS_CLIENT=cli|python`, default `cli`). Requirements, written so future-me / an
agent cannot shortcut them:

- R1. The legacy `redis-cli` path stays in the tree and runnable until the new path is
  **verified against a live TLS (managed) bus** — the same way `Restart=always` was made a
  fact verified against *both* kill signals, not a hope.
- R2. Default stays `cli` until R3 passes.
- R3. The `python` path is proven on a TLS bus: functional equivalence (identical
  dispatch/ping/get behaviour) AND the measured amortization below.
- R4. Only after R3 may the default flip to `python`; the `cli` path is retired only one
  release later (fallback window).
- R5. **The selector fails CLOSED to `cli`.** The `AGENT_REDIS_CLIENT` selector is itself new
  critical-path code; an unset/unknown value, a missing Python entrypoint, an import error, or
  any helper-startup failure MUST fall back to the `cli` path, never hard-fail a dispatch the
  `cli` path would have served. (R1–R4 govern *which* path runs; R5 pins the failure mode of
  the chooser.)
- R6. **Both paths are kept honest by the same tests.** Parameterize the functional suite to
  run against `cli` AND `python` against a local Redis, so the paths can't drift functionally
  during the (possibly long) deferral window.

## Bootstrapping-safety protocol (rewriting the dispatcher *through* the dispatcher)

`agent-dispatch` is the tool used to run the codex/agy review panel. Converting it risks
degrading the very mechanism that would catch a regression — the one increment whose safety
net is made of the same rope being cut. Protocol:

- B1. **Convert in risk order, dispatcher LAST:** `agent-bridge-ping` and
  `verify-bridge-supervision` (read-only, not review-critical) → the inbox watchers → the
  dispatcher's BLPOP-loop last.
- B2. **Review the dispatcher change with the panel dispatched via the OLD path** (flag off),
  so the review mechanism is never the thing under test while it's being changed.
- B3. **Prove the new dispatcher path out-of-band** before it is default: a handful of manual
  dispatches with `AGENT_REDIS_CLIENT=python` against a TLS bus, including the full
  LPUSH→BLPOP-loop→reply cycle and a timeout case.
- B4. **The new path must run the review panel itself successfully at least once** (dispatch
  the reviewers through the `python` path) before the merge that flips the default. The
  dispatcher proves itself on its own critical job before it owns it.
- B5. Keep the `cli` fallback one release; document the one-line revert (flip the env back).
- B6. **Exercise the fallback, don't assume it.** Per this repo's own kill-test precedent
  (R1 cites it: `Restart=always` was proven by injecting both signals, not by configuring it),
  after the `python` default lands, actually flip the selector back to `cli` and run one real
  dispatch through it — so B5's "one-line revert" is a *verified fact*, not a hope. A fallback
  you never exercised is exactly the failure mode R1 was written against.

## When to execute / Deferred acceptance test (hard trigger)

This refactor is a **performance** change; its purpose is amortizing the per-op TLS
handshake. On the current bus (`127.0.0.1:6390`, `AGENT_REDIS_TLS=0`, localhost) there is
**no handshake to amortize**, so there is **no signal on this host that the change did what
it exists for**. Shipping it here would be flying a performance refactor onto the critical
path with no available verification — exactly where that's least acceptable.

Therefore:

- **Trigger to execute:** a managed / TLS bus exists (the cross-host "Pattern E" topology in
  `docs/orchestrator-patterns.md` / README "Managed Redis/Valkey buses"). Execute there, not
  here.
- **Deferred ACCEPTANCE TEST (the definition of "complete"):** handshake amortization is
  **measured** against the TLS bus via the server-side `INFO stats` `total_connections_received`
  **delta across one representative dispatch**. Note the legacy count is up to **~2K**, not K:
  the wait loop spawns a fresh `redis-cli` for the `BLPOP` poll AND another for the
  `RPUSH`-readback of each non-matching message, so a dispatch resolved after K polls with
  interleaved traffic opens up to ~2K connections; the `python` path opens 1. **Measure on a
  quiesced / isolated bus** — `total_connections_received` is server-global, so heartbeats,
  other agents, and the daemon's `health_check_interval` pings pollute the delta on a shared
  bus; quiesce it (or scope the count to the dispatch) so the headline metric is clean. Record
  wall-clock before/after as a corroborating signal only (`redis-cli --latency` is a per-op
  latency baseline, NOT a connection count — don't use it as the count source). The increment
  is **not complete** until the ~2K→1 connection delta is demonstrated on a TLS connection.
- **Testable on THIS host (now):** *functional equivalence* — the `python` path produces
  identical dispatch/ping/get behaviour — unit-tested hermetically against a local Redis. The
  performance acceptance is deferred by construction; do not claim the refactor "done" off
  functional tests alone.
- **A verifiable-now, non-performance win (so "defer" ≠ "nothing here is worth doing"):** the
  helper consolidates the **duplicated, drift-prone env-resolution + flag-building blocks**
  copy-pasted across `agent-dispatch`, `agent-bridge-ping`, and `verify-bridge-supervision`
  (near-identical, independently maintained) into the one place that already knows how to
  connect (`RedisConfig`/`RedisCli`). That correctness/maintainability payoff is real and
  unit-testable **even at TLS=0** — it just isn't a reason to flip the dispatcher default
  ahead of the TLS-bus measurement. If a slice of this lands early, it's the *extraction*, not
  the dispatcher flip.

## Out of scope

- A long-lived client-side connection daemon / pool (per-invocation one-connection suffices).
- Any change to the bridge daemon (already persistent `redis-py`).
- Converting `docs/claude-peer-coordination.md`'s ad-hoc snippets — fold them into the helper
  opportunistically once it exists, not as a blocker.

## See also

- `9da7761` — the acute per-token-streaming fix (this is the generic version).
- `src/agent_redis_bridge/redis_io.py` — the daemon's `RedisConfig`/`RedisCli`; the helper
  reuses it so connection logic lives in one place.
- `docs/orchestrator-patterns.md` (Pattern E, cross-host) — the topology where the benefit is real.
