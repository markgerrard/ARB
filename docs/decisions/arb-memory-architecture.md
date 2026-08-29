# ARB Memory + audit architecture — decision record

Consolidated decision record for **ARB Memory** (formerly the standalone `ai-brain` /
"OpenBrain"). Captures what was settled, what was deferred (with the trigger that builds
it), and the one decision still open. Naming: the system is **ARB Memory**; the Postgres
database and role are **`arb_memory`** (underscore — hyphens force double-quoting in psql).

The governing principle throughout: **match the guarantee to the stakes.** Spend durability
where loss is unrecoverable; stay lossy where loss is a recoverable cache miss. Most of the
design falls out of applying that consistently.

> **Corrections folded vs the authoring draft:** (a) §5 — CF **Access** dropped; the
> interactive MCP door is **CF Tunnel + the MCP server's own OAuth 2.1 as the sole gate**
> (CF Access can't gate claude.ai/ChatGPT's MCP OAuth flow). (b) §2 — artefact boundary
> discipline refined for interactive review (full content + repo pointer, not pointer-only,
> when the consumer has no repo access). (c) naming → ARB Memory / `arb_memory`.

---

## 1. What memory is

ARB Memory is a **hint cache**, not a system of record. It exists so a seat can check the
vector DB before grepping the repo. This single framing decides most tolerances:

- A lost or stale memory is a **cache miss** — the seat falls through to grepping. Loss is
  acceptable by design.
- Memory must **never be the only copy** of anything. It is always a fast copy of something
  recoverable by other means (the repo, a doc, a commit). The moment memory holds original,
  unrecorded content, it becomes evidence and inherits audit's durability needs.
- Memory is **subordinate to `CLAUDE.md`**. CLAUDE.md is handed, deterministic,
  version-controlled doctrine read every session with no retrieval step. ARB Memory is
  retrieved, probabilistic recollection that may or may not surface.

The boundary rule: **if it's a rule, it goes in `CLAUDE.md`; if it's a recollection, it
goes in ARB Memory.** Rules are few, stable, enforced, diffable; recollections accrete, are
fuzzy, and are allowed to be missed. `CLAUDE.md` states this contract so every session
inherits it: "memory holds hints; treat them as leads, not gospel; an empty result means
*go look*, not *authoritatively absent*; this file is the source of truth."

The risk to guard against is convenience pulling doctrine into the cache: saving to memory
is one sentence, editing `CLAUDE.md` is a commit. A faster, local memory strengthens that
pull — hold the discipline; a faster cache is not a reason to file rules in it.

---

## 2. Store

- **Postgres + pgvector**, on **DO Managed** specifically for backups and PITR — the one
  capability not worth hand-rolling, on the one component holding the actual
  (recoverable-but-real) data.
- **One database (`arb_memory`), not two.** Semantic hints, versioned artefacts, and audit
  all live in the single database — separate **tables** with separate contracts, never a
  second database to provision/back up/monitor/credential. Consolidation makes some
  guarantees cheaper: artefact+hint write in one transaction, and an audit event can be
  written atomically with the thing it audits, because everything shares one transactional
  boundary.
- Dedicated vector DBs (Qdrant/Milvus/Weaviate) rejected: a memory store is ~90% relational
  (~10% vector), so consolidation beats a faster ANN nobody perceives at this scale.
  `pgvectorscale`/VectorChord stay in the back pocket for a measured-bottleneck future.
- **Retention discipline matters more with one DB.** Audit (§8) is the highest-volume,
  fastest-growing data; hints/artefacts are small. Audit growth hits the same DB memory
  reads use, with no isolation backstop — so audit's `MAXLEN` + retention must stay honest,
  or audit silently balloons the one DB everything depends on. Not a reason to split; a
  reason to bound audit.

### Schema — two tables, two retrieval contracts

Two genuinely different jobs → two tables with opposite contracts (two stores sharing one
Postgres instance, not two flavours of one thing):

- **Hint table (fuzzy lane)** — retrieved *by meaning* (pgvector nearest-neighbour).
  Chunked, embedded, dedup'd, **lossy by design**; an entry is a fragment. This is the
  hint cache §1 describes and the orchestrator-primes read model (§3) queries.
