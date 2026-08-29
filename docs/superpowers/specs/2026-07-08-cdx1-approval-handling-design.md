# CDX-1: answer every server-initiated codex request — design (v1.2, panel rounds 1+2 absorbed)

Status: v1.2 CLOSED — round 3 (`panel-cdx1design-r3-20260708T133108Z-4fa114`, brief
`06341f5`) unanimous approve incl. codex advisory (its r2 block explicitly closed).
Rounds: r1 `panel-cdx1design-20260708T130054Z-98421d` (unanimous, P1 revisions),
r2 `panel-cdx1design-r2-20260708T132303Z-1df55f` (all P1s = the post-interrupt return
path, now D2a). V1 protocol probe EXECUTED 2026-07-08 (codex-cli 0.142.5, artifact
`docs/superpowers/probes/2026-07-08-cdx1-v1-probe/`): error⇒no-execution VERIFIED,
deny-all-unknowns contingency dormant; `availableDecisions` carried in-request
(cancel=deny, accept=allow); sandbox gates the ask, not just approvalPolicy.
Author: warm orchestrator (Fable, inline — Mark-selected 2026-07-08).
Certify quorum for this stage: agy-print + pi-GLM + pi-M3. codex reviews as a
NAMED NON-CERTIFYING contributor (finding source: its own engine's harness —
conflict flagged in the 2026-07-07 engine-seat audit handoff); cold-Opus reviews
non-certifying (Anthropic author lineage). Findings from both count fully.

Scope decision (Mark, 2026-07-08): **"Deny now, design both"** — v1 implements
fail-closed deny; this document also fully specifies the human-routing v2 so a
future build is a fill-in, not a redesign. v2 is NOT part of the v1
implementation or its gates.

## Problem (hinge facts, re-verified at `e25f6e9` on 2026-07-08)

Audit finding CDX-1 (P0, latent): the codex app-server protocol is
bidirectional — under a non-`never` approval policy, codex **initiates its own
JSON-RPC request** (approval ask) and blocks until it gets a response. The
bridge never answers:

- The turn loop (`codex.py:162–284`) branches on exactly five notification
  methods (`item/agentMessage/delta`, `item/completed`, `item/started`,
  `item/commandExecution/outputDelta`, `turn/completed`); anything else falls
  through every `elif` and is dropped with no log.
- `request()` (`codex.py:341–369`) now *identifies* server-initiated requests
  (per-side id guard, `:358–360`, landed with the CDX-4 fix `7aa5edd`) — and
  then `continue`-drops them. So the drop exists in **two** wait loops.
- `approval_policy_for_policy` (`codex.py:434–439`): `trusted` → `"never"`,
  `human` → `"on-request"`, default → `"on-request"`. Sender-policy vocabulary
  is `{trusted, human, reject}` (`bridge.py:2429`).
- Consequence: a `human`-policy dispatch whose turn wants to run a command
  leaves the app-server blocked on an unanswered approval request; the bridge
  waits out the full turn timeout (up to 3600s) and returns a misleading
  "turn timed out". Stall detection sees it (detect-only); nothing unsticks it.

**Latency:** every running codex seat launches with
`--codex-bypass-approvals-and-sandbox` (flag plumbed at `bridge.py:2541`;
present in `envs/project-g-dev.env`, `envs/dev-project-g-consultant-local.env`,
`envs/agent-redis-bridge-dev.env` — host observations: `envs/` is gitignored,
so these claims are not checkable from the repo tree [codex r1]), and
`trusted` maps to `"never"` — so no
approval request is ever emitted today. The bug goes live the first time a
seat runs without bypass; `envs/dev-project-g-consultant-local.env` already carries
`human-codexctl=human`, which invites exactly that.

**Wire-shape uncertainty (named):** the audit cites
`item/commandExecution/requestApproval` (current) plus legacy
`execCommandApproval` / `applyPatchApproval`. A `strings` probe of the
installed binary (codex-cli 0.142.5) was inconclusive (stripped). The decision
vocabulary of the response is therefore **unverified**. The design below is
correct regardless (see D1); the exact shapes are pinned by an implementation-
phase protocol probe (see Verification obligations).

