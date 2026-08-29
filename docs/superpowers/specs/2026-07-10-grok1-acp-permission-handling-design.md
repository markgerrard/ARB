# GROK-1: spec-correct ACP permission answers + CDX-1 hardening for grok-acp — design (v1.3, panel rounds 1–3 absorbed)

Status: v1.3 CLOSED — round 4 (`panel-grok1design-r4-20260710T192458Z-035861`, brief
`d10e993`) UNANIMOUS approve, all seats severity none; codex closed its own r3 block on
its own terms (run-H-verified correlation), cold-Opus closed both its r3 P2s and swept
the gate's four windows fail-closed. **Build pins from r4 notes (normative for the
implementation):** (1) flip `self.session_id` only AFTER a successful `session/new`
response [grok r4]; (2) a JSON-RPC-error prompt response counts as UNCLEAN for health
marking — fail-closed default; add a V7 case if opt-out churn ever matters
[cold-Opus r4 sub-P2].
Round 1: `panel-grok1design-20260710T182111Z-60e882` (v1 @ `162f1ae`) — codex-sol **block**,
pi-GLM **approve**, grok **needs-changes**, cold-Opus **approve**; all four seats severity P1.
Convergent P1 clusters absorbed: (1) explicit authorization-context threading (all 4
seats), (2) `is_healthy()` pool-quarantine wiring made mandatory (all 4 seats; pool behavior
verified at `engine_pool.py:128-135`), (3) V5 must prove the responder was exercised (codex),
plus the stale-`session/update` leak (GLM P1-2 / codex P2) and five P2s.
Round 2: `panel-grok1design-r2-20260710T183536Z-ed4a02` (v1.1 @ `742ce73`) — all round-1
findings confirmed closed by all four seats (grok/GLM/cold-Opus approve, severity none).
One NEW P1 (codex): the D3b one-shot drain is not an authorization boundary — a prior-turn
ask can cross the drain/prompt boundary on an opt-out seat and inherit the NEW turn's policy
(trusted ⇒ allowed), since asks carry no turn correlation.
Round 3: `panel-grok1design-r3-20260710T184629Z-d49d7b` (v1.2 @ `82fe1de`) — grok approve/none,
GLM approve/none, cold-Opus approve/P2 (two build-time gaps: non-exhaustive healthy partition
via the `EngineError` raise path at `bridge.py:1877`; retire-masked V7 vacuity), codex
**block**/P1 (strict-premise residual: the clean/unclean partition cannot exclude a
hypothetical post-response ask from a CLEAN turn; its named acceptable alternative:
"reliable correlation and reject mismatches"). Operator decision (Mark, 2026-07-10 evening):
**opt-out = fresh session per dispatch, gated by sessionId** — probe runs H/I/G (artifact
addendum) established `session/new` works on a live process with context isolation and
sessionId-stamped asks, no permission-bypass layer exists (`--always-approve` inert over
ACP), and `allow_always` grants are cosmetic. D3b below is the v1.3 realization; codex's
correlation alternative is satisfied.
Author: warm orchestrator (Fable, inline). Probe executed by a cold-Opus subagent
(`[ARB_RUN:probe-grok1-v1-20260710]`), artifact
`docs/superpowers/probes/2026-07-10-grok1-v1-probe/` (7 runs, raw JSONL + stderr per run).
Certify quorum for this authored stage: codex-sol + pi-GLM. grok-bridge-dev = named
NON-certifying contributor (own-engine conflict); cold-Opus non-certifying (author lineage).
Scope decision (Mark, 2026-07-10): **grok_acp.py only** — gemini-acp (deprecated engine)
and kimi-code-acp likely share the reply-shape weakness but are OUT of scope; the shared
helper (D1) makes their later fix a two-line import.

## Problem (probe-verified root cause — the BACKLOG story was wrong)

Grok dispatches that trigger a permission-requiring operation (out-of-cwd writes; the
turn-death family filed as GROK-1 in `docs/BACKLOG.md`) do no work and end as failed
turns. The V1 probe (grok 0.2.93) pinned the mechanism with a controlled A/B where the
reply shape was the only variable:

