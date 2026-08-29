# asdk seats: ARB Memory local read MCP via in-Python tier resolution

Date: 2026-07-22. Approved shape: option C (tier logic in Python).

## Problem

The read-only ARB Memory MCP (`arb-memory-local`: `memory_get` / `memory_search`
/ `memory_recent`) activates only when the seat daemon's environment carries
`ARB_MEMORY_LOCAL_MCP` (normalised to `1`) plus `ARB_MEMORY_LOCAL_DSN`. That
resolution currently lives exclusively in the launch wrapper
`scripts/agent-redis-bridge-systemd` (tier → `~/.arb-memory-local/readers.env`
→ export DSN → venv bin on PATH). The asdk seats launch via direct
`/bin/bash -c … python3 -m agent_redis_bridge` plists that bypass the wrapper,
so `local_memory_mcp_config()` returns `None` and the MCP never mounts.

Field cost: a remediation panel lost both Anthropic certifying lenses —
`asdk-bridge-dev-opus48` and `asdk-piext-dev-fable5` abstained (correctly,
fail-closed) because they could not fetch the pinned subject; FABA had to
execute three lenses firsthand.

## Change

All in `src/agent_redis_bridge/local_memory_mcp.py`; engines untouched.

1. **Tier resolution in `local_memory_mcp_config()`.** Accepted
   `ARB_MEMORY_LOCAL_MCP` values:
   - `1` (or any other non-tier truthy value): current behaviour, DSN must
     already be in the environment — unchanged.
   - `dev` | `prod` (new): read `~/.arb-memory-local/readers.env` (mode-600,
     `KEY=VALUE` lines with an optional `export ` prefix — the host file uses
     `export KEY=…` — values may be double-quoted), select
     `ARB_MEMORY_LOCAL_DSN_DEV` / `_PROD` as the DSN, and take
     `OPENAI_API_KEY` from the file when present (readers-file value wins over
     process env for consistency with the wrapper's sourcing). Missing file or
     empty tier DSN → return `None` (parity with the wrapper's silent no-op on
     hosts without secrets).
   The existing cross-store guard (`local_read_dsn`) still runs, fed a merged
   view so `ARB_MEMORY_DSN` mismatch fails closed exactly as today.

2. **Venv-anchored command resolution.** Resolve the server binary as
   `Path(sys.executable).parent / "arb-memory-local-mcp"` when that file
   exists, falling back to the bare name (PATH) otherwise. Direct-launched
   plists whose PATH lacks the venv bin then work without PATH surgery; the
   wrapper path is unaffected (same binary either way).

3. **Plists.** Add `ARB_MEMORY_LOCAL_MCP = dev` to `EnvironmentVariables` of
   the asdk seat plists (`asdk-bridge-dev-opus48`, `asdk-piext-dev-fable5`,
   `asdk-piext-dev-opus48`, `asdk-bridge-dev-haiku45`,
   `asdk-bridge-dev-sonnet5`, `asdk-project-e-dev-opus48`), then
   `launchctl kickstart -k` each.

The wrapper's shell tier logic becomes redundant-but-harmless: it exports a
resolved DSN and flag `1`, which the new code accepts via the legacy branch.

## Error handling

- readers.env unreadable / malformed line → treat as missing (return `None`),
  matching wrapper semantics; never raise at daemon startup for a missing
  optional feature.
- Tier key present but empty → `None`.
- Cross-store mismatch → raise (existing `local_read_dsn` behaviour, fail
  closed — a *misconfigured* store is not the same as an *absent* one).
- Secrets never on argv: agent-sdk passes the env dict in-process; the codex
  argv-safe path (`local_memory_mcp_argv_safe_config`) continues to route
  secrets through the mode-600 relay file unchanged.

## Testing

TDD in `tests/arb_memory/` alongside the existing local-memory injection
tests (`test_local_memory_injection_agent_sdk.py` et al.):

- tier `dev`/`prod` selects the right DSN from a tmp readers file
- missing file / empty DSN → `None`
- legacy `1` + env DSN unchanged
- readers-file `OPENAI_API_KEY` propagates into the child env
- command resolves venv-anchored when the sibling binary exists, bare
  otherwise
- cross-store mismatch still raises

Live gate after deploy: dispatch a probe to `asdk-bridge-dev-opus48` asking it
to `memory_get` a pinned artefact id/version and echo the content hash — the
exact operation the v13 panel abstains failed on.

## Live gate result (2026-07-22)

PASSED on both target seats, after two operational corrections found live:

- `launchctl kickstart -k` restarts a job with its stale loaded definition —
  plist env edits require bootout → bootstrap → kickstart (the plists have no
  RunAtLoad, so bootstrap alone leaves the job loaded-but-not-running).
- Tier `dev` points at a near-empty store (10 artefacts, no polisher rows);
  the real ARB Memory content lives in the `prod` tier store (630 artefacts).
  All six asdk plists now carry `ARB_MEMORY_LOCAL_MCP = prod`.

Probes (fetch a pinned artefact via `memory_get`, echo hash):

- `asdk-bridge-dev-opus48`: tool called, body fetched, store content hash
  matches the pin; local recompute blocked by the seat's fail-closed tool
  ceiling (disclosed, not fabricated).
- `asdk-piext-dev-fable5`: tool called, body fetched, raw SHA-256 recomputed
  and matched exactly — the same verification the earlier panel's abstains
  could not complete.

Note for panel briefs: bridge-dev asdk seats can verify the STORE content
hash (returned by the tool) but may not be able to recompute the raw body
digest under their tool ceiling — dual-pin briefs remain satisfiable because
either pin verifying is sufficient to proceed (STOP only if both fail).

## Out of scope

Stall/turn-timeout handling (tracked separately); migrating asdk seats onto
the launch wrapper; any change to codex/agy seat wiring.
