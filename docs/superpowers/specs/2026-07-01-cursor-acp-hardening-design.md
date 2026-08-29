# `cursor-acp` hardening + fast-toggle + reliable arb-watch seat (design)

> Status: design, **round 2 complete** 2026-07-01, **all four seats APPROVE WITH NOTES — ready
> to implement.** (4-seat panel: codex contributor + cold-Opus + agy-print + pi-GLM judgment
> seat, all independent, both rounds.) Follows the
> `2026-07-01-cursor-acp-implementor-viability-brief.md` review, which ran the same decorrelated
> panel against `src/agent_redis_bridge/engines/cursor_acp.py` and converged unanimously: do not
> ship on the confirmed one-line fix alone; a broader hardening pass is required first. This
> spec is that pass. It also folds in two things the panel round didn't cover, added after the
> panel by direct follow-up: (1) the operator's requirement that a live cursor-acp seat show up
> reliably in arb-watch, verified empirically, not just by code-path inspection; (2) a
> genuine fast-mode on/off toggle, defaulting off, since Cursor's own ACP protocol describes
> `fast` as "significantly faster but consumes more usage" — directly relevant to this seat's
> reason for existing (a cheaper/faster alternative to Sonnet-tier implementors).
>
> **Round 1 result — REVISE (agy-print found a genuine P0 liveness-check race none of the
> other three caught; all four converged on five P1s), incorporated in round 1's revision.**
> **Round 2 result — no P0s; all four APPROVE WITH NOTES; one more unanimous P1 found**
> (`BRIDGE_MAX_PARALLEL=1` alone doesn't close the P1.2 dispatcher-hang risk — a
> crash-then-respawn re-triggers it; the actual fix is now a small `bridge.py` change, brought
> in-scope rather than deferred) **plus a converged fast-toggle warning-mechanism fix** (no
> `on_event` channel exists during `start()`; resolved via Python's `logging` module instead),
> **plus two cheap defense-in-depth additions** (a reader-thread-join hardening for the
> liveness check; an a-b-a trusted/non-trusted/trusted deployment-gate sequence). See inline
> `<!-- r1: ... -->` / `<!-- r2: ... -->` markers throughout for exactly what changed, when,
> and why. No reviewer required a third round.
>
> No seat has ever been deployed for this engine — this spec's "before/after" comparisons are
> against the merged-but-never-run code, not a live regression.

## Problem

Four independent reviewers (never exposed to each other's findings) converged on the same
substantive verdict for `cursor_acp.py`: the ACP handshake, streaming normalization, and
tool-event handling are sound and well-tested, but the engine has several defects that make
it unsafe to route real work to as-is, especially for the intended role — a faster/cheaper,
higher-volume, lower-per-task-scrutiny implementor lane comparable to the existing Sonnet-tier
or qwen3-coder-next lanes.

### P0 — confirmed, must fix before any live seat

**P0.1 — `set_session_mode_for_policy` sends an invalid mode for non-trusted policy**
(`cursor_acp.py:253-262`):

```python
def set_session_mode_for_policy(self, policy: str) -> None:
    ...
    mode_id = "agent" if policy == "trusted" else "default"
    self.request("session/set_mode", {"sessionId": self.session_id, "modeId": mode_id}, ...)
```

Live-probed against the real `agent acp` server (`/Users/<user>/.local/bin/agent`,
`2026.06.04-5fd875e`): the only valid `modeId` values are `agent`, `plan`, `ask` — `"default"`
returns JSON-RPC error `-32603 "Invalid mode ID: default. Valid modes: agent, plan, ask"`.
`set_session_mode_for_policy` runs unconditionally as the first step of every turn
(`cursor_acp.py:184`), and `request()` raises `EngineError` on any `"error"` field
(`cursor_acp.py:302-303`). `bridge.py` accepts `"human"` as a real, first-class sender policy
(`bridge.py:2218`, `:2361`) and passes it straight through to
`run_turn_with_progress(policy=policy)` — so every `"human"`-policy turn fails immediately,
before the prompt is ever sent. `"trusted"`-policy turns are unaffected.

**P0.2 — the permission plane ignores policy entirely, even after the mode fix** (all four
reviewers found this independently — the single highest-value finding of the round):
`_respond_to_client_request` (`cursor_acp.py:369-399`) auto-approves every
`session/request_permission` (picks an offered allow option and returns `selected`),
auto-cancels `cursor/ask_question`, and auto-accepts every `cursor/create_plan` —
**unconditionally, with no reference to `policy` at all.** The method never receives `policy`
as a parameter; nothing on the engine instance records it either. Cold-Opus's framing is the
sharpest: *"if you 'fix' P0.1 by sending `modeId="ask"` for non-trusted, the server will emit
`session/request_permission` prompts — which this handler auto-approves anyway. The mode label
changes but the containment does not."* Fixing only P0.1 produces a seat that looks
policy-gated and isn't — worse than the current all-fail state, because it fails silently
into full trust instead of failing loud.

### P1 — should fix before this seat is a default routing target

**P1.1 — no subprocess-liveness check in the turn loop** (`cursor_acp.py:197-246`,
`request()` at `:296-298`): if the `agent` subprocess crashes mid-turn, `_read_stdout`'s
`for line in stdout` loop ends and `self.messages` goes permanently empty. Neither
`run_turn_with_progress` nor `request()` check `self.process.poll()` inside their polling
loops — a dead process is indistinguishable from a slow one, so a crash costs a full
`turn_timeout` (default 3600s, bridge-side `AGENT_TURN_TIMEOUT` can lower this) of a hung slot
instead of a fast failure. `is_healthy()` exists (`cursor_acp.py:284-285`) but nothing consults
it during a turn.

**P1.2 — silent model-resolution fallback undermines the seat's entire cost/speed premise**
(`_set_model`, `cursor_acp.py:123-149`): if the configured model string doesn't resolve
against `availableModels`, or if `session/set_model` itself errors, the engine prints a
warning to stdout and continues on Cursor's default model — no bridge-visible event, no
failed `start()`. A rejected/typo'd model pin silently and invisibly defeats the "cheaper than
Sonnet" premise this seat exists for.

**P1.3 — a null/non-dict `session/prompt` result is reported as success**
(`cursor_acp.py:208-221`): when the JSON-RPC response's `"result"` field is absent or not a
dict, the engine returns `TurnResult(ok=True, ...)` with synthesized text
(`"ACP prompt N completed."`), bypassing the `stopReason` branch entirely. A genuinely
aborted/cancelled/malformed turn is laundered into an apparent success — directly corrupts the
throughput metric that would justify this seat's existence (a cheap-seat's value is measured
by tasks *correctly* completed per dollar).

**P1.4 — `stopReason: "refusal"` is treated as success** (codex's finding, unique to this
review — `cursor_acp.py:219-237`): `ok = stop_reason not in {"cancelled", "failed", "error"}`
omits `"refusal"`, which `src/agent_redis_bridge/engines/base.py:22-27` documents as a real
hard-stop the bridge's continuation guard already understands
(`bridge.py:1195-1204` treats `"refusal"` as non-continuable). Cursor's engine would report a
refusal as `ok=True`, then the bridge's own continuation logic would separately treat it as a
hard stop — an internally inconsistent, misleading task outcome.

### New requirement 1 — fast-mode toggle, default off

Not a bug — a feature gap discovered during follow-up investigation, directly relevant to
"faster/cheaper implementor" positioning. Live-verified (not just read from a third-party
report) against the real ACP server:

- `cursor_acp.py`'s `initialize` call (`:89-101`) does not advertise
  `clientCapabilities._meta.parameterizedModelPicker: true`. Without it, Cursor's ACP server
  collapses Composer 2.5 to a single exploded wire, `composer-2.5[fast=true]` — no non-fast
  variant is reachable through `session/set_model` in this mode (confirmed: `[fast=false]`,
  plain `composer-2.5`, and the CLI-catalog name `composer-2.5-fast` are all rejected as
  `"Invalid model value"`).
- **With that capability advertised**, `session/new` returns clean unbracketed `modelId`s and
  a genuinely separate `configOptions` entry:
  `{id: "fast", category: "model_config", currentValue: "true", options: [{value:"false",
  name:"Off"}, {value:"true", name:"Fast"}], description: "Significantly faster but consumes
  more usage"}`. Cursor's own description directly matches the operator's stated reasoning for
  wanting it off by default.
- **The set method is `session/set_config_option`**, params
  `{sessionId, configId: "fast", value: "false"}` — **string** values (`value: false` as a
  JSON boolean is rejected: `"expected string, got invalid_type"` at path `["value"]`).
  Confirmed live: the response echoes the full `configOptions` array with `fast.currentValue`
  genuinely flipped.
- Cursor's own default for `fast` is `"true"` — "off" must be set explicitly by the engine
  after `session/new`, it does not come for free.

### New requirement 2 — reliable, empirically-verified arb-watch seat

A parallel investigation (fork, read-only) into `tools/arb-watch-go/` and the Python
visibility/tailer pipeline found **no code gap**: `reduce.go:121-127`'s engine-label map
already has `"cursor": "cursor"`; `dedupSeatRuns`/`visibleSeats`/`upsertSeat` are all generic
on `seat_id`/`run_id`, no per-engine branching; `bridge.py`'s `handle_progress()` dispatches
purely on event-name string, not engine identity; `cursor_acp.py`'s event vocabulary
(`model_text`, `model_thinking`, `available_commands`, `command_started`, `command_finished`,
`session_info`, `session_update_unknown`) is a strict superset of `gemini_acp.py`'s, which has
a real deployed seat elsewhere in this fleet (`com.example.gemini-bridge.project-g-dev.plist`) — so this
render path already has real-world mileage under a different label. The turn-heartbeat
mechanism (`bridge.py:544-587`) is fully engine-agnostic.

**The remaining gap is empirical, not architectural**: nobody has ever watched a real
cursor-acp dispatch's events land on a live arb-watch screen — every piece of the above is
"the code path is shared/generic and gemini-acp exercises the same path," which is strong
circumstantial confidence, not a substitute for one real dispatch + one screen-watch. This
spec's deployment step (below) makes that observation a required gate, not optional
follow-up — the operator explicitly asked for "a reliable seat in arb," which this spec reads
as: verified live, not just architecturally plausible.

## Architecture

### Fix P0.1 + P0.2 together — genuine policy-aware trust boundary, not a cosmetic one

Store the active policy on the engine instance and gate the permission plane on it, mirroring
the existing pattern in `engines/grok_acp.py:208-210`
(`self._auto_approve_permissions = (policy == "trusted")`), rather than inventing a new
pattern:

```python
def run_turn_with_progress(self, task, *, timeout=3600, policy="trusted", on_event=None):
    self.policy = policy
    ...

def set_session_mode_for_policy(self, policy: str) -> None:
    ...
    mode_id = "agent" if policy == "trusted" else "ask"   # was "default" — the confirmed bug
    ...

def _respond_to_client_request(self, message):
    ...
    if method == "session/request_permission":
        if self.policy != "trusted":
            result = {"outcome": {"outcome": "cancelled"}}
        else:
            option_id = _select_allow_option(...)
            result = {"outcome": {"outcome": "selected", "optionId": option_id}} if option_id else {"outcome": {"outcome": "cancelled"}}
    elif method == "cursor/create_plan":
        result = {"outcome": {"outcome": "accepted" if self.policy == "trusted" else "cancelled"}}
    ...
```

`self.policy` needs a default set in `__init__` so `_respond_to_client_request` never sees an
unset attribute if it's ever invoked before the first `run_turn_with_progress` call.

<!-- r1: all four reviewers independently flagged the original text's choice of `"trusted"`
as this default. `_respond_to_client_request` is reachable via `request()` →
`_handle_client_message` (`cursor_acp.py:314`) during `start()`'s own `initialize`/
`authenticate`/`session/new` calls — i.e. before any turn has ever set `self.policy`. That
window is operationally unlikely to see a real `session/request_permission` (ACP servers
request permission per-tool-call during a prompt turn, not during session creation), but this
is exactly the kind of defense-in-depth attribute where defaulting to the *permissive* value
silently reintroduces the P0.2 defect this whole fix exists to close. **The default must be
`"human"`** (or an equivalent deny-sentinel), so an unset/pre-turn policy denies by default and
a `"trusted"` turn has to explicitly open the gate — never the reverse. -->

```python
def __init__(self, ...):
    ...
    self.policy = "human"   # deny-by-default; a real turn always overwrites this immediately
```

`"ask"` mode for non-trusted matches Cursor's own stated semantics ("Q&A mode — no edits or
command execution") — combined with the permission-plane deny, a non-trusted turn now
genuinely cannot mutate anything, instead of advertising a boundary it doesn't enforce.

**Do not** choose the alternative cold-Opus raised (delete the policy→mode mapping entirely,
document trusted-only) — the sibling ACP engines (`grok_acp.py`) already implement genuine
policy gating, so making `cursor-acp` trusted-only-by-design would be an inconsistent,
one-off carve-out rather than matching the fleet's existing convention. Build it the same way
the others already work.

### Fix P1.1 — liveness check

<!-- r1 (P0, agy-print — the round's one genuine must-fix, kept on its merits though only one
of four reviewers caught it): checking `self.process.poll() is not None` unconditionally at
the top of each loop iteration races the reader thread. `_read_stdout` and the main loop are
two independent, unsynchronized threads — if the subprocess writes its final response and
exits immediately after, there is no guarantee the main loop's `poll()` check runs *after* the
reader thread has drained that final message into `self.messages`. A poll-first ordering can
therefore report "process exited unexpectedly" on a turn that actually completed successfully,
discarding a real result. -->

Inside both polling loops (`run_turn_with_progress`'s `while time.monotonic() < deadline:` and
`request()`'s equivalent), check `self.process.poll() is not None` **only when
`_get_message()` returns `None`** (i.e. its own short internal timeout expired with nothing in
the queue) — never unconditionally before attempting to read a message. This guarantees the
queue gets first refusal on every iteration; a dead process is only diagnosed once a read
attempt has genuinely come up empty, so a message that was already sitting in the queue (or
arrives before the next `_get_message` timeout) is always drained and returned first:

```python
message = self._get_message(timeout=...)
if message is None:
    if self.process.poll() is not None:
        # queue confirmed empty AND process confirmed dead — not a race, a real crash
        raise EngineError("cursor process exited unexpectedly")  # or return ok=False in the turn loop
    continue
...
```

<!-- r2 (P2, codex + agy-print independently, near-identical proposed fix — cheap
belt-and-suspenders, not a required blocker): `_get_message()` only inspects the already-`put()`
Python queue, not the OS pipe directly — in principle, under extreme scheduling starvation, the
reader thread could still be mid-flight (blocked on a read that already has the final line
buffered, just not yet scheduled to run) at the exact moment `_get_message` times out and
`poll()` is checked. In practice this window is astronomically unlikely to matter (the reader
thread has the entire `_get_message` timeout — up to 5s in the turn loop, 2s in `request()` —
to get scheduled and drain an already-buffered line), so this is defense-in-depth, not a fix
for an observed defect. Free to add given both reviewers proposed nearly the same shape: -->

On a dead process, **join the reader thread with a short bound (e.g. 1s) before declaring
crash**, then do one final non-blocking queue check, so a reader thread merely awaiting
scheduling (not stuck) gets one last chance to drain a buffered final line:

```python
message = self._get_message(timeout=...)
if message is None:
    if self.process.poll() is not None:
        if self.reader_thread is not None:
            self.reader_thread.join(timeout=1.0)
        message = self._get_message(timeout=0)
        if message is None:
            raise EngineError("cursor process exited unexpectedly")
        # else: fall through and process the drained message normally
    else:
        continue
```

Break immediately with `ok=False, error="cursor process exited unexpectedly"` (turn loop) /
raise `EngineError("cursor process exited unexpectedly")` (`request()`) — same outcome as
originally specified, just gated correctly and now hardened against the residual scheduling
window above.

### Fix P1.2 — fail loud on model-resolution failure

In `_set_model`: if `_resolve_model_id` returns `None` for an operator-configured (non-empty)
model string, raise `EngineError` from `start()` instead of printing and continuing — mirrors
the existing fail-fast precedent already in `start()` for a missing `session/new` `sessionId`
(`cursor_acp.py:105-109` <!-- r1 (P2, codex): was miscited as `:88-90`, the `initialize` call;
the actual `session/new`-missing-`sessionId` raise is at `:105-109` -->). Same treatment if
`session/set_model` itself returns a JSON-RPC error — today that's swallowed to a warning
(`:148-149`); make it fatal too. An operator who didn't configure a model at all (the
default/unset case) is unaffected — only an explicit, unresolvable pin fails loud.

<!-- r1 (P1, agy-print + cold-Opus + pi-GLM independently, cold-Opus traced the exact
mechanism): a raised `EngineError` from `start()` does not reach the dispatcher cleanly in
every bridge code path. r1's proposed mitigation was `BRIDGE_MAX_PARALLEL=1` (pin the seat to
single-slot so only warmup ever calls `start()`). r2 (all four reviewers, independently,
unanimous — the single strongest finding of round 2) found that mitigation's own claim false:
"warmup is the only path that calls `start()`" does not survive a crash. Superseding text
below; the `MAX_PARALLEL=1` constraint stays (still good hygiene, still avoids the *original*
concurrent-scale-up trigger) but is no longer load-bearing on its own — the actual fix is now
in `bridge.py`. -->