- Grok delivers a standard ACP **`session/request_permission`** server→client request to
  the adapter on every permission-requiring write. It arrived in every probe run; it
  never routed through a dead internal worker. (**H2 refuted.**)
- The adapter answers it — with `{"outcome": {"outcome": "approved"}}`
  (`grok_acp.py:345`). `"approved"` is not an ACP outcome; grok treats the reply as a
  non-acceptance: the operation does NOT execute and the turn ends
  `stopReason=cancelled` (Run A; `cancellationCategory: "PermissionRejected"`). The
  spec-correct reply `{"outcome": {"outcome": "selected", "optionId": "allow-once"}}`
  executes the operation and the turn ends `end_turn` (Run B). (**H1 confirmed — this is
  the bug.**)
- `session/set_mode "yolo"` is accepted but does NOT suppress the ask **for out-of-cwd
  writes** (Runs A vs E identical; `session/new` advertises no modes). In-cwd writes were
  NOT probed — yolo may or may not suppress those (probe README §caveats). A mode-based
  fix is unavailable for the failing class. (**H3 confirmed, secondary.**)

Two attributions this overturns, recorded here because they live in three stores (see
Residuals): the stderr fatal `worker quit with fatal: Transport channel closed, when
Auth(AuthorizationRequired)` appears on **successful** runs too — it is benign noise,
not a dead permission worker. And out-of-cwd **reads** do not ask at all (Run E2,
auto-resolved, `end_turn`) — the worktree-read infeasibility claim was a co-traveler of
the same misattribution.

Other probe facts the design leans on:

- **Error ⇒ no-execution** (Run D): a JSON-RPC error reply to the ask degrades to
  cancelled, no crash, no execution. The `-32601` fallback for unknown methods is
  fail-closed on the real binary; CDX-1's escalate-all-unknowns-to-deny-shape
  contingency stays dormant for grok too.
- **Ask params shape:** `{sessionId, toolCall: {toolCallId, kind, title, rawInput,
  _meta}, options: [{optionId, name, kind}, ...]}` with ACP-standard option kinds
  (observed: `allow-edits-session`/allow_always, `allow-once`/allow_once,
  `reject-once`/reject_once). cursor_acp's `_select_allow_option` applies verbatim.
- **Server request ids start at 0 and overlap client ids** — the per-side
  `"method" not in message` guard (already test-pinned) is load-bearing; nothing here
  may weaken it.
- The observed failure spelling on 0.2.93 is `stopReason=cancelled`, not the `-32603`
  seen in seat logs — version- or path-specific. Gates assert the behavior
  (execute + `end_turn`), never the error spelling.
- **No ACP ordering guarantee is assumed** [codex r1]: the prompt response is the turn's
  terminal message for *our* control flow, but nothing proves no notification can be
  queued after it — D3b's sessionId correlation handles staleness structurally instead of
  claiming a barrier.
- **No permission-bypass layer exists** (addendum runs G/I): `grok --always-approve` is
  inert for the ACP stdio server (the ask still arrived; a `cancelled` reply still blocked
  the write), and selecting the `allow_always` option (`allow-edits-session`) neither
  suppresses subsequent asks in-session nor persists across `session/new`. The adapter's
  answer is the sole permission authority — D2 is load-bearing for ALL traffic.
- **`session/new` on a live process works and isolates** (addendum run H, 0.2.93): a
  second session gets a fresh sessionId, functions normally, cannot recall the prior
  session's planted content, and its permission asks carry ITS sessionId. Sessions are
  the correlation unit for asks; this is what D3b builds on. Single-binary evidence —
  re-gated at V5b before any opt-out seat ships.

## What is already sound (gap is narrower than CDX-1)

Both wait loops (`run_turn_with_progress` and `request()`) already route every
server-initiated request through `_handle_client_message` →
`_respond_to_client_request`; unknown methods already get `-32601` + return. GROK-1 is
NOT "build the responder" — it is: fix the responder's accept shape, make its decisions
policy-correct and legible, and bound the deny-loop failure mode. The CDX-1 decisions
(trusted=allow, deny-and-continue, budget default 10, v2 seam) are reused by reference,
not re-litigated.

## Constraints

