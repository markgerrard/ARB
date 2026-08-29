# CT-1 — claude-tail fail-loud design (v1.6, FINAL — design phase closed by Mark)

Status: FINAL. Six panel rounds (record below). Round 6: agy approve/none, grok
approve/P2, codex needs-changes/P1 (the hook-handoff crash window — folded into this
revision), GLM/cold-Opus votes recorded as they land. Mark closed the design phase
2026-07-11 (~04:30): "fold any remaining P0/P1 remediation into the spec and proceed" —
v1.6 folds codex's r6 P1 and grok's r6 P2, and the design proceeds to implementation
planning. No further design rounds.
Author: warm orchestrator (Claude, inline) — Anthropic lineage, so cold-Opus is
non-certifying; certify quorum = codex + pi-GLM + agy-print (GLM's seat is scoped to the
judgment tier and abstains on reliability — recorded, not silent shrinkage).
Decision forks were adjudicated by Mark on 2026-07-10/11; panels review soundness *within*
those decisions.

## Problem (BACKLOG § CT-1)

The orchestrator visibility tee (`scripts/claude-tail-daemon`, launchd
`com.example.claude-tail.bridge-dev`) zombied 2026-07-06 12:44 → 2026-07-10 21:37: the process
stayed alive (KeepAlive inert) while no orchestrator events reached the live bus for 4 days.
Discovered only by a human noticing "no orchestrators".

### Incident forensics (evidence, and what it does NOT establish)

- Last stderr write (`/tmp/claude-tail.bridge-dev.launchd.err`, 54MB, mtime frozen
  2026-07-06 12:44): the **eval tee's fail-soft handler** (`tailer.py:308`) logging
  `redis.exceptions.ConnectionError: ... Broken pipe` (TLS bus), preceded by a raw
  `BrokenPipeError` from `ssl.py sendall`. Then 4 days of total silence from a live pid.
- **Route order proves the live bus was healthy at the freeze moment**: `_route_event`
  emits live → trace → eval (`tailer.py:234-237`), so that event's live+trace XADDs had
  succeeded when eval failed (panel r1, cold-Opus).
- The frozen stderr therefore does NOT establish "loop hung" uniquely. Candidate readings,
  and which leg of this design covers each (panel r1, grok + cold-Opus):

  | Reading | Covered by |
  |---|---|
  | Loop hung in redis/file IO despite socket timeouts | B (watchdog) |
  | Main thread blocked INSIDE a logging write, holding the handler lock | B — and B's last-gasp must not touch logging (see §B) |
  | Live `RedisError` raised every tick, swallowed | A (crash-fast) |
  | Discovery silently empty (registry dark, transcript-exists skips) | C payload legibility (`tailers: 0` while a session runs) |
  | Loop alive, per-tailer failure every tick (e.g. corrupt offset) | A (self-heal) + C payload (`failing_tailers`) |
  | Genuinely quiet (no sessions) | C shows fresh + `tailers: 0` — correct, not dark |

  Weak prior toward a daemon-side wedge: a session was live at kickstart time and events
  flowed within seconds of restart, so the registry record existed while the old process
  was dark.
- Ops discriminator (unchanged): liveness = output freshness on the bus, never process
  existence or log mtime (a healthy-quiet daemon and a hung one have identical stderr).

### Current code, failure topology

- `__main__.py:13-18` `run_loop`: catches ALL tick exceptions, logs, continues — never exits.
- `service.py:105-114` per-tailer poll: `FileNotFoundError` → finish; **any other
  `Exception` → log + continue** (the designated swallow point).
- `tailer.py` emit path: `_emit_live`/`_emit_trace` raise raw `RedisError` upward;
  `_emit_eval` (`tailer.py:304-308`) fail-soft by design.
- `offset.py:21`: `int(value)` on the stored offset — a corrupt value raises `ValueError`
  every tick, swallowed at `service.py:112`: permanent per-tailer darkness that completes
  ticks (panel r1, codex).
- `tailer.py:119-120`: `json.loads` of a valid-but-non-dict line (`null`, `[]`, `"str"`)
  succeeds, then `obj.get(...)` raises `AttributeError` before the offset commits: the
  daemon re-reads the same line forever (panel r1, agy + grok convergent).

## Design principle

**The tee must never be silently dark.** Every failure mode either crashes the process
(launchd KeepAlive revives it) or is legible on the live bus — the heartbeat proves
*output-liveness* (what the loop is actually tailing and emitting), not mere process
liveness. Honest limitation: from inside, the daemon cannot distinguish "no sessions" from
"discovery dark" — the heartbeat's `tailers` count hands that discriminator to the human.

## Decision record (Mark, 2026-07-10/11 — do not re-litigate)

1. **Scope includes a self-watchdog.**
2. **Emit failure → crash immediately** (no in-process retry; KeepAlive +
   `ThrottleInterval 10` is the retry; offsets/registry are external so crash is
   near-lossless).
3. **Heartbeat on the live bus, written by the main loop, plus gateway surfacing in CT-1.**
4. **Rotation via in-process `RotatingFileHandler`.**

