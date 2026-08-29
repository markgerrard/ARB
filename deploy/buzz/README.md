# buzz relay — DigitalOcean deployment

Droplet in LON, DO Managed Postgres in the same region, **Cloudflare R2** for
the object store, redis local to the droplet.

> **DO Spaces is not approved for Buzz.** ARB Files proved sequential
> compare-and-swap only with an unquoted `If-Match` representation; Buzz's
> 32-writer admission gate has not been rerun in that representation. See
> "Object store" below before reaching for a Space out of DO-consistency instinct.

## We do not fork buzz

Our clone is a **pure consumer** — zero tracked modifications, verified
2026-08-02. A fork would buy a permanent merge burden for a capability we have
never used, so instead we pin (`UPSTREAM_REF`) and overlay.

Upstream already ships production deploy assets — `deploy/compose/compose.yml`,
helm charts under `deploy/charts`, and a cargo-chef multi-stage `Dockerfile`
that builds a stripped release image running as non-root `buzz:buzz`. We do not
reimplement any of it. `compose.do.yml` layers **only** the DO-specific
differences on top, so an upstream fix to the relay service reaches us on the
next pin bump with no merge.

## Files

| File | What it is |
|---|---|
| `UPSTREAM_REF` | The pinned buzz commit + the bump procedure |
| `compose.do.yml` | Overlay: managed PG, R2, upstream's PG/MinIO profiled out |
| `.env.example` | Every key the relay reads. Copy to `.env`, `chmod 600` |
| `buzz-deploy` | Wrapper that refuses to run against a moved/dirty upstream clone |

## Usage

```sh
cp .env.example .env && chmod 600 .env   # then fill it in
./buzz-deploy config                      # resolve and inspect, changes nothing
./buzz-deploy up -d
./buzz-deploy logs -f relay
```

`buzz-deploy` passes upstream's compose first and this overlay second, and
refuses (exit 3) if the upstream clone is off the pin or dirty. That refusal is
the point: without it a `git pull` in the buzz clone silently changes what
deploys, and you find out in production. It is proven able to fail — checking
out `HEAD~1` produces exit 3 naming both SHAs.

Point it at a different clone with `BUZZ_SRC=/path/to/buzz ./buzz-deploy ...`.

## What actually runs

`./buzz-deploy config --services` resolves to exactly **`relay`** and
**`redis`**. Upstream's `postgres`, `minio` and `minio-init` are disabled via
never-activated profiles — a compose override cannot delete a service, and
leaving them defined keeps `config` honest about what upstream ships.

The relay's inherited `depends_on` is cleared with `!reset`; without that the
stack would deadlock waiting on containers that no longer start.

## `.env` does NOT reach the container by itself

The `env_file: !reset` on the relay resolves to **no env-file at all** under
Compose v5.x — the reset applies, the replacement list does not. Verified at the
byte level on arb-buzz (2026-08-02): `RUST_LOG` sat in `.env` and never appeared
in `docker inspect`'s container env.

**So a variable reaches the relay only if it appears in an `environment:`
mapping in `compose.do.yml`.** `.env` governs compose-side interpolation and
nothing more. Adding a key to `.env` and expecting the relay to read it is the
easiest silent mistake to make in this directory — it fails by doing nothing.

Check with `./buzz-deploy config`, which shows the resolved mapping.

## Managed Postgres notes

- Use the **private-network** host from the DO console. Same region as the
  droplet, so traffic stays off the public internet and off the egress meter.
  **This only works if the droplet and the cluster share a VPC** — they are not
  placed together automatically. If they are not (and no peering exists), the
  private host simply refuses TCP and you must fall back to the public host,
  which is TLS'd and firewalled by trusted sources but does traverse the public
  internet and does meter egress. Check before assuming the private host works.
- DO mandates TLS; keep the `?sslmode=require` their URI already carries.
- `BUZZ_DB_POOL_SIZE` + `BUZZ_DB_READ_POOL_SIZE` must fit the cluster's
  connection cap, which is small on entry plans and shared with anything else
  that connects (psql, migrations, admin). Over-provisioning fails at connect
  time, not at deploy time.
