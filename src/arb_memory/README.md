# ARB Memory and the sibling planes

The comms plane moves work between seats. This plane is what the fleet **remembers** and what it
can later **prove**: a Postgres-backed store for versioned artefacts, semantic hints, panel audit
rows and transcripts, plus five sibling `arb_*` packages that carry the things a seat cannot do
for itself — privileged actions, file transfer, sealed secrets, email, and registration.

The governing decision record is
[`../../docs/decisions/arb-memory-architecture.md`](../../docs/decisions/arb-memory-architecture.md);
the deployment runbook is [`../../deploy/README.md`](../../deploy/README.md). Read the decision
record before changing anything here — most of the design falls out of one principle, *match the
guarantee to the stakes*, and the tolerances are deliberate.

## What ARB Memory is

**A hint cache, not a system of record.** A lost or stale memory is a cache miss: the seat falls
through to grepping its own repo. Memory must never be the only copy of anything, and it is
subordinate to [`../../CLAUDE.md`](../../CLAUDE.md) — *if it's a rule it goes in `CLAUDE.md`; if
it's a recollection it goes in ARB Memory.*

### Two tables, two retrieval contracts

`schema.sql` is the oracle for the shape. The two that matter most do opposite jobs, which is
exactly why they are split:

- **`artefacts` — the faithful lane.** Retrieved by identity (PK lookup), byte-for-byte, not
  chunked, **versioned rather than overwritten**. A re-save inserts a new version row. An entry
  is a whole document.
- **`hints` — the fuzzy lane.** Retrieved by meaning (pgvector nearest-neighbour); chunked,
  embedded, deduplicated, lossy by design. An entry is a fragment.

The hint table is the **semantic index over** the artefact table. A hint carries the artefact's
`(artefact_id, version)`, so retrieval is two-step: search hints to discover *that* something
relevant exists, then fetch the artefact by `(id, version)` for the exact bytes. Discovery is
fuzzy; retrieval is faithful. Because the hint pins a version, a hint embedded against v1 keeps
pointing at v1 instead of silently re-describing v2.

### Single writer

One consumer drains the write stream and writes the artefact **and** its linking hint rows in one
transaction — never a hint pointing at an artefact that did not land, nor an artefact with no way
to discover it. One writer, one embedding owner, two tables, one transaction. `writer.py` is that
HTTP write door; `consumer_loop.py` and `bus.py` are the stream plumbing; `store.py` holds the
SQL (`upsert_artefact`, the RRF fusion of vector and lexical ranking).

Everything rides the Valkey/Redis bus the seats are already authenticated on, so **authorization
is bus membership** and the memory host is a consumer rather than an endpoint anything connects
to. Writes are fire-and-forget `XADD`s. Reads are request/reply with a correlation ID and a
bounded timeout, on a separate lane from writes so a write burst cannot delay a read into a
needless timeout.

**A read timing out returns `None`, and `None` means *go look*, not *authoritatively absent*.**
Falling back to grep is the caller's contract, not a mechanism in this package — memory does not
and must not know what to grep. The common read path is not seats querying at all: the
orchestrator queries once at panel setup and *injects* the hint into the dispatch, stamped
hint-tier ("prior work suggests X; treat as a lead"), so seats never see an empty result to
misread.

### Visibility, grants and audit

- **Visibility** (`visibility.py`, `static/`) is a Starlette app over the same database: a login,
  an orchestrator list, a journey graph, and SSE streams per orchestrator and per seat
  (`/sse/orchestrator/{id}`, `/sse/seat/{task_id}`). `tools/arb-watch-go/` is the maintained
  terminal client for it; `watch/` here is the earlier Python/Textual one, retained for reference
  and explicitly not maintained.
- **Grants** (`mcp/grants.py`) are the access model in SQL, not in prose. The read-only role gets
  `SELECT` on `hints` and `artefacts`; `PUBLIC` is revoked from the `hint_read*` tables. The
  deny-proof tests reconnect *as that role* and assert `current_user`, so an "access denied" that
  actually came from your own OS user fails loudly instead of passing vacuously — see
  [`../agent_redis_bridge/README.md`](../agent_redis_bridge/README.md) § "Local host setup beyond
  the venv".
- **Audit** (`audit.py`, `panel_audit.py`, `panel_run.py`, `close.py`) is the highest-volume,
  fastest-growing data in the one database, so its `MAXLEN` and retention have to stay honest.
  It is what makes a panel verdict provable: exactly one dispatch manifest per run-id, or the
  close refuses as `refused_reconcile` and the run stays in Postgres as a scar.
- **Served-hint reads** (`hint_reads.py`, the `hint_read*` tables) record which hints were
  actually served. Note the trap before citing them: the retention window is shorter than the
  claim window, so any evidence artifact citing served-hint statistics must snapshot the rows and
  the window bounds at the time of the claim, including any period already purged
  ([`../../CLAUDE.md`](../../CLAUDE.md) § "Served-hint statistics").