1. **Zero behavior change when no ask arrives.** No-ask event streams stay
   byte-identical pre/post (V4a).
2. **Fail-closed.** Non-trusted senders must not gain execution; when no allow option
   is offered, params are malformed, or the authorizing context is absent (`policy=None`),
   the floor is `cancelled` — even under trusted policy.
3. **No silent anything.** Every ask gets a decision that is visible: progress event
   for denials, log line for trusted allows.
4. **Answer-everything holds unconditionally** — budget-exceeded asks and inter-turn
   stragglers included (deny path; log-only when no `on_event` is in scope).
5. **Reuse, don't fork, the cursor_acp precedent** — `_select_allow_option` moves to a
   shared module; cursor_acp behavior is unchanged (its tests keep passing through the
   re-export import).
6. **Authorization state is never inferred from observability wiring** [round-1
   convergent, 4 seats]: `on_event` presence is an event-emission concern only; the
   decision input is an explicitly threaded `policy`.

## Design

### D1 — shared allow-option selection

Move `_select_allow_option` from `cursor_acp.py` to a new `engines/_acp.py` (sibling of
`_stdio.py`); `cursor_acp.py` imports it from there (existing external references —
tests import it via the cursor_acp namespace — keep resolving through that import).
Behavior byte-identical: kind-authoritative (prefer `allow_once`, then `allow_always` —
no standing grants when a single-use option exists; when ONLY `allow_always` is offered
the picker takes it, accepting the standing grant exactly as cursor does [grok r1,
noted]), substring fallback that rejects negative-marked options, `None` when no allow
option exists.

### D2 — decision rule in `_respond_to_client_request`

**Authorization context is threaded explicitly** [round-1 convergent: grok P1-1, GLM
P2-1, cold-Opus P2, codex P1 — codex.py precedent, `policy=None` from `request()`]:
`_respond_to_client_request(message, *, policy, on_event)` where

- the **turn loop** passes the active turn's `policy` (the `run_turn_with_progress`
  parameter, not engine state) and its `on_event`;
- **`request()` passes `policy=None, on_event=None`** — always, including the
  `request()` calls made inside `set_session_mode_for_policy` (an ask arriving during
  the set-mode round-trip has no authorizing turn);
- `self._policy` and `self._auto_approve_permissions` are **deleted as decision inputs**
  [grok P2-1, cold-Opus P2]: the responder decides ONLY on the threaded parameter.
  `set_session_mode_for_policy` keeps mapping policy→mode but stores no approval state.

Decision table for `session/request_permission`:

- **`policy == "trusted"`:** `_select_allow_option(params)` → reply
  `{"outcome": {"outcome": "selected", "optionId": <picked>}}` + **DEBUG log** naming
  the toolCall title and picked option. (Delta from CDX-1's allow+WARNING, deliberate:
  on grok, out-of-cwd/worktree writes ask even under yolo, so trusted allows are
  *routine*, not drift — a WARNING per write would be spam.) If `_select_allow_option`
  returns `None` or params are malformed: reply `cancelled` + **WARNING** (this IS
  anomalous) — the fail-closed floor outranks trusted intent.
- **Any other non-None policy:** reply `{"outcome": {"outcome": "cancelled"}}`,
  increment `self._deny_count`, and emit a `command_denied` progress event
  `{command: <toolCall.title>, turn_id, item_id: <toolCallId>, kind: "command_denied",
  seq, deny_count, deny_budget}` when `on_event` is present, WARNING log otherwise
  (in-turn denies are budget-counted regardless of `on_event` [grok P2-3]). The engine
  also stores the denied title for the budget-exhaustion error message. Deny-and-continue:
  the deny does not abort the turn (budget bounds the loop, D3).
- **`policy is None`** (inter-turn / no authorizing context): unconditional deny +
  WARNING log, not budget-counted (bounded by the `request()` timeout). Fail-closed is
  the floor whenever the authorizing context is absent — CDX-1 rule verbatim.
- **Unknown methods:** unchanged `-32601` + warning (probe: error ⇒ no-execution, so
  this is fail-closed on the real binary).

The responder never makes control-flow decisions — it answers, counts, and emits
(CDX-1 D2a signalling split).