## Constraints

1. **Zero behavior change for the existing fleet.** Bypass seats and
   `trusted`-policy dispatches never receive approval asks; the new code must
   be provably inert there (regression suite stays green untouched; live gate
   includes a trusted-seat control run).
2. **Fail-closed.** A non-`trusted` sender must not gain command execution via
   this feature — the invariant "non-trusted senders can't run commands on a
   non-bypass seat" becomes structural (the deny is code, not config).
3. **No silent drops.** This is the same class AGY-2 killed: an enumerated
   handler list with a silent default re-creates the open-set problem (a new
   codex CLI version renames the method → hang returns). The correctness
   mechanism must not depend on the method-name enumeration being complete.
4. **CDX-4 interplay.** Server-initiated requests share the stdout channel with
   response waits; any new mid-turn traffic must respect the per-side id
   namespaces already pinned at `codex.py:358–360` (and mirrored in the ACP
   engines).
5. Follow the existing in-repo precedent where it is sound:
   `cursor_acp._respond_to_client_request` (`cursor_acp.py:440–473`) already
   implements non-trusted → deny (`cancelled` outcome) and — load-bearing —
   answers **unknown** client methods with JSON-RPC error `-32601` instead of
   silence.

## Design v1: answer-everything (structural) + legible deny

### D1 — the invariant: every server-initiated request gets a response

Core rule, enforced at the message-classification point in **both** wait loops
(turn loop and `request()`): any stdout message carrying `id` + `method` is a
server-initiated request and is routed to a single
`_respond_to_server_request(message, policy, on_event)` — it is never dropped.
The responder guarantees a reply:

- **Known approval methods** (`item/commandExecution/requestApproval`,
  `execCommandApproval`, `applyPatchApproval`): decide per policy (D2), reply
  with the protocol's decision shape.
- **Unknown approval-shaped methods** (method name contains `approval`,
  case-insensitive — the rename-survivable heuristic) [GLM r1 F1, upgrades
  cold-Opus P2-A]: take the **deny path**, replying with the decision shape
  pinned by the V1 probe, plus a warning log. Rationale: `-32601` on an
  approval ask is only safe if codex treats an errored ask as "don't run";
  that is an assumption, and if it is ever false a *renamed* approval method
  becomes command execution on a `human` turn — the exact invariant this
  design exists to make structural. A deny-shape guess is strictly no worse:
  if codex rejects the shape, the outcome degrades to the errored-ask case.
  V1 (extended) must probe codex's actual reaction to an errored ask; **if
  the probe shows error ⇒ execution, the fallback for ALL unknown
  server-initiated requests becomes the deny-shape** (not just
  approval-shaped ones) — the name heuristic alone cannot cover an approval
  method renamed to drop "approval", so the escalation is committed now
  rather than left to impl judgment [round 2: cold-Opus P2-A + GLM N2,
  convergent].
- **All other unknown methods**: reply JSON-RPC error `-32601 client method
  not supported: <method>` (cursor precedent) and log a warning naming the
  method. Codex receives an error for its pending request instead of blocking
  forever.

This is blind-until-proven's sibling: correctness comes from the closed-world
guarantee "we answer everything", not from an open-set enumeration of method
names. The enumeration only buys *legibility* (a proper decision shape and a
named progress event) — a codex version with a renamed approval method
degrades to a denied/errored item plus a loud log line, never to a hang.

Mechanically: the turn loop's dispatch gains, **before the
`if not isinstance(params, dict): continue` guard at `codex.py:183`** [cold-Opus
r1 P2-D — a server request with malformed/absent `params` must still be
answered, or it slips the closed-world net],

```
if "id" in message and isinstance(message.get("method"), str):
    self._respond_to_server_request(message, policy=policy, on_event=on_event)
    continue
```

and `request()`'s guard at `:358–360` changes from `continue` (drop) to the
same routing. `run_turn_with_progress` threads `policy` through (it already
receives it; today it is consumed only by `approval_policy_for_policy`).

