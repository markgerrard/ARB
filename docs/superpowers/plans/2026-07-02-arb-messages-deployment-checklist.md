# ARB Messages Deployment Checklist

ARB Messages is a generic privileged-action relay: a remote coding agent sends a sealed,
freeform request; a human-attended operator ("Codex app") fulfills it using whatever access is
appropriate, and delivers a sealed reply. There is no deterministic per-provider executor in
this codebase — the earlier Cloudflare-specific mint/revoke/sweep mechanism was retired entirely
during generalization (2026-07-02).

**Status: DEPLOYED to prod 2026-07-02.** All 20 tools (memory/files/email/messages) verified
registered on the live container; readiness green.

## Postgres Role Restriction

- Restrict write access on `arb_messages`, `arb_agent_keys`, and `arb_messages_settings` to the
  ARB Messages door role only (`arbmemory-mcp`, since the door and fulfillment tools both run
  inside the same MCP server process).
- Do not let general-purpose Postgres DSNs used by agent boxes write directly to these tables;
  otherwise a compromised box could bypass the OAuth-bound door and insert a self-approved
  request row.
- There is no separate standalone executor process/role in this architecture — fulfillment runs
  as MCP tool calls through the shared `arbmemory-mcp` connection, gated by the `messages.fulfill`
  OAuth scope, not by a distinct database role.

> **Grants-drift incident, found + fixed 2026-07-06:** `messages_register_key` started failing
> in prod with `permission denied for table arb_agent_keys` (no session had registered a *new*
> key since the fix landed, so it went unnoticed). Root cause: the rotate-on-register fix in
> `src/arb_messages/keys.py::register_key` (filed against the shared-agent_id collision, see
> `docs/BACKLOG.md` § "ARB Messages — scope agent_id per session/project") added an `UPDATE
> arb_agent_keys SET revoked_at = now() ...` statement ahead of the `INSERT` — Postgres checks
> `UPDATE` privilege the moment that statement runs, independent of whether any row matches.
> `arbmemory-mcp` had `SELECT`+`INSERT` on `arb_agent_keys` (from the original 2026-07-02
> deploy) but was never re-granted `UPDATE` when the rotate-on-register code shipped — a
> code/grants deployment mismatch, not a config regression. Fixed live in prod:
> `GRANT UPDATE ON arb_agent_keys TO "arbmemory-mcp";` (run as the app-owner role via
> `ARB_MEMORY_DSN`, no superuser needed — the owner role already had `GRANT` authority on
> tables it created). Verified via `has_table_privilege` from the door's own connection before
> and after, then confirmed end-to-end with a real `messages_register_key` call. **If this repo
> ever adds a schema-setup/grants automation step for `arb_messages`, it must grant `UPDATE` on
> `arb_agent_keys`, not just `SELECT`+`INSERT`** — this incident is exactly the gap such
> automation should close.

## OAuth Scope Provisioning

- `messages.fulfill` is in **both `valid_scopes` and `default_scopes`** — any connecting client
  (claude.ai or ChatGPT) is granted it automatically, same as `messages.request`.
- **History**: it was originally kept out of `default_scopes` as an operator-only capability
  (claim/deliver/deny ANY pending request in the whole queue), with `register_client` stripping
  it from self-service DCR requests — found necessary by a tri-model review (agy-print, sharpened
  by cold-Opus) after the MCP SDK's DCR handler was confirmed to grant any scope a client
  explicitly requests, regardless of `default_scopes` membership. That protected against
  self-service grant, but also meant ChatGPT — which only ever picks up scopes already in
  `default_scopes`, never requesting a valid-but-non-default scope explicitly the way claude.ai
  does — could **never** receive it through ordinary reconnection. The only path left was an
  out-of-band grant via `oauth_store.put_client`.
