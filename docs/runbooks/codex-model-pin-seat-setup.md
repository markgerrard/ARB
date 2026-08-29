# Runbook: stand up a model-pinned Codex seat (e.g. `gpt-5.6-luna` @ high)

Stand up a **model-pinned Codex bridge seat** — a warm, NO-AUTOSTART Codex daemon pinned to a specific
GPT-5.6 variant (`gpt-5.6-luna`, `-sol`, `-terra`, …) that you dispatch to at `--effort high`. These are
the implementor/reviewer seats the pipeline uses (`codex-bridge-dev-luna` = implementor; `-sol`/`-terra` =
decorrelated review samples). Verified against the live `codex-bridge-dev-luna` seat on this host
(macOS/launchd) 2026-07-13; the Linux/systemd path uses the same wrapper.

> **What "`gpt-5.6-luna high`" actually is:** the *seat* pins the **model** (`AGENT_MODEL=gpt-5.6-luna`);
> **`high` is the reasoning effort, set PER-DISPATCH** (`--effort high`), NOT baked into the unit. Because
> these seats run warm (`BRIDGE_CODEX_RETIRE_AFTER_TURN=0`), a warm thread **sticks the last effort** — so
> once you dispatch at `high`, subsequent turns on that thread stay `high` (see
> `docs/…/codex-effort-mechanics` memory). `luna`/`sol`/`terra` are **distinct GPT-5.6 pins** (decorrelated
> snapshots) — codex accepts `--model gpt-5.6-<pin>` directly; **no `~/.codex/config.toml` change is
> needed** per pin (the `[tui.model_availability_nux]` entries are cosmetic).

## The seat = wrapper + env, supervised by launchd (mac) or systemd (linux)

Both OSes run the **same entrypoint**, `scripts/agent-redis-bridge-systemd <instance>`, which parses the
engine from the instance name, forwards `AGENT_MODEL → --model`, resolves project/workdir/senders from env,
and picks the repo `.venv` python. The ONLY OS difference is the supervisor unit format + loader commands.

**Instance name:** `codex-dev-<pin>` → registers as agent-id **`codex-bridge-dev-<pin>`** (with
`AGENT_PROJECT=bridge`, `AGENT_WORKDIR=<repo>`).

### Load-bearing env (identical on both OSes) — mirror the live luna seat
| Var | Value | Why |
|---|---|---|
| `AGENT_MODEL` | `gpt-5.6-<pin>` | the model pin (forwarded to codex `--model`) |
| `AGENT_ENV_FILE` | `<repo>/envs/agent-redis-bridge-dev.env` | bus creds (Valkey db12) — shared dispatch env |
| `AGENT_PROJECT` | `bridge` | → agent-id `codex-bridge-…` |
| `AGENT_WORKDIR` | `<repo>` (e.g. `/Users/<user>/<workspace>`) | seat's checkout |
| `AGENT_TRUSTED_SENDERS` | `claude-bridge-dev=trusted,…` | who may dispatch |
| `BRIDGE_CODEX_RETIRE_AFTER_TURN` | `0` | **warm rotation** (no retire) → effort sticks, context reused |
| `BRIDGE_MAX_PARALLEL` | `4` | concurrent turns per seat |
| `BRIDGE_NOTIFY_INBOX` | `0` | notify-split (keeps the reply inbox O(1)) |
| `AGENT_BRIDGE_CODEX_BYPASS` | `1` | `--codex-bypass-approvals-and-sandbox` (writes to `.git`/out-of-cwd) |
| `ARB_MEMORY_LOCAL_MCP` | `dev` | inject the local ARB Memory read MCP (needs `~/.arb-memory-local/readers.env`) |

---

## macOS (launchd)

1. **Clone the live luna plist**, changing the `<pin>` token in exactly three places — `Label`, the
   `ProgramArguments` instance (`codex-dev-<pin>`), and `AGENT_MODEL` (`gpt-5.6-<pin>`):
   ```bash
   pin=terra   # your new pin
   sed -e "s/luna/${pin}/g" \
     ~/Library/LaunchAgents/com.example.arbseat.codex-bridge-dev-luna.plist \
     > ~/Library/LaunchAgents/com.example.arbseat.codex-bridge-dev-${pin}.plist
   ```
   (The template's env block is the table above; `RunAtLoad=false` + `KeepAlive=false` = **NO-AUTOSTART by
   design** — seats are started on demand, not at login.)