### D3 — per-turn deny budget + bounded exit (CDX-1 D2a, adapted)

Same env knobs as codex, shared deliberately: `BRIDGE_APPROVAL_DENY_BUDGET` (default
10), `BRIDGE_APPROVAL_GRACE_S` (default 10). `self._deny_count` resets at prompt start
in `run_turn_with_progress` (after `set_session_mode_for_policy`, before the loop — so
inter-turn stragglers during set-mode never count). The **turn loop** checks
`self._deny_count > budget` after each handled message and owns the exit:

1. Call `interrupt()` (`session/cancel` notification — already implemented),
   fire-and-forget.
2. Keep draining messages for `min(BRIDGE_APPROVAL_GRACE_S, remaining turn budget)`,
   answering any further asks per D2 the whole time (answer-everything holds; the
   budget-exceeding ask itself is still answered — denied).
3. If the response to our own `session/prompt` request arrives within grace: return
   `TurnResult(ok=False, error="approval deny budget exhausted (<N> denials); last:
   <toolCall.title>")` — and STILL set `self.healthy = False` (the turn was interrupted;
   see D3b's clean-completion rule — grace success makes the exit legible, it does not
   make the session reusable) [codex r2].
4. Grace expiry: set `self.healthy = False`, clear `active_prompt_id`, return the same
   TurnResult immediately.

**D3a — health wiring is a mandatory v1 deliverable, not a conditional** [round-1
convergent: GLM P1-1, cold-Opus P1, grok P1-2, codex P1 — `engine_pool.release()`
quarantines ONLY via a callable `is_healthy()` (`engine_pool.py:128-135`); a bare
`.healthy` attribute is invisible to it, and GrokAcpEngine has neither today]:

- `GrokAcpEngine` gains `self.healthy = True` and `is_healthy()` mirroring
  cursor/codex: `self.healthy and process is not None and process.poll() is None and
  reader_thread.is_alive()`.
- `_read_stdout` is wrapped try/except; reader death sets `self.healthy = False`
  (cursor parity) [GLM].
- The turn loop and the D3 grace drain check process liveness each iteration
  (`_process_exited` cursor parity) and exit with a legible `TurnResult(ok=False)` +
  `healthy=False` instead of spinning to the full timeout on a dead child
  [cold-Opus P2].
- **Affirmative clean-completion marking** [cold-Opus r3 P2 — the clean/unclean partition
  must be exhaustive by construction, not by enumeration: a raised `EngineError` on a
  live child exits through `bridge.py:1877`'s `except EngineError` arm, which does not
  set `healthy=False`, so an enumerated unclean set misses it]: `run_turn_with_progress`
  sets `self.healthy = False` at prompt start and sets it back to `True` ONLY on a
  cleanly-received terminal response with no interrupt issued. Every other exit —
  including raise paths the enumeration never names — leaves the engine unhealthy by
  default. Fail-closed reuse, structurally.

**D3b — opt-out = fresh session per dispatch, gated by sessionId** [operator decision
after codex r3; satisfies codex's named alternative "reliable correlation and reject
mismatches". Supersedes both v1.1's drain-as-boundary and v1.2's reuse-only-across-clean-
turns: asks carry no TURN correlation, but they DO carry sessionId (probe fact), and run H
proved fresh sessions on a live process are functional and context-isolated — so make the
session the dispatch boundary and correlate on it]:

- **Session rotation:** a non-retiring (`BRIDGE_GROK_RETIRE_AFTER_TURN=0`) engine keeps
  its warm PROCESS across dispatches but never reuses a session: at prompt start, if the
  engine has served a prior turn, `run_turn_with_progress` calls `session/new` and adopts
  the new sessionId before sending `session/prompt`. A failed `session/new` ⇒
  `healthy=False` + legible error (fail-closed, never fall back to the old session).
  This also closes the cross-dispatch context-accumulation leak (the original reason
  retire-after-turn exists) for opt-out seats — run H: fresh sessions cannot recall prior
  sessions' content.
