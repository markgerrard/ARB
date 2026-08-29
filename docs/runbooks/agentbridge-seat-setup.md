# Runbook: AgentBridge seat setup on a fresh node (Claude + pi)

Stand up a **Claude team seat** and a **pi (M3) seat** on a greenfield droplet and
join them to the bus (Valkey db12). Verified end-to-end on `host-c`
(Ubuntu 24.04) 2026-06-09. Standing up the adjacent service containers on that same
node is a **separate** runbook.

> Convention used here: node user `<user>`, home `/home/<user>`, bridge clone
> `~/AgentRedisBridge`, agent ids `claude-<node>` + `pi-<node>` (e.g. `claude-host-c-a`,
> `host-c`). Adjust paths/ids per host.

## 0. Prereqs (provided out-of-band)
- SSH access to the node; **passwordless sudo**.
- A **deploy key** for the bridge repo on the node (e.g. `~/.ssh/agentredisbridge_deploy`
  + an SSH `config` alias `github.com-agentredisbridge`). Clone via that alias host.
- **Claude Code** installed + authed for the node user; **node folder trust** pre-handled
  (or accept the one-time dialog — see §3).
- The node's egress IP **added to the db12 managed-DB trusted sources** (a fresh
  droplet/VPC is blocked until this is done — test: `timeout 6 bash -c 'cat </dev/null
  >/dev/tcp/<redis-host>/<port>'` → `OPEN`).
- The **MiniMax M3 API key** (off-bus — never paste a live key over the bus; bus messages
  persist cleartext in `/tmp` task outputs. scp host-to-host or hand it in directly).

## 1. Foundation
```bash
git clone git@github.com-agentredisbridge:example-org/AgentRedisBridge.git ~/AgentRedisBridge
sudo apt-get update -qq && sudo apt-get install -y redis-tools curl ca-certificates jq python3 tmux
mkdir -p ~/agent-comms ~/.config/systemd/user
# copy the Claude-peer toolkit (from any node/the project-a box):
#   send.sh send_file.sh inbox-tail.sh inbox-daemon.sh  ->  ~/agent-comms/
loginctl enable-linger <user>   # always-up --user services survive logout/reboot
```

## 2. Bus env — TWO naming conventions (important)
The Claude-peer scripts and the pi extension read **different** env var names. Keep two files:

- `~/agent-comms/.env` (Claude peer scripts: `send.sh`, `inbox-tail.sh`, `inbox-daemon.sh`):
  `REDIS_HOST/REDIS_PORT/REDIS_USERNAME/REDIS_PASSWORD`, `AGENT_REDIS_DB=12`,
  `AGENT_REDIS_PREFIX=agent_scratch:`, `AGENT_ID=claude-<node>`. (mode 600)
- `~/.config/pi-<node>.env` (pi `agent-bus-seat` extension + pi supervisor):
  `AGENT_REDIS_HOST/PORT/USER/PASSWORD`, `AGENT_REDIS_DB=12`, `AGENT_REDIS_TLS=1`,
  `AGENT_REDIS_PREFIX=agent_scratch:`, `AGENT_ID=pi-<node>`, `PI_MODEL=minimax/MiniMax-M3`,
  `PI_BIN=<vendored pi path>` (see §4). (mode 600)

Verify: `redis-cli --tls -h <host> -p <port> --user <user> -a <pass> -n 12 PING` → `PONG`.

## 3. Trust dialogs (both Claude and pi prompt separately)
- **Claude**: folder-trust dialog on first run in the workdir. Pre-handle, or `tmux
  send-keys "1" Enter` to accept "Yes, I trust this folder" (persists).
- **pi**: its OWN "Trust project folder?" dialog → blocks the extension/heartbeat until
  accepted. `tmux send-keys Enter` on the highlighted "Trust"; pi persists
  `~/.pi/agent/trust.json = {"<workdir>": true}`. **Without this, restarts crash-loop** at
  the prompt. Verify the file exists before relying on always-up.

## 4. pi (M3) seat
**Vendored standalone node** (NOT nvm/n/nodesource — keeps node+pi version-paired):
```bash
mkdir -p ~/.local/share/pi-node
curl -fsSL https://nodejs.org/dist/v22.22.3/node-v22.22.3-linux-x64.tar.xz | tar -xJ -C ~/.local/share/pi-node
NODE=~/.local/share/pi-node/node-v22.22.3-linux-x64
export PATH=$NODE/bin:$PATH    # REQUIRED — npm's shebang needs node on PATH or it errors
npm i -g @earendil-works/pi-coding-agent@0.79.0   # package name is NON-obvious
# PI_BIN = $NODE/bin/pi
```
- `~/.pi/agent/auth.json` (mode 600): `{ "minimax": { "type": "api_key", "key": "<KEY>" } }`.
- Scaffolding: copy `scripts/pi-project-b-1-{console,supervisor}` → `pi-<node>-*`, `sed`
  `/home/<user>`→home + `pi-project-b-1`→`pi-<node>`. Put `PI_BIN` in the pi env file (the console
  re-sources it after its default, so the env value wins).
