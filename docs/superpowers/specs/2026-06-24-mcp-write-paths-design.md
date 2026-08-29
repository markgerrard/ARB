# MCP write paths (artefacts + memories) — design

**Status:** design v6 — publish-proxy (door stays redis-free; sidecar+relay removed)
**Date:** 2026-06-24
**Author:** warm-Opus orchestrator (with Mark)
**Feature branch (impl):** `feat/mcp-write-paths`

> **Lineage (why v6 looks different from the panels):**
> - v1 → design panel: "via the bus, not direct DB write" upheld; folded scope/dedup/provenance fixes.
> - v2 → wanted an XADD-only Redis ACL user; **v3** found DO managed Valkey has no ACLs → topology
>   containment via a **local sidecar + relay**; **v4/v5** added a fail-loud accept-gate, then a
>   transport re-panel (BLOCK) hardened it against silent-loss (lag-eviction, PEL skip, dishonest
>   heartbeat, sidecar persistence).
> - **v6 (this):** implementation revealed the door was deliberately built **redis-free**, guarded by
>   three tests (no redis import / no redis in the runtime import graph / no REDIS env on the `mcp`
>   compose service). The sidecar gave the door a redis client — breaking that **structural** security
>   property and replacing it with a weaker **configurational** one. Pivoted to a **publish-proxy**:
>   the door makes one authenticated HTTP call to an internal writer service that holds the bus
>   credential. This restores the structural guarantee (door cannot speak bus, by construction) and
>   **removes the sidecar + relay + heartbeat entirely** — and with them the whole v4/v5 silent-loss
>   class (no buffer, no async drain → nothing to evict, skip, or lie about). See memory
>   `structural-not-configurational-containment`.

## Goal

Let the public ARB Memory MCP door accept **writes** — store artefacts and memories from claude.ai,
ChatGPT, and the Codex app via MCP tool calls — without giving the internet-exposed door direct
database-write authority **or any bus/redis client at all**. Adds `memory_store` and `memory_remember`.

## Constraints / context

- The door (`mcp` container) is public and internet-exposed. **It must hold no redis/valkey client**
  (a structural invariant guarded by `tests/arb_memory/test_mcp_readonly_import.py` + the compose-shape
  test) and its Postgres role stays **read-only** (`test_mcp_role_positive_control_denies_memory_write`).
- Writes use the existing single-writer path: `bus.memory_write()` publishes a write-intent to the
  remote `writes_stream`; the running `MemoryConsumer`/`WriteLoop` (the `memory` service) embeds,
  idempotency-checks (ULID), dedups (`content_hash`), persists, audits. **Unchanged** except its
  deterministic-bad path is upgraded from silent ack-drop to a deadletter table.
- Single trusted user (Mark) via his own connectors. Threat model: **mistakes / runaway connectors,
  not a malicious orchestrator** — but the door is the *internet-exposed* surface, so its capability
  must be minimal **by construction**, not by configuration.

## Approach (decided): authenticated publish-proxy

```
connector → DOOR (mcp, public, NO redis, read-only DB)
              validate + scope-gate + (linked) preflight
              → authenticated HTTP POST → WRITER (internal, not exposed, holds bus cred)
                                            → bus.memory_write(bus_redis, …) → remote writes_stream
                                          → MemoryConsumer (unchanged) → store + audit
```

- The door's only new capability is **"HTTP POST to one internal URL"** — strictly less than a redis
  client. It cannot reach the bus by construction (no redis library in the process).
- The **writer** proxy is internal-only (no published port; reachable only on the compose network),
  holds the bus credential, and is the single place with both bus access and write capability.
- **Fail-loud is structural:** the POST is synchronous — the writer must return `2xx` (intent
  published to the bus) before the door returns `accepted`. Writer unreachable or bus down → the
  writer errors / the POST fails → the door returns a **loud** "not accepted." No buffer ⇒ no silent
  loss; no heartbeat/lag machinery needed.

Rejected — **direct DB write from the door** (read-write role): gives the public door DB-write grants
+ duplicates embed/dedup/audit. Rejected — **sidecar + relay** (v3–v5): put a redis client in the
exposed door (breaks the structural invariant) and reintroduced a buffer with a silent-loss class.

## Components / changes

### 1. Door write tools (`src/arb_memory/mcp/tools.py`)