Inside inter-turn `request()` waits there is no per-dispatch policy in scope;
an approval ask arriving there (e.g. straggling after turn end) is answered
with the **deny** path unconditionally — fail-closed is the floor whenever the
authorizing context is absent. `on_event` is likewise absent there; the deny
is logged, not event-emitted.

### D2 — v1 decision rule: deny, fail-closed, legible

`_respond_to_server_request` decides:

- `policy == "trusted"`: **allow + WARNING log** [round-1 split resolved by
  Mark 2026-07-08: allow]. Unreachable on today's fleet (trusted ⇒
  `approvalPolicy "never"` ⇒ codex doesn't ask), but if config drift ever
  produces an ask on a trusted turn, allow matches operator intent — trusted
  is trusted; deny would mysteriously break trusted seats on drift. The
  WARNING makes the drift visible. (Mirrors cursor: trusted picks the allow
  option.)
- any other policy: **deny.** Reply with the protocol's deny decision; emit a
  `command_denied` progress event
  `{command?, turn_id, item_id, kind: "command_denied", seq, deny_count,
  deny_budget}` (command text included when the request params carry it) so
  the denial is visible on the observability plane in real time, not only in
  the final result.
- **Per-turn deny budget** [round-1 convergent, 4 seats]: denials are counted
  per **active turn** (inter-turn denies are not budget-counted — they are
  anomalous and bounded by the `request()` timeout [GLM r2 N3]); up to
  `BRIDGE_APPROVAL_DENY_BUDGET` (env, default 10) the deny does NOT abort the
  turn — codex is told "no" and continues, and the turn ends normally with
  the model's own output (deny-and-continue: the most legible artifact for a
  human sender, endorsed by the round-1 panel). Exceeding the budget means
  the model is retry-looping instead of concluding; the turn is then ended
  per D2a.

### D2a — budget-exhaustion return path (round-2 convergent P1: agy, M3 F8, codex, in composition)

The v1.1 text was silent on what happens between "interrupt" and "return";
three seats independently found three hazards in that silence (queue
stragglers [agy], wedged-engine unbounded wait [M3], still-active turn
released healthy to the pool [codex]). The specified path satisfies all
three:

1. **Signalling:** `_respond_to_server_request` never returns control-flow
   decisions; it increments `self._deny_count` (engine state, reset at
   `turn/start`) and answers the ask (the budget-exceeding ask itself is
   still DENIED — never left unanswered; D1 holds unconditionally). The
   **turn loop** checks `self._deny_count > budget` after each handled
   message and owns the exit. [cold-Opus r2 P2-B, GLM r2 N1]
2. **Interrupt + bounded grace drain:** the loop calls `interrupt()`
   (fire-and-forget), then continues draining messages for
   `min(BRIDGE_APPROVAL_GRACE_S (default 10), remaining turn budget)` waiting
   for `turn/completed`, answering any further server-initiated requests per
   D1 the whole time.
3. **Grace success** (`turn/completed` arrives): return
   `TurnResult(ok=False, error="approval deny budget exhausted (<N> denials);
   last: <cmd>")` with the queue drained and the engine healthy → pool reuse
   is safe [agy satisfied].
4. **Grace expiry** (codex wedged/ignoring interrupt): set
   `self.healthy = False`, clear `active_turn_id`, return the same
   `TurnResult` immediately — no 3600s burn-down [M3 satisfied];
   `engine_pool.release()` quarantines unhealthy engines (CDX-3 fix, shipped)
   so the still-active turn can never be recycled into the idle pool
   [codex satisfied].

The decision-string vocabulary per method (e.g. `denied` vs `reject` vs a
`cancelled` outcome object) is pinned by the protocol probe (V1) — the design
holds for any spelling.

### D3 — make the latent path loud (darkness-shrinking analogue)

`bridge.py` warns at daemon start when a codex seat is configured so the
latent path can go live: non-bypass launch AND (any **`human`** sender in
`AGENT_TRUSTED_SENDERS` OR `--unknown-sender-policy human` [agy r2 P2 —
verified: the fallback at `bridge.py:849` makes unknown senders `human`-policy
without any roster entry; the flag defaults to `reject` per `bridge.py:2584`])
[scoped from "non-`trusted`" per round 1: `reject` senders are refused at the
envelope gate (`bridge.py:850–856`, verified) and never reach an engine, so
they cannot produce an ask]. One line, INFO on the
compliant config, WARNING when `approvalPolicy` can reach `on-request` — so
an operator standing up the first non-bypass seat learns the approval path is
now active (and, pre-fix, would have learned it was a trap). Today's fleet is
all-bypass, so no seat logs the WARNING (no spam).