- systemd `--user` unit `pi-seat-<node>.service` → ExecStart the supervisor; set
  `AGENT_ENV_FILE=~/.config/pi-<node>.env`, `AGENT_ID_OVERRIDE=pi-<node>`.
- `~/AgentRedisBridge/.pi/settings.json` carries `{"hideThinkingBlock": true}` (committed).
- Start: `systemctl --user daemon-reload && systemctl --user enable --now pi-seat-<node>`
  (set `XDG_RUNTIME_DIR=/run/user/$(id -u)` for non-interactive `systemctl --user`).

## 5. Claude team seat
- `inbox-daemon` systemd `--user` service: sole reliable consumer → jsonl + `:daemon`
  heartbeat (`AGENT_ENV_FILE=~/agent-comms/.env`, `AGENT_ID=claude-<node>`). Durable
  inbox capture independent of the Claude session.
- `claude-<node>-console`: **hardcode** `ENV_FILE` + `export AGENT_ID=claude-<node>` +
  `export AGENT_ENV_FILE` — do NOT inherit them (see the tmux-socket gotcha below);
  `exec claude --model sonnet`.
- `claude-<node>-supervisor`: runs on the **default tmux server** (so a plain `tmux ls`
  shows it next to the pi seat — what the existing nodes do); singleton preflight on
  `:status`; poll the prompt marker
  (`bypass permissions|for agents|Try "`); `send-keys` the boot instruction (points at the
  card); confirm `:status` arms; watch-loop on `:status`.
- Boot card `~/claude/SESSION-PICKUP-claude-<node>.md`: tells the fresh Claude to arm the
  inbox Monitor (`AGENT_ID=claude-<node> AGENT_ENV_FILE=~/agent-comms/.env bash
  ~/agent-comms/inbox-tail.sh`) and read `roles/team-seat.md`.
- systemd `--user` unit `claude-seat-<node>.service`; `ExecStop=/usr/bin/tmux -L
  claude-<node> kill-session -t claude-<node>`.

### ⚠️ Gotcha — co-located seats contaminate via a SHARED tmux server
The first seat to start creates the **default** tmux server; its global env
(`AGENT_ID`, `AGENT_ENV_FILE`) leaks into any later session started on the same server. On
`host-c` the pi seat (started first) leaked `AGENT_ID=host-c` into the Claude console → the
Claude seat booted with the wrong identity and **replied as `host-c`**.
**Canonical fix (what the existing nodes do): HARDCODE identity/env in the Claude console** —
`ENV_FILE="<absolute path>"` (never `${AGENT_ENV_FILE:-…}`), `export AGENT_ID=claude-<node>`,
`export AGENT_ENV_FILE="$ENV_FILE"`. Then the polluted server env can't bleed in and the seat
stays on the **shared default server** (visible to a plain `tmux ls`). Verified on host-c
2026-06-09: with the hardcoded console it boots correctly *even though* host-c's pi service
pollutes the tmux global env. A dedicated `tmux -L <seat>` socket also closes the leak but
**hides the seat from `tmux ls`** — not worth it; hardcoded console is the standard. (The node
pi seats additionally don't pollute the tmux global env at all; host-c's does, but the hardcoded
console makes that irrelevant.) Single-host single-seat nodes don't hit this; multi-seat hosts do.

### ⚠️ Known refinement — Claude-seat self-heal
`:status` is refreshed by the inbox-tail **Monitor**, not claude itself. If claude dies but
the watcher is orphaned, `:status` stays green (false-green) and the singleton preflight can
block a restart. Mitigation (mirror the pi supervisor): **drain orphaned `inbox-tail`
watchers for this `AGENT_ID` before the preflight**, then `clear_stale_heartbeat`. pi seats
don't have this (the extension dies with pi). *(Open on host-c as of 2026-06-09.)*

## 6. Verify (per seat — fire it, don't assume)
1. `:status` (or `:daemon`) heartbeat TTL > 0 on db12.
2. Send a probe; confirm the **reply's `from` field is the correct agent id** (catches the
   identity leak) and the model is right.
3. pi: confirm it recites the irreversible-ops guard (role profile loaded) and `trust.json`
   exists.
4. **kill → re-arm**: kill the brain pid; supervisor restarts within `RestartSec`. For pi
   this proves always-up cleanly; for Claude, confirm a *fresh* claude + single watcher
   (see the self-heal refinement).
5. Add the seat to `fleet:health` (project-a `FleetHealth.php`: `SEATS` for Claude node
   seats, `CONTINUITY_SEATS` for pi seats — WARN-when-dark).
