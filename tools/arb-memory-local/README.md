# ARB Memory Local Read MCP

`arb-memory-local-mcp` runs the read-only local ARB Memory MCP server over stdio. It exposes only:

- `memory_search`
- `memory_get`
- `memory_recent`

It must run with `ARB_MEMORY_LOCAL_DSN` set to the dedicated read-only Postgres role
`arbmem_local_reader`. Do not use the OAuth-door MCP role for this local server.

## Read Target Policy

The local read DSN is explicit and environment-coherent:

- `ARB_MEMORY_LOCAL_DSN` is required; there is no default DSN.
- When `ARB_MEMORY_DSN` is present, `ARB_MEMORY_LOCAL_DSN` must point at the same Postgres store
  (same host, port, and database), just with the read-only `arbmem_local_reader` role.
- Cross-store reads, such as reading production memory from a dev writer environment, require an
  explicit operator opt-in: `ARB_MEMORY_LOCAL_ALLOW_CROSS_STORE=1`.

That means dev seats read dev by default, and orchestrator production reads use production only when
the process environment deliberately carries the production writer/read pair. The bridge never derives
a local read DSN from the writer DSN and never silently falls back to prod or dev.

## Register With Claude

Load the environment for the target memory store first. For development seats, use the dev store. For
orchestrator production reads, use the production store deliberately; do not mix dev writers with prod
reads.

`claude mcp add` is registered without `--env` on purpose, so the read-only DSN and OpenAI key are not
written into Claude's persistent config. The stdio MCP child inherits environment from the running
`claude` process. That means `claude` itself must be launched from a shell where
`ARB_MEMORY_LOCAL_DSN` and, for search, `OPENAI_API_KEY` are exported.

If `ARB_MEMORY_LOCAL_DSN` uses libpq key/value syntax with spaces, export it as one quoted shell value:

```sh
export ARB_MEMORY_LOCAL_DSN='user=arbmem_local_reader password=... dbname=arb_memory host=127.0.0.1 port=5544'
export OPENAI_API_KEY='...'
claude
```

From that `claude` session's environment, register the MCP server:

```sh
scripts/arb-memory-local-mcp-register
```

The registration script runs:

```sh
claude mcp add --transport stdio arb-memory-local -- arb-memory-local-mcp
```

The MCP child inherits `ARB_MEMORY_LOCAL_DSN`, `OPENAI_API_KEY`, `PATH`, and `PYTHONPATH` from the
launch environment. `memory_get` and `memory_recent` work without `OPENAI_API_KEY`; `memory_search`
requires it and returns a structured unavailable error when it is absent.

## Tombstones

Local reads mirror the public MCP door:

- `memory_search` searches hints and excludes tombstoned hints (`deleted_at IS NULL`).
- `memory_get` and `memory_recent` read artefacts through the same store functions as the door and do
  not filter tombstoned artefacts.

This keeps local read behavior aligned with the door. Filtering artefact tombstones locally would make
seat-side reads disagree with connector-side reads.