- **Operator decision (2026-07-02, explicit)**: default-grant `messages.fulfill` instead,
  accepting the wider blast radius (every future claude.ai/ChatGPT session receives it, not just
  the intended operator client) as reasonable for this single-operator deployment, where every
  token issuance already requires a personal passphrase + TOTP regardless of which client
  requests it. The strip mechanism in `register_client` was removed. See
  `src/arb_memory/mcp/server.py`'s `ClientRegistrationOptions` comment for the full reasoning.
- No further out-of-band action is needed to let Codex app fulfill requests — a normal ChatGPT
  connector connection now receives `messages.fulfill` like any other default scope.

## Structural Provider Allowlist

- `ARB_MESSAGES_ALLOWED_PROVIDERS` (required, non-empty, comma-separated) bounds which coarse
  categories of request this deployment will broker at all — checked at the door before
  enqueueing, independent of the freeform `capability` text. This is defense-in-depth, not the
  primary safety mechanism: an allowlisted agent requesting an allowlisted provider can still
  ask for anything within that category via freeform text; Codex's own judgment on the actual
  request is the real backstop. **Deployed prod value as of 2026-07-06:**
  `digitalocean,cloudflare,azure,office365` (expanded from `cloudflare,azure` — added
  `digitalocean`, needed for the ARB Memory vault-export role standup, and `office365` per
  explicit operator request at the same time). Changing this env var requires recreating the
  `mcp` container (`docker compose up -d --force-recreate mcp`) — docker-compose env vars are
  baked in at container start, not hot-reloaded; a plain `restart` does not pick up a
  `deploy/.env` change.
- `ARB_MESSAGES_ALLOWED_AGENTS` (required, non-empty) is the connector-identity gate — values are
  stable connector-host categories (`claude.ai`, `chatgpt.com`, `loopback` for native/CLI clients),
  not per-registration OAuth `client_id`s (see `src/arb_memory/mcp/redirect_policy.py`
  `connector_host_for_redirect_uris`). Deployed prod value: `claude.ai,chatgpt.com`.

## Current Residual

- Live verification of a real end-to-end fulfillment round-trip (agent requests → Codex
  fulfills using its own access → agent receives a working result) has not been run against the
  deployed instance — the queue/tools are wired and verified reachable, but no real request has
  been submitted and fulfilled yet.

## Running the test suite (added 2026-07-27)

`tests/arb_messages/*.py` read **`ARB_MESSAGES_TEST_DSN`** at import time. It is a test-only
variable — distinct from prod's runtime `ARB_MESSAGES_POSTGRES_DSN` — and until 2026-07-27 it was
defined in no env file in any clone, including the droplet's. A bare `pytest` therefore aborted at
collection with 6 `KeyError` errors and **the whole repo suite was unrunnable**, which is how 83
tests stayed invisible rather than passing.

Provision an ISOLATED, DISPOSABLE database. Never point this at prod or at a database holding real
rows: the fixtures run `TRUNCATE arb_messages, arb_agent_keys RESTART IDENTITY`, and in prod the
messages tables live *inside* the `arbmemory` database (`ARB_MESSAGES_POSTGRES_DSN` and
`ARB_MEMORY_DSN` target the same managed DB), so the prod DSN is the one value that must never be
used here. The local dev `arb_memory` DB is also unsuitable — it holds rows in those tables.

    createdb arb_messages_test            # or CREATE DATABASE from the dev owner role
    # then, in an env file OUTSIDE the repo (mode 600, same shape as envs/arb-memory-dev.env):
    ARB_MESSAGES_TEST_DSN=postgresql://<user>:<pw>@<host>:<port>/arb_messages_test

The schema is created by `arb_messages.run.setup_schema()`, so the database can be dropped and
recreated freely. On this host the file is `envs/arb-messages-test.env` in the <workspace> clone.

**Deliberately NOT changed:** the `os.environ[...]` KeyError that aborts collection. Converting it
to the `pytest.mark.skipif` pattern used by `tests/arb_memory/` would let one missing variable
silently remove 83 tests from every run — a skip-green hole, and strictly worse than a loud abort.
The abort is what made this gap findable at all; keep it loud.