```
async def memory_store(self, content: str, *, artefact_id: str | None = None,
                       mime: str = "text/plain", access_token: str | None = None) -> dict
async def memory_remember(self, text: str, *, tags: list[str] | None = None,
                          artefact_id: str | None = None, artefact_version: int | None = None,
                          access_token: str | None = None) -> dict
```

- `MemoryTools` gains `writer_url`, `writer_token`, and an httpx client (`http_client`) — **no
  redis**. It keeps the read-only `conn_factory` (used for the linked-artefact preflight only).
- `_publish(intent: dict) -> dict`: `POST {writer_url}/publish` with `Authorization: Bearer
  {writer_token}` and the intent JSON; on non-2xx or transport error → raise `RuntimeError("memory
  store unavailable — item NOT stored; retry shortly")` (loud, no `accepted`); on 2xx → return the
  writer's JSON `{"ulid": …}`.
- `memory_store`: scope-gate → rate-limit → `validate_content` → derive/validate `artefact_id`
  (content-hash-derived when omitted: `art-<sha256(content,mime)[:16]>`) → `_publish({"artefact":
  {artefact_id, content, mime, source:"mcp", author}})` → `{"accepted": True, "ulid", "artefact_id"}`.
- `memory_remember`: scope-gate → rate-limit → `validate_text` → if linked, require both
  `artefact_id`+`artefact_version`, `validate_artefact_id`, and **preflight
  `store.fetch_artefact(read_conn, …)` (reject if missing)** → `_publish({"hints": [{text, metadata,
  artefact_id, artefact_version}]})` → `{"accepted": True, "ulid"}`.
- Provenance `source="mcp"`, `author=<client_id>` stamped (carried in the intent the writer publishes).

### 2. Validation + rate limit + scope (`tools.py`, `config.py`)

- `validate_content` (non-empty, ≤ `write_max_content_bytes`=256 KiB, mime ∈ allow-list
  `{text/plain,text/markdown,application/json}`), `validate_text` (non-empty, ≤
  `write_max_text_chars`=8192), `validate_artefact_id` (`^[A-Za-z0-9_-]{1,64}$`),
  `derive_artefact_id`. All deterministic-fail shapes (incl. the linked-artefact preflight) are
  rejected **at the door before the POST**.
- `_check_write_allowed` per-token sliding window (`write_rate_per_min`=30).
- `_require_write_scope`: `get_access_token().scopes` must contain `memory.write`; anonymous/`None`
  ⇒ **deny**. Single `memory.write` scope (no split).
- (Removed vs v5: `write_backlog_max`, `relay_heartbeat_max_age` settings — no buffer/heartbeat.)

### 3. Writer proxy (`src/arb_memory/writer.py`, `run.py`)

- A small Starlette ASGI app, one route `POST /publish`:
  - Auth: constant-time compare `Authorization: Bearer` against `ARB_MEMORY_WRITER_TOKEN`; 401 on
    mismatch/absent.
  - Body: `{"artefact": {...}|null, "hints": [...]}`. Calls `bus.memory_write(bus_redis,
    artefact=…, hints=…, source="mcp", author=…)` (the writer holds the bus redis client), returns
    `{"ulid": <ulid>}` 200; on bus error → 503 (door surfaces it as not-accepted).
- `run.py`: `run_writer()` (builds the bus redis client from `ARB_MEMORY_REDIS_URL`, serves the app
  on `ARB_MEMORY_WRITER_HOST`/`PORT`), `writer` added to the service choices.
- Internal-only: no published host port; reachable only at `writer:<port>` on the compose network.

### 4. Tool registration + scope (`server.py`)

- `valid_scopes`/`default_scopes` = `["memory.read","memory.write"]`; `required_scopes` stays
  `["memory.read"]`. Register `memory_store` + `memory_remember` alongside the read tools.
- `build_server` constructs the httpx client + reads `ARB_MEMORY_MCP_WRITER_URL` /
  `ARB_MEMORY_WRITER_TOKEN` from env and passes them to `MemoryTools` — **no redis import in the door**.

### 5. Schema provenance (`schema.sql`, `store.py`) — unchanged from v5

Add `source`/`author` columns to `artefacts` (+ idempotent `ALTER`), threaded through
`upsert_artefact`/`write_artefact_and_hints`/`fetch_artefact`, so MCP-written artefacts are auditable
symmetrically with hints.

