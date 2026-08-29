# Host config knobs + session-leakage fixes (current bridge-dev setup)

The knobs a host running the orchestration + bridge-dev fleet needs turned, and the session-leakage fix
that's live in the current config. Machine config (not code) — secrets stay off this doc; it records
*which* knob and *why*, not values.

## Session leakage — the plugin MCP-server orphan leak (FIXED, keep it fixed)

**Symptom (2026-07-12):** dozens of orphaned `bun … server.ts` MCP processes accumulated (measured 43
procs, ~4.2 GB RSS, two runaways at ~96 % CPU). They leaked from the **telegram plugin's** MCP server,
spawned by **interactive `claude` sessions** — NOT by asdk/bridge seats (those launch with
`setting_sources=[]`, which gates plugin MCP autostart, so they never spawned it). Each interactive
session that didn't cleanly tear down left its telegram MCP child orphaned.

**Fix (live in `~/.claude/settings.json`):**
```jsonc
"enabledPlugins": { "telegram@claude-plugins-official": false, /* … others true … */ }
```
Disabling the plugin stops new leaks. **Then reap the existing orphans** — and mind the gotcha: the
sandbox blocks a plain `kill`, so reap with `dangerouslyDisableSandbox` + `xargs`:
```bash
pgrep -fl 'bun .*server\.ts' | ...   # identify telegram MCP orphans (not other bun procs)
# kill via a sandbox-bypassing shell, e.g. pgrep -f … | xargs kill -TERM   (run with the sandbox override)
```
**Do not re-enable the telegram plugin** without a teardown fix. Related machine change: the `claudeyy`
zsh alias was stripped of its dev-channels wiring. (memory `plugin-mcp-server-orphan-leak`)

**Discriminator if it recurs:** `pgrep -fl 'server\.ts'` growing across sessions ⇒ a plugin MCP isn't
being torn down. Check `enabledPlugins` first; confirm the culprit is spawned by *interactive* sessions
(asdk seats with `setting_sources=[]` are exonerated by construction).

## Config knobs — current setup

### `~/.claude/settings.json` (the orchestration session)
| Knob | Current | Why |
|---|---|---|
| `model` | `opus[1m]` | 1M-context Opus for the orchestrator |
| `effortLevel` | `high` | orchestration reasoning effort |
| `enabledPlugins` | telegram **false**; superpowers/frontend-design/context7/code-review/feature-dev/playwright/claude-md-management **true** | telegram=false is the leak fix (above) |
| `skipDangerousModePermissionPrompt` | `true` | fewer prompts in the trusted local fleet |
| `remoteControlAtStartup` / `agentPushNotifEnabled` | `true` | remote control + push notifications on |

### Bridge-dev fleet (seat env / launchd plists — see `bridge-seat-catalogue.md`)
| Knob (env) | Value | Why |
|---|---|---|
| `BRIDGE_CODEX_RETIRE_AFTER_TURN` | `0` | **warm rotation** — reuse context, effort sticks (memory `fleet-flipped-warm-rotation`) |
| `BRIDGE_MAX_PARALLEL` | `4` | concurrent turns per seat |
| `BRIDGE_NOTIFY_INBOX` | `0` | **notify-split** — reply inbox stays O(1) under N-parallel dispatch |
| `AGENT_BRIDGE_CODEX_BYPASS` | `1` | codex writes to `.git`/out-of-cwd (`--codex-bypass-approvals-and-sandbox`) |
| `ARB_MEMORY_LOCAL_MCP` | `dev` | inject the local ARB Memory **read** MCP (needs `~/.arb-memory-local/readers.env`, mode 600) |
| `AGENT_ENV_FILE` | `envs/agent-redis-bridge-dev.env` | bus creds (Valkey **db12** dispatch) — **gitignored** |
| `RunAtLoad` / `KeepAlive` (plist) | `false` / `false` | **NO-AUTOSTART** — seats are started on demand, DOWN after reboot |

### Orchestration-clone audit routing (this clone, `<workspace>`)
- `ARB_MEMORY_REDIS_URL` in `envs/agent-redis-bridge-dev.env` points at the **prod audit/memory Valkey
  db/5** (repointed from db/3, 2026-07-12) — so panel manifests/votes and the bus verdict-close reach the
  live audit plane, and `arb_memory.client` writes land in prod `arbmemory`. (memory
  `arb-audit-votes-activation`, `arb-memory-prod-deploy`.) The DSN stays only on the arb-prod
  `audit-close-consumer`; the orchestrator never holds it (bus-close only).

## Cross-host note
On a *fresh* host, these knobs must be set explicitly — none are defaults. Seat env goes in the launchd
plist / systemd drop-in (`bridge-seat-catalogue.md`), the orchestration knobs in `~/.claude/settings.json`,
and the ARB Memory read MCP needs `~/.arb-memory-local/readers.env` present or `ARB_MEMORY_LOCAL_MCP=dev`
is a safe no-op.
