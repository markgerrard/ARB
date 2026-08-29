# ARB Memory — local read-side MCP for the fleet (design v2, post-panel)

**Status:** design v2. 2026-06-26. Forks resolved by a 3-seat decorrelated panel (codex + cold-Opus
+ agy) + Mark's calls. Panel verdicts: APPROVE-WITH-CHANGES ×2, NEEDS-CHANGES ×1 — unanimous on
every fork; the one NEEDS-CHANGES gates on the security-role fix (§Security), now folded in.

## Problem (verified)

ARB Memory is built, deployed (`arb-memory.example.com`), and reachable **only by external
browser AI connectors** over the public OAuth MCP door. The agents that do the work have **no read
path**: bridge seats open with `mcpServers: []` (`engines/*_acp.py`); codex seats wire only
`node_repl`; the bridge's `ARB_MEMORY_REDIS_URL` is the *audit-emit bus* (write-only telemetry), not
memory. The orchestrator (Claude Code) has no connector either. This slice gives the local fleet
(orchestrator + every seat) `memory_search` / `memory_get` / `memory_recent` as first-class tools.

## Decisions (locked)

| Fork | Decision | Basis |
|---|---|---|
| **F1 language** | **Python** — reuse `store`/`embed`; no Go reimplementation of search | Panel unanimous; Mark: ML/pgvector tracks stay Python ([[go-python-boundary]]). Search is RRF-fused dual-arm (pgvector cosine + Postgres `tsquery`, `rrf_k=60`) — a Go reimpl drifts **silently**. |
| **F2 transport** | **stdio** (per-consumer subprocess) | Panel unanimous: zero network surface, auto-lifecycle, no local token. |
| **F3 data target** | **env-coherent DSN** — read target = the env's *write* target (orchestrator→prod, dev seats→dev); prod-read is a deliberate opt-in, never a silent default | Panel unanimous: write-to-dev / read-from-prod is incoherent and contaminates. |
| **F4 wiring** | **per-engine adapters**, one `LocalMemoryMCPConfig` → engine-specific injection; **full-fleet** rollout (orchestrator + all seats) this slice | Panel: not one abstraction. Mark: full fleet in one slice. |
| **F5 scope** | **read-only** — `memory_search/get/recent` only, via a new `ReadMemoryTools` + `build_local_server`; never `build_server` (registers writes) | Panel unanimous: read-only *by construction*, not by role failure. |

## Required changes from the panel (build gates)

### G1 — dedicated read-only DB role (HARD GATE; all 3 seats found this independently)
The design must **not** reuse the door's `arbmemory-mcp` role. `apply_mcp_grants` (grants.py) gives
it `SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA mcp_auth` — DML on the **OAuth-token
schema**. Distributing that to local boxes = any compromised local process can mint tokens / backdoor
clients → global memory compromise. Mint a new role:
```sql
CREATE ROLE arbmem_local_reader LOGIN PASSWORD '…';
GRANT USAGE ON SCHEMA public TO arbmem_local_reader;
GRANT SELECT ON public.hints, public.artefacts TO arbmem_local_reader;
-- NO mcp_auth, NO eval_*, NO audit_*, NO transcript_*
```
**Deny-proof test:** the role cannot INSERT/UPDATE/DELETE `hints`/`artefacts`, and has no access to
`mcp_auth`/`eval_*`/`audit_*`/`transcript_*` (inject-revert, both directions).

### G2 — new local server + ReadMemoryTools (the reuse seam)
`build_server` is welded to OAuth (`AuthSettings`, streamable-HTTP at `/`, `_TokenResourceMiddleware`,
`login_routes`) and `load_settings()` **hard-raises** without door secrets
(`…_PUBLIC_BASE_URL`/`…_LOGIN_SECRET`/`…_TOTP_SECRET`). So **do not lift it**. Build:
- `ReadMemoryTools` — wraps `store.retrieve/fetch_artefact/recent_artefacts` + `embed`; **read methods
  only, no writer/bus fields** (so write is impossible by construction, not by role).
