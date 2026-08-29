# systemd units for the agent_redis_bridge

Two ways to supervise a bridge instance live here:

| Unit | ExecStart | Per-instance config | Restart |
|---|---|---|---|
| `agent-redis-bridge@.service` (legacy) | `scripts/agent-redis-bridge-systemd %i` (parses engine from the instance name; legacy project-c defaults) | derived from `%i` + the project `.env` | `on-failure` |
| `agent-bridge@.service` (recommended) | venv `python -m agent_redis_bridge` directly | `<envs>/instance-%i.env` (`EnvironmentFile`) | `always` |

New deployments should prefer **`agent-bridge@.service`**: it invokes the venv
interpreter directly (the legacy wrapper assumed `/usr/bin/python3`, which lacks
the package unless installed system-wide), takes every per-instance knob from an
`EnvironmentFile`, and restarts on *any* exit.

## Why `Restart=always`, not `on-failure`

systemd treats `SIGTERM`/`SIGHUP`/`SIGINT`/`SIGPIPE` and a clean `exit(0)` as
*success*. With `Restart=on-failure` a bridge that dies by one of those is **not**
respawned — it just stays dead, which is the exact failure mode supervising it is
meant to remove. We never reliably know how a given death happened, so we cover
all of them with `Restart=always`. A `StartLimitBurst=5`/`60s` backstop stops a
genuine crash loop (e.g. broken engine auth) from spinning forever.

**Verify, don't assume.** A unit that merely *starts* on boot but has the wrong
`Restart=` looks correct and still leaves you unsupervised. After installing,
prove the supervisor catches the death:

```sh
svc=agent-bridge@codex-project-g-laravel-dev.service
old=$(systemctl --user show -p MainPID --value "$svc")
kill -KILL "$old"   # then repeat with -TERM (the case on-failure would miss)
# expect: a new MainPID, is-active=active, and the bridge re-registers:
agent-bridge-ping --engine codex dev   # heartbeat=alive
```

## Install (user service, the layout this repo runs on)

The bridge runs as the engine-owning user (codex/agy auth, node, `~/.local/bin`
all live in `$HOME`), so a **user** service + linger is the natural fit.

```sh
# 1. unit
mkdir -p ~/.config/systemd/user
cp systemd/agent-bridge@.service ~/.config/systemd/user/

# 2. per-instance env file(s) — copy an example and edit the paths
cp systemd/instance-codex-project-g-laravel-dev.env.example \
   /srv/agent-redis-bridge/envs/instance-codex-project-g-laravel-dev.env
cp systemd/instance-agy-project-g-laravel-dev.env.example \
   /srv/agent-redis-bridge/envs/instance-agy-project-g-laravel-dev.env

# 3. survive logout/reboot, then enable + start
sudo loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now agent-bridge@codex-project-g-laravel-dev \
                                agent-bridge@agy-project-g-laravel-dev
```

Paths in `agent-bridge@.service` (venv, `WorkingDirectory`, node on `PATH`) are
absolute and host-specific — adjust them for your host.

## Add another bridge

Drop a new `instance-<agent-id>.env` next to the others and:

```sh
systemctl --user enable --now agent-bridge@<agent-id>
```

No edits to the unit. `<agent-id>` is the bridge's registered id, e.g.
`gemini-acp-project-g-laravel-dev`.

## Manage / observe

```sh
systemctl --user status   agent-bridge@codex-project-g-laravel-dev
systemctl --user restart  agent-bridge@codex-project-g-laravel-dev
journalctl  --user -u     agent-bridge@codex-project-g-laravel-dev -f
agent-bridge-ping --engine codex dev      # or --engine agy-print
```

Replies are durable in Redis (`agent_scratch:task:<id>:result`), so a bridge
restart never loses a queued review — the relaunched bridge consumes the still-
queued request and writes its result where the dispatcher can recover it.

## Seat registration consumer

`arb-seat-registrar.service` is the system-level, arb-buzz unit for one-time
operator-approved seat admission. It is not an agent engine instance. See
`docs/seat-self-registration.md` and copy
`arb-seat-registrar.env.example` to `/etc/arb-seat-registrar.env` mode 0600.
Automated NIP-OA profile tags remain off unless the operator deliberately sets
the protected `ARB_REGISTRAR_OWNER_KEY_FILE`; the registrar refuses a public or
wrong-owner key file at startup.

## Known characteristics (not defects)

- **Graceful SIGTERM shutdown takes ~30s.** On `stop`/`restart` the bridge drains
  in-flight work — the codex app-server + its node/git subprocesses are slow to exit
  cleanly — so the process can take ~30s to terminate before `RestartSec` + startup
  (measured ~35s SIGTERM→respawn on a live codex bridge; this is why
  `scripts/verify-bridge-supervision`'s default timeout is 150s). This is correct drain
  behaviour, not a hang: supervision recovers on both SIGKILL and SIGTERM (verified).
  `TimeoutStopSec` tuning *could* speed restarts but would trade drain-safety for
  restart-speed — shorten it wrong and a stop SIGKILLs a review mid-flight instead of
  letting it finish. Only worth revisiting if restart latency ever becomes operationally
  painful (e.g. the managed-bus move makes restarts frequent); until then it's a
  characteristic, not a TODO.
- **Hard-crash recovery may wait one heartbeat TTL.** SIGKILL/OOM cannot clean its
  bus-global identity lease. The successor waits in-process for that stale lease to
  expire instead of crash-looping through systemd's `StartLimitBurst`, then registers
  with a new boot token. The supervision verifier's default timeout (150s) covers the
  worst case — a just-refreshed 60s lease plus restart delay, interpreter startup, and
  (agent-sdk seats) engine start — with headroom, not merely equality.