- **sessionId gate on asks (unconditional hardening, applies to ALL seats):** any
  `session/request_permission` whose `params.sessionId` differs from the engine's current
  `self.session_id` is denied via the D2 `policy=None` path + WARNING — regardless of the
  active turn's policy. A stale ask from an abandoned session is structurally
  unauthorizable: the leak codex r2/r3 identified becomes impossible by correlation, not
  by ordering assumptions or drains. (Stale `session/update`s are already discarded by
  the existing sessionId filter at `grok_acp.py:312-315`; that behavior becomes
  test-pinned.)
- **Clean-completion rule retained for engine health (D3a):** an interrupted turn (budget
  exhaustion — both grace arms), a timeout, a dead child, or any unnamed raise path still
  leaves `healthy=False` ⇒ the process is retired. Session rotation defends
  *authorization*; the health rule defends against *wedged processes* — they are
  orthogonal and both stay.
- The prompt-start drain is DROPPED: with the sessionId gate, any stale message is
  handled correctly whenever it surfaces (ask ⇒ denied by mismatch; update ⇒ discarded
  by mismatch), so a drain adds no property. [YAGNI]

Retire-after-turn (default ON since `5d43b2b`) remains the default; D3b's rotation path
exists for opt-out seats. The isolation claim is single-binary evidence (0.2.93) — V5b
gates it live before any opt-out seat enters service.

**Recorded dissent (codex-sol, impl panel `panel-grok1impl-20260710T210809Z-750516`;
operator-adjudicated spec-side, Mark 2026-07-10):** sol rated as P1 that a terminal
response with `stopReason` failed/refusal/cancelled (no interrupt) marks the engine
reusable, proposing `healthy=True` only when `ok and not interrupted`. Rejected: turn
failure and engine sickness are separate predicates — a refusal is a healthy engine
enforcing policy, and the affirmative marking already keeps the failure modes sol's rule
targets (truncated turns, mid-flight death, transport errors) out of the reusable path,
since none of them produce a clean stopReason. The behavior is pinned by a
characterization test (`test_cleanly_failed_turn_reidles_by_design_d3b`) so it cannot
drift silently. **Revisit tripwires:** (a) any opt-out (`BRIDGE_GROK_RETIRE_AFTER_TURN=0`)
seat goes live; (b) evidence appears of a seat class where a clean `failed` correlates
with corrupted session state rather than clean completion. Related future ticket, not a
merge condition: a consecutive-clean-failure circuit breaker (an engine that cleanly
fails N turns running is protocol-healthy but operationally useless).

### D4 — what deliberately does NOT change

- `set_session_mode_for_policy` keeps trying yolo/default. Mode is legibility-only for
  the failing class (probe: it doesn't gate out-of-cwd asks), but removing it is
  gratuitous churn.
- The per-side id-namespace guards in both loops.
- The `-32601` unknown-method fallback (probe-verified fail-closed).
- v2 human-routing is **not redesigned**: CDX-1's v2 spec (blocking `decide()` seam,
  `approval_request`/`approval_response` envelopes, timeout semantics) is
  engine-agnostic; grok binds the same static v1 rule and inherits that spec by
  reference (`2026-07-08-cdx1-approval-handling-design.md § Design v2`).

### Rejected alternatives

- **Minimal shape fix only (option A)** — fixes trusted turns but leaves non-trusted
  denials invisible (no event, no budget) exactly on the engine where asks are routine;
  Mark selected B 2026-07-10.
- **Mode-based fix ("set yolo harder")** — refuted empirically (H3): yolo is accepted
  and changes nothing about out-of-cwd asks.
- **Prefer `allow_always` to reduce ask volume** — a standing grant outliving the
  decision context; allow_once keeps every operation individually adjudicated.
- **Infer inter-turn from `on_event is None`** — couples an authorization decision to
  optional observability wiring; rejected by all four round-1 seats.
- **Launch-level or grant-level permission bypass** (`grok --always-approve`, `[ui]`
  config, answering with the `allow_always` option) — all probed INERT for the ACP
  server (runs G and I): the flag doesn't reach the stdio agent, and the allow_always
  grant neither suppresses in-session asks nor survives `session/new`. There is no
  codex-bypass analogue for grok; the D2 responder is the only permission path.