- **Artefact table (faithful lane)** — retrieved *by identity* (PK lookup, byte-for-byte).
  Not chunked, not fuzzy, **faithful, versioned**; an entry is a whole document.

Opposite operations (vector search vs PK lookup) — one schema can't be optimal for both;
that's *why* they're split. The split retires the document-vs-hint tension: faithful
artefacts stop being forced through the chunking/dedup hint path, and the hint table stays
lean.

**The hint table is the semantic index *over* the artefact table.** Store a spec as an
artefact (verbatim, keyed by ID, versioned); *also* write hint-row(s) embedding a
description of it and carrying the artefact's `(id, version)`. Retrieval is two-step:

1. Semantic-search the hint table → a hint whose payload is the **artefact `(id, version)`**
   (discovery: *that* it exists and is relevant).
2. Fetch the artefact by `(id, version)` → the full faithful document (exact retrieval).

Discovery is fuzzy; retrieval is faithful. This is the review path.

**Write both tables in one transaction.** The single consumer (§4) writes the artefact *and*
its linking hint-rows together — never a hint pointing at an artefact that didn't land, nor
an artefact with no way to discover it. One writer, one embedding owner, two tables, one txn.

**Artefacts are versioned, not overwritten — universally.** Versioning is a property of the
artefact table itself, not conditional on scope. Schema:
`artefact(artefact_id, version, content, repo_pointer, created_at)`; re-save inserts a new
version row; a hint carries `(artefact_id, version)` and so **pins to a specific version**.
Reasons: review the diff; a hint embedded against v1 keeps pointing at v1 rather than
silently re-describing v2 (the staleness overwrite reintroduces); a stale hint is
recoverable because its version still exists.

**Boundary discipline — refined for who consumes it.** "Never the only copy" (§1) still
holds, but *who reads it* decides pointer-vs-copy:

- **Consumer has repo access (seats grep)** → store a **hint pointing at the repo
  path/commit**, not a copy. The artefact lives in the repo; review runs against current
  truth.
