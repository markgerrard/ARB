# Design: `pi-rpc` bridge engine

**Date:** 2026-06-04
**Status:** Revised v2 after tri-model review (codex: REQUEST CHANGES, agy: REQUEST
CHANGES, cold-opus: APPROVE WITH NOTES). All findings folded in below; see the panel's
review report for the full record.
**Affects:** `src/agent_redis_bridge/engines/` (new file), `bridge.py`, `ctl.py`,
`scripts/agent-dispatch`, example env + systemd template, tests.

## 1. Purpose

Add `pi` (the Earendil `@earendil-works/pi-coding-agent` CLI, installed at
`/home/<user>/.npm-global/bin/pi`) to the bridge as a new engine, peer to the
existing `codex`, `gemini-acp`, `grok-acp`, and `agy-print` engines.

`pi` is a terminal coding agent supporting 15+ providers (Anthropic, OpenAI,
Google, xAI, Mistral, Groq, Cerebras, Bedrock, and custom via `models.json`,
including MiniMax-M3). It exposes a headless `--mode rpc` interface: a JSON-lines
protocol over stdin/stdout. Adding it gives the bridge a single uniform engine
that can reach any of pi's providers/models.

**Role:** role-neutral conduit. Reviewer / worker behavior is a per-bridge-instance
configuration (a `pi-review` unit vs a `pi-worker` unit), not a per-dispatch switch.
Specific downstream uses are deferred — the goal of this work is to wire pi in cleanly.

**Driving use case for parallelism (informs §5.5):** run two review tasks
concurrently on *different* pi models — e.g. one on `kimi-coding/kimi-k2-thinking`
and one on `minimax/MiniMax-M3` — to get cheap model-diverse second opinions. Both
models are confirmed present in this host's `~/.pi/agent/models.json`
(`pi --list-models`).

## 2. Approach (decision)

**Chosen: persistent subprocess RPC engine.** Spawn `pi --mode rpc` once, keep it
warm in the existing `EnginePool`, talk the JSON-lines protocol over stdio. This
mirrors `engines/gemini_acp.py` / `engines/grok_acp.py` (reader thread → message
queue → turn loop) and satisfies the `AgentEngine` Protocol with **zero changes to
`bridge.py`'s dispatch core**. It inherits warm-pool reuse, steering, progress
events, and worktree isolation for free.

**Rejected — one-shot `pi --print` per turn:** cold process per task; loses warm-pool
reuse, breaks the persistent-session contract every other engine honors, no streaming
progress.

**Rejected — pi SDK / `AgentSession` embed:** that API is Node/TypeScript; the bridge
is Python.

## 3. The engine contract

New file `src/agent_redis_bridge/engines/pi_rpc.py`, class `PiRpcEngine`,
implementing the `AgentEngine` Protocol from `engines/base.py`:
`start()`, `run_turn_with_progress(task, *, timeout, policy, on_event) -> TurnResult`,
`steer()`, `interrupt()`, `stop()`.

Structure copied from `gemini_acp.py` (reader thread + `queue.Queue` + `_send` under a
`send_lock`), with one **mandatory deviation** the review caught:

**Framing — read in BINARY mode, not text mode (review: agy major).** pi's RPC uses
strict JSONL with LF as the *only* delimiter. Python's `text=True` /
`for line in stdout` uses *universal newlines*, which also splits on bare `\r` (and the
docs warn about U+2028/U+2029). A model response or file content containing a `\r`
would split a JSON object mid-record — both halves fail `json.loads`, and if the split
lands on the terminal `agent_end` the turn hangs to its deadline. So:

- Spawn with `text=False` (binary). The reader accumulates `bytes`, splits strictly on
  `b"\n"`, strips a trailing `b"\r"`, then `decode("utf-8", errors="replace")` and
  `json.loads`. (This is stricter than `gemini_acp.py`, which has the same latent issue
  but rides on ACP not emitting bare `\r`; we do not assume that for pi.)
- `_send()` writes `json.dumps(...).encode("utf-8") + b"\n"` to stdin under the lock.

A malformed/undecodable line is skipped (non-fatal), same as gemini.

## 4. Turn loop & data flow

### start()

Spawn `pi --mode rpc --no-session [tool-flags] [--model <provider/id>]` (binary mode,
§3) with `cwd=workdir`. pi needs no `initialize`/`session/new` handshake, but **do a
cheap readiness probe (review: cold-opus minor)** rather than launching blind: send
`{"id":<x>,"type":"get_state"}` and wait briefly (e.g. 10s) for the id-matched
response. If the process exited or doesn't answer (bad `--model`, bad `--tools`,
missing provider key), raise `EngineError` now — otherwise a misconfigured instance
registers as "alive" (heartbeat is independent of the engine) and the failure only
surfaces on a user's first dispatch, after a wasted round-trip + timeout.