## A. Error classification — infra crashes, data skips, corruption self-heals

Imports first (panel r1, agy — a handler that references an unimported name `NameError`s
into the generic swallow): `service.py`, `__main__.py`, and `tailer.py` gain module-level
`from redis.exceptions import RedisError`. The daemon hard-depends on redis; the import is
free.

- **Infra (any `RedisError`)** raised anywhere in the tick path — live/trace emit, offset
  get/commit, registry read/prune, heartbeat write, **and every draining-record op
  (write/read/delete/prune/TTL-refresh)** (panel r6, cold-Opus P2-1: the natural prune
  implementation would otherwise mirror the swallowing idiom at `service.py:250-259`) —
  propagates out of `Service.tick()` and out of `run_loop`: process exits nonzero.
  Concretely:
  - `service.py:112` handler: `except RedisError: raise` ahead of the generic handler.
  - `run_loop`: `except RedisError: log; raise` — all other exceptions keep
    log-and-continue. (Normal exception propagation exits through the interpreter, which
    flushes logging handlers — no special flush handling needed on this path.)
  - Registry-prune (`service.py:258-259`) and every other `except Exception` on the tick
    path: same split.
- **Data errors — skip the line, never spin; but the skip is scoped to the PARSE stage
  ONLY** (panel r1, agy + grok; re-scoped after panel r2, agy P0: v1.1's "any
  non-`RedisError` from `_process_line`" would have reclassified emit-path CODE BUGS as
  data errors — a `NameError`/`AttributeError` in `_route_event`/enrich/redact would skip
  the line, advance the offset, and silently discard events forever under a green
  heartbeat). Line processing splits into two stages with different failure semantics:
  - **Parse/map stage** (bytes decode, `json.loads`, `isinstance(obj, dict)` validation,
    marker capture, `map_line` → events): ANY exception here → skip the line (offset
    advances past it — `new_offset` is already `fh.tell()`), increment the tailer's
    cumulative `skipped_lines` counter, one log record with **path + offset ONLY — never
    the line bytes** (transcript content; panel r1, GLM guardrail G1), continue.
  - **Emit stage** (`lifecycle`, `_route_event` and everything under it): `RedisError` →
    propagate (infra crashes). Any OTHER exception → **first commit the offset up to the
    end of the last FULLY-emitted line**, then propagate to the per-tailer handler in
    `Service.tick`, which logs and marks the tailer failing (feeds `failing_tailers`, §C).
    The prefix-commit is load-bearing (panel r3, cold-Opus P1): without it, lines
    O..N-1 of the chunk re-emit EVERY tick while line N keeps failing — up to 500
    duplicate events/tick flooding `events:live` (maxlen 10000), evicting other
    orchestrators' events and keeping `last_emit_at` fresh, which falsifies §C's
    discriminator. With it, the re-attempt is bounded to the single failing line: a
    deterministic emit-path bug is a visible one-line spin (`failing_tailers > 0`,
    stalled `last_emit_at`), other tailers progress, and the one-time duplicates of the
    failing line's own earlier events remain at-least-once (panel r2, grok P2).
  - **`DriftError` is NOT a parse-skip** (panel r3, grok P1 + cold-Opus P2): it keeps its
    existing dedicated arm exactly (`drift_count`, `_emit_drift_error`, threshold →
    `_DriftThresholdExceeded` with its commit semantics). Sweeping it into the skip would
    silently kill the drift signal. Its `drift_error` EMISSION follows emit-stage rules:
    a `RedisError` raised while emitting it propagates (never classified as a skip), so
    the emission must not sit inside the parse-stage try. Implementation pin (panel r4,
    agy P2): the dedicated `except DriftError` arm must be ORDERED BEFORE the generic
    parse-skip arm regardless of `DriftError`'s base class — a generic
    `except Exception` first would swallow it.
  - **Non-ENOENT filesystem errors** (`PermissionError`, `EIO`, any `OSError` from
    `os.stat`/`open`/reads other than `FileNotFoundError`): mark the tailer failing —
    sticky until a clean poll — and continue; no offset movement (panel r3, codex P1 +
    grok P2). A permanently unreadable transcript is a visible `failing_tailers > 0`,
    not silence. `FileNotFoundError` keeps its finish semantics.
  - **`failing_tailers` semantics (unified):** ANY per-tailer exception path that does
    not finish the tailer marks it failing for that tick, sticky until a poll completes
    cleanly (panel r3, grok P2).
