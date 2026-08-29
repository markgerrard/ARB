# Seat-host container (Track 2)

Run bridge seats from a baked image instead of the per-host clone / venv / `npm i -g` /
launchd ceremony. Implements Track 2 of
`docs/superpowers/specs/2026-06-27-go-client-edge-and-seat-host-container-design.md`.

**What's baked:** the bridge (Python venv at `/opt/venv`) + Node 22 + the engine CLIs
`codex`, `gemini`, `pi-coding-agent`. **What's NOT baked:** the repo (bind mount) and engine
auth (named volumes, device-code login once per host). `agy` is not yet in the image — see
[Limitations](#limitations).

## The two things that stay on the host

| | Mechanism | Why |
|---|---|---|
| **The repo** | bind mount `REPO_ROOT → /repos` | worktree-per-dispatch makes the working tree mutable shared state; it can't be baked |
| **Engine auth** | named volumes (`codex-auth`, `gemini-auth`, `pi-auth`) | device-code OAuth once per host; token survives restarts |

The only per-host config is `REPO_ROOT` (and the env file). The image, bridge wrapper, and
`AGENT_WORKDIR` convention are byte-identical everywhere.

## Build

```sh
cd <repo-root>
docker build -f deploy/seat-host/Dockerfile -t arb-seat-host:dev .
# or: REPO_ROOT=/Users/<user> docker compose -f deploy/seat-host/docker-compose.seat-host.yml build
```

## First run — auth once per host (device code)

Each engine authenticates with a headless device-code flow (approve in a browser anywhere);
the token persists in the named volume, so this is **once per host**, exactly as today.

Run the auth step **as the host UID with `HOME=/home/seat`, overriding the command (not the
entrypoint)** — the entrypoint shim is what pins `HOME` onto the mounted volume; `--entrypoint`
would bypass it and write the token to an ephemeral `/root/.codex` that `--rm` then discards.

```sh
# codex
docker run --rm -it --user "$(id -u)" -e HOME=/home/seat \
  -v codex-auth:/home/seat/.codex arb-seat-host:dev codex login
# gemini
docker run --rm -it --user "$(id -u)" -e HOME=/home/seat \
  -v gemini-auth:/home/seat/.gemini arb-seat-host:dev gemini
# pi — add each provider to ~/.pi/agent/auth.json (minimax, zai, kimi-coding, …)
docker run --rm -it --user "$(id -u)" -e HOME=/home/seat \
  -v pi-auth:/home/seat/.pi arb-seat-host:dev pi login
```

The image pre-creates `/home/seat/.codex|.gemini|.pi` world-writable (0777) so a fresh named
volume inherits a writable mountpoint — otherwise Docker initialises the volume `root:root` and
the non-root `--user $(id -u)` seat hits `EACCES` on login. Running auth as the **same** host UID
as the seat means the 0600 token files are owned by — and refreshable by — the seat.

> **Prefer a fresh device-code login over copying the host's `~/.codex` into the volume.** A
> *copied* auth dir carries the host's sessions/state DB, whose rollout paths are host-absolute
> (`/Users/you/.codex/sessions/…`) and don't exist under the container's `/home/seat/.codex` —
> codex then logs harmless but noisy `ERROR codex_rollout::list: state db returned stale rollout
> path …` lines on startup. A clean device-code login into the named volume avoids it entirely (and
> isolates the container's session state from the host seat's).

## Run seats

```sh
REPO_ROOT=/Users/<user> \
AGENT_UID=$(id -u) \
SEAT_AGENT_WORKDIR=/repos/<workspace> \
SEAT_AGENT_ENV_FILE=/repos/<workspace>/envs/agent-redis-bridge-dev.env \
docker compose -f deploy/seat-host/docker-compose.seat-host.yml up -d codex gemini pi-glm
```

(The overrides are `SEAT_`-prefixed on purpose: the compose file reads `SEAT_AGENT_WORKDIR` /
`SEAT_AGENT_ENV_FILE`, NOT `AGENT_WORKDIR` / `AGENT_ENV_FILE` — so an ambient host `AGENT_WORKDIR`
that the launchd bridge seats export, host-absolute, cannot silently bleed into the container.)

Or a single seat directly:

```sh
docker run -d --name codex-seat \
  --user "$(id -u)" \
  -v /Users/<user>:/repos \
  -v codex-auth:/home/seat/.codex \
  -e SEAT_HOME=/home/seat \
  -e SEAT_INSTANCE=codex-dev \
  -e AGENT_WORKDIR=/repos/<workspace> \
  -e AGENT_PROJECT=bridge \
  -e AGENT_ENV_FILE=/repos/<workspace>/envs/agent-redis-bridge-dev.env \
  -e AGENT_TRUSTED_SENDERS=claude-bridge-dev=trusted \
  arb-seat-host:dev
```

## The one real footgun — container paths

Dispatch briefs and `AGENT_WORKDIR` must use **container paths** (`/repos/<project>/…`), not
host-absolute paths. Nothing in the bridge reads `REPO_ROOT`; `AGENT_WORKDIR` is consumed
verbatim. A host path like `/Users/<user>/<workspace>/.claude/worktrees/x` does not exist in the
container and fails **silently as "file not found"**. The entrypoint warns if `AGENT_WORKDIR`
is not under `/repos`.

## UID / ownership

Run as the host user (`--user $(id -u)`) so files the bridge writes to the bind-mounted tree
(commits, edits, worktrees) are owned by you on the host. The image pre-creates the engine auth
dirs (`/home/seat/.codex|.gemini|.pi`) world-writable (0777), so a fresh named volume inherits a
writable mountpoint and any host UID can complete device-code login and refresh tokens — no
manual `chown` of the volume needed.

## Env file

The seat reads its config from `AGENT_ENV_FILE` (mounted via `/repos`). Keys are documented in
the repo's `.env.example` (Redis transport `AGENT_REDIS_*`, identity `AGENT_PROJECT`/`AGENT_WORKSPACE`,
`AGENT_TRUSTED_SENDERS`, and the observability tee URLs `ARB_EVAL_REDIS_URL` / `ARB_TRACE_REDIS_URL` /
`ARB_LIVE_REDIS_URL` / `ARB_BRIDGE_BUS_URL`). Per-seat knobs (`AGENT_MODEL`, `AGENT_TURN_TIMEOUT`,
`BRIDGE_MAX_PARALLEL`, …) are set as `environment:` in the compose service.

## Limitations

- **agy is not baked.** The Antigravity CLI has no clean in-repo `npm i -g` install; install it
  at first run into the `gemini-auth` volume's `.gemini` tree (agy shares `~/.gemini/antigravity-cli/`)
  and uncomment the `agy` compose service. Confirm the install method before adding a build layer —
  don't guess it.
- **grok-acp / agent-sdk seats** are not in this image (grok needs the local Grok Build TUI login;
  agent-sdk needs the `.[agent-sdk]` extra + vendor API keys). Add as needed.
- The pi dep tree (~300MB) is the bulk of the image; `pi-sdk-host/install.sh` runs at build and
  self-heals on first run if the symlink layout differs.
