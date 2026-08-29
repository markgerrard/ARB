# Spec — the `agent-sdk` bridge engine (rev 2)

**Status:** design approved (design panel, corrected) → spec panel (SPEC-NEEDS-CHANGES) → revised here →
**stateful-path spike PROVED** the two foundational mechanics. **Owner:** warm Opus. **Builds on:**
`docs/decisions/m3-judgment-seat.md` (PATH 2), probe `tools/agent-sdk-probe/` (merged @97b75e6).
All SDK claims below are verified against the installed `claude-agent-sdk` 0.2.104 (the spec panel read
the SDK source; rev-1's false assertions are corrected).

## Goal
`src/agent_redis_bridge/engines/agent_sdk.py` + `ENGINE_TO_TOOL["agent-sdk"]="agent-sdk"` — an
`AgentEngine` wrapping **`claude-agent-sdk`** to drive non-Anthropic models (M3/Kimi/GLM-5.2) as a
**mutation-capable implementer seat** (PATH-2 mutation harness), with a one-shot stateless mode for
oracle-style use.

## Architecture (corrected, spike-proven)
Both `query()` and `ClaudeSDKClient` spawn the `claude` CLI as a **subprocess over stdio** (process
boundary in both — the codex-app-server shape). `query()` = spawn-per-call (async generator — consume in
an async helper; `asyncio.run(query(...))` directly is invalid). `ClaudeSDKClient` = one persistent CLI
child with session continuity + mid-turn control. **Two lifetimes, one engine:**
- **Stateful (default) — implementer.** One `ClaudeSDKClient` for the engine's life;
  `supports_continuation=True`. **`ClaudeSDKClient` has NO `steer()` method** — `steer()` is a documented
  soft no-op in stateful mode (re-prompt happens via the drive-to-completion loop, not mid-turn).
- **One-shot — stateless oracle.** `--agent-sdk-oneshot`: each turn is an async helper consuming
  `query(...)`; `supports_continuation=False`; `steer()` raises `EngineError` (like `agy_print`). Note:
  `can_use_tool` requires a **streaming (AsyncIterable) prompt even in one-shot** — a string prompt +
  `can_use_tool` raises; pass the prompt as a one-item async iterable.
Build the stateful implementer first; one-shot is a flag over shared config/mediation/event code.

**Spike-proven (this session, against M3):** (1) `can_use_tool` fires and **denies a Write** through a
live `ClaudeSDKClient` (`file_absent=True`); (2) `session_store` + `resume` on a **fresh client recalled
prior session state** (codeword across clients). The two load-bearing stateful mechanics work.

## Mediation — `can_use_tool` is the authoritative gate (spec-panel P0)
The fail-closed permission gate is **`can_use_tool`** (the SDK's explicit permission API: returns
`PermissionResultAllow`/`PermissionResultDeny` for every tool permission request; requires streaming —
`ClaudeSDKClient` always streams). NOT the `PreToolUse` hook (its `permissionDecision` is one input the
default `permission_mode` can override, and hook events need `include_hook_events`). Policy = hybrid,
both checked, fail-closed:
1. **Static ceiling** — `BRIDGE_AGENT_SDK_TOOLS` parsed once in `__init__`; empty/malformed → **refuse to
   start** (the `pi_sdk.py:81-90` / `readonly_gate` lesson). Tool outside ceiling → Deny.
2. **Sender gate** — `policy=="trusted"` → Allow within ceiling; non-trusted → **refuse the turn** (don't
   silently degrade — a silent no-op reads as `no_changes_clean` PASS).
3. Unknown tool / `can_use_tool` exception/timeout → **Deny** (fail-closed).
- **`can_use_tool` fires ONLY on the CLI's "ask" path** (`types.py:1804-1812`): it is BYPASSED by
  `allowed_tools` (auto-allow), by ambient `permissions.allow` rules loaded via `setting_sources`, and by
  non-`default` `permission_mode`. Therefore the gate's preconditions are **normative** (a context-free
  build that puts the ceiling on `allowed_tools` — what every probe driver does — would silently
  fail-OPEN for the governed tools): set **`permission_mode="default"` AND `allowed_tools=[]` (the ceiling
  lives ONLY in the `can_use_tool` policy, NEVER in `allowed_tools`) AND `setting_sources=[]`** (no ambient
  settings `permissions.allow`). Use `PreToolUse` only for audit events (with `include_hook_events`).