### Rejected alternatives

- **Auto-approve non-trusted asks** — deletes the meaning of the `human`
  policy tier; bypass-with-extra-steps. Violates constraint 2.
- **Enumerate-and-handle only the three known methods, keep silent default** —
  re-creates AGY-2's open-set structure (constraint 3); a renamed method
  reintroduces the hang.
- **Abort the turn on first deny** — round-1 panel endorsed deny-and-continue;
  the deny budget (D2) bounds the retry-loop failure mode that motivated
  abort-on-first.
- **Fix by config ("just always use bypass")** — configurational, not
  structural; it is the status quo that makes the bug latent instead of fixed.

## Design v2 (specified now, built later): human-routed approvals

Everything below is fill-in for a future build; v1 ships none of it. The seam
v1 leaves: `_respond_to_server_request` takes its decision from a
`decide(request, context) -> Decision` hook that v1 binds to the static policy
rule. **The seam is blocking-with-deadline, not fire-and-forget** [round-1:
agy P1, GLM F6 — a sync-only seam cannot host v2]: the hook may block up to
`min(approval_timeout, remaining turn budget)`; v1's binding returns
immediately, v2's binding waits on a thread-safe reply queue fed by the
control lane. The engine's message loop is unaffected either way (the ask is
answered from the loop's thread before the next `_get_message`).

**v2 is NOT a pure fill-in in two named places** [codex r1 P2-1]:
`kind=approval_response` is a NEW envelope kind (envelope validation must
learn it) and `handle_control` today branches only on `steer`/`cancel`
(`bridge.py:1582`) — both extensions are part of the v2 build, listed here so
the estimate is honest.

- **Transport:** the approval ask is relayed as a bus envelope
  `kind=notify, event=approval_request` on the SENDER's inbox (the sender is
  the `human`-policy principal — the human's own dispatch channel is the
  natural answer path), payload
  `{task_id, turn_id, request_id, method, command, cwd, reason?, expires_at}`.
- **Answer path:** a `kind=approval_response` envelope (new), matched on
  `(task_id, request_id)`, carrying `decision: allow|deny` and `actor`. The
  bridge control lane (steer/cancel precedent, `handle_control`) is the entry
  point, so answers work mid-turn by construction.
- **Timeout semantics:** single deadline `min(approval_timeout, remaining turn
  budget)`, default 120s, env `BRIDGE_APPROVAL_TIMEOUT`. Expiry ⇒ the v1 deny
  path verbatim (fail-closed is the floor, always). One reminder event at 50%.
- **Audit:** every ask and answer is emitted on the audit plane
  (`arb-audit-emit`-compatible payloads; actor = the answering principal), so
  a human-approved command is attributable end-to-end.
- **Non-goals for v2:** no per-command allowlists, no approval memory
  ("always allow ls"), no cross-seat approval brokering. Each of those is a
  new trust surface needing its own design.

## Verification obligations (v1)

- **V1 — protocol probe (implementation gate, before impl starts [M3 r1 P1],
  not CI):** drive the installed `codex app-server` directly with
  `approvalPolicy: "on-request"` and a task that must run a command; capture
  the actual request method + params and the accepted decision shape. **Also
  probe the error reaction** [cold-Opus P2-A, GLM F1]: answer one ask with a
  JSON-RPC error and one with a deny, and record whether the command executes
  in each case (the fail-closed claim rests on error ⇒ no execution). The
  probe artifact (raw JSON lines) is committed alongside the impl brief.
  (test-behind-framework → drive directly.)