### run_turn_with_progress(task, ...)

0. **Drain the queue first (review: agy/codex major — stale-event contamination).**
   The engine is warm and pool-reused; a prior turn that timed out/aborted may have left
   late events (`message_update`, a trailing `agent_end`, an `abort` response) queued.
   Before sending the new prompt, fully drain `self.messages` (`get_nowait` until empty)
   so this turn can't read another turn's tail and terminate early.
1. Send `{"id": <pid>, "type": "prompt", "message": task}` with a unique id.
2. **Handle the prompt response before waiting for `agent_end` (review: codex major).**
   `prompt` returns a `{"type":"response","command":"prompt","id":<pid>,"success":bool}`.
   If `success:false`, the prompt was rejected pre-acceptance and **no `agent_end` will
   come** — return `TurnResult(ok=False, error=...)` immediately instead of blocking to
   the deadline. Intervening events seen while waiting for the response are processed
   normally (step 3).
3. Read events until terminal `agent_end`, mapping each to a bridge progress event via
   `on_event`, mirroring what `normalize_session_update` emits for gemini:
   - `message_update` / `text_delta` → append `delta` to `chunks`; emit `model_text`.
     A `message_update` with `assistantMessageEvent.type == "error"` (reason
     `aborted`/`error`) is a turn-ending error → `ok=False`.
   - `tool_execution_start` → `command_started`; `tool_execution_end` →
     `command_finished` (`isError` → exit code 0/1).
   - `auto_retry_*`, `compaction_*` → optional informational events. A final
     `auto_retry_end{success:false}` is a turn-ending error → `ok=False`.
   - `agent_end` → normal terminal: stop reading this turn.
   The loop's exit conditions are therefore: `agent_end` (normal), a turn-ending error
   event, prompt `success:false`, or the timeout deadline — it can never block forever
   waiting for an `agent_end` that won't arrive.
4. **Final result via id-matched `get_last_assistant_text` (review: codex/cold-opus).**
   After `agent_end`, send `{"id":<gid>,"type":"get_last_assistant_text"}` and read the
   response by **matching `type=="response"` AND `id==<gid>`** (draining non-matching
   late events), exactly like gemini's `request()`/`_get_message` id loop. Read
   `response["data"]["text"]` (NOT a top-level `text`). Fallback chain: `data.text` →
   `"".join(chunks).strip()` → placeholder `f"pi-rpc prompt {pid} completed."` so a
   tool-only turn never returns an empty string (matches gemini's behavior).
5. **Timeout — poison, don't silently reuse (review: agy/codex major).** On deadline,
   emit `turn_timeout`, send `{"type":"abort"}`, then mark the engine **unhealthy** so
   the pool discards/restarts it rather than reusing a subprocess that may still be
   mid-stream. (Combined with step 0's drain, this closes the contamination window from
   both ends.) Return `TurnResult(ok=False, error="timed out after Ns")`.

### steer / interrupt / stop — return `str` per `base.py` (review: all three, minor)

`AgentEngine.steer` and `interrupt` are typed `-> str` and the bridge logs the return as
`turn_id`. So:

- `steer(message)` → send `{"id":<sid>,"type":"steer","message":message}`; return the
  `sid` string. pi **supports** mid-run steering (grok/gemini raise `EngineError`).
- `interrupt()` → send `{"type":"abort"}`; return the active prompt id (or
  `"pi-rpc"` if none) as a `str`.
- `stop()` → `terminate()`, `wait(5)`, then `kill()` — identical to gemini.

## 5. Policy → tool allowlist (rewritten after review — the convergent blocker)

**The problem all three reviewers found.** The bridge derives `policy` *per dispatch*
from the sender's trust tier (`bridge.py:270`/`:467`:
`policy = sender_policies.get(sender, unknown_sender_policy)`) and passes it into every
`run_turn_with_progress(..., policy=...)`. The ACP engines honor it *at turn time*
(`gemini_acp.py:83` switches session mode per turn). pi can only set its toolset at
*spawn* and has no runtime re-scope, and it runs built-in tools without permission
dialogs. So if `pi-rpc` just ignores the per-turn `policy`, a full-tools `pi-worker`
that receives a non-trusted sender's task would execute it with full
`read,bash,edit,write` — the per-sender gate every other engine enforces becomes a
no-op. That is an authorization gap, not a style choice.

**Resolution (two layers — reviewer-endorsed):**