### The MCP server

`mcp/` exposes the store over MCP with its own OAuth 2.1 (`oauth.py`, `oauth_store.py`,
`login.py`; state in the `mcp_auth` schema) — that OAuth is the sole gate for the interactive
door, because Cloudflare Access cannot gate a claude.ai / ChatGPT MCP OAuth flow. The tool
surface is `memory_store`, `memory_remember`, `memory_search`, `memory_related`,
`memory_references`, `memory_get`, `memory_recent`, scope-checked per access token
(`mcp/tools.py`). `mcp/local_server.py` + `mcp/read_tools.py` are the local read-only variant for
seats on the same host. `server.py` also mounts the sibling planes' tools, so one connector
reaches memory, messages, files and email.

## The sibling planes

| Package | What it is |
|---|---|
| [`../arb_messages/`](../arb_messages/) | **Privileged-action broker.** A seat cannot act outside its own sandbox, so it *asks*: `messages_request(request_id, capability, provider)`, then `messages_poll`. A fulfiller on the other side claims and answers — `messages_claim_next`, `messages_deliver_result`, `messages_deny`, `messages_fail`. Every row carries its policy decision and reason, so the audit trail records not just what happened but under which rule. |
| [`../arb_files/`](../arb_files/) | **Object-store file transfer** for agents that have no shared filesystem: `file_list`, `file_head`, `file_get_url`, `file_get_inline`, `file_put_inline`, `file_put_url`, `file_delete`. Inline writes are restricted to a MIME allowlist (`text/plain`, `text/markdown`, `application/json`); anything else goes through a presigned URL. |
| [`../arb_secrets/`](../arb_secrets/) | **Sealed peer↔peer secret transfer.** NaCl sealed boxes over the bus (`arb_crypto`'s `box_seal` / `box_open`), with a replay window (`SEEN_TTL`, 24h) and explicit `Rejection`. For handing a peer an API key or an env fragment without it passing through a transcript. Bootstrap: [`../../docs/runbooks/arb-secrets-bootstrap.md`](../../docs/runbooks/arb-secrets-bootstrap.md). |
| [`../arb_email/`](../arb_email/) | **Outbound email as a gated capability** (`email_send`). Recipients are parsed and allowlisted, control characters rejected, and both a time window and a rate limit apply per actor; a denial is audited with its reason rather than being silently dropped. |
| [`../arb_registration/`](../arb_registration/) | **Operator-approved, one-time seat registration** for ARB and Buzz. A signed event protocol — request → profile-ready → provision → grant/deny — so a new seat joining the bus is an approved act with a signature behind it, not a config edit. Includes the bus registrar and its ACL side (`bus_registrar.py`, `bus_acl.py`). Operations: [`../../docs/runbooks/bus-registrar-operations.md`](../../docs/runbooks/bus-registrar-operations.md). |

`../arb_crypto/` is the shared primitive layer the sealed-transfer and registration paths sign
and seal with.

## Running it

The stack, its Cloudflare tunnel, the Postgres version floor (**≥ 17**, for the bus-side claim
gate's `MAINTAIN` probe), the grant sequence and the connector canary are all in
[`../../deploy/README.md`](../../deploy/README.md). Do not point a real connector at the MCP host
until the local suite is green and that canary passes.

For a local checkout, the DSNs and the `schema.sql`-into-`public` step are in
[`../agent_redis_bridge/README.md`](../agent_redis_bridge/README.md) § "Local host setup beyond
the venv". The database-backed tests skip without their DSNs; the non-skippable gate is
`scripts/graph-sql-gate`, which refuses to run rather than skip green.

## See also

- [`../../docs/decisions/arb-memory-architecture.md`](../../docs/decisions/arb-memory-architecture.md)
  — the decision record: what was settled, what was deferred and what triggers building it.
- [`../../deploy/README.md`](../../deploy/README.md) — provisioning, grants, tunnel, retention.
- [`../../docs/runbooks/arb-memory-seat-e2e.md`](../../docs/runbooks/arb-memory-seat-e2e.md),
  [`../../docs/runbooks/arb-memory-artefact-audit-e2e.md`](../../docs/runbooks/arb-memory-artefact-audit-e2e.md),
  [`../../docs/runbooks/arb-memory-door-client-access.md`](../../docs/runbooks/arb-memory-door-client-access.md)
  — the end-to-end proofs, and how a client actually gets through the door.
- [`../../docs/self-hosted-bus.md`](../../docs/self-hosted-bus.md) — per-identity ACLs, and why a
  missing audit-emitter grant surfaces as a panel `refused_reconcile` rather than an error you
  can see from the cockpit.