- **V2 — deny-proof:** with the handler present, a fabricated approval request
  on a `human`-policy turn yields (a) a reply on stdin whose **contents** are
  asserted (deny decision string + echoed request id [M3 r1]), (b) a
  `command_denied` progress event with `deny_count`/`deny_budget`, (c) turn
  completes without timeout. **Delete the handler ⇒ the test must go red** by
  observing the unanswered request (fabricated app-server fixture asserts it
  received a response — fixture must not short-circuit the guarded path).
- **V3 — unknown-method proof:** in BOTH loops (turn loop and inter-turn
  `request()` wait): a never-seen non-approval method gets `-32601` + warning
  log; a never-seen **approval-shaped** method gets the deny path; a **known**
  approval method arriving inter-turn gets the unconditional deny + log
  [GLM F5].
- **V4 — inertness proof (split)** [agy, M3, GLM r1]: (a) trusted-policy and
  bypass-seat turn fixtures that emit **zero server-initiated requests**
  produce byte-identical event streams pre/post change (no new events, no new
  stdin writes) — byte-identity is valid exactly because no ask ⇒ no new
  code path; (b) a separate case: a trusted turn WITH a fabricated ask gets
  the allow reply + WARNING and emits **no** `command_denied` event [M3 r1].
- **V7 — budget exhaustion (both D2a arms):** `BRIDGE_APPROVAL_DENY_BUDGET+1`
  fabricated asks on one `human` turn ⇒ budget denials with incrementing
  `deny_count`, the exceeding ask still answered (denied), then interrupt.
  Arm (a) fixture delivers `turn/completed` within grace ⇒ legible error
  TurnResult, engine still healthy, **no stale message leaks to the next
  turn on the same engine** [GLM N1]. Arm (b) fixture never completes ⇒
  return within grace bound, `healthy == False`, and
  `engine_pool.release()` quarantines (assert completion-observed OR
  unhealthy-before-release [codex r2]).
- **V5 — live gate (required, per live-verification-catches-cli-glue):**
  a real non-bypass codex seat + a `human`-policy dispatch whose task tries to
  run a command: observe deny + `command_denied` on the visibility plane +
  turn completing promptly (not at timeout). Control: same dispatch to a
  bypass seat behaves exactly as today.
- **V6 — hermeticity check (new scar, 2026-07-08):** all new tests must pin
  their environment explicitly (no host-state-dependent defaults; CI runs
  without a codex binary — fixtures only, binary probes live outside CI).

## Live gate V5 — EXECUTED 2026-07-08 (both arms green, impl `8791030` + remediation `c9194d9`)

- **Deny arm:** temp seat `codex-cdx1gate-dev` (this checkout, NON-bypass, sandbox
  `read-only`, `claude-bridge-dev=human`); dispatched "touch /tmp/cdx1-live-gate-executed".
  Turn completed in seconds (not the 300s timeout), sentinel NOT created, task events show
  `command_started → command_denied {deny_count:1, deny_budget:10} → command_finished
  status:"declined"` (task `7e39d6a6`, run `livegate-cdx1-145127`). The P0 hang is dead.
- **Control arm:** same dispatch to `codex-bridge-dev` (bypass, trusted): sentinel CREATED,
  reply "done", zero `command_denied` events — existing-fleet behavior byte-for-byte.
- **D3 notice:** live log-line capture was frustrated by stdout block-buffering in the
  ad-hoc (non-launchd) launch; evidence is unit-level (three tests pin level+message per
  config). Note for manual-seat recipes: `PYTHONUNBUFFERED=1` if file-redirecting stdout.
- **Impl panel:** `panel-cdx1impl-20260708T135206Z-db38be` — faithful-to-v1.2 from all
  five seats; all P2 findings remediated at `c9194d9` (agy's two escalated mechanisms
  refuted: turn-start reset + turn-id filter, both now test-pinned).

## Non-goals (v1)

- No sandbox-policy changes (`danger-full-access` handling untouched).
- No change to `approval_policy_for_policy`'s mapping itself (panel may
  challenge whether `reject`-policy senders should map differently — today
  they are refused upstream at the envelope gate, so the mapping never sees
  them).
- No human-routing (v2), no per-command policy, no UI.
- agy/pi/asdk engines untouched; cursor_acp untouched (already sound).