2. **Load + start** (a *new* plist needs `bootstrap`; a plist edit needs `bootout` then `bootstrap`; an
   env-file-only change just needs `kickstart`):
   ```bash
   uid=$(id -u); label=com.example.arbseat.codex-bridge-dev-${pin}
   launchctl bootstrap gui/$uid ~/Library/LaunchAgents/${label}.plist
   launchctl kickstart -k gui/$uid/${label}
   ```
   (To reload after editing the plist: `launchctl bootout gui/$uid/${label}` then `bootstrap` again.)

## Linux (systemd, user service)

The same wrapper runs under the **legacy** template `systemd/agent-redis-bridge@.service` (it shells to
`scripts/agent-redis-bridge-systemd %i`, which forwards `AGENT_MODEL`). Drive every knob from an
`EnvironmentFile` so no unit edit is needed per pin.

1. **Unit** (once): `mkdir -p ~/.config/systemd/user && cp systemd/agent-redis-bridge@.service ~/.config/systemd/user/`
   (adjust its absolute `WorkingDirectory`/`ExecStart` paths to your clone; ensure the venv is on the path
   it uses — the wrapper auto-prefers `<repo>/.venv/bin/python3`).
2. **Per-seat env drop-in** — the wrapper reads `AGENT_*`/`BRIDGE_*` from the process environment, so put
   the table above in a systemd drop-in for the instance:
   ```bash
   pin=terra
   mkdir -p ~/.config/systemd/user/agent-redis-bridge@codex-dev-${pin}.service.d
   cat > ~/.config/systemd/user/agent-redis-bridge@codex-dev-${pin}.service.d/env.conf <<EOF
   [Service]
   Environment=AGENT_MODEL=gpt-5.6-${pin}
   Environment=AGENT_PROJECT=bridge
   Environment=AGENT_WORKDIR=/path/to/AgentRedisBridge
   Environment=AGENT_ENV_FILE=/path/to/AgentRedisBridge/envs/agent-redis-bridge-dev.env
   Environment=AGENT_TRUSTED_SENDERS=claude-bridge-dev=trusted
   Environment=BRIDGE_CODEX_RETIRE_AFTER_TURN=0
   Environment=BRIDGE_MAX_PARALLEL=4
   Environment=BRIDGE_NOTIFY_INBOX=0
   Environment=AGENT_BRIDGE_CODEX_BYPASS=1
   Environment=ARB_MEMORY_LOCAL_MCP=dev
   EOF
   ```
3. **Enable + start** (linger keeps it across logout; systemd's `Restart=on-failure` here — for an
   always-supervised seat prefer the `agent-bridge@.service` template's `Restart=always`, see
   `systemd/README.md`):
   ```bash
   loginctl enable-linger "$USER"
   systemctl --user daemon-reload
   systemctl --user start agent-redis-bridge@codex-dev-${pin}.service
   ```

---

## Verify + first dispatch (both OSes)

```bash
# alive? (status TTL > 0, consumer alive = inbox loop progressing)
AGENT_PROJECT=bridge scripts/agent-bridge-ping --engine codex dev-${pin}
# or directly:  redis-cli … -n 12 TTL agent_scratch:agent:codex-bridge-dev-${pin}:status

# first dispatch AT EFFORT HIGH (this is what makes it "…-<pin> high"; warm thread sticks it):
FROM_AGENT_ID=claude-bridge-dev BRANCH=dev \
AGENT_ENV_FILE=<repo>/envs/agent-redis-bridge-dev.env \
scripts/dispatch-dev --engine codex --target-id codex-bridge-dev-${pin} \
  --effort high --run-id <rid> "Read the brief at <path> and execute it."
```

## Gotchas
- **`--effort high` is per-dispatch, not in the unit.** Forgetting it → the seat runs at codex's default
  effort. (Optionally bake a default with `model_reasoning_effort = "high"` in `~/.codex/config.toml`, but
  that's global to codex; the per-dispatch flag is the norm and warm-sticks.)
- **NO-AUTOSTART is intentional** (`RunAtLoad=false`). A fresh mac reboot leaves seats DOWN until
  bootstrapped/kickstarted — don't assume they're up; ping first (memory `manual-seats-promoted-launchd`).
- **A new plist needs `bootstrap`; an edited plist needs `bootout`+`bootstrap`; an env-only change just
  needs `kickstart`** (memory `bridge-dev-fleet-launchd`).
- **`ARB_MEMORY_LOCAL_MCP=dev` is a no-op** unless `~/.arb-memory-local/readers.env` (mode 600) exists on
  the host — safe to leave set.
- The `.venv` must have `agent_redis_bridge` importable (macOS `/usr/bin/python3` does NOT); the wrapper
  auto-prefers `<repo>/.venv/bin/python3`.