- **Startup guard** (before register/serve; engine `start()` self-probe + a `readonly_gate`-style config
  assertion — `readonly_gate.py` gains an `agent-sdk` branch): assert (a) `can_use_tool` is wired and
  `allowed_tools`/`setting_sources` are empty; (b) the parsed ceiling is non-empty; (c) a live self-probe
  proving BOTH directions — a **ceiling tool still routes through `can_use_tool`** (the POSITIVE case: that
  the gate is actually consulted for governed tools, not pre-allowed) AND a non-ceiling `Write` is
  **denied**. Deny-a-Write alone is insufficient (it only exercises the "ask" path). Refuse to serve on
  any failure.

## Turn execution + the silent-death fail-open (spec-panel P0)
`run_turn_with_progress` drives `client.query(prompt)` then iterates `client.receive_response()`.
`receive_response()` ends only on a `ResultMessage` — **a mid-turn child death ends the iterator with NO
`ResultMessage` and NO exception** (transport emits synthetic error+end → parsed to None). That reads as a
clean PASS. **Mandate:** track `saw_result`; if the stream ends without a `ResultMessage` → `TurnResult(
ok=False, error="stream ended without result")` AND mark the engine unhealthy (don't reuse the session).
**TurnResult mapping:** `thread_id = ResultMessage.session_id`; `stop_reason` from the result subtype;
`tool_calls` = deduped tool-use count (so `drive_to_completion` gets its inputs).

## Concurrency — one loop per client for life (spec-panel + design-panel)
The engine owns ONE asyncio loop on ONE background thread for the client's whole life. EVERY async call
(connect / run-turn / `interrupt` / `set_model` / `set_permission_mode` / disconnect) is marshalled onto
that loop via `run_coroutine_threadsafe`; never invoked from the bridge thread directly. The bridge calls
`interrupt()`/`stop()` **cross-thread while a turn runs**, so: control calls must be **lock-free relative
to the turn lock** (the worker blocks on `future.result(timeout)`, leaving the loop free to service
`interrupt`; an interrupt awaiting the turn-held lock would deadlock). `interrupt()` with no active turn =
defined no-op. One client ↔ one loop ↔ one thread.

## Per-model config + auth isolation (spec-panel P1)
- Port the probe's `models.py` routing table (secret-free): `{base_url, auth_style, model_id-or-tier-lane,
  key_env}`. `--model` selects a **logical name** (`minimax-m3`/`kimi`/`glm-5.2`), never a payload base_url.
- **Auth isolation** — `SubprocessCLITransport` merges the **entire parent `os.environ`** into the child
  (only filters `CLAUDECODE`), and `options.env` **overrides keys, it cannot remove them** AND the SDK runs
  **in-process** (so the probe's fresh-`subprocess.run(env=...)` `_child_env` is NOT directly portable).
  Implementable fix: in `options.env`, **explicitly set the selected vendor's vars to its values AND
  explicitly neutralize every sensitive ambient var the bridge might carry** — `ANTHROPIC_API_KEY`,
  `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_DEFAULT_*_MODEL`, and all `AGENT_SDK_*` not
  belonging to the selected model (set to empty/absent) — so the child sees ONLY the selected vendor's
  creds, no cross-route. (Prefer this over a custom sanitized-child transport, which risks breaking the
  SDK's SessionStore-resume materialization.) Re-apply per respawn (child inherits env once at spawn).
  Keys read by name from gitignored `envs/agent-sdk-models-dev.env`.
- **agent-id**: `derive_agent_id` role pattern is lowercase/digits/hyphen ≤16 → use a **normalized slug**
  (`m3`/`kimi`/`glm`), not `MiniMax-M3`/`glm-5.2[1m]` → `agent-sdk-<project>-<workspace>-<slug>`.

## Progress events + secret scrub (spec-panel P0/P1)
Map: `TextBlock`→`model_text`, thinking→`model_thinking`, **`ToolUseBlock`→`command_started`** (dedupe by
`tool_use_id`; NOT the `PreToolUse` hook, which also fires on *denied* calls), tool result→
`command_finished`, `ResultMessage`→turn result. Blocks are whole-block (chunkier than codex/pi deltas) —
documented. **Scrub on EVERY exfil path**, not just `on_event`: (a) the `on_event`→Redis path; (b) the
**stderr callback** (SDK stderr is callback-driven); (c) **the SessionStore transcript** — wrap the store
in a **`ScrubbedSessionStore`** decorator that scrubs entries on `append` (the transcript persists raw tool
args/output; `on_event` scrubbing does NOT cover it). Port `tools/agent-sdk-probe/scrub.py`.

## Completion + continuation + respawn (spec-panel P0/P1, spike-proven)
- **Commit via the bridge's `orchestrator_commit`** (worktree gate) — never have the model run `git`
  (bypasses the allowed-artifact check). **Hard-guard:** in stateful mutation mode, **refuse a trusted
  mutation turn unless `worktree_spec is not None`** (completion enforcement is worktree-only; a shared-cwd
  mutation seat is unattributable) — enforce before `run_engine`, not a log warning.
- **`supports_continuation=True`**; drive-to-completion re-prompts over the live session.
- **SessionStore respawn-durability** — concrete adapter: a **file-backed** `SessionStore`
  (`session_store` option) under a per-seat namespace keyed by `agent_id` (durable; `InMemorySessionStore`
  is not). `session_store_flush` = end-of-turn (`"batched"`). Respawn = **build a NEW client/loop/thread**
  with `resume=<last_completed_session_id>` (resume is consumed at `connect()`, so "one loop for life" is
  *per client instance*; respawn destroys+recreates the trio). Capture `session_id` from `ResultMessage`
  and persist it durably (so the dead instance's id survives). Use **`resume`, never `fork_session`** for
  recovery (two live children on one session diverge; fork is future deliberate-branch only). If **no
  completed `session_id` exists yet** (child died before the first `ResultMessage`), start FRESH — don't
  pass `resume` (a missing/non-UUID id silently degrades to fresh anyway; make it explicit).
- **Mid-turn child death**: surface clean turn-failed (above) → reconnect fresh child → `resume` last
  *completed* `session_id`. **At-least-once**: tool side-effects from a dead partial turn are NOT rolled
  back; the worktree may carry uncommitted edits the completion gate reconciles on re-prompt — document it.

## Lifecycle (spec-panel P1/P2)
- `start()`: spawn loop thread + connect client; **prove the permission path** (the startup guard's
  deny-a-Write self-probe doubles as the CLI/feature check — stricter than the SDK's warn-only version
  check).
- **Timeout algorithm**: mark the turn cancelled under the active-state lock; stop accepting new turns;
  on the loop, `disconnect()`/close transport with a bounded outer timeout; discard the client; **reap the
  OS child** (confirm `disconnect()` SIGKILLs after its grace, else kill the pid — like `codex.py:234-239`);
  only reconnect+resume after the old receive future has ended (no stale message bleed into the next turn).
  Return `TurnResult(ok=False, ...)`. Do NOT use `stop_task()` unless a concrete SDK task id was captured.
- **Role profile**: load `BRIDGE_ROLE_PROFILE_FILE` into `ClaudeAgentOptions.system_prompt`;
  `consumes_role_profile=True`.

## Testing
Unit (mock the SDK client/transport): the async→sync marshalling + lock-free control (interrupt-no-turn
no-op; no deadlock); `can_use_tool` hybrid gate (ceiling deny, non-trusted refuse, unknown deny,
exception→deny); **silent-death detection** (stream ends w/o ResultMessage → ok=False+unhealthy); per-model
routing + slug; **env whitelist** (no ambient leak into the child env); event dedup by tool_use_id;
**ScrubbedSessionStore** + event/stderr scrub (canary); worktree hard-guard (refuse non-worktree trusted
mutation); TurnResult mapping. Integration (live, gated): stateful mutation on a worktree → mediated calls
→ orchestrator_commit; **respawn-resume** (kill child mid-session → resume via the file SessionStore →
continuity); one-shot oracle mode. (`can_use_tool` denial + `session_store` resume already spike-proven.)

## Out of scope (YAGNI)
No separate oracle engine (one-shot = flag; pi seat serves the judgment oracle). No `fork_session` beyond
recovery-resume. No new event vocabulary. No bare-`anthropic` path. Qwen dropped.

## Pipeline
This revised spec → spec panel → plan → plan panel → codex-subagent TDD build → tri-model review → test.