- `READ_DATABASE_URL` is optional — the relay serves membership checks from a
  replica when set (upstream #4124). **`BUZZ_REPLICA_READ_MAX_AGE_MS=0`
  disables replica routing even when the URL is set**, so setting the URL alone
  configures nothing. Set both or neither.

## Object store — R2, and why not Spaces

The relay admits its object-store backend at startup against a linearizable
conditional-write axiom (the "git object-store conformance probe / A3 gate").
It races 32 `If-Match` writers against one pointer and requires **exactly one
winner**. Failure is fatal by design: a backend that cannot do pointer CAS
invalidates the manifest-pointer protocol.

**DO Spaces fails the Buzz gate as currently exercised.** The original live
measurement (2026-08-02) and the ARB Files representation probe (2026-08-17) show:

| Operation | Spaces | R2 |
|---|---|---|
| `PUT` with the **correct quoted** `If-Match` ETag | **412** | 200 |
| Sequential `PUT` with the **correct unquoted** `If-Match` ETag | **200** | not rerun |
| `PUT` with a wrong `If-Match` ETag | 412 | 412 |
| `PUT If-None-Match: *` on a fresh key | 200 | 200 |
| `PUT If-None-Match: *` on an existing key | 412 | 412 |

Spaces does create-if-absent and ARB Files now proves sequential direct and
presigned compare-and-swap on update when it sends the unquoted opaque ETag;
stale and wrong tokens still return 412. The earlier correct-ETag 412 therefore
demonstrates a representation incompatibility, not absence of CAS. The inference
that Buzz exercised the quoted form comes from that measured 412; Buzz source is
not in this repository and was not inspected for this correction. Its 32-writer
gate remains untested with the unquoted representation, so this evidence does
not authorize moving Buzz's git object store to Spaces.

Two traps worth naming:

- **`BUZZ_GIT_CONFORMANCE_PROBE=false` is not a fix.** It disables the check,
  not the gap: git pointer updates silently become last-writer-wins.
- **You cannot keep media on Spaces and put git elsewhere.** `GitStore::new` is
  constructed from `config.media.s3_*` (`crates/buzz-relay/src/state.rs:694`),
  so one backend serves both.

R2 notes:

- `BUZZ_S3_ENDPOINT` is the **account-scoped** endpoint
  (`https://<account_id>.r2.cloudflarestorage.com`), so use
  `BUZZ_S3_ADDRESSING_STYLE=path` and `BUZZ_S3_REGION=auto`.
- Keep the bucket **private** — no `r2.dev` domain, no custom public domain.
  Media is served through the relay at `GET /media/{object}`, not from the
  bucket.
- `BUZZ_MEDIA_BASE_URL` is **the public relay origin ending in `/media`**, NOT
  the bucket URL. The relay proxies media, and `imeta` validates event media
  URLs against this base. Point it at the bucket and uploads succeed while every
  link 404s — invisible on the write path. Default is
  `http://localhost:3000/media` (`config.rs:706`).

Acceptance test is buzz's own gate, not a vendor's docs: start the relay and
look for `git object-store backend admitted: A3 conformance probe passed`.

## Why redis is droplet-local, and when that stops being true

The relay uses redis overwhelmingly as a **pub/sub fan-out bus** (105
`subscribe` / 6 `publish` / 2 `psubscribe` across the crates); Postgres is the
durable source of truth. On a single droplet, managed Valkey would add a
network hop to every publish and buy durability for data whose truth lives in
Postgres — and if the droplet dies the relay dies with it, so a surviving
Valkey keeps nothing up. Local is correct here.

**Revisit the moment a second relay instance exists.** Then redis stops being a
local cache and becomes shared coordination state between nodes, and
droplet-local is immediately wrong. The swap is two lines: give the relay a
`REDIS_URL` override and profile the `redis` service out, exactly as `postgres`
is handled here.

## Remote builds

`UPSTREAM_URL=https://github.com/block/buzz` — verified fetchable anonymously,
so a droplet needs no deploy key. The pinned REF is an ancestor of GitHub's
`main`, so it can actually be checked out remotely; a pin that existed only in
our local mirror would have failed at deploy rather than here.

Our dev clone's origin is `/Volumes/<workspace>/mirrors/buzz.git`, a local mirror
no droplet can reach — off-box builds must clone `UPSTREAM_URL`. Use the HTTPS
form: the dev Macs carry a global `insteadOf` that rewrites HTTPS to SSH, but a
droplet has no such rewrite and the repo is public.

## Build quirk, if you ever build outside Docker

`CARGO_NET_GIT_FETCH_WITH_CLI=true` is required on the Mac: the machine-global
`insteadOf` rewrite turns the `aws-creds` HTTPS dependency into SSH and cargo's
libgit2 cannot authenticate where the `git` CLI can. Building via upstream's
Dockerfile avoids this entirely, which is one more reason to use their image.

## Do not run the relay as a session child

It has died twice that way during development — a background process started by
an agent session goes away with the session. That is what compose/systemd is
for.
