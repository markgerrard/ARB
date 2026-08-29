# ARB Files MCP — design

> Status: **design, approved to build** (2026-06-29), panel-reviewed (see below). Companion to ARB Memory.
> Supersedes the backlog stub `docs/superpowers/specs/2026-06-27-arb-files-mcp-backlog.md`
> (keep that file as the origin note; this is the design of record).
> ARB Memory artefact: `art-1ce1e5a1975087e4`.

## Panel review outcome (2026-06-29)

Reviewed by the canonical quorum — **codex + cold-Opus + pi-GLM + agy** — all returned
**SOUND_WITH_CHANGES** (see the panel's briefs and reports for the full record). The
load-bearing capability-matrix claims verified (image→vision true in principle; hosted-assistant
binary-reemit limit true; ChatGPT = no redesign blocker, just per-connector OAuth metadata work).
Changes folded into this spec:

- **Pre-build fix 1 — atomic clobber guard.** Non-`force` puts use `If-None-Match: *` (direct +
  presigned), not a best-effort HEAD. **agy empirically confirmed DO Spaces returns `412
  PreconditionFailed`** on the conditional re-PUT — no staging-key fallback needed.
- **Pre-build fix 2 — delete safety plane** (now load-bearing: the operator chose to keep
  convention-level containment, so this carries the read-write blast-radius risk). Deletes/overwrites
  emit an audit event and are recoverable (Spaces versioning if available, else soft-delete to
  `agent-files/.trash/<date>/`). See Local MCP + Error handling.
- **Pre-build fix 3 — image return type.** `file_get_inline` must return `mcp.types.ImageContent`
  (or a `CallToolResult` wrapping it), **never a literal dict** — FastMCP serializes a plain dict to
  JSON `TextContent` and the image→vision path silently dies. Requires a claude.ai canary before the
  matrix cell is marked ✓.
- **Containment (operator decision):** **stay convention-level** on the shared `arb-files` bucket
  (Decision #5 stands). pi-GLM's P1 to go dedicated-bucket-now was surfaced and declined; mitigated by
  fix 2. Migration to a dedicated bucket remains a config-only change.
- **P2s folded:** host-configurable inline caps (image cap ~3.5 MiB post-base64); `file_head`
  primitive; `file_list` truncation signal; provenance re-stamp on the human presigned-PUT path;
  enforced MIME allowlist on `file_put_inline`; `boto3` is a real new dep (confirmed absent — *not*
  importable via the aws CLI); validate the local `local_path` param; DO addressing resolved (both
  styles work, wildcard cert covers the dotted bucket).

## Purpose

A file-exchange plane for the fleet: let **seats**, **claude.ai**, **ChatGPT**, and a future
**macOS shell extension** hand opaque files (binaries, large blobs, model-authored text artefacts)
to each other. ARB Memory carries durable *searchable notes*; ARB Files carries *opaque files*.
Same front door, same OAuth, same fleet-distribution story.

Backend: DigitalOcean **Spaces** (S3-compatible), bucket `<bucket>`, region `lon1`, endpoint
`https://<region>.digitaloceanspaces.com`. All ARB Files objects live under the **`agent-files/`**
prefix (the bucket is shared with unrelated prefixes — see Containment).

## Decisions (resolved at design time)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Consumers | Full mirror — fleet **local stdio MCP** + external **OAuth door** |
| 2 | Door topology | **Add `file_*` tools + `files.*` scopes to the existing ARB Memory door** (one connector, one login, reuse all OAuth/DCR/TOTP/token-store code). No separate door. |
| 3 | Transfer model | **Both** — presigned URLs (primary; any size; downloads + binary uploads) **and** small-inline base64 (model-authored text artefacts; image→vision on read) |
| 4 | Layout | **Flat named shared pool** under `agent-files/`, keyed by human-meaningful name; **clobber guard** (refuse overwrite unless `force=true`) |
| 5 | Containment | **Reuse `arb-files` bucket + prefix convention** (full-bucket key). Convention-level, *not* structural — logged trade-off (see Containment). |
| 6 | Local MCP | **Read-WRITE** (deliberate divergence from ARB Memory's read-only local server — see Local MCP) |

## The capability matrix (the load-bearing reality)

The MCP door is client-agnostic — all surfaces hit the *same* tools — but what each client can
physically do with bytes differs. This table is the contract the tool surface is shaped around.

| Client | Download binary | Image → model vision | Write text artefact | Write arbitrary binary |
|---|---|---|---|---|
| **Seat** (local stdio + key) | ✓ direct, any size | n/a (no model) | ✓ direct | **✓ direct, any size — automatic** |
| **macOS shell ext** (door, holds bytes) | ✓ presigned | n/a | ✓ | ✓ presigned PUT, any size |
| **claude.ai** (door) | ✓ presigned link (human) | ✓* inline→vision *(canary-gated; needs `ImageContent` return type)* | ✓ inline | broker only (presigned; **human** PUTs) |
| **ChatGPT** (door) | ✓ presigned link | ~ verify | ✓ if dev-mode write | broker only |

Why the asymmetry:

- **Download is easy everywhere** — a presigned GET link is just a URL a human (or seat, or the
  extension) fetches; any size; bytes never transit the model.
- **Reading an *image into the model's vision* works via MCP image-content results** — `file_get_inline`
  returns `{type: "image", data: <b64>, mimeType}`, which claude.ai re-injects as a vision block.
  Non-image binaries returned as base64 are opaque to the model — for those, "read" only ever means
  *presigned download*.
- **Upload is the hard direction.** Only seats (and the Mac extension) can push arbitrary bytes
  hands-free. A hosted assistant cannot losslessly re-emit a human-attached binary — it never has the
  raw bytes (it gets extracted text / a vision image). So from claude.ai/ChatGPT: `file_put_inline`
  is honestly scoped to *text the assistant authored*; arbitrary binaries go via `file_put_url` and a
  **human** does the actual PUT (the assistant brokers).

## Architecture

Two transports over one S3 backend, mirroring ARB Memory's door/local split:

```
                         ┌─────────────────────────────┐
 claude.ai ─┐            │  ARB Memory front door       │
 ChatGPT  ──┼─ OAuth ──▶ │  (existing FastMCP server)   │── presign/inline ──┐
 mac ext  ──┘            │  + file_* tools              │                    │
                         │  + files.read / files.write  │                    ▼
                         └─────────────────────────────┘            DO Spaces  s3://arb-files/agent-files/
                                                                              ▲
 seats ── stdio ──▶  arb-files-local (read-WRITE, holds scoped key) ── direct ┘
```

- **Door path** (external): OAuth-gated `file_*` tools registered on the *existing* ARB Memory door.
  Holds the Spaces key to mint presigned URLs and do inline writes. Scope-gated.
- **Local path** (fleet): a new stdio MCP `arb-files-local` (mirrors `arb-memory-local`) that holds
  the scoped key and does **direct** CRUD — read **and write**, any size, automatic, for seats.

### Components (units, each independently testable)

| Unit | File | Responsibility | Depends on |
|------|------|----------------|------------|
| Config | `src/arb_files/config.py` | Load + validate `ARB_FILES_*` env; defaults (prefix, TTL, caps) | env |
| Store | `src/arb_files/store.py` | S3 client wrapper: `list/head/get/put/delete`, `presign_get/presign_put`, name→key, provenance metadata, clobber check | boto3 (new dep), config |
| Door tools | `src/arb_files/mcp/door_tools.py` | `FileTools` — scope-gated presigned + inline tools for the door | store, mcp auth-context |
| Local tools | `src/arb_files/mcp/local_tools.py` | read-write CRUD tools for seats (direct, no scope gating) | store |
| Local server | `src/arb_files/mcp/local_server.py` | FastMCP stdio server `arb-files-local` wiring local tools | local tools |
| Door wiring | edit `src/arb_memory/mcp/server.py` (+ scope registry) | Register `file_*` tools + `files.*` scopes on the existing door | door tools, oauth |
| Provisioning | `tools/arb-files-local/` (README + PROVISIONING) | Seat install + env, mirroring `tools/arb-memory-local/` | — |

## Tool surface

Names are shared across transports where the operation is the same; the **door** adds scope gating
and the presigned/inline split, the **local** MCP exposes direct read+write.

### Door tools (OAuth, on the ARB Memory front door)

| Tool | Scope | Returns | Notes |
|------|-------|---------|-------|
| `file_list(prefix="")` | `files.read` | `{items:[{name, size, modified, etag}], is_truncated, next_token?}` | lists under `agent-files/<prefix>`, **excluding `.trash/`**; **`is_truncated` set when the S3 page cap (`list_max`) is hit — never a silent partial**. Provenance/content_type via `file_head` (S3 list carries no user-metadata). |
| `file_head(name)` | `files.read` | `{exists, name, size, content_type, etag, modified, uploaded_by, uploaded_at}` | cheap existence/metadata probe; the handoff primitive (don't `file_list` to check existence) |
| `file_get_url(name)` | `files.read` | `{url, method:"GET", expires_in, size, content_type}` | presigned GET; any size |
| `file_get_inline(name)` | `files.read` | image → **`mcp.types.ImageContent`** (or `CallToolResult` wrapping it) — **never a literal dict** (FastMCP would JSON-ify it to text and kill vision); non-image → `{content_b64, content_type, size}` | HEAD-gates size first; rejects if over the relevant inline cap → directs to `file_get_url` |
| `file_put_inline(name, content, content_type="text/plain", force=false)` | `files.write` | `{name, size, etag}` | model-authored text; `size ≤ inline_put_max`; clobber guard |
| `file_put_url(name, content_type=null, force=false)` | `files.write` | `{url, method:"PUT", expires_in}` | presigned PUT; any size; **`If-None-Match: *` signed into the URL** for non-force (atomic, not best-effort) |
| `file_delete(name)` | `files.write` | `{deleted:true, name, recovery}` | emits an audit event + recoverable — see Delete safety |

### Local tools (stdio, seats — read-WRITE, direct key, no scope gating)

`file_list`, `file_head`, `file_get` (return b64 or write to a local path), `file_put` (from a local
path or b64), `file_delete`. Plus `file_get_url` / `file_put_url` so a seat can hand a human a
presigned link. Direct S3 ops — no presigned round-trip needed for the seat's own transfers.

**`local_path` validation:** `file_get(..., to_path)` and `file_put(from_path)` take a host
filesystem path. This is unauthenticated host-FS access — validate it: require an absolute path,
resolve symlinks, and confine reads/writes to an allowlisted root (`AGENT_WORKDIR` + a configurable
`ARB_FILES_LOCAL_ROOT`); reject traversal outside it. A seat tool should not be a write-anywhere
primitive.

**Delete safety (local + door):** the operator kept convention-level containment (Decision #5), so
delete blast-radius is mitigated here, not by the key. Every `file_delete` (and every `force`
overwrite) (a) **emits an audit event** — actor, op, name, etag/version, timestamp — to the fleet's
existing audit plane (`audit_events`, read by `src/arb_memory/visibility.py`), and (b) is
**recoverable**: enable DO Spaces **bucket versioning** if available (delete leaves a delete-marker;
prior version restorable), else **soft-delete** by server-side copy to `agent-files/.trash/<date>/`
then delete. Hard, unrecoverable, unaudited delete is not a tool the design ships.

### Shared rules

- **Name → key:** server confines every op to `agent-files/`. `name` is validated against
  `^[A-Za-z0-9._/-]{1,256}$`, rejecting leading `/`, `..`, and empty segments — a name cannot escape
  the prefix. The `agent-files/` prefix is server-prepended, never client-supplied.
- **Clobber guard (atomic, `If-None-Match`):** non-`force` puts carry `If-None-Match: *` — the store
  uploads only if the key is absent, else the backend returns `412 PreconditionFailed` surfaced as
  `"exists; pass force=true to overwrite"`. This holds for **both** direct (local) puts and presigned
  PUTs (the condition is signed into the URL), eliminating the HEAD-then-PUT TOCTOU. `force=true`
  omits the condition. *Confirmed against DO Spaces 2026-06-29 (412 on conditional re-PUT).*
- **MIME allowlist on `file_put_inline`:** enforced, mirroring ARB Memory's `WRITE_MIME_ALLOWLIST`
  (`text/plain`, `text/markdown`, `application/json`). Inline put is *honestly* text/small-artefact;
  binaries (incl. images a human attached) go via `file_put_url`. Not a convention — a rejected MIME
  is a `ValueError`.
- **Provenance:** every put writes object metadata `uploaded-by` (OAuth `client_id` for door, seat-id
  for local) and `uploaded-at`. `file_list` / `file_get_*` / `file_head` surface `uploaded_by`. On the
  **human presigned-PUT path** (where the door can't see the upload), the door re-stamps provenance
  with a server-side `copy_object` (metadata-REPLACE) when the object lands, so `uploaded_by` is never
  silently blank.
- **Caps (host-configurable env):** `inline_put_max` default **256 KiB** (matches ARB Memory content
  cap); `inline_get_max` default **5 MiB** for non-image base64, with a separate
  `inline_get_image_max` default **~3.5 MiB** (≈ 5 MiB ÷ 1.33 base64 inflation, to stay under the
  claude.ai image budget — pin via canary); `presign_ttl` default **900 s**; `list_max` default
  **1000** entries (and see `file_list` truncation, below).

## Auth & scopes

- Two new OAuth scopes — `files.read`, `files.write` — registered alongside `memory.read`/
  `memory.write` in the door's scope set. Write tools enforce `files.write` exactly as
  `MemoryTools._require_write_scope` enforces `memory.write` (fail-closed `PermissionError`).
- Per-token rate limits mirror ARB Memory (`*_rate_per_min`), keyed by access token.
- The **local** stdio MCP is unauthenticated by transport (stdio on the trusted fleet host, same
  trust model as `arb-memory-local`) and therefore read-write.

## Containment (explicit trade-off)

DO Spaces keys scope at the **bucket** level, not the prefix level. The `arb-files` bucket is shared
with several unrelated prefixes belonging to other projects (confirmed by smoke test 2026-06-29).
A key able to write `agent-files/` can therefore also reach those siblings. We **accept convention-level containment** (namespace + name-validation confine ARB Files
*code* to `agent-files/`, but the *key* is bucket-wide) under the ARB threat model — *mistakes, not
malice*, trusted solo infra (memory `arb-threat-model-recalibration`).

This is a **known divergence** from the structural-containment posture
(memory `structural-not-configurational-containment`). The structural fix — a **dedicated bucket**
with a bucket-scoped key — is recorded as the productization-era follow-up. The design must not
*depend* on the shared bucket: `ARB_FILES_BUCKET` + `ARB_FILES_PREFIX` are config, so migrating to a
dedicated bucket later is an env change, not a code change.

**Panel note (2026-06-29):** pi-GLM filed a P1 to go dedicated-bucket *now*, arguing the read-write
local MCP makes the full-bucket key materially riskier than ARB Memory's read-only case. codex +
cold-Opus judged convention-level defensible under the threat model, and found no prefix-escape via
intended tool use. **The operator chose to keep convention-level** (this decision), accepting the
residual risk, which is mitigated by the Delete-safety plane (audit + recoverable delete) rather than
by the key. Defence-in-depth guards (name validation, server-prepended prefix) remain.

## Error handling

- Backend/network errors surface as `RuntimeError` with a retry hint; never a silent success.
- Oversize inline (`get`/`put`) → `ValueError` that names the limit and points to the URL variant.
- Missing object on `get`/`delete` → explicit not-found error (no silent empty).
- Invalid name (prefix escape) → `ValueError` before any S3 call (fail-closed).
- Invalid `local_path` (outside the allowlisted root) → `ValueError` before any FS access.
- Existing key on a non-`force` put → `412`→`ValueError` `"exists; pass force=true"` (atomic).
- Missing `files.write` scope → `PermissionError` before any S3 call.
- `file_delete` / `force` overwrite → audit event emitted; recovery info (`version`/`.trash` key)
  returned. Audit-emit failure does **not** silently drop (evidence plane; memory
  `evidence-store-no-silent-drop`).

## Testing

Unit (inject a fake S3 client via a `client_factory`, mirroring ARB Memory's `conn_factory`/`embed`
injection — no live Spaces in unit tests):

- name validation: prefix-escape (`../` segment, leading `/`, empty segment) rejected before any S3
  call — assert `..` is checked **per path-segment** (split on `/`), not as a substring.
- `local_path` validation: a path outside the allowlisted root (incl. symlink escape) rejected.
- clobber guard: non-`force` put raises on existing key via the conditional path (mock the `412`);
  `force=true` overwrites; both inline and presigned-mint paths.
- scope enforcement: `file_put_*`/`file_delete` raise `PermissionError` without `files.write`.
- inline caps: oversize put/get rejected and directs to the URL variant; image vs non-image caps.
- image return type: `file_get_inline` on an image returns `ImageContent`/`CallToolResult`, **not**
  a dict that FastMCP would stringify (assert the returned type, not just the bytes).
- MIME allowlist: `file_put_inline` rejects a non-allowlisted `content_type`.
- provenance: put records `uploaded-by`; list/head surface it; presigned-PUT re-stamp path covered.
- delete safety: `file_delete` emits an audit event and returns recovery info; audit-emit failure is
  not swallowed.
- `file_list` truncation: a page-capped listing returns `is_truncated: true`.
- presign shape: returned URL is signed, has expiry, correct method/key; non-force PUT carries the
  conditional.

E2E smoke (separate, against real Spaces — like `tests/arb_memory/e2e_local_read_mcp.py`): full
put→list→get→delete lifecycle through both transports, under a disposable `agent-files/_e2e/<run>/`
prefix, asserting writer-quiesced cleanup (memory `run-isolated-verdict`).

## Out of scope (this build)

- The **macOS shell extension** — a downstream *client* of these tools; the design guarantees the
  presigned + inline tools it needs, but the extension is built separately.
- **Dedicated-bucket migration** — productization-era (above).
- **Search / indexing of files** — that's ARB Memory's job; ARB Files is fetch-by-name + list.
- **Deployment** — design + build + panel only this pass; prod-deploy is a later step.

## Verify-at-build (assumptions to confirm, not block on)

Still open:

1. **ChatGPT MCP write-tool + OAuth/DCR compatibility** with the door (claude.ai quirks are already
   special-cased in `server.py`; ChatGPT's connector client may need its own accommodation —
   confirmed it *supports* OAuth2.1/DCR/PKCE/scopes/write-with-confirmation, so no redesign, but
   expect per-connector metadata work). Confirm with a real ChatGPT connection.
2. **claude.ai MCP image-content rendering into vision** + its size ceiling (drives
   `inline_get_image_max`) — the canary that lets the matrix cell be marked ✓ rather than ~.
3. **DO Spaces bucket versioning availability** (drives whether delete-recovery is versioning vs
   `.trash/` soft-delete).

Resolved during the panel (2026-06-29, agy empirical):

- **`If-None-Match: *` honored by DO Spaces** → `412 PreconditionFailed`. Clobber guard is atomic.
- **boto3/botocore NOT importable** in the venv (the aws CLI bundles its own, non-importable). boto3
  is a **real new dependency** — pin it in the dependency manifest; do not hand-roll SigV4. Its full
  CRUD + presign + multipart justify the dep (the repo's zero-dep posture is the Go edge, not the
  Python stack, which already carries psycopg/httpx/starlette/redis/mcp).
- **DO Spaces addressing:** both virtual-host and path-style presigned GETs return 200; the
  `*.<region>.digitaloceanspaces.com` wildcard cert covers the dotted bucket name — no SAN issue. Default
  to boto3's virtual-host; no special accommodation required.
- **No prefix-escape** under the name rule (S3 keys are opaque; `agent-files/../x` stored literally).

## Build order (for the plan)

1. `config.py` + `store.py` (+ fake client) with unit tests — the isolated core.
2. `local_tools.py` + `local_server.py` + `tools/arb-files-local/` — the seat path (read-write).
3. `door_tools.py` + door/scope wiring in `src/arb_memory/mcp/` — the external path.
4. E2E smoke through both transports.
5. Panel review → integrate. (Deploy later.)
