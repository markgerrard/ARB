# Session handoff — 2026-06-05 (cursor-acp + stderr drain + pi-rpc/minimax)

Snapshot for resuming after a context clear. All work below is **committed**; working tree clean.

## What shipped (commits, newest first)

- `ffc223d` docs(pi-rpc): route MiniMax-M3 back to pi-rpc, retire mini-agent-acp detour
- `c199d05` feat(pi-rpc): prompt-ack watchdog + minimax wedge resolution
- `db29197` feat(cursor-acp): Cursor CLI `agent acp` bridge engine
- `501a4c9` fix(engines): shared stderr drain to prevent pipe-buffer deadlock

### 1. Cursor ACP engine (`src/agent_redis_bridge/engines/cursor_acp.py`)
New engine `cursor-acp` (tool `cursor`) driving Cursor CLI's `agent acp` JSON-RPC-over-stdio,
modeled on `gemini_acp.py`. Wired into `ENGINE_TO_TOOL` + `build_engine` + `--engine` choices.
Verified end-to-end against the real binary (returns real replies; tool calls work). Hardened after
a tri-model review (codex + agy + cold-Opus):
- Deterministic model resolution from `session/new` `models.availableModels` (exact modelId → exact
  name → bracketed passthrough → keep default), one `set_model`, no probing loop.
- Permission auto-approve selects an offered allow option by ACP `kind` (allow_once > allow_always),
  rejects negatives (disallow/deny), cancels if none — `_select_allow_option`.
- Emits `turn_completed` (gemini parity); dedups `command_started` per `tool_call_id` (cursor sends
  pending→in_progress→completed).
- Tests: `tests/test_cursor_acp.py` (de-tautologized, ~0.02s).

### 2. Cross-engine stderr drain (`src/agent_redis_bridge/engines/_stdio.py`)
`start_stderr_drain(process, label)` — a **reader + forwarder** split with a bounded drop-on-full
queue. The child-stderr reader only does `put_nowait`, so it can never block even if the bridge's own
stderr sink backpressures (codex round-2 caught the single-thread version shifting the deadlock one
pipe downstream). Wired into cursor/gemini/grok/codex; `pi_rpc` migrated onto it; `agy_print`
untouched (uses `communicate()`). Tests: `tests/test_stdio.py`.

### 3. pi-rpc prompt-ack watchdog (`pi_rpc.py` `run_turn_with_progress`)
If pi emits nothing within `BRIDGE_PI_ACK_TIMEOUT` (default 30s) of a prompt, the turn fast-fails,
aborts, and marks the engine unhealthy (pool respawns) — instead of waiting the full turn timeout
(up to 5400s). Root-cause-agnostic; preserves rpc/steer/cancel.

### 4. New reviewer agent (`.claude/agents/code-reviewer-report-writer.md`)
Write-capable clone of `code-reviewer` (adds Write/Edit, opus default) for the tri-model review flow.
NOTE: in this session the harness still **blocked subagent file writes** to /tmp — cold-Opus
delivered reports inline. If you want it to persist report files, the session permission settings
must allow subagent `Write` to the report path.

## pi-rpc × minimax: resolved
- The 2026-06-04 "wedge" does **not** reproduce on current code; rpc+minimax runs end-to-end.
- Real blocker was a malformed key in `~/.pi/agent/auth.json` (stored without its `sk-cp-` prefix →
  401). **pi reads the minimax key from `auth.json`, NOT the `MINIMAX_API_KEY` env var.** Fixed.
- Validated: 5/5 bridge dispatches (`pi-bridge-dev`, `--model minimax/MiniMax-M3`) returned PONG.
- Leading-but-unverified root-cause hypothesis for the original wedge: macOS `getaddrinfo` AAAA hang
  on IPv4-only `api.minimax.io` under a launchd/systemd bootstrap namespace. See
  `docs/upstream/pi-rpc-minimax-wedge.md` (full writeup + the `turn_started`-fires-before-ack
  correction).

## Open items / next steps
- **Operational:** when relaunching the production minimax review bridge on the DO valkey bus, use
  `--engine pi-rpc --model minimax/MiniMax-M3` (the `envs/pi-minimax-project-g-dev.env` was
  previously repurposed to `gpt-5.5`). auth.json key fix is machine-local.
- **Deferred:** a one-shot `pi-print` engine (`pi --mode json --print`, parse JSON event stream like
  `agy_print`) was investigated as a wedge-free fallback but **not built** — only needed if pi-rpc
  minimax regresses. `--mode json` interactive is NOT pipe-drivable (TTY-bound).
- **If the wedge recurs** under the real launchd service: robust DNS fix = c-ares `dns.resolve4`
  bypass or `dns.setDefaultResultOrder('ipv4first')`, NOT a hardcoded IP (host load-balances ≥2 IPs).
  Verify against an actually-reproduced wedge.

## Bus / bridge notes
- This repo's own bridges: `.env.pi-dev` → local Redis `6390/db12`, project `bridge`, trusts
  `claude-bridge-dev`; agent ids `pi-bridge-dev` / `codex-bridge-dev` / `agy-bridge-dev`.
- The project-g fleet (codex/agy/gemini/pi-kimi/pi-minimax/mini-agent) runs on the DO valkey bus
  via `envs/*project-g*.env` (`127.0.0.1:6379` locally or the managed host) — **do not** reuse
  those for AgentRedisBridge work.
- Full suite green at handoff: **170 passed, 4 skipped**.
