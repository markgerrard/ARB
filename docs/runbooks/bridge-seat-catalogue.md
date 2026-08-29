# Bridge seat catalogue — every engine family + how to stand one up

The **supervision mechanism is identical for every engine** — a launchd plist (macOS) or systemd unit
(Linux) runs `scripts/agent-redis-bridge-systemd <instance>`, which parses the engine from the instance
name and takes project/workdir/senders/model from env. **The full mechanism (env table, launchd
bootstrap/kickstart, systemd unit + drop-in, NO-AUTOSTART, verify) lives in
[`codex-model-pin-seat-setup.md`](codex-model-pin-seat-setup.md)** — read that for the *how*; this doc is
the *catalogue*: which engines exist, what's per-engine different, and where each one's auth lives.

**Instance → agent-id:** instance `<engine>-<project>-<workspace>` (e.g. `codex-dev-luna`) registers as
`<engine>-<project>-<workspace>` with `AGENT_PROJECT`/`AGENT_WORKDIR` (e.g. `codex-bridge-dev-luna`).
`--effort` is **codex-only** and **per-dispatch** (warm seats stick it). Every seat is **NO-AUTOSTART**
(`RunAtLoad=false`) — ping before dispatch.

## The eight dispatch engine families (live on this host)

| Engine (`--engine`) | Example seats | Model pin | Auth store — how to authenticate | Reviewer/implementor | Key gotchas → pointer |
|---|---|---|---|---|---|
| **codex** | `codex-bridge-dev-{luna,sol,terra}` | `AGENT_MODEL=gpt-5.6-<pin>` (luna/sol/terra = decorrelated pins) | codex CLI login (ChatGPT/OpenAI); `--codex-bypass-approvals-and-sandbox` via `AGENT_BRIDGE_CODEX_BYPASS=1` | both (impl=luna, review=sol/terra) | effort is per-dispatch + warm-sticks; `[codex-model-pin-seat-setup.md]` |
| **agy-print** | `agy-bridge-dev` | none | **`agy` interactive login once** (own dotfiles, separate from bridge env). Unauthed `agy --print` exits 0 with **empty stdout** → looks like success | reviewer (Gemini-family) | unauthed=silent-ok on old builds; FD-leak looked like DNS. memory `agy-fd-leak-masquerades-as-dns` |
| **agent-sdk** (`asdk`) | `asdk-bridge-dev-{haiku45,opus48,opus5,sonnet5}` | `--target-id asdk-…-<model>`; model in the id | **subscription setup-token** (auths on the Plan, not an API key); `setting_sources=[]` gate | reviewer (Sonnet=WIKI default; Opus=cold-review) | reviewer slot is **per seat process**, not fleet-wide (below) — seats run concurrently, one turn each; memory `agent-sdk-subscription-seat`; `review-seat-opus-not-fable` |
| **pi-sdk** / pi-rpc | `pi-glm-bridge…` (GLM-5.2), `pi-m3-bridge…` (MiniMax-M3), `pi-sdk-…-glm` | `AGENT_MODEL=<provider>/<model>` (pi pins ONE model per daemon) | **`pi login` per provider** into `~/.pi/agent/auth.json` (separate from kimi-code creds). Missing provider → first turn wedges | adjunct reviewer (GLM best epistemic hygiene) | `process.title` clobbers argv (pgrep blind); unauth→30s wedge. memory `pi-sdk-glm-wedge-root-cause`; `qwen-worker-seats.md` |
| **grok-acp** | `grok-bridge-dev` | none | **`grok` interactive login** (same creds as the TUI) | reviewer (structured static) | out-of-cwd writes need spec-correct ACP permission (GROK-1, fixed ≥`635c398`); pings pass even when broken. memory `grok1-shipped-live-gated` |
| **cursor-acp** | `cursor-bridge-dev` | Composer 2.5 | cursor CLI login | **implementor only** (not a reviewer, per routing ladder) | non-certifying; `cursor-agent update` if PENDING self-update wedges under launchd |
| **omp-acp** | `omp-bridge-dev` | `AGENT_MODEL=<provider>/<model>` via `--model` at spawn (omp rejects `session/set_model`) | omp's own store `~/.omp/agent/` (sqlite, NOT pi's `~/.pi/agent/auth.json`); inherits other tools' creds on first run | adjunct reviewer / implementor, `experimental` | needs **bun ≥1.3.14** if installed via bun — use `brew install can1357/tap/omp` (standalone) instead; default surface is 29 tools incl. browser/computer/github, so **always set `BRIDGE_PI_TOOLS`** |
| **opencode-acp** | `opencode-bridge-dev` | `AGENT_MODEL=<provider>/<model>` via ACP `session/set_model` | `opencode auth login` → `~/.local/share/opencode` | adjunct reviewer, `experimental` | **no tool allowlist** — read-only is ACP `plan` mode only, so a seat here can NOT be readonly-gate certified |