- **Consumer has NO repo access (claude.ai / ChatGPT)** → a repo pointer is useless (they
  can't resolve a path), so the artefact holds the **full content *plus* a repo pointer**.
  Full content so it's reviewable where there's no repo; pointer for provenance + a path
  back to current truth. Still "not the only copy" (repo stays canonical), and **versioning
  keeps the snapshot honest about which commit it captured** — so the full-copy doesn't
  reintroduce staleness, it makes faithful content reachable from a no-repo client.

So artefact scope = **born-in-conversation drafts** (memory is their only home until
committed) **+ repo content you want to review interactively** (full-content + pointer).
Store verbatim only when ARB Memory is the artefact's only home, or when interactive review
requires the full bytes; committing a draft to the repo later converts it from
artefact-resident to repo-pointer hint.

---

## 3. Transport — everything rides the existing Valkey bus

The unlocking constraint: **seats already hold an authenticated Valkey connection to the
bridge.** Memory and audit ride the channel that's already open and mesh-spanning:

- No CF tunnel, no per-seat MCP auth, no second endpoint across Netcup/site-a/Hetzner/DO.
- **Authorization = bus membership.** The auth happened once when the seat joined the bus.
  The memory host stops being an endpoint anything connects to; it is purely a consumer.
- MCP stays the *interface* internally; it is no longer a network boundary seats cross.

### Writes (fire-and-forget)

Seat `XADD`s a write-intent to a write stream. The single memory consumer drains it,
embeds, dedups, inserts. The seat does not wait.

### Reads (request/reply over the bus) — the one piece with sharp edges

1. Seat publishes a query with a **correlation ID** and a **reply-to** location.
2. Seat blocks on its reply channel **with a bounded timeout**.
3. Memory consumer (`XREAD BLOCK`, not polling) runs the pgvector search, publishes the
   result keyed to the correlation ID.
4. Seat matches the ID and unblocks.

Three non-negotiables:

- **Timeout → fallback to grep — CALLER-side contract, NOT a mechanism in `arb_memory`.**
  A read is synchronous from the seat's view. Consumer down/slow or reply lost → the seat must
  not hang; `memory_query` returns **`None`** (a bounded timeout), which is the *signal* for the
  caller to fall back. **The grep itself is the caller's job** — the seat greps its **own source/
  repo**, because memory is a lossy index and the authoritative content lives with the agent, not
  with the index (`arb_memory` does not and must not know what to grep — that would couple the
  memory layer into the seat's working directory). This is §1's "empty means *go look*, not
  authoritatively absent" made precise. **The seat client owns the `None`→grep behaviour** (wired +
  tested there); `memory_query` correctly returns `None` and stops. *(Corrected 2026-06-21: the
  prior "build this before the happy path" wording implied a memory-layer mechanism that was never
  built and could not be coherent at this layer; the misnamed `test_..._then_grep` was renamed.)*
- **Per-seat reply routing.** Concurrent seats must not receive each other's answers.
  Correlation ID + per-seat reply channel (or a shared reply stream filtered by ID). Breaks
  only under concurrency — the multi-seat panel case — so single-seat tests won't catch it.
- **Robust reply mechanism.** Prefer a short-lived reply stream / `BLPOP` over pub-sub
  (pub-sub drops the message if the seat isn't subscribed at publish instant — a race).
  Tolerable at hint tier (lost reply → timeout → grep), but choose it knowingly.

**Separate read and write lanes** (distinct streams, same consumer service): a read has a
seat blocked on it (latency-critical); a write can sit in a backlog. Don't let a write burst
delay reads into unnecessary timeouts.

### Read model — orchestrator primes, seat checks on exception (single-reader-then-fan-out)

- **Orchestrator checks at panel setup**, and if a hint is relevant **injects it into the
  seats' dispatch as context** — seats receive it pre-resolved, like the task. They do not
  query memory themselves in the common case.
- **One read per panel, not per seat** — drops read volume to per-panel; relevance resolved
  once by the component that holds the panel's intent.
- **Absence is structural** — orchestrator finds a hint and injects, or doesn't and
  dispatches without one. Seats never see an empty result to misread as "authoritatively
  nothing exists."
- **Seat-checks-on-demand is the exception** — a seat may do a targeted mid-task check if
  priming didn't cover something surfacing; rare, instructed.
- **Read-only judge seats (M3, GLM) can ONLY be primed — never self-check.** Their model tool
  surface is `read,grep,find,ls` with no Bash (read-only **by tool-absence** — see
  [m3-judgment-seat.md](m3-judgment-seat.md)), so they cannot run the `arb_memory.client` CLI, and
  they must not be given a tool to: adding Bash breaks the read-only guarantee, and even a read-only
  memory-query tool changes the certified surface and re-opens the gate. For these seats **injection
  is the sole path** — the orchestrator queries and injects, the seat reads the hint with its
  existing tools, needing **no new tool**. The seat-checks-on-demand exception above therefore
  applies only to **builder** seats (codex/agy), which carry Bash and can run
  `python -m arb_memory.client query`. (Memory recall is harness/orchestrator Python over the bus,
  never a judge-seat model tool — keep it that way when the INTERNAL canary wires recall in.)
- **Injected hints stamped hint-tier** — "prior work suggests X; treat as a lead, verify
  against current reality," distinct from the authoritative task instruction. Undifferentiated
  injection is how a stale hint gets mistaken for an instruction.
- **Topic quality is retrieval quality** — a sharp orchestration-time topic
  ("fixture-masks-reality in ARB consumers") retrieves better than a seat improvising
  mid-task. The read-side mirror of rule-vs-recollection.

Trigger lives as a standing skill instruction for **defined hint-rich topic areas** (defect
classes, architectural decisions), or orchestrator-injected per-panel. Scope it to areas
where hints genuinely accumulate, or it edges back toward always-check.

---

## 4. Single-writer property

One memory consumer is the **only** thing that inserts into pgvector:

- Embedding centralized on that one node → no embedding-space drift across the fleet (two
  writers with divergent embedding models put incompatible vectors in one column and cosine
  distance silently becomes noise — `fixture-masks-reality` at fleet scale).
- Delivery is **at-least-once**, so inserts are **idempotent** — content hash or
  client-supplied ULID with `ON CONFLICT DO NOTHING`. Also absorbs the real duplicate
  source: the same fact written from many seat sessions.
- Preserving single-writer is the in-repo expression of `control-proves-only-its-path` — the
  guarantee lives in *what's deployed where*, not in good intentions (§7).

---

## 5. Two front doors

Two client kinds with different access patterns, exposure, and trust → two interfaces over
one store (matched to client, not redundancy):

- **Seats — machine clients on the bus.** High-frequency, programmatic, auth = bus
  membership. Ride Valkey for everything (§3). Internal, on the mesh, never cross a public
  boundary.
- **Interactive clients — claude.ai / ChatGPT via MCP.** Human-in-the-loop: you, querying
  and discussing memory conversationally, occasionally adding by hand. The MCP host already
  exists (one of the three singletons), so this path adds no new component.

### The two doors need different auth, because they have different exposure

The seats' "no auth, bus membership is the gate" reasoning **does not transfer** to the
interactive path: claude.ai/ChatGPT reach in from the public internet, so the MCP host must
be reachable from outside.

**Corrected posture (CF Access dropped):**

- **CF Tunnel** — outbound-only `cloudflared`, no open inbound port, nothing to port-scan.
  Kept.
- **MCP-OAuth-is-the-gate** — auth is the **MCP server's own OAuth 2.1** (already implemented
  in `ai-brain`'s `mcp_oauth_state`: access/refresh tokens, dynamically-registered OAuth
  clients; short-lived auth codes in-memory). **CF Access is dropped** — it can't gate
  claude.ai/ChatGPT, whose MCP connectors only speak MCP OAuth, not CF Access's
  SSO/service-token; CF Access would intercept and break the OAuth handshake.
- **Implication (load-bearing):** without CF Access, MCP OAuth is the **sole gate on a
  publicly-reachable endpoint** — the one genuine public trust boundary in the system. So
  when `mcp_server.py` is ported, its OAuth is **security-critical and must be scrutinized**
  (sound token validation, no bypass, DCR scoped sensibly), not rubber-stamped. Accepted
  posture: **OAuth-alone, scrutinized on port** — no second factor (anything beyond OAuth
  risks breaking the clients' DCR flow).

### Single-writer must hold across both doors

> **AS-BUILT (Phase 3, see §8a v3) — read this section as the design rationale, not the deployed shape.**
> The §5 "write through the bus / write-intent producer" path below was the *design*; the **shipped public
> door is READ-ONLY** — `memory_capture` is **not exposed**, so the deployed MCP host has no write path of any
> kind (a leaked/forged token exposes recall, not corruption). Write-via-intent remains the design for a
> *future, separately-reviewed* external-capture surface; it is not deployed. The reasoning below still holds
> for *why* a future write would go via the bus — but as of Phase 3 there is no external write to reconcile.

An external MCP client writing directly would be a **second writer** — the thing §4
prevents. Resolution: **interactive clients read directly but write through the bus.** The
MCP host reads Postgres directly (conversational — fine), but to *store* it emits a
**write-intent onto the bus**, like a seat, which the one memory consumer drains. Exactly
one thing inserts into pgvector regardless of trigger. The MCP host is a read endpoint plus
a write-intent *producer*, never an independent writer. (Rejected: MCP host as a second
sanctioned writer using the shared library — keeps embedding coherent only as long as two
call sites stay in sync, the drift single-writer exists to eliminate.)

### Full picture

- **Seats** → Valkey bus → memory consumer (one writer / reader-responder) → Postgres. Auth
  = bus membership. Internal, high-frequency, machine.
- **claude.ai / ChatGPT** → MCP host (behind CF Tunnel; **MCP OAuth is the gate**; no open
  inbound port) → reads Postgres directly; ~~writes by emitting a bus write-intent the same
  single consumer drains~~ **(AS-BUILT: read-only — no external write; see §8a v3)**.
  Interactive, human, external, trust-boundary-crossing.

---

## 6. Deployment topology

| Component | Lifecycle | Supervision | Where |
|---|---|---|---|
| Seats (panel members) | Ephemeral, per-run | systemd / nohup, orchestrator-fired | Every ARB node |
| Orchestrator | Per-invocation | — | Every ARB node |
| MCP host | Persistent singleton | Container, `restart: unless-stopped` | MCP-host box only |
| Memory consumer | Persistent singleton | Container | MCP-host box only |
| Audit consumer | Persistent singleton | Container | MCP-host box only |

**Match supervision to lifecycle.** Ephemeral + orchestrator-managed → cheap systemd/nohup.
Persistent + singleton + dependency-frozen + owns-state → containers (cleanest enforcement of
"there is one writer").

- **Normal ARB nodes run no memory/audit containers** — clients of the central trio over the
  bus.
- The three singletons are **stateless** (state in DO managed) — what makes "set up once and
  leave" safe; a recreated container reconnects and resumes.
- **Two supervision planes** on the MCP-host box (systemd for local seats, Docker for the
  trio) is the right separation; box boot brings up Docker + trio as base infra.

Two things that make "set up once and left" true:

- **Reboot survival is config, not default** — `restart: unless-stopped` *plus* Docker
  enabled at boot. Verify with a real reboot once (classic failure: trio doesn't come back
  after a kernel update, unnoticed until a memory read silently returns nothing).
- **Liveness must be visible** — singletons are accepted SPOFs (memory down → grep; audit
  down → no capture). But "fine because of the fallback" silently becomes "dead three
  weeks." The orchestrator's pre-flight health check **emits on failure**, not just gates.

**Readiness gate:** ARB must not fire seats until the trio is **actually answering**, not
just containers-up. `pg_isready`/port-open is not readiness — `green and empty` is
`fixture-masks-reality` in deployment costume.

---

## 7. Repo structure

Everything goes into the **ARB repo** — no separate repo. But consolidation must not become
responsibility dissolution:

- **Shared write-library** (embedding/chunking/dedup) lives in the repo, imported **only** by
  the three MCP-host services. One embedding owner.
- **Deploy-target scoping** pins the three services to the MCP-host box. The singleton
  property is enforced by *what's deployed where*: the writer isn't reachable elsewhere
  because it isn't deployed elsewhere.
- The rest of ARB has **no code path into memory except the bus.** "One repo now" must not
  make the embedding path a free function call from any seat — that reintroduces the
  multi-writer failure single-writer exists to prevent.

Collapse the **repo, the deploy, and the auth boundary**; keep the **responsibility seams**
as modules.

---

## 8. Audit

A **separate consumer group** on the bus (own cursor, own process), independent of memory —
their contracts are opposed (memory: lossy, lean, latency-sensitive; audit: completeness,
fire-and-forget, latency-indifferent). A crash that's a shrug for memory can be an incident
for audit; separate consumers let each outage carry its correct severity. Audit-over-Valkey
is the easy direction: pure append, no request/reply, no correlation-ID-on-read, no timeout
machinery.

### Both layers, joined

- **Orchestrator-level** = the **decisions** (dispatch, vote outcomes, certifier verdict).
- **Seat-level** = each model's **position** and where it diverged.

Both together reconstruct *how a panel reached a verdict* → the audit log is a **superset of
the disagreement corpus**; every panel auto-produces the raw material. The join is the game:

- **One run-correlation ID** minted by the orchestrator at panel start, propagated to every
  seat, carried on **every** audit event in that run.
- **A monotonic sequence number** (orchestrator's POV) for ordering — wall-clock can't order
  events across clock-skewed boxes on four providers. Cheap now, impossible to backfill.
- **One flat event schema** with a `source` discriminator (orchestrator / which seat), run
  ID, sequence, timestamp, payload. One stream, one consumer, one table, differentiated by
  column. Flat so you can query it freely later.

### Store

**Postgres alone**, personal/debugging tier — easier to search/review than object storage,
and no external party must trust the record against your ability to edit it (tamper-evidence
is theatre here). But build with a **sink list of length one** (`write(event)`, Postgres
behind it) and a **named ack policy** ("ack after required sinks; required = [postgres]").
Write events **record-shaped** (stream entry ID, content hash, raw entry alongside structured
columns). This is the seam: object-store sink and training-export sink become later
*additions*, never refactors. Build the socket, ship one cord — do not build the object-store
sink "while you're in there."

Protect Valkey: **`MAXLEN`** on the audit stream (higher volume than memory; a wedged
consumer with an unbounded stream OOMs Valkey). Overflow drops oldest — acceptable at
debugging tier.

---

## 8a. Phase 3 — public-door build decisions (folded 2026-06-21; v3 = built + code-reviewed)

Refines §5/§6/§7 into the buildable, **locally-validated** public-door phase. Full design:
`docs/superpowers/specs/2026-06-21-arb-memory-phase3-design.md`. Decided with Mark, then hardened by the
5-reviewer design panel (cold-Opus + agy + codex + M3; GLM pending) — which returned DESIGN-HOLES and moved
several decisions. The panel's findings ARE the build checklist:

- **Read-only external surface, enforced by the DATABASE (not a grep).** The door exposes reads only
  (`memory_search`/`memory_get`/`memory_recent`); no `memory_capture`, and **no Valkey client in the MCP host
  at all**. The host connects under a dedicated `arbmem_mcp` role with **`SELECT`-only on `hints`/`artefacts`**
  and DML only on its own `mcp_auth` schema — so SQL-injection / raw SQL / a future import **cannot** write
  memory (the credential lacks the privilege). Negative tests prove `INSERT/UPDATE` → `permission denied`.
  (Panel 3/3: the symbol-grep guard was hollow.)
- **The redirect_uri allowlist is THE central control.** The panel's load-bearing attack (M3): open DCR +
  phishing → an attacker registers `redirect_uri=attacker.com`, the user authenticates (2FA passes — they
  really are logging in), the code goes to the attacker, who reads the whole store. **2FA and audience-binding
  do not stop this.** Defense: a **pinned allowlist enforced at authorize-time** — scheme `https`, host ∈
  {claude.ai, chatgpt.com}, the exact/prefix connector callback paths; no wildcard, no other host. Mark's
  **claude.ai + ChatGPT-only (no Claude Code) call makes this pinnable** — Claude Code reaches memory via the
  **bus**, not the door, so no RFC 8252 loopback surface exists.
- **Two-factor login gate (passphrase + TOTP) as a real route.** Passphrase-alone blocked. The gate is a
  proper `GET/POST /login` route with **CSRF**, rate-limiting (global/per-session — not per-IP, the CF tunnel
  masks source IP), `Secure/HttpOnly/SameSite` cookies, authorize-state bound to the login session — NOT
  "inside authorize" (the SDK `provider.authorize` returns a redirect).
- **OAuth = our own AS on the SDK provider; the SDK does LESS than first assumed (cold-Opus, source-verified).**
  mcp 1.28 gives S256-PKCE verification + the route/metadata envelope + bearer plumbing — and **nothing more**.
  **Audience-binding (RFC 8707), auth-code single-use + binding, refresh rotation, and the redirect allowlist
  are PROVIDER code we write and adversarially test.** Connector reqs (search-verified): RFC 9728 PRM, RFC 8414
  ASM, DCR/CIMD, S256-only, `resource`+audience, 401+`WWW-Authenticate`. Tokens stored as **hashes**.
- **Proxy-trust:** a required fixed `PUBLIC_BASE_URL` drives all issuer/resource/redirect/metadata; inbound
  `Host`/`X-Forwarded-*` never trusted (tested with hostile headers).
- **Open DCR safe via the gate + allowlist** — registration grants nothing without 2FA (tested); global cap +
  unused-client GC + metadata size cap; registered redirects validated under the allowlist.
- **Readiness ≠ liveness.** MCP readiness gates on a memory read AND `mcp_auth` reachability, but as *readiness*
  (degraded + backoff), never *restart* (anti-flap). The public-search **embed is bounded** (rate-limit +
  query-length cap) so a forged token can't run a cost-DoS.
- **Generic code, DO's shape baked in** (SSL-configurable + pooled day one; the `arbmem_mcp` role/grants are
  standard DO-compatible DDL; one real DO SSL+pooled+grant check before go-live).
- **Local validation = branch DoD; connector-compat is a pre-go-live CANARY** (local proves the OAuth state
  machine + protocol conformance; the real claude.ai-mobile/Cowork/ChatGPT handshake is verified live, Mark's
  hands). Go-live (DO, CF tunnel, DNS, secrets, reboot-survival) is documented in `deploy/README.md`, not
  automated by the thing under review.

### Built + code-reviewed outcome (what shipped, not just what was designed)

Built on `feat/arb-memory` (T0–T16) and run through the full pipeline: design (4/5 panel) → plan (4/4 panel)
→ codex TDD build → **code-review 4/5 BLOCK + a dedicated cold-Opus OAuth-security pass** → 9 fixes → cold-Opus
re-review **APPROVE-WITH-NITS** → P1/residual fixes. Suite 127 passed/1 skipped. Deltas the review forced into
the *built* shape (these are now true of the code, not just the design):

- **Read-only is the DB role, not a code guard.** `arbmem_mcp` has `SELECT`-only on `hints`/`artefacts`; no
  Valkey client in the MCP host. Enforced by Postgres privilege + negative `permission denied` tests.
- **Connectors are PUBLIC PKCE clients** (no client secret). The code-review found an in-memory secret cache
  that *failed open* on restart (the SDK skips the secret check when the secret is absent); registering
  connectors as public PKCE clients removes the secret — PKCE + the redirect allowlist is the auth.
- **Persistence resting state = autocommit default + explicit transactions on the multi-write paths.** The
  build's connections never committed (a store that didn't store, masked by an autocommit test fixture);
  fixed to autocommit, then the multi-write `exchange`/`rotate` paths were wrapped in explicit transactions
  so a torn write can't leave partial auth state (done **before go-live** — partial-auth-state-under-
  concurrency is the hardest class to diagnose on the public boundary).
