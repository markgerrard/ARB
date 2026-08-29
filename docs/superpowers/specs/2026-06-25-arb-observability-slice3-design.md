# ARB Observability — Slice 3 design (v2): orchestration decision record (manifest + votes + reconcile-gated verdict)

**Status:** design v2 (autonomous run, 2026-06-25) — folded the 3-seat design panel (NEEDS-CHANGES → P0
parse_stance-not-fenced, P1 timeout-vote gap, P1 actor-naming desync, P2 manifest scope, P2 stale citations).
Builds on Slice 1 (eval→prod, merged `9e6d423`). Supersedes roadmap-Slice-2 (dropped: the bridge already
tees its lifecycle to `eval_event_raw` via `push_task_event → _tee_eval_event`).

## Goal
Make `audit_events` + `reconcile()` LIVE (zero production emitters today). Wire the three-emitter decision
record for a panel run so a verdict is acceptable only if the committed audit rows reconcile against the
declared roster:
- **manifest** (`kind=dispatch`, `seq=1`) — orchestrator declares the roster at panel start.
- **votes** (`kind=vote`) — each seat's explicit stance, transcribed to an audit row by the bridge.
- **verdict** (`kind=verdict`) — orchestrator's final call, **gated by `reconcile()`** (fail-loud).

## Existing spine (do not rebuild)
`audit_events (run_id, seq, source, kind, ts, payload, content_hash)`, `UNIQUE(run_id, seq)`; `seq` via
`next_seq()` Redis INCR on `arbmem:audit:run:{run_id}:seq`; `AuditRun(redis, run_id).emit(...)`;
`PostgresAuditSink` (idempotent); `AuditConsumer` (retries infra, deadletters bad rows);
`reconcile()` (`panel_audit.py` — exactly one `dispatch` at **seq 1**, manifest precedes votes, every
rostered actor votes once, no unrostered votes, verdict stances match vote rows; missing vote ⇒ fail-loud,
`panel_audit.py:62-64`); `parse_stance()` (`stance.py`); `arb-audit-emit --kind dispatch|verdict` (verdict
already reconcile-gated, REFUSES on non-reconcile); `arb-panel-vote` (manual vote CLI). `agent-dispatch
--audit-panel` stamps `payload.audit_vote_expected=true` (Slice 1) AND currently calls `arb-panel-vote` on
reply and on dispatcher-timeout (the option-B wrapper path — REMOVED by this slice).

## The build

### 1. Bridge-daemon vote extraction (option A) — core new code
In `process_request` at task-finish (`bridge.py:~810-833`, where the original request envelope, the
`TurnResult`, and the reply text `result.result` are all in scope — NOT `send_reply`), the bridge emits the
seat's terminal vote when the run is a declared panel. Two terminal outcomes, both owned by the bridge:
- **Reply received:** `parse_stance(result.result, require_fence=True)` (strict — see §1a) → emit
  `AuditRun(audit_redis, run_id).emit(source="seat:<agent_id>", "vote", {actor, stance, severity, refs, note})`.
- **Turn timed out** (`result` indicates timeout — `bridge.py` `_is_timeout`): emit a **synthesized**
  `timed-out` vote (stance `"timed-out"`, a valid `STANCE`, `stance.py:9`) — no parse.

Guards (settled + corrected):
- **(a) declared-panel only** — emit only when `request.payload.get("audit_vote_expected")` is truthy AND
  `request.run_id` is present. `Envelope.from_json` preserves arbitrary payload keys (`envelope.py:61-99`),
  so the Slice-1 marker is readable here. Non-panel tasks never produce votes.
- **(b) explicit fenced block only** — the bridge MUST use the strict fenced-only parse (§1a). The default
  `parse_stance` also accepts a bare trailing `{...}` (`stance.py:19-29`); that is unacceptable for an audit
  vote (an incidental trailing JSON object would become a vote). `require_fence=True` rejects it.
