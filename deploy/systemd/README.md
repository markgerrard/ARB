# systemd unit — boot autostart for the docker-compose stack

`arb-memory-stack.service` makes systemd start the whole `deploy/docker-compose.yml`
stack on boot. It is **additive** to the per-container `restart: unless-stopped`
policy: Docker already restarts individual containers on crash and (with
`docker.service` enabled) on reboot; this unit adds a declarative
`docker compose up -d` reconcile at boot so a container that was left *stopped*
is brought back to match the compose file, and gives you a single
`systemctl start|stop arb-memory-stack` lifecycle handle.

## Install (prod: arb-prod / host `arb-memory`)

The unit runs as the unprivileged `claude` user, which must be in the `docker`
group (`id claude` → `…(docker)`). `docker.service` must be boot-enabled
(`systemctl is-enabled docker` → `enabled`).

```sh
sudo cp deploy/systemd/arb-memory-stack.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now arb-memory-stack.service
# verify
systemctl is-enabled arb-memory-stack.service   # -> enabled
systemctl is-active  arb-memory-stack.service    # -> active (exited)
```

`enable --now` runs `ExecStart` immediately; because `docker compose up -d` is
idempotent this is a no-op reconcile on an already-running stack.

## GOTCHA — name the service on `up -d` after a rebuild

All eight services share one image tag (`arb-memory:phase3`). `docker compose up -d`
reconciles against image **content**, not just config — so after a
`docker compose build …` a **bare** `up -d` recreates *every* service still on the
old image, not just the one you rebuilt. To touch a single service, name it:
`docker compose -f deploy/docker-compose.yml up -d visibility`. A bare `up -d`
(e.g. this unit's `ExecStart`, or a manual reconcile) will roll the whole stack
onto the freshly-built image — fine when that image is the merged target, wider
blast radius than intended otherwise.

## Redeploy recipe (single service)

```sh
git pull
docker compose -f deploy/docker-compose.yml build memory      # only `memory` has the build: block; rebuilds the shared tag
docker compose -f deploy/docker-compose.yml run --rm memory setup-schema   # owner DSN; applies migrations (idempotent)
docker compose -f deploy/docker-compose.yml up -d visibility  # NAMED — recreate just the gateway
```

> **Dev host (mac-mini)** uses launchd, not systemd — this unit is prod-only.
> The dev-host reboot-survival note lives in `deploy/README.md`.