- **Login throttle is client-INDEPENDENT.** A per-client counter was bypassable by registering fresh DCR
  clients; a `__global__` ceiling locks regardless of `client_id`.
- **DCR GC keys off real use** (`last_used_at` written on use), so it can't evict the owner's live connector.
  `/token` enforces the RFC 8707 `resource`. Empty login/TOTP secrets fail closed.
- **The connector canary is the definition-of-done ceiling.** Local validation proves the OAuth state machine
  + protocol conformance; only a real claude.ai/ChatGPT connector completing DCR→login→2FA→token→call proves
  the boundary — the deploy-time equivalent of "readiness is a real round-trip, not `pg_isready`."

---

## 9. Deferred — built only when its trigger fires

| Deferred | Solves | Trigger |
|---|---|---|
| Local read-replica sidecar (per node) | Read latency at volume | A **measured** read pattern where the central round-trip hurts. Orchestrator-primes makes volume per-panel not per-seat, pushing this far out. Capture a read-frequency signal from day one. |
| Local durable audit + drain to DO | Audit during DO partition/failover | Actually running seats on a local bus during a partition *and* caring about that window. |
| Immutable object-store audit sink (Wasabi/B2) | Tamper-evident record | Someone **other than you** must trust the audit against your edit ability. Sink seam already in place. |
| Trajectory / ShareGPT export | Training data capture | A real fine-tuning need. Distinct purpose, store, grain — out of the audit table (§10). |