**r2: the bridge.py pool-acquire gap is now fixed, not deferred.** Traced mechanism
(unanimous across codex/agy-print/cold-Opus/pi-GLM in round 2): under `MAX_PARALLEL=1`, a
mid-turn subprocess crash (exactly the scenario P1.1 exists to detect) causes
`EnginePool.release()` to see `is_healthy() == False`, decrement `_started` to `0`, and drop
the engine (`engine_pool.py:109-127`) — it does **not** go back to idle. The *next* dispatch
then calls `pool.acquire(envelope.id, ...)` (`bridge.py:817`), finds `_started(0) <
max_size(1)`, and re-runs `_factory()` + `start()` **during request handling**
(`engine_pool.py:83-92`) — the identical uncaught-path the `MAX_PARALLEL=1` constraint was
supposed to make unreachable: the local `try` at `bridge.py:816-835` catches only
`AffinityMissError`/`AffinityBusyError`/`AffinityAmbiguousError`, so a generic `EngineError`
from this respawn's `start()` escapes to the broad `except Exception` in the inbox loop, the
envelope is dropped **with no reply**, and the dispatcher hangs to its full timeout — the
crash-then-respawn path re-opens the exact gap `MAX_PARALLEL=1` claimed to close, just
triggered by "first dispatch after a crash" instead of "second concurrent dispatch." Given the
P1.1 fix makes crashes a first-class detected outcome (not a silent hang) and P1.2 makes
`start()` itself newly fallible, this respawn path is a realistic, not exotic, event for an
unproven seat.