- **(c) fail-loud on unparseable** — `StanceError` ⇒ do NOT emit a vote; log it. The missing row makes
  `reconcile` fail loud at verdict time ("seat declared in roster but never voted"). Never fabricate.
- **(d) fail-soft transport** — try/except around the emit; a down audit bus never crashes the worker turn.
  A dropped vote → fail-loud reconcile gap later, never a silent pass.

The wrapper (`agent-dispatch --audit-panel`) STOPS emitting votes entirely (remove both the reply-vote call
~379-389 and the timeout-vote call ~410-418) — otherwise a wrapper vote + a bridge vote = `duplicate vote`
reconcile failure. The bridge is the single vote emitter. `arb-panel-vote` is retained as a manual recovery
CLI (e.g. to record an abstain for a seat that never ran).

#### 1a. Strict fenced-only parse (stance.py change)
Add `parse_stance(text, *, require_fence=False)`. When `require_fence=True`, `_candidates` yields ONLY
fenced ```vote/```json blocks — it skips the bare-trailing-`{` fallback. Default `False` preserves existing
behavior for `arb-panel-vote`/manual callers. The bridge vote path passes `require_fence=True`. Test: a reply
whose only stance-like content is a bare trailing `{...}` ⇒ `require_fence=True` raises `StanceError` (no
vote), `require_fence=False` parses (existing behavior unchanged).

### 2. Never-popped / bridge-dead timeout = fail-loud gap (no synthesized vote)
A task that is never popped (bridge down, queue stuck) produces NO bridge vote — the bridge never ran. That
leaves a missing vote ⇒ `reconcile` fails loud ⇒ the orchestrator must resolve (re-dispatch, or record an
explicit stance via the manual `arb-panel-vote`). This is **intended** under mistake-prevention: a seat that
silently never executed must block the verdict, not be papered over. (Contrast the old wrapper, which
auto-synthesized a `timed-out` vote on any dispatcher timeout — that hid the never-ran case.)

### 3. Manifest = orchestrator preflight (no agent-dispatch change)
The orchestrator emits the manifest ONCE as an explicit preflight, before dispatching any seat:
`arb-audit-emit --kind dispatch --payload '{"roster": ["seat:<id1>", "seat:<id2>", ...]}'`. Because it runs
synchronously before the first dispatch and `audit_events` carries only manifest+votes for the run (lifecycle
lives in eval), the manifest takes `seq=1` via the per-run INCR — satisfying `reconcile`'s seq-1 requirement
structurally. No new `agent-dispatch --panel-roster` flag (panel rejected it as unnecessary scope + ordering
ambiguity). This step is documented in the panel runbook and covered by an integration test.

### 4. Actor-naming contract (frozen)
`reconcile` matches `vote.payload.actor` against `manifest.payload.roster` (`panel_audit.py:48-64`). The
canonical name is **`seat:<bridge_agent_id>`** where `<bridge_agent_id>` is the bridge's own
`self.agent_id` (`bridge.py:164-170`). The bridge emits `actor = "seat:" + self.agent_id`. The orchestrator's
preflight roster entries MUST be the bridges' **registered agent-ids** (what each bridge self-derives), NOT
arbitrary `--target-id` strings. These coincide in the common case (`--target-id` == registered id, e.g.
`codex-bridge-dev`) but **desync** if a dispatch carries `--role` or the bridge was launched with an explicit
`--agent-id` (the role suffix lands in `self.agent_id` but not the roster). Mitigation: a contract test pins
the bridge's emitted actor to `"seat:" + self.agent_id`; the runbook states roster entries must be the
registered agent-ids; for `--role` panels the orchestrator derives roster names from the same role-suffixed
ids.

### 5. Bridge audit-Redis config (new, mirrors eval)
The bridge loads NO `ARB_MEMORY_*` today (only `ARB_EVAL_*` via `resolve_eval_redis`, `bridge.py:116-122`).
Add `resolve_audit_redis(env)` mirroring `resolve_eval_redis` (exported env wins, `.env` fallback; covers
URL + prefix) and a `self.audit_redis` client in `__init__` (short socket timeouts, fail-soft). Audit bus =
`arbmem:audit` on prod db-5 / dev db-3. Vote emission is skipped (no-op) if `audit_redis` is unconfigured.