- `build_local_server()` — bare `FastMCP("arb-memory-local")` registering only the 3 read tools.
- `run_local_read_mcp()` + console script `arb-memory-local-mcp` (stdio transport).

### G3 — per-engine injection without global-file mutation
Do **not** rewrite `~/.codex/config.toml` (or any global config) at launch — race-prone, non-atomic.
- ACP (grok/gemini/cursor): pass the server in the `session/new` `mcpServers` array (today `[]`) —
  in-memory, clean.
- codex: a per-launch `--config` / CLI override or a per-launch temp config (restrictive perms), not
  the global TOML.
- agent-sdk: its MCP options list.
- One `LocalMemoryMCPConfig` struct + per-engine adapters. **Per-engine test:** the launched config
  contains exactly `memory_search/get/recent` and **no** write tools.
- Gate injection behind an env flag (`ARB_MEMORY_LOCAL_MCP=<read-only-dsn>`); a seat without it is
  unchanged. Full-fleet build, but the flag lets us stage *enablement* to bound blast radius.

### G4 — implementation hardening (consensus)
- **Connection/client reuse:** `MemoryTools` opens a PG connection per call (`conn_factory()`) and
  `embed` constructs a new `OpenAI` client per call. For a long-lived stdio daemon: one
  process-scoped (or small-pool) PG connection with reconnect, and a module-singleton OpenAI client.
- **Partial availability:** `memory_search` needs `OPENAI_API_KEY` + network; `memory_get`/`recent`
  do not. A missing/expired key must degrade *only* search (still serve get/recent), not crash.
- **stdio hygiene:** structured JSON-RPC errors only — a raw traceback to stdout corrupts the channel
  and crashes the parent seat's parser.
- **Rate limiter:** `MemoryTools` keys on `access_token`, collapsing local callers to `"anonymous"`
  (one shared 30/min bucket). Use a per-process local limit, or pass a per-seat synthetic token.
- **Tombstones:** `search_hints` filters `deleted_at IS NULL`; `recent_artefacts`/`fetch_artefact`
  do **not** — local `recent`/`get` will surface deleted artefacts (same as the door). Confirm
  intended or filter.
- **No secret leakage:** the read-only DSN + `OPENAI_API_KEY` go to the MCP subprocess via env /
  per-launch temp config (restrictive perms), never persisted into user-global config.

## Non-goals
Writes (`memory_store`/`memory_remember`) stay on the door/publish-proxy. The public OAuth door is
untouched. No new public network surface.

## Security posture (corrected)
Read-only-by-construction (`ReadMemoryTools` has no write methods/bus client), dedicated SELECT-only
role (G1), stdio/loopback only, no OAuth — justified by the trusted-local threat model
([[arb-threat-model-recalibration]]: threat = mistakes) **and** only because the role is genuinely
read-only and the transport is local. Door unchanged.

## Build order (full fleet, blast-radius-bounded)
1. Role `arbmem_local_reader` + grants + deny-proof (G1).
2. `ReadMemoryTools` + `build_local_server` + `run_local_read_mcp` stdio entrypoint + tool-list test
   (G2, G5-scope) + connection/client reuse + JSON-RPC error hygiene (G4).
3. Orchestrator wiring (`claude mcp add`) — first live end-to-end (validates role/DSN/embed/search).
4. Per-engine adapters: ACP `mcpServers` → codex `--config` → agent-sdk, each with the no-write-tool
   test (G3). Enable per-engine via the env flag.
5. Env-coherent DSN policy + docs (F3).

## Open questions for the next stage (plan/spec panel)
- Connection-reuse shape: single process connection + reconnect vs a tiny pool (stdio is single-client
  per seat, so probably one connection).
- Whether to filter tombstones in local `recent`/`get` (diverge from door) or match the door.
- Per-seat synthetic token vs disabling the limiter locally.