**Fix (bridge.py, small and generic — benefits every engine, not cursor-acp-specific):**
broaden the `try/except` around `pool.acquire()` at `bridge.py:816-835` to also catch a
generic engine-start failure and reply cleanly instead of dropping the envelope:

```python
try:
    engine = self.pool.acquire(envelope.id, thread_id=getattr(envelope, "thread_id", None))
except (AffinityMissError, AffinityBusyError, AffinityAmbiguousError) as exc:
    ...  # existing handling, unchanged
except EngineError as exc:
    self.send_reply(envelope, TurnResult(ok=False, result="", error=f"engine-start-failed: {exc}"))
    return False
```

This is a `bridge.py`-level fix affecting the shared pool-acquire path for every engine, but
it's now **in scope for this pass** — round 1 deferred it as a separate backlog item on the
theory that `MAX_PARALLEL=1` made it unreachable for cursor-acp specifically; round 2
established that premise was wrong, so the fix has to land here for this pass's own
"fail loud, don't hang" goal to actually hold. `BRIDGE_MAX_PARALLEL=1` for the cursor-acp seat
is retained as good hygiene (still avoids the original multi-slot scale-up trigger entirely),
but the bridge.py fix is what actually closes the respawn-after-crash gap. -->

<!-- r2 (P2, agy-print + cold-Opus — belt-and-suspenders, cheap): add a crash-then-redispatch
observation to the deployment gate (see gate step 3 below) so this path is verified live, not
just reasoned about. -->