## Data flow
```
orchestrator preflight: arb-audit-emit --kind dispatch {roster:[seat:a,seat:b,seat:c]}
                                                          → audit stream → AuditConsumer → audit_events seq=1
orchestrator: agent-dispatch --audit-panel (per seat)     → run_id + audit_vote_expected on envelope
   bridge: task-finish + audit_vote_expected? →
     reply  → parse_stance(reply, require_fence=True) → emit vote {actor=seat:<id>, stance}  → seq=2..N
     timeout→ emit synthesized vote {actor=seat:<id>, stance="timed-out"}                     → seq=2..N
     unparseable → log, NO emit (reconcile catches the gap)
orchestrator: arb-audit-emit --kind verdict {roster,stances} → reconcile(conn,run_id) → ok? emit : REFUSE
```

## Error handling
- Vote emit fail-soft (d): never crash the worker; dropped vote ⇒ fail-loud reconcile gap, never silent pass.
- Unparseable stance fail-loud locally (c): no vote row; reconcile catches the absence.
- Never-popped: no vote ⇒ reconcile fails loud (§2), orchestrator resolves.
- Verdict fail-loud: `reconcile` REFUSES a verdict whose votes don't match the roster.
- Consumer unchanged (retries infra / deadletters bad rows).

## Testing
- `stance.py`: `require_fence=True` rejects bare-trailing-`{` (deny-proof); `=False` unchanged.
- Bridge vote-emit unit (drive `process_request` or a focused helper): (a) emits only with marker+run_id;
  (b) uses require_fence=True; (c) StanceError ⇒ NO vote row (inject-revert deny-proof); (d) down audit bus ⇒
  worker turn still succeeds + logged; timeout outcome ⇒ synthesized `timed-out` vote.
- Actor-naming contract: emitted actor == `"seat:" + self.agent_id`.
- Migration B→A: `agent-dispatch` no longer emits any vote; update/remove the stale wrapper-vote assertions
  (verify no test depends on the wrapper emitting — design panel confirmed none do today).
- reconcile integration (extend `test_panel_audit`): full panel run reconciles; inject-revert deny-proofs —
  drop one vote ⇒ "seat never voted"; unrostered vote ⇒ fail; manifest seq≠1 ⇒ fail (seq-1 guard);
  verdict stance ≠ vote row ⇒ fail.
- **E2E (live):** manifest (roster of 2 fake seats) → drive the bridge vote path to transcribe two fenced
  stance replies into `audit_events` → emit matching verdict → assert `reconcile` ok. Negative E2E: a
  garbled/bare-JSON stance ⇒ no vote ⇒ verdict REFUSED. (Mirrors `tests/e2e_eval_roundtrip.py` against the
  real audit stream + Postgres.)

## Resolved decisions
1. Vote extraction = **option A (bridge-daemon)**; wrapper stops emitting (single emitter, no double-votes).
2. Bridge owns **all** terminal votes it sees (reply→strict-fenced parse; turn-timeout→synthesized timed-out);
   never-popped = fail-loud gap (intended).
3. Strict **`parse_stance(require_fence=True)`** for the bridge path; loose default retained for manual CLI.
4. Manifest = **orchestrator preflight `arb-audit-emit --kind dispatch`** (seq=1; no `--panel-roster`).
5. Actor = **`seat:<self.agent_id>`**, frozen contract; roster uses registered agent-ids; `--role` desync noted.
6. seq-1 holds structurally — `audit_events` carries only manifest+votes (lifecycle is in eval).

## Out of scope
ARB Visibility SSE gateway (Slice 4, reads audit_events + eval_event_raw by run_id); span tables (Slice 5);
adversarial-auth hardening (productization-era; ARB threat = mistakes).