1. **Spawn-time toolset from a dedicated instance knob.** Add a bridge arg
   `--pi-tools <list>` (env `BRIDGE_PI_TOOLS`), resolved in `Bridge.__init__`, default
   empty = pi's full built-ins. A review instance launches with
   `--pi-tools read,grep,find,ls`. `build_engine` passes this as a **distinctly named**
   constructor arg `tool_policy` / `pi_tools` — NOT the per-turn `policy` (which isn't
   available at construction; there is no `args.policy`). pi builds permission gates as
   extensions, so a bare `pi --mode rpc` doesn't prompt — the spawn toolset IS the
   enforcement boundary.
2. **Turn-time guard so the per-turn `policy` is not dead.** In
   `run_turn_with_progress`, if the engine was spawned full-tools (no restriction) and
   the turn's `policy != "trusted"`, **refuse the turn**:
   `TurnResult(ok=False, error="non-trusted turn refused by full-tools pi instance")`.
   This makes a worker instance safe even if a `human`/unknown sender reaches it:
   untrusted work is rejected rather than run with write/bash. A review instance
   (already read-only) accepts any sender. Net rule: *non-trusted senders only get
   served by review-tools instances; full-tools instances serve trusted senders only.*

**`extension_ui_request` handling — exact, per method (review: codex/cold-opus).** The
auto-respond net only prevents hangs if it uses the right shapes:
- **Ignore fire-and-forget methods** entirely (`notify`, `setStatus`, `setWidget`,
  `setTitle`, `set_editor_text`) — they expect no response; replying is a protocol
  violation.
- **Cancel the four dialog methods** deterministically:
  `{"type":"extension_ui_response","id":<id>,"cancelled":true}` for
  `select`/`input`/`editor`, and `{"...","id":<id>,"confirmed":false}` for `confirm`.
  ("Allow" is not a valid generic `select` reply — it needs a concrete option string;
  cancelling is the safe deterministic rule, and in headless trusted-tools mode these
  dialogs shouldn't normally arise anyway.) Dialogs carrying a `timeout` auto-resolve
  agent-side, so only no-timeout dialogs could hang — which is exactly why we answer.

## 5.5 Parallelism & multi-model deployment

The bridge already provides parallelism generically via `EnginePool` + `--max-parallel`;
this section confirms pi fits it and specifies how to run *different models* at once.

**Intra-instance (same model, N concurrent turns).** With `--max-parallel N`, the pool
holds N warm engines and runs N turns concurrently. The pi engine is **pool-safe by
construction**: each engine is its own `pi --mode rpc --no-session` subprocess with its
own stdin/stdout + reader thread and no cross-instance shared state, so N concurrent pi
subprocesses don't interfere. This needs **no engine-specific work** beyond honoring the
existing `AgentEngine` contract — the pool, acquire/release, and `task_engines` routing
already handle it. All N share the instance's single configured model.

**Cross-model (different models concurrently) — the driving use case.** A pooled engine's
model is fixed at spawn (per §6), so kimi-vs-minimax-at-once is achieved by running
**separate bridge instances, one per model**, and dispatching to each. They are parallel
simply by being separate daemons. Example for parallel model-diverse review:

| Instance role | `--model` | derived agent-id |
|---|---|---|
| `pi-kimi`    | `kimi-coding/kimi-k2-thinking` | `pi-bridge-dev-kimi`    |
| `pi-minimax` | `minimax/MiniMax-M3`           | `pi-bridge-dev-minimax` |

**Agent-id collision (important).** `derive_agent_id` is `tool-project-workspace`, which
is `pi-bridge-dev` for *both* instances — a collision on the registry. Disambiguate with
the existing `--role` flag (appends to the id, `ROLE_PATTERN = ^[a-z0-9-]{1,16}$`): launch
with `--role kimi` / `--role minimax` to get `pi-bridge-dev-kimi` /
`pi-bridge-dev-minimax`. (`--agent-id` is an alternative but `--role` is the idiomatic
knob and is already validated.) The orchestrator dispatches to each `--target-id`
in parallel via two backgrounded `agent-dispatch` calls (Pattern A/B), then collects both
replies. **No new bridge code is required for cross-model parallelism** — it falls out of
per-instance model config (§6) + the existing `--role` and pool machinery. The only
engine requirement is that `--model` reaches pi correctly (§6).

**Verified no hidden shared dependency (review: cold-opus).** `usage_identity` defaults
to `agent_id` (`bridge.py:77`), so the two `--role` instances get *separate* usage
budgets/keys (`usage:{agent_id}:...`); the registry key is per-agent-id
(`redis_io.py`); and the pool is per-process (one `EnginePool` per daemon). Two
per-model instances therefore run side by side with no shared-scope collision.

## 6. Provider + model

pi's `--model` already accepts the `provider/id` form (e.g.
`anthropic/claude-sonnet-4-...`, `minimax/MiniMax-M3`). **Reuse the existing
`--model` flag**; do NOT add a new `--provider` arg. CLI surface stays identical to
the other engines.