### Fix P1.3 — treat non-dict prompt result as failure

In `run_turn_with_progress`, when `message.get("result")` is not a dict for the matched
`prompt_id`, return `TurnResult(ok=False, result="".join(chunks).strip(), error="cursor ACP
returned a null/malformed prompt result")` instead of synthesizing `ok=True`.

<!-- r1 (P2, codex + cold-Opus independently): the current non-dict-result branch
(`cursor_acp.py:212-216`) also emits a `turn_completed` progress event with `ok=True` before
returning — the spec's fix only changed the returned `TurnResult`, not this event. Update the
emitted `turn_completed` event's `ok` field to `False` here too, matching the dict/stopReason
branch's behavior (`:222-231`) — otherwise arb-watch/status would show a contradictory
"completed successfully" event for a turn the return value now reports as failed. -->


### Fix P1.4 — `refusal` is a hard stop

Add `"refusal"` to the `stop_reason` failure set:
`ok = stop_reason not in {"cancelled", "failed", "error", "refusal"}` — aligns with
`engines/base.py`'s documented contract and `bridge.py`'s own continuation-guard treatment of
refusals (`bridge.py:1198-1206`, `_continuable` <!-- r1 (P2, pi-GLM): was miscited as
`:1195-1204` -->), so the two layers agree instead of one calling it success and the other
calling it a hard stop. Verified no double-negative: `bridge.py`'s drive-to-completion loop
checks `not result.ok` first (`:1171-1175`) and returns immediately on failure, before ever
reaching `_continuable` — so `ok=False` for a refusal makes the loop exit earlier and for the
same reason `_continuable` would have stopped it anyway; the two checks agree, they don't
fight, and `orchestrator_commit` (gated on `result.ok`) correctly stops running commit/adopt
logic on a refusal's non-output.