**gemini-acp is DEAD** (Google deprecated the CLI 2026-07-03) — do not stand up gemini seats.

**The asdk reviewer slot is ONE PER SEAT PROCESS, not one per fleet.** A `reviewer=True` seat
(`opus48`, `opus5` — `engines/agent_sdk_models.py:67,76`) acquires
`_SUBSCRIPTION_OPUS_SEMAPHORE = BoundedSemaphore(1)` for each turn (`engines/agent_sdk.py:120,509-552`),
and that semaphore is **module-global, i.e. per bridge process** (`agent_sdk.py:118-119`: *"These
limits are per bridge process"*). Since one process serves exactly one seat/model (`agent_id` derived
per-process, `bridge.py:4233`; own queue, `bridge.py:880`) and each seat is its own launchd job, the
cap is **intra-seat**: one seat cannot run two concurrent reviewer turns. **Different seats are
independent — `opus48` and `opus5` DO run concurrently** and need no staggering. Implementor
(non-reviewer) seats get `BoundedSemaphore(2)`.

This is by design, not a limitation to route around: the fleet-by-default consensus
(`enginepool-admission-thread-flaw.md § Resolution`, 3/3 panel) is to scale agent-sdk concurrency by
**adding seats**, not intra-seat parallelism — each turn spawns a heavy subprocess regardless, so
intra-seat yields no resource saving and reopens the admission-thread flaw (ED-002). There is no
cross-process cap today; if subscription seats are ever scaled out, that is the filed future
hardening (`agent_sdk.py:118-119`).

## Standup, per family (deltas from the codex runbook)

For **any** engine: clone/write a plist (mac) or systemd unit+drop-in (Linux) that runs
`agent-redis-bridge-systemd <engine>-dev-<workspace>` with the load-bearing env
(`codex-model-pin-seat-setup.md` § table) — then **authenticate the engine's own store** (the column
above) **before** the first dispatch, because every non-codex engine keeps auth in its own dotfiles, not
the bridge env file, and several fail *silently* when unauthed:

- **codex** → full runbook: `codex-model-pin-seat-setup.md`.
- **pi-sdk / pi-rpc** → routing + gate: `qwen-worker-seats.md`. Auth: `pi login` for every provider the
  seat's `AGENT_MODEL` uses. Diagnose a "dead" pi by **PPID** (`pgrep -P <bridge-pid>`), never argv.
- **agy-print / grok-acp / cursor-acp** → same wrapper; run the engine's interactive login once, then
  re-dispatch (the engine spawns fresh per turn — no bridge restart needed after auth).
- **agent-sdk (asdk)** → subscription seats: `docs/…` memory `agent-sdk-subscription-seat`; the OAuth
  token is process-env, not the env file.
- **remote node (fresh droplet), Claude-peer + pi** → `agentbridge-seat-setup.md` (a different shape:
  shape-2 Claude peer + M3, not an engine-dispatch seat).
- **always-up Claude console peers** (shape 2, survive reboot) → `always-up-seats.md`.

## Cross-cutting (all seats)
- **NO-AUTOSTART by design** — after a host reboot every seat is DOWN until `launchctl kickstart`
  (mac) / `systemctl --user start` (linux). Never assume up; `agent-bridge-ping` first. memory
  `manual-seats-promoted-launchd`.
- **Warm rotation** — the bridge-dev fleet runs `BRIDGE_CODEX_RETIRE_AFTER_TURN=0` + `--max-parallel 4`
  + notify-split (`BRIDGE_NOTIFY_INBOX=0`). memory `fleet-flipped-warm-rotation`.
- **Auth lives in each engine's own store, off-bus** — never paste a live key over the bus (bus messages
  persist cleartext in `/tmp` task outputs); use ARB Secrets or hand it in directly.
- **plist change → `bootout`+`bootstrap`; env-file change → `kickstart`** (mac). memory
  `bridge-dev-fleet-launchd`.
