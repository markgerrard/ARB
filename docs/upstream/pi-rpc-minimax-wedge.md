# Upstream bug: pi `--mode rpc --model minimax/MiniMax-M3` wedges when spawned by a daemon

**Filed:** 2026-06-04
**Affected:** pi-coding-agent v0.78.0 (homebrew install on macOS 26.5 arm64)
**Provider/model:** `minimax/MiniMax-M3` specifically. `kimi-coding/kimi-for-coding` and `openai-codex/gpt-5.5` against the same binary in the same bridge configuration work cleanly.
**Bridge engine that hit it:** `src/agent_redis_bridge/engines/pi_rpc.py` in this repo (Python `subprocess.Popen` driving pi via JSON-RPC over stdio).

---

## 2026-06-05 update — does not reproduce; leading hypothesis; mitigation shipped

A tri-model panel (codex, agy-print, cold-Opus) plus direct testing revisited this. Net result:

- **The wedge does NOT reproduce on the current checkout.** `pi --mode rpc --model minimax/MiniMax-M3`
  was driven end-to-end through the real bridge daemon shape (nohup, `stdin=/dev/null`, live TLS
  Redis threads, `set_thinking_level` between probe and prompt) and reached the network every time
  — full event stream (`response → agent_start → … → agent_end`), no kevent hang. Two reviewers
  independently failed to reproduce it; one reached minimax 3/3.
- **The actual current blocker was a malformed credential**, not a wedge: `~/.pi/agent/auth.json`
  held the minimax key *without its `sk-cp-` prefix`, so every call 401'd. pi reads the minimax key
  from `auth.json`, NOT from the `MINIMAX_API_KEY` env var. After fixing the stored key,
  `rpc + minimax` returns content (`PONG`) cleanly.
- **Doc correction:** the original claim that "`turn_started` proves pi received the prompt" is
  **wrong**. The bridge emits `turn_started` itself immediately after `_send(prompt)`
  (`pi_rpc.py`, in `run_turn_with_progress`) — *before* any pi ack. It only proves the bytes were
  written, not that pi processed them. Treat the original "pi received the prompt then hung"
  framing below with that caveat.

**Leading hypothesis for the original 2026-06-04 wedge (best mechanistic fit, still unverified):**
macOS `getaddrinfo` hangs on AAAA (IPv6) lookups for **IPv4-only** hosts under a daemon bootstrap
context. `api.minimax.io` is IPv4-only (A records only); the working providers' endpoints
(`api.kimi.com` → Cloudflare) are dual-stack. Node delegates `dns.lookup()` to the libuv 4-thread
pool running blocking `getaddrinfo(3)`; hung AAAA queries exhaust the pool, leaving the event loop
idle in `kevent` with no TCP — matching *every* discriminator (minimax-only, daemon-only,
no-network, kevent-idle). This explains why kimi/openai-codex never wedged. It remains **unverified**
because it does not reproduce under `nohup`-from-an-interactive-shell (only suspected under a true
launchd/systemd bootstrap namespace), and `getaddrinfo` for minimax demonstrably succeeds from
shell-spawned processes. If it ever recurs under the real service, the robust fix is a c-ares-based
`dns.resolve4` bypass or `dns.setDefaultResultOrder('ipv4first')` — **not** a hardcoded-IP
monkeypatch (the host load-balances across ≥2 IPs).

**Mitigation shipped (root-cause-agnostic, preserves rpc semantics):** a **prompt-ack watchdog** in
`PiRpcEngine.run_turn_with_progress`. If pi emits no output within `BRIDGE_PI_ACK_TIMEOUT` (default
30s) of a prompt, the turn aborts and fails fast and the engine is marked unhealthy (pool respawns),
instead of blocking the full turn timeout (up to 5400s). So any future wedge of this shape surfaces
as a fast, recoverable error rather than an apparent infinite hang.

The original 2026-06-04 investigation follows, preserved for the record.

## Symptom

When the bridge daemon spawns pi as a child via `subprocess.Popen(..., stdin=PIPE, stdout=PIPE, stderr=PIPE)` and dispatches a turn with `--model minimax/MiniMax-M3`, pi:

- Responds correctly to `get_state` (probe round-trips cleanly with full model info, `thinkingLevel`, session id).
- Receives the subsequent `{"id": 2, "type": "prompt", "message": "..."}` (verified — Redis event store shows `turn_started` with `turn_id: "2"` which only fires after the bridge's `_send(prompt)` call completes).
- Then **never emits any further output**. No `response` ack, no `agent_start`, no `message_start`. Pi child sits at 0% CPU, no TCP connections (so no API call attempted), main thread blocked in `kevent` (libuv event-loop idle wait).

The wedge persists indefinitely (observed >120s). Killing pi and respawning produces the same wedge on the next prompt. Only changing the `--model` away from minimax resolves it.

## Reproducer

The same pi binary in a shell pipe **works every time** with the same env, same auth, same flags. Repro the working case:

```bash
set -a; source <env-file-with-MINIMAX_API_KEY>; set +a
(printf '%s\n' '{"id":1,"type":"get_state"}'; sleep 5; \
 printf '%s\n' '{"id":2,"type":"prompt","message":"Reply with exactly: OK"}'; sleep 30) \
 | pi --mode rpc --no-session --no-themes --model minimax/MiniMax-M3