<!-- r1 (P2, agy-print, corroborated by cold-Opus — noted, not fixed here): `engines/grok_acp.py`
has the textually identical omission (`ok = stop_reason not in {"cancelled", "failed",
"error"}`, no `"refusal"`). After this fix ships, cursor-acp and grok-acp will disagree on
refusal semantics. Fixing grok-acp is out of scope for a cursor-acp-only hardening pass —
flagging the divergence here so it's a known, tracked follow-up rather than a silent
inconsistency discovered later. -->


### Build fast-toggle — default off, seat-level standing default

<!-- r1 (P1, all four reviewers independently — codex, agy-print, cold-Opus, pi-GLM): the
original heading said "per-dispatch override" and step 3 claimed an operator could "opt a
specific dispatch into fast mode." That is not achievable with a constructor/CLI-flag
mechanism alone: `bridge.py`'s pooled engines are built once via a factory that captures
`self.args` at daemon-launch time (`bridge.py:421`, `factory=lambda: build_engine(self.args,
cwd=...)`), and the pool reuses the same idle instance across dispatches
(`engine_pool.py:83-95`) — `fast` would be applied once in `start()` and never revisited.
Even the worktree per-dispatch path (`bridge.py:966-967`) rebuilds from the bridge's standing
`self.args`, not from anything envelope-derived. So what's actually being built is a
**seat-level standing default**, retitled accordingly below. Genuine per-dispatch override
would require threading a `fast` field through `Envelope.payload` and re-issuing
`session/set_config_option` inside `run_turn_with_progress` before each prompt — a materially
bigger change, explicitly deferred, not part of this pass. -->

1. `start()`'s `initialize` call (`cursor_acp.py:89-101`) adds
   `"_meta": {"parameterizedModelPicker": True}` to `clientCapabilities`.
2. After `session/new` (and after any explicit `_set_model` call, since model selection and
   fast-toggle are separate `configOptions` entries and either order should work per the live
   probe, but doing model first then fast keeps the two independent concerns in a predictable
   order), call `session/set_config_option` with `{sessionId, configId: "fast", value:
   "false"}` **unless the operator explicitly requested fast mode** — so the default is off
   without any caller action, matching the operator's stated requirement. **JSON path (r1,
   resolved — live-verified this session, not left ambiguous):** `configOptions` is a
   **top-level** key of the `session/new` result, a sibling of `models`, i.e.
   `response["configOptions"]` — **not** nested under `response["models"]`. Locate the entry
   with `id == "fast"` in that top-level list.