### 6. Consumer deadletter (`bus.py`, `schema.sql`) — unchanged from v5

`WriteLoop._handle_entry`: deterministic-bad (parse error / non-retryable, e.g. FK `IntegrityError`)
→ write to a `write_deadletter` table then ack, instead of silent ack-drop. Infra errors still retry.

## Security posture

- **Door (internet-exposed):** no redis/valkey client (structural — uses `httpx`); read-only DB role
  (preflight is a `SELECT`); capability = authenticated HTTP POST to one internal URL. `memory.write`
  scope gate (deny anonymous); rate-limit + size/mime/id-charset caps.
- **Writer (internal):** not internet-exposed, no published port; holds the bus credential; bearer
  token so only the door can publish. The bus credential + write capability live **only** in
  non-exposed processes (writer, consumer).
- **Perimeter (operator-side, assumed):** Cloudflare Access (Entra/Azure + enforced MFA) on
  `/authorize` + `/login`, then passphrase + TOTP. Browser-only paths; connectors hit `/token`,
  `/register`, `/`, `/.well-known` server-side.
- Provenance (`source="mcp"`, `author`) on hints + artefacts; ULID idempotency; `content_hash` dedup;
  retry-idempotent ids (content-hash-derived).

## Error handling

- **Writer unreachable / bus down** → the POST fails or returns 5xx → the door raises a **loud** "not
  accepted"; nothing persisted (synchronous; no buffer). This *is* the fail-loud behaviour, by
  construction.
- **Deterministic-fail shapes** (oversize, bad mime, empty, bad id charset, missing linked artefact)
  → rejected at the door **before** the POST.
- **Consumer deterministic-bad** (parse / FK) → **deadletter** (not silent ack); infra errors retry.
- (Gone vs v5: maxlen eviction, PEL skip, heartbeat dishonesty, sidecar persistence — no buffer,
  no async drain.)

## Testing

- **Unit — door tools (`test_write_tools.py`, fake httpx client):** `memory_store`/`memory_remember`
  POST the correct intent (artefact `source=mcp`+author / hint) to `{writer_url}/publish` with the
  bearer token; return `{accepted, ulid, …}`; content-hash id stable across two omitted-id stores;
  **writer 5xx / transport error ⇒ RuntimeError, not `accepted`** (fail-loud); scope gate denies
  missing-scope + anonymous (deny-proof); validation (oversize/mime/empty/id-charset) rejected before
  POST; linked preflight (missing version / nonexistent pair ⇒ reject before POST; valid ⇒ posts).
- **Unit — writer (`test_writer.py`, fake bus redis):** bad/absent bearer ⇒ 401, no publish; valid
  ⇒ `bus.memory_write` called, `{ulid}` returned; bus error ⇒ 503.
- **Invariant — the door stays redis-free:** `test_mcp_readonly_import.py` (both) must **stay green**
  unchanged; `test_compose_shape.py` updated only to add the `writer` service to the expected set
  (the no-REDIS-on-mcp assertion stays green). `test_mcp_tools` scope/tool assertions updated to
  include `memory.write` + the two write tools.
- **Consumer (`test_write_deadletter.py`):** deterministic-bad intent ⇒ deadletter (deny-proof reds
  if removed).
- **E2E (orchestrator, after review):** drive a real write as a connector would — door tool →
  writer → bus → consumer → store, assert it lands **searchable**; plus fail-loud (writer down ⇒
  loud reject) and provenance (`source=mcp`).

## Out of scope (this slice)

- Sidecar / relay / heartbeat (removed in v6).
- Delete / update / version-mutation tools (create + append + link only).
- Multi-user ACLs / per-client scoping beyond the single `memory.write` scope.
- The Cloudflare Access configuration (operator config; documented, not built).

## Open questions — resolved

1. **Scope resolution** → `get_access_token().scopes`; anonymous/`None` ⇒ deny.
2. **Door↔writer transport/auth** → HTTP POST (`httpx`) to an internal Starlette writer; **bearer
   token** (`ARB_MEMORY_WRITER_TOKEN`), constant-time compare. Door holds no redis.
3. **`artefact_id` when omitted** → content-hash-derived (`art-<sha256(content,mime)[:16]>`).
4. **Single `memory.write` vs split** → single (YAGNI).