```

Expected (and observed): `response`, `agent_start`, `turn_start`, `message_start`, … `agent_end`.

The wedge appears when this same binary is launched via `subprocess.Popen` from a Python process that:

1. Is `nohup`-daemonized (stdin = `/dev/null`), AND
2. Maintains long-lived TLS Redis connections in other threads, AND
3. Sends the prompt soon after the get_state probe responds.

A minimal Python script (no Redis, no other threads, just Popen + reader thread + send probe + sleep 5s + send prompt) **works** under nohup with the same Popen flags. So at least one of the three factors above is load-bearing — we haven't been able to isolate which.

## Bisection results (none of these are the cause)

Each tested with a 3-run stress against fresh bridge launches:

| Hypothesis | Test | Result |
|---|---|---|
| Python's `BufferedWriter` flush atomicity | Replace `stdin.write()/flush()` with raw `os.write(fd, line)` | 3/3 wedge |
| Pi's preflight timing race after get_state | Insert `time.sleep(0.5)` then `time.sleep(5.0)` post-probe | 3/3 wedge each |
| Process group inheritance | Spawn with `start_new_session=True` | 3/3 wedge |
| Env not reaching pi | Explicit `env=os.environ.copy()` | 3/3 wedge |
| Provider auth not configured | `pi login` for all 5 providers → `~/.pi/agent/auth.json` populated | Wedge persists |
| `--no-themes` flag interaction | Removed | Wedge persists |
| `set_thinking_level` RPC interaction | `BRIDGE_PI_THINKING_LEVEL` unset, no `set_thinking_level` call | Wedge persists |
| First-turn warmup race | Wait 30s between bridge launch and first dispatch | 2/2 wedge |
| Stale Redis task state | Manual `DEL` of `task:<id>:status`, `:events`, `:result` keys | Wedge persists |

## What discriminates the failure

Two switches change the outcome:

1. **Model.** With identical bridge configuration:
   - `--model kimi-coding/kimi-for-coding` → **works**
   - `--model openai-codex/gpt-5.5` → **works**
   - `--model minimax/MiniMax-M3` → **wedges**
2. **Parent process shape.** With identical model:
   - Shell pipe to pi → **works**
   - Direct `PiRpcEngine` instantiation in a small Python script → **works**
   - Same `PiRpcEngine` reached via the bridge daemon's `engine_pool` → **wedges**

Both switches are required to trigger. Only the combination `(bridge daemon) × (minimax provider)` wedges.

## Suggested investigation surface (inside pi)

The wedge looks like pi hanging inside `session.prompt()` preflight — before `output(success(id, "prompt"))` at `rpc-mode.js` ~L302. Candidates worth poking from pi's harness:

- **Minimax provider preflight** — anything specific to the minimax provider config init (auth resolution, base URL handshake, model registry validation) that takes a different code path than kimi-coding or openai-codex.
- **`hasConfiguredAuth` / `emitBeforeAgentStart`** — both fire pre-emission.
- **Compaction `agent.continue()` loop** — if minimax provider triggers a compaction probe that loops indefinitely.
- **Provider/model registry lookup** with minimax — the recurring `Warning: Model "MiniMax-M3" not found for provider "minimax". Using custom model id.` on stderr suggests the registry doesn't know MiniMax-M3; minimax provider's fallback path might do something the other providers' fallbacks don't.
- **Interaction with non-interactive parents** — if any minimax-specific code path branches on `isatty()` of stdin/stdout/stderr or expects a controlling terminal in a way the other providers don't.

## Routing (updated 2026-06-05): MiniMax-M3 back on pi-rpc

**Superseded.** MiniMax-M3 review dispatches route via **pi-rpc** again (`--engine pi-rpc
--model minimax/MiniMax-M3`), matching the panel-composition policy in
`skills/using-agent-bridge/SKILL.md` (which lists the M3 adjunct as pi-rpc). This was validated
end-to-end on 2026-06-05 once the malformed `auth.json` minimax key was fixed — see the update at
the top of this doc. The prompt-ack watchdog (`BRIDGE_PI_ACK_TIMEOUT`) bounds any recurrence so a
future wedge fails fast instead of hanging a turn.

`mini-agent-acp` remains available as the official MiniMax Mini-Agent ACP adapter (talks
Anthropic-compat directly to `api.minimax.io`) and is a reasonable fallback if pi-rpc + minimax
ever regresses, but it is no longer the default routing for M3.

The bridge ships pi-rpc support for `kimi-coding/*`, `openai-codex/*`, and `minimax/*` — all work
end-to-end.

## Historical workaround (2026-06-04, retired)

During the original wedge, MiniMax-M3 was temporarily routed through `mini-agent-acp` to bypass the
pi-rpc hang. That detour is retired as of the routing update above.

## What we'd want from pi's side

A reproducer reduced to pi alone (no bridge), and ideally a fix or workaround upstream. Once pi-rpc + minimax/MiniMax-M3 works headlessly, the bridge will pick it up without any code change on our side.