3. Add a new constructor/config parameter (`fast: bool = False`) threaded from `bridge.py`'s
   engine-construction site the same way `model` already is (`bridge.py:2420`,
   `CursorAcpEngine(cwd=cwd, model=args.model)` → add `fast=getattr(args, "cursor_fast",
   False)`), and a new `agent-dispatch`/CLI flag (e.g. `--cursor-fast`) so an operator can
   change the **seat's standing default** at launch time. Bridge-wide default for the flag is
   `False`, matching "default off." <!-- r1: dropped the "opt a specific dispatch" claim — see
   note above. --> A future per-dispatch mechanism, if wanted, is a separate follow-up spec.
4. If the ACP server ever returns `configOptions` without a `"fast"` entry (e.g. a future
   protocol version, or an account without the capability), treat that as: skip the
   `set_config_option` call entirely (there's nothing to set) rather than erroring — this is a
   capability-detection question, not a hard requirement, and Composer 2.5 is not necessarily
   the only model this engine will ever run.
5. <!-- r1 (P1, codex + cold-Opus + pi-GLM independently): the original spec only handled
   `"fast"` being *absent* from `configOptions`, not `session/set_config_option` itself
   *erroring* when `"fast"` IS offered (e.g. a transient server fault). Left unspecified, two
   implementors could reasonably pick opposite behaviors. Resolved here: -->
   **If `"fast"` is offered but `session/set_config_option` returns a JSON-RPC error, this is
   non-fatal** — unlike P1.2's model-resolution failure (which changes *what ran*, a
   correctness/identity concern), a failed fast-off call only changes *cost/latency*, and
   Cursor's own default (`fast="true"`) is a degraded-but-working state, not a wrong-model
   state. Do not raise from `start()` for this specific failure.

   <!-- r2 (P1, all four reviewers independently — codex, agy-print, cold-Opus, pi-GLM): r1
   said "log a bridge-visible warning event" without naming a mechanism, and none exists —
   `start()` has no `on_event` callback (that's turn-scoped only, `cursor_acp.py:179`), so the
   spec forbade the one real mechanism (`print()`) and named one that doesn't exist. Resolved:
   drop the "bridge-visible warning event" language entirely — use Python's standard `logging`
   module instead (agy-print's suggestion, the simplest of the four reviewers' proposed
   alternatives, and the only one requiring no new engine-interface plumbing): -->
   Emit via `logging.getLogger("agent_redis_bridge.engines.cursor_acp").warning(...)`, not a
   bare `print()` — this is captured by the bridge daemon's configured log handlers (unlike a
   raw stdout print, which is genuinely invisible outside the process's own console) without
   requiring a new `on_event`/health-callback parameter on `start()`. This is a real,
   implementable distinction from a `print()` (log handlers can be routed, filtered, and
   persisted independently of stdout capture), just not the turn-level bus event the r1 prose
   implied.

### Deployment + live verification gate (new requirement 2)

Only after the P0/P1 fixes and the fast-toggle land, and only after the panel round below
converges clean:

1. Stand up `com.example.cursor-bridge.bridge-dev` (mirroring the existing
   `agy-bridge.bridge-dev`/`codex-bridge.bridge-dev` plist pattern — `AGENT_PROJECT=bridge`,
   `AGENT_WORKDIR=/Users/<user>/<workspace>`, `AGENT_ENV_FILE` pointed at
   `envs/agent-redis-bridge-dev.env`), tool prefix `cursor` per the existing
   `ENGINE_TO_TOOL["cursor-acp"] = "cursor"` mapping — target id `cursor-bridge-dev`.
   **`BRIDGE_MAX_PARALLEL=1`** (retained as good hygiene per the P1.2 fix note above — r2: no
   longer the sole safeguard against the scale-up/respawn gap, since the `bridge.py`
   pool-acquire fix now closes that directly, but still avoids the original concurrent-scale-up
   trigger entirely and matches the existing single-slot pattern used elsewhere in this fleet).
2. Prove the auth/handshake round-trip live before trusting the seat (the same
   `initialize`→`authenticate`→`session/new` probe already run manually this session) — a
   deployment gate, not a code change.
3. <!-- r1 (P1, all four reviewers — the round's single most-repeated finding): the original
   text said "dispatch one real, bounded task" without specifying its policy. An
   orchestrator/peer dispatch resolves to `policy="trusted"` (`bridge.py:777`), which drives
   `mode_id="agent"` and the auto-approve branch — exactly the path that was never broken.
   The riskier half of P0.1+P0.2 — `mode_id="ask"` plus the new permission-deny for
   non-trusted policy — is what this whole pass is actually about, and a trusted-only gate
   would ship it completely unverified. Expanded to two required dispatches. r2 (P2,
   agy-print): sequence them **trusted → non-trusted → trusted (a-b-a)**, not just a-then-b, to
   also confirm the stateful ACP session recovers cleanly back to `agent` mode after an
   `ask`-mode detour rather than staying pinned/polluted. --> Dispatch real tasks to the new
   seat in this sequence, all watched rendering in arb-watch (or `events:live` directly) —
   confirm roster entry, live streaming text, tool-call display, and turn completion all show
   up as expected for each:
   - **(a) a trusted, bounded, non-mutating-or-mutating task** — proves the already-working
     `agent` mode + auto-approve path still works post-refactor.
   - **(b) a non-trusted (`"human"`-policy) dispatch, in two parts**: first a purely
     non-mutating prompt (e.g. "explain this file") — must complete normally under `ask` mode;
     then a prompt that requires a tool call / mutation — must be cleanly denied (turn ends
     with a clear `ok=False`/refusal-shaped result), **not** spin. <!-- r1 (P1, pi-GLM +
     agy-print independently, sharper than "just test it"): if the mutation-attempt case
     instead shows the model retry-looping against repeated permission denials — the P1.1
     liveness check will NOT catch this, since the subprocess stays alive, just spinning, so
     the turn would burn the full `turn_timeout` — implementation must add a bounded
     consecutive-permission-denial cap (e.g. after N denials in one turn, fail the turn with a
     clear error) before this seat is trusted for any non-trusted-policy traffic at all. Do
     not ship the `ask` mapping on the strength of case (a) alone. --> Confirm case (b) also
     shows `fast` genuinely defaulting to off in the live session state (or `set_config_option`
     response), and confirm the configured model pin took effect — folding the fast-toggle and
     model-pin verifications into this same live gate rather than trusting the unit tests
     alone for real-server behavior.
   - **(a again)** — a second trusted dispatch on the same seat, confirming `agent` mode and
     auto-approve resume cleanly after (b)'s `ask`-mode detour.
   - <!-- r2 (P2, agy-print + cold-Opus independently): observe one crash-then-redispatch cycle
     — kill the `agent` subprocess mid-turn (or let a real crash happen) and confirm the next
     dispatch either respawns cleanly or, if the respawn's `start()` fails, the dispatcher gets
     a clean `ok=False` reply (per the bridge.py fix above) rather than hanging to timeout. This
     is the one process-lifecycle transition the rest of this gate never otherwise exercises. -->
   - <!-- r1 (P2, agy-print): a concurrent-dispatch/profile-collision test was suggested here.
     Moot given the `BRIDGE_MAX_PARALLEL=1` constraint above — only one `agent acp` process
     runs at a time for this seat, so there is no concurrent-headless-Cursor state to collide.
     Revisit only if `max_parallel` is ever raised for this seat later. -->
4. A short soak — a handful of real dispatches over the course of normal use, not a synthetic
   load test — before treating this as a default routing target for bounded lower-end tasks,
   per the panel's quota/rate-limit caution (Cursor's stack is interactive-IDE-first; headless
   bridge cadence is unprobed territory that no code review can resolve). <!-- r1 (P2, pi-GLM):
   until the soak has actually run, this seat stays explicitly non-default-routed — do not
   promote it in `docs/agent-role-routing.md`/`docs/implementor-routing.md` (already flagged by
   the prior panel as currently aspirational) — so the qualitative deferral doesn't silently
   become permanent. -->


## Testing

`tests/test_cursor_acp.py` currently has **zero coverage** of the non-`"trusted"` policy
branch — every `run_turn_with_progress` call in the suite passes `policy="trusted"`. Add:

- `policy="human"` (or any non-`"trusted"` value) → `set_session_mode_for_policy` sends
  `modeId="ask"`, not `"default"`; permission requests get `outcome: "cancelled"`, not
  `"selected"`; plan requests get `outcome: "cancelled"`, not `"accepted"`.
- Unresolvable configured model (operator explicitly set one) → `start()` raises `EngineError`,
  not a silent continue. Existing test `test_start_skips_set_model_when_unresolvable` currently
  asserts the *old* (silent) behavior for this exact case — update it to assert the raise
  instead, don't leave both behaviors "tested."
- `session/set_model` returning a JSON-RPC error → `start()` raises.
- `session/prompt` response with `result: null` (or `result` key absent) → `TurnResult(ok=False,
  ...)`, not `ok=True`.
- `stopReason: "refusal"` → `TurnResult(ok=False, ...)`.
- Process death mid-turn (`FakeProcess.poll()` returns non-`None` after some point) → the turn
  loop and `request()` both exit promptly with an error, not spin to timeout. Use a short
  `timeout` value in the test so a regression (spin-to-timeout) fails fast rather than hanging
  the test suite.
- `_meta.parameterizedModelPicker: true` is present in every `initialize` call's
  `clientCapabilities` — a structural assertion on the sent JSON-RPC payload, not just on
  behavior.
- Default construction (`fast` not specified) → no `session/set_config_option` call for
  `"fast"` is made... **unless** the default is `False` and the fixture's `session/new`
  response includes a `"fast"` configOption (as a **top-level** `configOptions` key, sibling
  of `models` — r1: JSON path resolved, see fast-toggle step 2 above), in which case a
  `set_config_option(configId="fast", value="false")` call IS expected (matches "default off,
  explicit call required" from the design — pick the fixture shape that matches the
  parameterizedModelPicker-enabled response, since that's what `initialize` now always
  requests). `fast=True` construction → the call is made with `value: "true"` instead.
- A `session/new` fixture response with no `"fast"` configOptions entry at all → no
  `set_config_option` call is attempted (capability-detection path, not an error).
- **r1 additions (all four reviewers' P1s, now closed by tests):**
  - `set_config_option` for `"fast"` returning a JSON-RPC error → `start()` does **not** raise
    (non-fatal per the r1/r2 fast-toggle step 5), a `logging.warning(...)` call fires
    (assertable via Python's `unittest.TestCase.assertLogs` or an equivalent log-capture
    fixture — r2: not a bus/event assertion, since no such channel exists during `start()`),
    and the engine still starts successfully.
  - A client request (e.g. `session/request_permission`) routed through `request()` **before**
    `run_turn_with_progress` has ever run (simulating the pre-turn handshake window) → the
    permission is cancelled, not auto-approved — a direct regression guard on the `self.policy
    = "human"` `__init__` default (would fail if the default were still `"trusted"`).
  - Process death mid-turn, with a valid final response already sitting in the queue *ahead of*
    the death being observable via `poll()` → the turn returns the real successful result, not
    a spurious "process exited unexpectedly" — the direct regression guard for the P1.1 race
    fix (would fail under the original poll-first ordering).
  - Non-dict prompt result → the emitted `turn_completed` progress event carries `ok=False`
    (not just the returned `TurnResult`) — regression guard for the P1.3 event-consistency fix.
- **r2 additions:**
  - Process death detected via `poll()`, with the reader thread's join returning a drained
    final message on the last-chance check → the turn returns the real result, not a crash
    error — regression guard for the r2 reader-thread-join hardening (belt-and-suspenders; not
    expected to ever fire in practice, but should be exercisable in a fixture where
    `FakeProcess`'s reader thread is deliberately made to finish exactly at the boundary).
  - **`bridge.py`-level test (new, since the fix now lives there):** `pool.acquire()` raising a
    generic `EngineError` (simulating a failed respawn after a crash) → the dispatcher receives
    a clean `TurnResult(ok=False, ...)` reply, not a dropped envelope / silent hang — regression
    guard for the r2 pool-acquire fix. This test lives in `tests/test_bridge.py` (or wherever
    `bridge.py`'s existing pool-acquire/affinity tests live), not `test_cursor_acp.py`.

**Live verification** (not unit-testable): the deployment gate above (real dispatch, watch
arb-watch render it) stands in for what no fixture-based test can prove — that the real
Cursor ACP server's actual behavior matches everything this spec assumes about it.

## Risks / open questions

1. **`ask` mode for non-trusted has not been confirmed to still permit no-tool-use prompt
   turns to complete normally** (only that `session/set_mode` accepts it, and that
   `session/request_permission` prompts occur that this fix now cancels). **r1: this is no
   longer just a note — the deployment gate (step 3) now requires live confirmation of both
   the non-mutating and mutating-and-denied cases before the seat is trusted for any
   non-trusted traffic**, and a consecutive-denial cap is a required follow-up fix if the
   mutating case spins instead of failing cleanly. If Cursor's `ask` mode simply never asks for
   tool permission in the first place (by design, since it's "Q&A... no edits or command
   execution"), the permission-deny code path may rarely fire under `ask` mode specifically —
   still correct to keep as defense-in-depth in case a future non-trusted mode does request
   tools.
2. **Fast-toggle ordering** (model-select then fast-config, vs. the reverse) is asserted safe
   based on the live probe's specific sequence tested this session; if a different order
   produces different server-side behavior, that would surface as a test/live-verification
   failure, not a silent bug, given the testing plan above.
3. **`parameterizedModelPicker` is confirmed via this account/CLI version only**
   (`2026.06.04-5fd875e`). A future Cursor CLI update could change this behavior; the
   capability-detection fallback in the fast-toggle design (skip the call if `"fast"` isn't in
   `configOptions`) is the guard against that, not a version pin.
4. **Soak-test scope is intentionally left qualitative** ("a handful of real dispatches over
   the course of normal use") rather than a specific number/duration — no reviewer or this
   spec has enough live data yet to propose a real threshold. Revisit once the seat has run for
   a while.
5. **r1 addition, r2: item (a) resolved, no longer deferred.** ~~The bridge's pool-acquire
   error handling has a pre-existing gap... mitigated here only by pinning
   `BRIDGE_MAX_PARALLEL=1`~~ — r2 found `MAX_PARALLEL=1` doesn't actually close this (a
   crash-then-respawn re-triggers the identical uncaught-path), so the `bridge.py` fix
   (broadened `try/except` around `pool.acquire()`, see the P1.2 fix section above) is now
   **in scope and required** for this pass, not a deferred follow-up. (b) still deferred,
   unchanged: `engines/grok_acp.py` has the identical refusal-omission bug P1.4 fixes here —
   after this ships, cursor-acp and grok-acp will disagree on refusal semantics until grok-acp
   gets the same one-line fix separately.
6. **r2 addition:** the reader-thread-join hardening (P1.1) and the `logging`-based fast-toggle
   warning (fast-toggle step 5) are both defense-in-depth additions with no observed defect
   behind them — cheap to include, not blockers if an implementor judges them unnecessary, but
   included since two/four reviewers independently converged on each.