## 7. Wiring touchpoints

- `bridge.py`:
  - `from .engines.pi_rpc import PiRpcEngine`
  - add `"pi-rpc": "pi"` to `ENGINE_TO_TOOL`
  - add `--pi-tools` arg (env `BRIDGE_PI_TOOLS`) in `build_parser` (~line 805+);
    resolve it in `Bridge.__init__`
  - add a `build_engine` branch:
    `return PiRpcEngine(cwd=cwd, model=args.model, pi_tools=args.pi_tools)` — pass the
    instance toolset (§5), NOT a per-turn `policy` (which doesn't exist at construction)
- `ctl.py`: **auto-covered (verified, review: all three).** `ctl.py` *imports*
  `ENGINE_TO_TOOL` from `bridge` (`ctl.py:13`) and uses `choices=sorted(ENGINE_TO_TOOL)`,
  so adding the key in `bridge.py` is sufficient — no second edit. (Earlier "update both"
  hedge resolved.)
- `scripts/agent-dispatch`: **the one place that redefines the map** — add
  `pi-rpc) TOOL=pi ;;` to the engine→tool `case` (~line 115), plus the usage string
  (~line 32) and the header comment (~line 2).
- Example env file `.env.pi-dev` + a note for the `agent-bridge@` systemd template so
  a pi instance is deployable. Example units: `pi-worker` (full tools) and per-model
  review instances `pi-kimi` / `pi-minimax` distinguished by `--role` + `--model`
  (see §5.5). Document the multi-instance pattern so cross-model parallel review is a
  copy-paste deploy, not a code change.

Engine name: **`pi-rpc`** (transport-suffixed, matching `grok-acp`/`gemini-acp`),
tool id `pi`. (Confirmed.)

## 8. Error handling

- Process not running / stdin closed → `EngineError`.
- `start()` readiness probe fails (process dead / no RPC response) → `EngineError` (§4).
- Prompt `response` with `success:false` → `TurnResult(ok=False)` immediately, no wait
  for `agent_end` (§4 step 2).
- Post-acceptance failure via the event stream (`message_update` error reason, final
  `auto_retry_end{success:false}`) → `ok=False` turn-ending (§4 step 3).
- Timeout → emit `turn_timeout`, `abort`, **mark engine unhealthy for pool restart**,
  `ok=False` (§4 step 5). Queue is drained on the next turn's entry regardless (§4 step 0).
- Non-trusted turn on a full-tools instance → refused with `ok=False` (§5 guard).
- Malformed/undecodable line → skipped in reader (binary decode `errors="replace"`),
  not fatal (§3).

## 9. Testing

- **Unit** (`tests/test_pi_rpc.py`): feed canned pi RPC event lines through the
  engine's event handler; assert `TurnResult` and the emitted progress-event sequence.
  Mirrors the existing engine unit tests. No live model. Must include the
  review-driven cases:
  - **binary `\r` framing** — a JSON line containing a bare `\r` inside a string value
    parses as one record (not split); a record delimited only by `\n` is read whole.
  - **stale-queue drain** — events left from a prior turn don't terminate the next turn.
  - **prompt `success:false`** → immediate `ok=False`, no hang.
  - **id-matched `get_last_assistant_text`** → reads `data.text`; late events ignored;
    empty `data.text` falls back to chunks, then placeholder.
  - **policy guard** — non-trusted turn on a full-tools instance is refused.
  - **extension_ui** — fire-and-forget ignored; dialog methods cancelled with the
    correct per-method shape.
- **E2E** (`tests/test_pi_rpc_e2e.py`): mirror `test_grok_acp_e2e.py` — real
  `agent-dispatch --engine pi-rpc` against a live pi instance; `@external`/opt-in;
  skips cleanly when no instance is registered. Two progressive tests: command-exec
  with output markers, then file-write-and-verify (worker policy only).
- **Parallelism** (unit, deterministic): a pool test asserting two `PiRpcEngine`
  instances run concurrent turns without cross-talk (mirror the existing deterministic
  parallelism test added with worktree-isolated dispatch). No live model — fake
  `popen_factory` feeding canned event streams.

## 10. Out of scope (YAGNI)

- No per-dispatch policy/tool switching (per-instance only).
- No per-dispatch model override: cross-model parallelism is multi-instance (§5.5), not a
  model field in the dispatch envelope. (Avoids breaking the warm pool, which keys on a
  fixed model per engine.)
- No new `--provider` CLI arg (use `--model provider/id`).
- No images/attachments support in v1 (pi supports it; not needed yet).
- No bespoke handling of pi extensions/skills beyond the auto-respond safety net.
- Downstream use cases (review panels, MiniMax-M3 call-selection, etc.) are separate
  follow-on work.