- **Session reuse across dispatches on opt-out seats** (the pre-v1.3 semantics) — the
  accumulation leak (5d43b2b) plus the uncorrelatable-straggler leak (codex r2/r3);
  replaced by D3b session rotation, which run H shows costs nothing in capability.
- **Fix grok upstream / wait for x.ai** — the callback is standard ACP and the fix is
  ours; nothing is upstream-blocked (BACKLOG's framing, confirmed).

## Verification obligations (v1)

- **V1 — protocol probe: EXECUTED 2026-07-10** (this design's input, not a pending
  gate). Artifact: `docs/superpowers/probes/2026-07-10-grok1-v1-probe/README.md` §
  KEY EMPIRICAL FACTS.
- **V2 — deny-proof:** fabricated ask on a non-trusted turn ⇒ (a) stdin reply contents
  asserted (`cancelled` outcome + echoed request id), (b) `command_denied` event with
  `deny_count`/`deny_budget`, (c) turn completes without timeout. **Delete the
  `session/request_permission` arm of `_respond_to_client_request` ⇒ the test must go
  red**: the fixture's completion is contingent on receiving the adapter's JSON-RPC
  response to the ask (it must not auto-cancel on timeout and green-wash a mute
  responder) [codex r1].
- **V3 — allow-proof + context matrix:** (a) trusted turn with fabricated ask carrying
  the probe's option vocabulary ⇒ reply is `selected` + the offered `allow-once` id
  (asserted contents), DEBUG log, **no** `command_denied` event. (b) Fail-closed floor:
  options with no allow *kind* AND reject-only optionIds carrying no "allow" substring
  (otherwise the picker's substring fallback defeats the case [cold-Opus r1]) ⇒
  `cancelled` + WARNING even under trusted. (c) Active trusted turn with
  `on_event=None` ⇒ still allows (policy decides, not callback presence). (d) Active
  non-trusted turn with `on_event=None` ⇒ deny IS budget-counted, WARNING logged.
  (e) Stale-trusted inter-turn: after a completed trusted turn, an ask arriving in a
  bare `request()` wait ⇒ denied (`policy=None` path), NOT allowed from stale state
  [codex r1].
- **V4 — inertness:** (a) no-ask trusted fixture ⇒ byte-identical event stream and
  stdin writes pre/post change; (b) unknown method still `-32601` in both loops;
  (c) inter-turn ask ⇒ unconditional deny + log, no event, not budget-counted.
- **V7 — budget exhaustion + reuse hygiene (all arms).** Pool-quarantine arms MUST run
  the fixture engine with `retire_after_turn=False` pinned — otherwise `release()`'s
  retire branch (`engine_pool.py:132-134`) stops the engine without ever consulting
  `is_healthy()` and the assertion passes vacuously [cold-Opus r3 P2].
  `BRIDGE_APPROVAL_DENY_BUDGET+1` fabricated asks on one non-trusted turn ⇒
  incrementing `deny_count`, the exceeding ask still answered (denied), then
  `session/cancel` observed on stdin. Arm (a): fixture delivers the prompt response
  within grace ⇒ legible error TurnResult AND `is_healthy()` returns False (grace
  success is still an unclean end [codex r2]). Arm (a2, sessionId gate [v1.3]):
  a fabricated ask carrying a NON-current sessionId during an active TRUSTED turn ⇒
  denied via the `policy=None` path + WARNING (contents asserted: `cancelled` + echoed
  id), no `command_denied` event mis-attributed to the turn's budget; a stale
  `session/update` with the old sessionId ⇒ discarded (existing filter, now
  test-pinned); and session rotation: a second turn on a non-retiring fixture engine
  observes `session/new` on stdin BEFORE `session/prompt`, with the new sessionId
  adopted (a failed `session/new` ⇒ `is_healthy()` False + legible error, old session
  never reused). Arm (b): fixture never responds ⇒ return within the grace bound,
  `is_healthy()` returns False, real `EnginePool.release()` stops the engine and does
  not re-idle it — assert idle-list absence + `stop()` called (not merely a flag — the
  flag alone is a vacuously-green guard [all 4 seats r1]). Arm (c): dead-child exit —
  fixture's process dies mid-turn ⇒ turn returns promptly (not at timeout),
  `is_healthy()` False. Arm (c2, affirmative marking [cold-Opus r3]): a turn that
  exits via a RAISED `EngineError` on a live child ⇒ `is_healthy()` False (the
  prompt-start False / set-True-only-on-clean-end rule catches paths the unclean
  enumeration never names). Arm (d): clean end control — a normal `end_turn` fixture
  turn ⇒ `is_healthy()` True, engine re-idled (reuse is not over-revoked).
- **V6 — hermeticity:** fixtures only; CI has no grok binary; all new tests pin their
  env (no host-state defaults).
- **V5 — live gate (required):** restart `grok-bridge-dev` onto the new code (fleet
  restart discipline: check running tasks first), then dispatch a write task whose
  target is **demonstrably outside the grok session's cwd** (in-cwd asks were never
  probed; a cwd-internal write may not ask at all, passing the gate without exercising
  the responder [codex r1]). Success = ALL of: (1) evidence the permission callback
  fired and was allowed by the new path (the trusted-allow DEBUG record or the
  corresponding event), (2) the file exists with the dispatched content, (3) the turn
  ends `end_turn` with a real reply — asserted from the filesystem and events, not
  reply prose. Control arm: a read-only brief on the same seat behaves exactly as
  today. (Criterion is callback-exercised + execute + `end_turn`, NOT "-32603 absent" —
  the error spelling is version-specific.)
- **V5b — opt-out isolation live gate (required before any opt-out seat enters
  service; NOT required for the v1 merge, which ships only retire-ON seats):** on a
  seat launched with `BRIDGE_GROK_RETIRE_AFTER_TURN=0` on the new code, dispatch #1
  plants a distinctive fact; dispatch #2 (same warm process, rotated session) asks for
  it and MUST NOT recall it (run H's plant/recall design, live); dispatch #2's
  permission ask must carry the rotated sessionId. Re-run on every grok binary upgrade
  — the isolation claim is version-scoped evidence, not a protocol guarantee.

## Live gate V5 — EXECUTED 2026-07-10 22:2x (all arms green, merged dev `635c398`)

Seat `grok-bridge-dev` restarted onto `635c398` (fleet clone pulled; re-registered
22:23:42). Arm 1 (terminal-command out-of-cwd write): file written, `end_turn`, clean
reply. Arm 2 (edit-tool out-of-cwd write — the probe-proven always-asks path; every
probe run A–I raised `session/request_permission` for this op class): file
`/private/tmp/grok1-v5-gate/probe2.txt` written byte-exact, `command_finished` +
`stop_reason: end_turn`, reply "written" — execution of an always-asks operation IS the
callback-exercised proof (the seat log filters DEBUG, so the allow line is not visible
there; noted, not load-bearing). Control arm (read-only brief): unchanged behavior.
On pre-fix code the identical edit-tool operation died (probe Run A). The P0 turn-death
class is dead. V5b (opt-out isolation) remains gated on any opt-out seat standup.

## Non-goals (v1)

- No gemini-acp / kimi-code-acp changes (Mark's scope call; helper import is their
  future two-liner).
- No v2 human-routing build, no per-command allowlists, no approval memory.
- No changes to sender-policy vocabulary or the envelope gate.
- No upstream grok issue-filing as a blocker (may still be worth reporting the benign
  worker-fatal noise, but nothing here waits on it).

## Residuals (ship-time obligations, not code)

Three stores carry the refuted dead-worker attribution and need correcting when this
merges (document-findings-three-stores discipline): `docs/BACKLOG.md § GROK-1` (fix
shape + root cause paragraphs), ARB Memory artefact `art-d893502c280b1740`
(symptom→cause→discriminator needs the H1 cause and the Run-A/B discriminator), and
`skills/using-agent-bridge` failure-shapes table (grok cwd-only/inline-reply rules
relax after the V5 live gate passes — reads never asked at all, and writes work once
answered correctly). Local memory `manual-seats-promoted-launchd` (grok
implementation-INFEASIBLE claim) and `pi-sdk-glm-wedge-root-cause` (grok gotcha
paragraph) also reference the old story.