Discipline: build the **integration** when intent appears; build the **scaling tier** only
when usage proves it. "I want to start using memory" triggers the integration, not the
replica.

---

## 10. Open decision — seat-level audit grain

- **Decision-grain** — the seat's final position + vote. Joins cleanly, modest volume, keeps
  the audit table a queryable decision log. **Recommended (and taken unless revisited).**
- **Trajectory-grain** — the seat's full reasoning stream. Large; a *different capture
  purpose* (training-export firehose) wearing an audit label, different storage economics.

Run-correlation and schema are identical either way; the grain decides whether the audit
table stays a decision log or silently becomes a trajectory store. **Audit grain = decisions
and positions; trajectory grain = everything**, kept separate if/when training need is real.

---

## Invariants worth keeping in view

- **"Is this the only copy?"** is the real durability axis. Audit = evidence (unrecoverable)
  → durability. Memory = pointer to a recoverable thing → lossy. Treating them the same in
  *either* direction is the mistake.
- **Readiness ≠ port-open.** Gate on actually-answering. `green and empty` is
  `fixture-masks-reality`.
- **Single writer** is `control-proves-only-its-path` — enforce by deployment scope, not
  intention.
- **Timeout → grep** is the load-bearing safety valve that makes bus reads acceptable. Build
  it first.
- **Artefacts versioned, hints pin to a version** — discovery fuzzy, retrieval faithful, and
  re-capture is a new version not an overwrite.