- **Chunked, time-budgeted polls — catch-up can never outrun the watchdog** (panel r2,
  grok P0: `poll()` currently commits the offset only after reading to EOF
  (`tailer.py:104-105`); a large catch-up backlog — exactly the post-incident state, and
  what the offset self-heal below deliberately creates — replayed with per-event TLS
  XADDs can exceed the 300s watchdog, which `os._exit`s mid-poll with ZERO committed
  progress; the respawn replays the same backlog and dies again: a zero-progress death
  loop under a fresh-looking start-beat. Host evidence: transcripts up to ~59MB/~23k
  lines exist). Fix: each `poll()` call processes at most a budget — default
  **500 lines or 2.0 seconds wall-clock, whichever first** (`ARB_CLAUDE_TAIL_POLL_BUDGET_LINES`
  / `_SECS`) — then **commits the offset and returns**, resuming next tick. Budget
  granularity (panel r3, codex P0 — a per-line check between lines does not bound a
  single line's event fan-out, and per-poll budgets do not bound the tick):
  - The wall-clock check runs **between individual event emissions**, not only between
    lines. If the budget expires mid-line, the current line's remaining events finish
    (offsets are line-granular; a partial line must never commit), then the chunk commits
    and returns. Worst-case overshoot = one line's residual fan-out; a single line whose
    own emission exceeds the watchdog is the accepted pathological residual (stated, not
    silently possible — it would watchdog-crash loudly; panel r4, cold-Opus additionally
    traced `mapper.py`'s per-line event bounds and assessed this residual as unreachable
    in practice).
  - **A shared tick deadline** (default 30s, `ARB_CLAUDE_TAIL_TICK_DEADLINE_SECS`) bounds
    `Service.tick` across N tailers: tailers are polled round-robin with a persistent
    cursor, and the tick returns when the deadline expires, resuming with the next tailer
    on the next tick. Worst-case tick ≈ deadline + one line's fan-out ≪ the 300s
    watchdog; no tailer starves (panel r3, grok P2 — the doc previously implied
    "tick ≤ 2s", false for N tailers).
  - **`poll()` reports EOF**: it returns `(emitted, at_eof)` (or sets `tailer.at_eof`),
    where `at_eof` means "no more COMPLETE lines were available at this call" — i.e. the
    read position reached file size, or the only remainder is a non-newline-terminated
    partial tail (panel r4, grok pin: without the partial-tail clause, a torn final line
    would block a completed seat's finish forever). Every lifecycle decision in
    `Service.tick` that previously assumed "one poll = caught up" now gates on it — see
    "Finish paths" below (panel r3 convergent P0).
  Consequences, all load-bearing: ticks stay short by construction (watchdog fires only
  on genuine hangs, not honest catch-up); offsets are durable per chunk (a kill
  mid-catch-up loses ≤1 chunk); heartbeats beat between ticks so catch-up is visibly
  alive (resolves panel r2, grok P1).
- **Finish paths gate on `at_eof` — chunking made "finished" ≠ "drained" possible**
  (panel r3, convergent P0: agy + grok + cold-Opus; the comment at `service.py:122-124`
  documents the exact invariant chunking voids). All four finish/abandon paths in
  `Service.tick` change:
  - **Cold sidecar `completed:true`** (`service.py:121-129`): finish + delete files ONLY
    when the triggering poll returned `at_eof`. Otherwise keep draining chunks on
    subsequent ticks; the sidecar check re-fires once drained. Without this, a backlog
    > one chunk is silently discarded AND the transcript deleted under a green heartbeat.
  - **In-band `[ARB_SEAT_DONE]`** (`service.py:116-119`): same `at_eof` gate.
  - **Warm deregister** (key drops out of `live_keys`, `service.py:89-93`): do not
    finish-and-forget mid-backlog — drain to `at_eof`, then finish (panel r3, cold-Opus —
    the warm analogue). **The draining state is DURABLE and part of discovery, not
    special in-memory state** (panel r4, agy P1 + codex P1 — two faces of one gap: the
    poll loop iterates `live_keys` only, so a memory-only draining tailer is never polled
    again; and a crash mid-drain loses the registry record, so a fresh process cannot
    rediscover the transcript). Mechanism: a draining record at
    `{prefix}claude:draining:{session_id}` on the LOCAL bus (same store as offsets) is
    written UNCONDITIONALLY at deregister — by the SessionEnd hook first, and by the
    daemon as fallback when it observes a warm key disappear; see Record lifecycle below
    (panel r6, grok P2: an earlier version of this sentence gated the write on prior-tick
    `at_eof`, contradicting the lifecycle rule);
    `_discover_specs` unions registry records with draining records, so draining tailers
    stay in the polled set (in-process) and are rediscovered after a restart (offsets
    were durable all along — the draining record supplies the path+identity).
    **Record lifecycle (panel r5, convergent across all seats; hook handoff panel r6,
    codex P1):**
    - **The handoff is durable at the SOURCE**: the SessionEnd hook
      (`scripts/claude_tail_hooks/session_end.py`) writes the draining record BEFORE
      removing the registry record (write-then-delete ordering; both on the same local
      bus the hook already uses). Without this, the daemon-observation path alone has a
      crash window — hook deletes the registry field, daemon dies before its next tick
      observes the disappearance, restart finds NEITHER record and the final backlog is
      silently lost (panel r6, codex P1, verified against `session_end.py:11-15`). A
      crash between the hook's two steps leaves both records present, which degrades to
      the flap rule (registry supersedes) — safe. The daemon-side write below remains as
      the fallback for non-hook registry removals.
    - The daemon also writes the record **unconditionally when it observes a warm key
      disappear** (fallback path) — never gated on the previous tick's `at_eof`, which
      is stale by one tick: a session that reaches EOF, writes a final burst, then
      deregisters within one tick interval would otherwise have that burst
      finish-and-forgotten (panel r5, cold-Opus P2-1; panel r6 grok P2 caught an earlier
      sentence contradicting this — the rule is UNCONDITIONAL, here and everywhere). A
      fresh poll's `at_eof` gates the finish; the extra record write/delete cycle for
      already-drained sessions is accepted.
    - The record is deleted on **ANY terminal finish of the draining tailer** — the
      `at_eof` finish, the `FileNotFoundError` finish, and flap-supersede — not only at
      `at_eof` (panel r5, codex + agy + grok P1 / cold-Opus P2-2: with `at_eof`-only
      deletion, a transcript removed mid-drain either leaks the record forever or
      re-creates a tailer every tick that re-emits `task_finished` under a green
      heartbeat).
    - `_discover_specs` extends the registry path's **transcript-existence check +
      prune** (`service.py:147-150` idiom) to the draining union: a draining record
      whose transcript is missing is pruned, with one finish, no per-tick churn.
    - Belt-and-braces: the record carries a **7-day TTL** (refreshed while draining) so
      no unforeseen path can leak it indefinitely — retention semantics, mirroring the
      heartbeat key; deterministic cleanup is the prune + delete-on-finish above.
    If the session re-registers while draining (flap), the registry record supersedes:
    same `session_id` → same tailer key → one tailer, and the draining record is dropped
    (panel r4, cold-Opus P2).
  - **Idle-finish**: requires `at_eof` in addition to the idle window, and
    `last_activity` bumps on OFFSET PROGRESS or real emitted events — a chunked
    catch-up through event-less lines is activity, not idleness (panel r3, agy P1:
    otherwise a 200k-line zero-event backlog idle-finishes mid-file and never resumes).
    Synthetic `task_continuing` heartbeat emissions do NOT count as activity — they would
    otherwise defeat idle-finish entirely (panel r4, cold-Opus P2).
- **Offset corruption self-heals** (panel r1, codex): `OffsetStore.get` catches
  `ValueError`/`TypeError` from `int(value)`, logs, resets the stored offset to 0, and
  returns 0. The tailer re-reads the transcript from the top — duplicate events
  (at-least-once) instead of permanent silent darkness. `expire`/`set` failures there are
  `RedisError` → crash, per the infra rule.
- **Eval tee unchanged** (fail-soft): optional adjunct plane.
- **`FileNotFoundError` → finish-tailer** unchanged.

## B. Watchdog — covers the hang modes

- Main loop stores `last_tick_completed = time.monotonic()` after every completed tick.
  **Initialization** (panel r1, grok — unspecified init is an immediate-fire footgun):
  set to `time.monotonic()` immediately before the watchdog thread starts; a test pins
  "no fire during the first threshold window with zero completed ticks".
- A `threading.Thread(daemon=True)` wakes every ~15s; on
  `monotonic() - last_tick_completed > threshold`:
  1. one **raw `os.write(2, <pre-encoded bytes>)`** — the last gasp goes straight to the
     launchd stderr fd. It must NOT touch the logging framework: if the main thread is
     hung inside a logging write holding the handler lock (a live candidate for the actual
     incident), a logging call here deadlocks the watchdog behind the same lock and the
     process never exits (panel r1, cold-Opus P1). No flush concerns either — the write is
     unbuffered. **The write is wrapped in `try/except Exception` so `os._exit` ALWAYS
     runs** — an EBADF on a detached stderr must not kill the watchdog thread before the
     exit (panel r2, agy P2). Blocked-pipe risk noted and accepted: the plist routes
     stderr to a FILE (`StandardErrorPath`), not a pipe, so a blocking `os.write(2, ...)`
     is not a live mode on this deployment.
  2. `os._exit(86)` — bypasses the hung main thread; nonzero → KeepAlive revives.
- Threshold: env `ARB_CLAUDE_TAIL_WATCHDOG_SECS`, default 300; **effective threshold =
  max(configured, 3 × tick interval + 60s)**, with a startup log line when raised —
  otherwise `ARB_CLAUDE_TAIL_INTERVAL_SECS=360` would false-positive-crash a healthy
  sleeping daemon (panel r1, agy). `0` disables (tests).
- The watchdog does time arithmetic, one raw write, and exit — no Redis, no locks, no
  logging, no allocation-heavy work.
- Coverage note: a loop that *completes* ticks while dark is invisible to the watchdog by
  design — those modes are covered by A (no swallow, self-heal) and C (payload legibility).

## C. Heartbeat + gateway surfacing — output-liveness, not process-liveness

Panel r1 (cold-Opus P1, codex P1, grok P1) reshaped this leg: a bare alive-beat is
false-green over discovery-empty and per-tailer darkness, a 90s TTL erases the evidence the
gateway needs to say "dead since", and TTL(90) vs watchdog(300) leaves a false-stale window
for legitimately long ticks.

- **Writer:** the main loop (never a helper thread) SETs
  `{AGENT_REDIS_PREFIX}tail:heartbeat:{label}` on the **live bus**, at **tick start AND
  tick end**, throttled to one write per 10s. A long-but-progressing tick keeps the key
  fresh via its start-beat; a hung tick stops beating and the watchdog bounds the darkness
  at ~300s.
- **Value (all fields structural — any future free-text field must route through
  `redact()`; the raw `SET` bypasses the `live_tee` redaction boundary; panel r1, GLM
  guardrail G2):**
  ```json
  {"ts": iso-utc, "pid": int, "started_at": iso-utc,
   "tailers": int, "failing_tailers": int, "skipped_lines": int,
   "last_emit_at": iso-utc-or-null, "stale_after_s": int}
  ```
  `tailers` = live tailer count this tick; `failing_tailers` = tailers marked failing
  per the unified sticky semantics in §A — fed from tailer-level failure state, not only
  exceptions that reach `Service.tick`, so per-line failures are not invisible (panel r2,
  agy P1); `skipped_lines` = cumulative parse-stage skips since process start (monotonic
  per-process; resets on respawn — panel r4, cold-Opus P2); `last_emit_at` = wall time of
  the last successful live emit; `stale_after_s` = the daemon's own effective watchdog
  threshold + 30s margin, so the gateway derives staleness from the daemon's REAL config
  instead of a hardcoded 330s — otherwise a legitimately slow-interval config (which §B's
  floor explicitly permits) gets false-staled (panel r2, cold-Opus P2-1). This is what
  makes darkness legible: "fresh heartbeat, `tailers: 0`" while you're running a session =
  discovery is dark; "fresh, `failing_tailers > 0`" = a tailer is stuck; growing
  `skipped_lines` = parse trouble (panel r1, cold-Opus + codex + grok; panel r2, agy).
  **`failing_tailers` is the PRIMARY stuck-signal**: under an emit-failure spin,
  `last_emit_at` can keep moving (the failing line's earlier events re-emit each
  attempt), so it must not be read as the stuck-discriminator on its own (panel r4, grok
  wording pin).
- **TTL = 7 days (retention, NOT the staleness signal).** Staleness is judged from `ts`
  age with threshold **330s** (watchdog 300 + margin): the guarantee this system can
  honestly make is "a dark tee is visible within ~5.5 minutes", not 90s. A dead tee's key
  persists, so the gateway renders **"tee stale since <ts>"** from the last value instead
  of losing the evidence at TTL expiry ("missing" then only means never-ran/&gt;7d, which
  is fair; panel r1, codex). Clock note: `ts` is compared against the gateway's clock; both
  hosts are NTP-disciplined and the 330s threshold dwarfs NTP drift — do not use
  TTL-remaining as freshness, it now means retention (panel r1, agy P2 resolved by the
  threshold margin).
- **Heartbeat write failure** is a `RedisError` → crash (A).
- **Gateway** (`src/arb_memory/visibility.py`): extend the **`/orchestrators`** response —
  already behind `request_authenticated` (panel r1, GLM guardrail G3) — with
  `tees: [{label, ts, pid, started_at, tailers, failing_tailers, skipped_lines,
  last_emit_at, state}]`, `state ∈ fresh|stale|missing`, where "stale" means `ts` age
  exceeds the payload's own `stale_after_s` (self-describing; panel r2, cold-Opus P2-1).
  **The tee roster is deployment-configured, not discovered**: env
  `ARB_VIS_EXPECTED_TEES` (comma-separated labels, e.g. `claude-tail.bridge-dev`); the
  gateway MGETs exactly those keys. This kills two round-2 findings with one mechanism: a
  tee that never started (or expired past 7d) renders **`missing`** because the expected
  label has no key — SCAN could never say that, since it only enumerates keys that exist
  (panel r2, codex P1) — and there is no O(keyspace) `scan_iter` walk at all (panel r2,
  agy P2 + cold-Opus P2-3). An unconfigured-but-beating tee is not rendered; the env list
  IS the roster. The MGET runs in the same threadpool-offload pattern the app already
  uses for blocking bus reads (panel r1, cold-Opus P2). UI: a header chip — "tee stale
  since <ts>" / "tee missing" / "tee: 0 tailers" states.
  **Roster-empty is loud, not inert** (panel r3, grok P1: an unset/empty
  `ARB_VIS_EXPECTED_TEES` would make the whole leg observationally identical to
  "feature off"): the gateway logs a startup WARNING when the list is empty, and the
  deploy live gate asserts the configured label actually renders (`fresh`, or `missing`
  pre-start) — a mis-deploy fails the gate instead of silently disabling discovery.
  **Coupling note** (panel r3, cold-Opus P2): the daemon's heartbeat writer and the
  gateway's MGET must resolve the SAME live bus and `{prefix}` — both take them from
  the shared env contract (`ARB_LIVE_REDIS_URL` + `AGENT_REDIS_PREFIX`); the live gate's
  render assertion is the end-to-end proof of that coupling.
- **Visibility SLA (documented honestly):** after a watchdog kill, the last start-beat's
  `ts` can read "fresh" for up to ~30s past the kill (the staleness margin is one-sided);
  combined with the watchdog bound, the guarantee is **"a dark tee is visible within
  ~effective-threshold + margin"** (~5.5 min at defaults), never 90s (panel r2, grok P2 —
  accepted, stated).
- Rate: ≤0.2 op/s — nowhere near the managed-bus backpressure regime.

## D. Log rotation

- Daemon startup configures the bridge logger with
  `logging.handlers.RotatingFileHandler`: default `~/Library/Logs/claude-tail/<label>.log`,
  `maxBytes=5MB`, `backupCount=3`, env `ARB_CLAUDE_TAIL_LOG_FILE`. Startup creates the
  log directory first (`os.makedirs(..., exist_ok=True)`) — otherwise a missing directory
  crash-loops the daemon at boot (panel r3, agy P2).
- launchd `StandardErrorPath` becomes crash-only: uncaught tracebacks (crash-fast exits)
  and the watchdog's raw last-gasp line. Worst case during a day-long flap ≈ 17MB of
  respawn tracebacks — accepted.
- One-time deploy cleanup of the existing 54MB stderr file.

## E. Testing + live gate

Hermetic TDD (targeted files only):

1. `RedisError` from `live_tee` propagates out of `Service.tick`. **Deny-proof:** delete
   the re-raise → red.
2. `RedisError` from offset commit / registry read / registry prune propagates likewise.
3. **`run_loop` re-raises `RedisError`** (fake service raising it → `run_loop` raises;
   other exceptions still swallowed). **Deny-proof:** restore the blanket swallow → red.
   Binds the two layers so a service-only fix can't go green while the process still never
   exits (panel r1, grok).
4. Stage classification (panel r2, agy P0): PARSE-stage failures (non-dict JSON `null`/
   `[]`/`"str"`, invalid JSON, decode errors, `map_line` raises) → line skipped, offset
   advances, `skipped_lines` increments, ONE log record containing path+offset and NOT
   the line bytes, next good line emits, no re-read next poll. EMIT-stage non-`RedisError`
   (injected bug in `_route_event`) → propagates to the per-tailer handler, offset does
   NOT advance, tailer marked failing, other tailers unaffected. **Deny-proof:** widen the
   skip to cover the emit stage → the emit-bug test goes red. `RedisError` mid-line
   (either stage) → offset does NOT advance, propagates out of `tick`.
5. Offset corruption: corrupt stored value → reset to 0, re-read from top, no raise
   (panel r1, codex).
5b. Chunked poll (panel r2, grok P0): a backlog exceeding the line budget is processed
   across multiple `poll()` calls with the offset committed per chunk and monotonically
   advancing; a simulated kill between chunks resumes from the last chunk boundary (no
   re-read of committed work); the wall-clock budget is honored with a slow fake emitter
   AND checked between events — a single high-fan-out line finishes its events, commits,
   returns (panel r3, codex P0). **Deny-proof:** remove the per-chunk commit → the
   kill-resume test goes red.
5c. `failing_tailers` population (panel r2, agy P1; unified semantics panel r3): emit-stage
   per-line failure, poll-entry `OSError` (`PermissionError`), and service-level catches
   all mark the tailer failing (sticky until a clean poll) in the heartbeat payload.
5d. EOF-gated finish (panel r3, convergent P0): backlog > one chunk + sidecar
   `completed:true` after the first chunk → tailer is NOT finished, files NOT deleted;
   subsequent ticks drain to EOF, THEN finish + delete. Same for in-band
   `[ARB_SEAT_DONE]` and warm deregister (drain-then-finish). **Deny-proof:** drop the
   `at_eof` gate → this test goes red. The fake tailer used here MUST model partial
   catch-up (the round-3 hole survived precisely because the existing fake could not;
   panel r3, grok — fixture-supplies-what-code-lacks class).
5e. Idle-finish during catch-up (panel r3, agy P1): a zero-event multi-chunk backlog does
   not idle-finish mid-file — offset progress counts as activity; idle-finish fires only
   at `at_eof` + idle window.
5f. Emit-failure prefix commit (panel r3, cold-Opus P1): chunk with lines O..N-1 emitting
   cleanly and line N failing → offset commits to end of N-1 before the failure
   propagates; next tick re-attempts ONLY line N. **Deny-proof:** remove the prefix
   commit → the flood assertion (≤1 line re-emitted per tick) goes red.
5g. Tick deadline (panel r3, codex P0 + grok P2): N slow tailers → tick returns at the
   deadline with a persistent round-robin cursor; every tailer progresses across ticks;
   no starvation.
5h. `DriftError` unchanged (panel r3, grok P1): unknown line types still emit
   `drift_error`, still count toward the threshold, are NOT skipped as parse errors; a
   `RedisError` during drift-event emission propagates; the dedicated arm precedes the
   generic parse-skip arm (panel r4, agy P2 — arm-ordering deny-proof: swap the arms →
   drift tests go red).
5i. Durable warm drain (panel r4, agy P1 + codex P1; lifecycle panel r5): (a) registry
   key removed mid-backlog → draining record written, tailer REMAINS in the polled set,
   drains to EOF across ticks, finishes, record deleted; (b) simulated restart between
   drain chunks → fresh service rediscovers the transcript from the draining record,
   resumes at the committed offset, drains, finishes; (c) re-register during drain →
   single tailer, draining record dropped; (d) transcript deleted mid-drain → record
   pruned/deleted, exactly ONE `task_finished`, no per-tick tailer re-creation churn
   (panel r5 convergent); (e) session reaches EOF, appends a final burst, deregisters
   within one tick → the burst is drained BEFORE the finish (record written
   unconditionally; panel r5, cold-Opus P2-1); (f) SessionEnd hook removes the registry
   record and the daemon dies BEFORE observing it → fresh service rediscovers via the
   hook-written draining record and drains the final burst (panel r6, codex P1).
   **Deny-proofs:** make the draining state memory-only → (b) red; scope record deletion
   back to at_eof-only → (d) red; revert the hook to delete-only → (f) red.
5j. `at_eof` with a torn final line (panel r4, grok pin): a non-newline-terminated tail
   with `completed:true` still finishes (partial tail counts as EOF); a complete line
   appended later is still read (no premature offset past it).
5k. Idle-finish activity source: synthetic `task_continuing` does not bump
   `last_activity`; offset progress and real events do (panel r4, cold-Opus P2).
6. Watchdog: fake clock + injected exit/write fns — fires at threshold; no fire below; no
   fire in the first window with zero ticks (init pinned); effective-threshold floor vs
   interval (360s interval → no false fire); last-gasp path calls raw-write then exit and
   never calls into `logging` (assert no handler interaction — non-vacuous per panel r1,
   cold-Opus).
7. Heartbeat: start+end beats, 10s throttle, payload shape (**all eight fields,
   explicitly listed** — panel r3, grok P2 caught the doc saying "six"), `tailers`/
   `failing_tailers`/`skipped_lines` reflect injected states, write failure raises.
8. Gateway: `tees` reduction fresh/stale/missing against a fake redis — staleness at the
   payload's `stale_after_s` boundary (not a constant); a configured-but-absent label
   renders `missing`; an unconfigured key is not rendered; payload only on authenticated
   routes.
9. Logging: rotating handler configured from env.

Live gate at deploy:

1. Kickstart the real seat → heartbeat fresh on the live bus within 10s, payload shows
   `tailers ≥ 1` with a session running; gateway chip renders the configured label
   (`fresh`) — this arm doubles as the empty-roster mis-deploy check and the
   daemon/gateway bus+prefix coupling proof (panel r3, grok P1 + cold-Opus P2).
2. Throwaway instance pointed at a black-hole `ARB_LIVE_REDIS_URL` → exits nonzero within
   seconds. (Known limitation: this arm exercises connect-failure, not a
   broken-established-pipe; the established-conn case is covered hermetically by tests 1-3;
   panel r1, cold-Opus P2 — accepted.)
3. Orchestrator events flow for a live session (existing check).
4. Rotating log receives records; launchd stderr stays quiet.

## Out of scope

- Bridge seats' own tee paths / heartbeats.
- In-process emit retry (decided against).
- Other daemons' log rotation.
- A general external oracle for "sessions expected but registry empty" (the `tailers`
  count + human is the CT-1 answer; a hook-side cross-check could be a future ticket).

## Deploy

Runs from the <workspace> clone (plist → `/Users/<user>/<workspace>/...`). After merge: plist
gains the heartbeat-label env (or accept hostname default) — plist changes need
bootout+bootstrap, not kickstart; the gateway deployment gains `ARB_VIS_EXPECTED_TEES`;
one-time stderr cleanup; kickstart; live gate.

## Panel record

- Round 1 `panel-ct1design-20260711T032251Z-f6f2a6`: needs-changes (codex P1×2, agy P1×3
  + P2×2, grok P1×5 + P2×4 advisory, cold-Opus P1×2 + P2×5 advisory, GLM abstain —
  judgment-tier ORACLE-CLEAN with three guardrails adopted as spec constraints). All
  verdicts drained + reconciled in dev PG. Every P1's hinge claim was reality-checked
  against the code before adoption (offset.py int(), missing redis imports, tailer.py
  non-dict path, watchdog arithmetic).
- Round 2 `panel-ct1design-r2-20260711T033620Z-48f6b0`: needs-changes. All r1 resolutions
  confirmed sound in isolation (grok's resolution table; cold-Opus approve/P2 advisory
  after re-verifying every r1 hinge), but two NEW composition P0s: agy — v1.1's skip
  scope reclassified emit-path code bugs as data errors (silent discard under green
  heartbeat), plus `failing_tailers` bypass; grok — watchdog × end-only offset commit ×
  catch-up backlog = zero-progress death loop, made worse by the offset self-heal
  (verified against `tailer.py:104-105` + 59MB host transcripts). codex P1: SCAN cannot
  render `missing` for a never-started tee. Resolved in v1.2 by: parse-stage-scoped skip
  with emit-stage propagation, chunked/budgeted polls with per-chunk commits,
  tailer-state-fed `failing_tailers`, expected-labels MGET roster, `stale_after_s`
  self-description, guarded last-gasp write, stated visibility SLA. GLM: ORACLE-CLEAN,
  declared non-vote recorded as abstain. Drained + reconciled in dev PG.
- Round 3 `panel-ct1design-r3-20260711T034716Z-27fcdb`: needs-changes. r2 resolutions
  hold; the chunking fix itself opened new composition holes. **Convergent P0 (4/4
  reliability seats — agy, grok, cold-Opus explicitly; codex adjacent):** every finish
  path assumes "one poll = EOF" (`service.py:122-124`'s own comment), so a
  budget-limited poll + `completed:true` sidecar finishes AND DELETES an undrained
  transcript (warm-deregister analogue included). codex P0: budget neither tick-global
  (N×2s) nor event-granular (single-line fan-out). P1s: idle-finish on `emitted==0`
  during catch-up (agy); `DriftError` swept into parse-skip (grok); empty
  `ARB_VIS_EXPECTED_TEES` silently inert (grok); emit-failure re-emits the chunk prefix
  every tick, flooding `events:live` and falsifying `last_emit_at` (cold-Opus). GLM:
  ORACLE-CLEAN on the new surfaces, abstain. Resolved in v1.3 by: `at_eof`-gated finish
  on all four paths (drain-then-finish), progress-counts-as-activity idle rule,
  between-event budget checks + shared 30s tick deadline with round-robin cursor,
  emit-failure prefix commit (re-attempt bounded to one line), `DriftError` carve-out,
  non-ENOENT `OSError` → sticky failing state, roster-empty startup warning + live-gate
  render assertion, log-dir makedirs, eight-field test list. Drained + reconciled in
  dev PG.
- Round 4 `panel-ct1design-r4-20260711T035838Z-2caf87`: grok APPROVE (traced all four
  v1.3 changes against the code; invariant "finish ⇒ drained" holds in-process) and
  cold-Opus APPROVE/P2 (single-line residual shown unreachable via mapper bounds;
  RedisError complete at the redis-py boundary); GLM ORACLE-CLEAN abstain. codex + agy:
  needs-changes P1, convergent on ONE feature — warm-deregister drain was memory-only
  (agy: the poll loop iterates `live_keys` only, draining tailer never polled again;
  codex: not restart-safe, registry record gone → no rediscovery). Resolved in v1.4 by
  the durable draining record unioned into discovery (one mechanism, both P1s), plus
  carried pins: `at_eof` partial-tail clause, DriftError arm ordering, `task_continuing`
  excluded from idle activity, `failing_tailers` as primary stuck-signal,
  `skipped_lines` monotonicity, re-register flap supersedes drain. Drained + reconciled
  in dev PG.
- Round 5 (focused) `panel-ct1design-r5-20260711T041001Z-d7aff1`: the durable draining
  record confirmed the right mechanism, offsets/inode interaction clean, all six r4 pins
  verified landed (all seats). ONE convergent residual — record deletion specified only
  for the `at_eof` finish (codex+agy+grok P1, cold-Opus P2-2: transcript vanished
  mid-drain → leaked record or per-tick `task_finished` churn under a green heartbeat) —
  plus cold-Opus P2-1 (deregister consulted stale prior-tick `at_eof`; final-burst race).
  cold-Opus APPROVE/P2, GLM ORACLE-CLEAN abstain. Resolved in v1.5: unconditional record
  write at deregister, delete on ANY terminal finish, existence-check+prune extended to
  the draining union, 7d TTL backstop, tests 5i(d)(e) + deny-proof. Drained + reconciled
  in dev PG.
- Round 6 (ultra-focused) `panel-ct1design-r6-20260711T041806Z-a63b34`: agy
  APPROVE/none (all lifecycle edges verified); grok APPROVE/P2 (lifecycle sound; P2 =
  contradicting Mechanism sentence, fixed in v1.6); codex needs-changes/P1 — the
  handoff crash window (SessionEnd hook deletes the registry record at
  `session_end.py:11` before the daemon can write a draining record; daemon death in
  that window loses the final backlog at restart) — folded into v1.6 as hook-side
  write-then-delete handoff + test 5i(f). **Design phase CLOSED by Mark at this point**
  ("fold any remaining P0/P1 remediation into the spec and proceed"); GLM and cold-Opus
  r6 votes recorded in the audit trail as they landed. The v1.6 hook-handoff paragraph
  is panel-unreviewed by design — flagged for implementation-review attention.
