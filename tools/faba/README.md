# FABA launch harness (prototype)

Per-round bounded orchestration per the FABA ADR (ARB Memory `art-81438f2f5a5c4955`):
the parent shells this harness; the process is the round; process death is the
automatable `/clear`. State lives in ARB Memory and the round workspace — a fresh
instance per round, succession via the decision record, never via carried context.

The round contract, record schema, and rails live in `round-contract.md` — the ONE
instruction surface shared by both FABA forms (this SDK harness composes it into
`bootstrap_template.md` at launch; the subagent form embeds it in the dispatch brief).

## Lifecycle (contract v2 — harness-publish)

```
faba_launch.py --artefact-id <subject> --round N [--prior-record-id ID --prior-record-file F] --task "..."
  1. parses bus creds from --env-file (default .env.oi-r26; ARB_MEMORY_* whitelist) into a
     LOCAL variable — the credential never enters os.environ, so the SDK child never inherits it
  2. pre-generates request_id + record id; materialises round-input.json (+ prior-record.md)
     into a throwaway workspace; extracts the prior record's open finding ids
  3. composes round-contract.md into bootstrap_template.md and renders (invariant prefix
     SHA-logged, cache-stable; placeholders validated both directions — PF5/PF6)
  4. runs a root Agent SDK session under manifest.json tools; the agent's ONLY output
     obligation is decision-record.md in the workspace + the FABA_EXIT line
  5. INTEGRITY GATE, both halves harness-owned (PF1/PF2 remediation):
     a. CONTENT: decision-record.md must parse against the schema and carry every prior
        open finding (faba_schema.validate_decision_record) — garbage or gaps fail the round
     b. INGESTION: the harness DELetes the deterministic receipt key (clean slate), publishes
        the record ITSELF over the bus with its own request_id, and polls its own receipt —
        publish→poll is immediate, inside the receipt TTL by construction
     The agent never publishes, never holds the credential, and its FABA_EXIT line never
     decides the gate.
```

A failed gate is a failed round (nonzero exit) — crash equivalence; the parent
re-dispatches. Unit tests: `.venv/bin/python -m pytest tools/faba/tests/`.

## Proven live

- An initial smoke round and a succession round (contract v1, sonnet, low
  single-digit-cents-per-round on list pricing): full loop green; zero context carried
  between rounds.
- A subsequent audited panel review of the prototype returned verdict needs-changes —
  PF1 (receipt gate forgeable/trust-broken), PF2 (receipt TTL race), PF3 (permission
  surface). PF1/PF2 are remediated by the v2 harness-publish gate above; PF3 is deferred
  by owner decision.
- Subagent form (Workflow A twin): `subagent/README.md` — panel-cleaned and e2e-proven
  the same day.

## Known gaps (deliberate, prototype — the honest and complete list)

1. **Permission surface (PF3, DEFERRED BY OWNER 2026-07-18).** The manifest bounds the
   session only via allowed/disallowed tools; there is no deny-by-default callback, no
   settings isolation, no `strict_mcp_config`. Consequence that matters for the gate:
   a child with bare `Bash` and repo read access can harvest the bus credential from the
   repo's env file even though the harness no longer hands it over — TRUE credential
   containment is blocked on this deferred work. The v2 gate NARROWS the forgery
   window rather than closing it: content is validated from disk, and the pre-publish
   DEL kills receipts pre-seeded mid-session — but session end is not process-group
   death, so a detached credentialed descendant could re-seed after the DEL (flagged
   independently by two review seats in the panel round above). Full closure = PF3
   containment, or a store read-back once a fetch-by-id path exists.
2. **No verdict caps / seat dispatch** — rounds are single-agent; panel fan-out via the
   bridge is the next layer (dispatch/synth split per the r3 retro R1 directive).
3. **No reconcile-then-resume** — a crash after seat dispatch would need the
   pre-dispatch-record + poll pattern before this runs real panels (append-only votes
   make naive re-dispatch a superseding re-run).
4. **Subject-version spot-diff implemented (bounded scope)** — author and probe drivers
   compare the subject's round-start and round-close HEAD versions and report the result
   post-hoc. This is version-only detection (no content diff, refusal, rollback, or
   mid-round polling); a publish with an unknown receipt is `indeterminate` because its
   own version delta cannot be known honestly.
5. **Reopen predicate is coverage-only** — the schema gate enforces that prior open
   findings are CARRIED, but whether a closure's evidence justifies closing remains
   instructional (the do-not-reopen discipline lives in the contract, not the code).
6. **Headless Memory read path** — the parent materialises prior records; the local
   read MCP (`ARB_MEMORY_LOCAL_MCP`) wires in automatically when its env is present.
7. **Incident record (fixed):** an early `load_env_file` implementation copied the whole
   env file into the process env and mis-routed a panel round's dispatches. Fixed by the
   `ARB_MEMORY_*` whitelist, then superseded by parse-don't-export in v2.
8. **Workspace retention (deliberate keep-policy, PF9 residue closed by documentation).**
   Round workspaces (`mkdtemp` prefixes `faba-r*` / `faba-sa-r*`) are never auto-cleaned:
   `session-final.txt`, `round-input.json`, and the record are the only post-mortem trail
   for a failed round (the r3 incident's final message was lost exactly once — never
   again). Retention is bounded by the OS tempdir lifecycle; prune older rounds manually
   when needed, e.g. `find "${TMPDIR:-/tmp}" -maxdepth 1 -name 'faba-*' -mtime +7 -exec rm -r {} +`.
   Anything load-bearing must already be in the published record — the workspace is
   forensics, not storage.
