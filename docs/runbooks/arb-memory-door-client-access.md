# ARB Memory door access for external clients (no claude.ai connector)

How to get read/write access to ARB Memory from **any** HTTP-capable client — a
non-Claude agent, a headless orchestrator, a Claude Code CLI session on a host
with no claude.ai MCP connector, or a plain script. Proven end-to-end 2026-07-08
from a CLI host with nothing but Python stdlib (first write:
`art-c2ea574298ae1da3`, readback-verified).

The door is a standard MCP streamable-HTTP server behind OAuth 2.1. Because the
authorization server supports Dynamic Client Registration and the out-of-band
(OOB) code-display redirect, a client needs **no pre-provisioned credentials and
no loopback listener** — just a human who can pass the passphrase+TOTP login once
and paste back a code.

## What you need

- The door base URL (deploy-specific; `ARB_MEMORY_MCP_PUBLIC_BASE_URL` in
  `deploy/.env`). Metadata: `GET <base>/.well-known/oauth-authorization-server`.
- A human with the door login (passphrase + TOTP) available for one interactive
  step. Tokens refresh non-interactively afterwards.

## Step 1 — register a client (DCR)

`POST <base>/register` with an OOB redirect and no client secret (public client,
PKCE carries the proof):

```json
{
  "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob"],
  "token_endpoint_auth_method": "none",
  "grant_types": ["authorization_code", "refresh_token"],
  "response_types": ["code"],
  "client_name": "<who-you-are>",
  "scope": "memory.read memory.write"
}
```

Response contains `client_id`. Persist it (mode 600) — you reuse it for refresh.

The OOB sentinel is exact-match, lowercase, no suffix (`redirect_policy.py`
rejects anything else). Scopes supported are listed in the AS metadata
(`memory.read`, `memory.write`, `files.*`, `email.send`, `messages.*`) — request
only what the client needs.

## Step 2 — authorization URL (PKCE S256)

Generate a PKCE verifier (43–128 chars, URL-safe) and its S256 challenge, then
send the human to:

```
<base>/authorize?response_type=code&client_id=<id>
  &redirect_uri=urn%3Aietf%3Awg%3Aoauth%3A2.0%3Aoob
  &scope=memory.read+memory.write
  &code_challenge=<S256(verifier)>&code_challenge_method=S256&state=<random>
```

They log in with passphrase + TOTP. Because the client registered the OOB
redirect, the success page **displays the authorization code** (with a copy
button) instead of redirecting. The human pastes the code back to the client.

## Step 3 — token exchange

`POST <base>/token` (form-encoded):

```
grant_type=authorization_code&code=<pasted-code>
&redirect_uri=urn:ietf:wg:oauth:2.0:oob
&client_id=<id>&code_verifier=<verifier>
&resource=<base>
```

> **Gotcha (will bite you):** the `resource` parameter (RFC 8707) is REQUIRED and
> must equal the door's public base URL exactly. Omitting it returns
> `400 invalid_target: token request resource must match this resource server`.
> The same applies to refresh requests.

Response: `access_token` (Bearer, ~1 h), `refresh_token`, granted `scope`.
Store both, mode 600, out of any repo. Refresh non-interactively with
`grant_type=refresh_token&refresh_token=...&client_id=<id>&resource=<base>`.

## Step 4 — speak MCP over streamable HTTP

Three wire facts that are easy to get wrong:

1. **The MCP endpoint is the root path `/`** (the server mounts
   `streamable_http_path="/"`), not the conventional `/mcp` — that 404s.
2. Responses are **SSE-framed**: parse the `data:` lines for the JSON-RPC
   payloads, even on plain POSTs.
3. Capture the **`Mcp-Session-Id` response header** from `initialize` and send it
   back on every subsequent request.

Sequence (all `POST <base>/` with `Authorization: Bearer <token>`,
`Content-Type: application/json`, `Accept: application/json, text/event-stream`):

1. `initialize` (JSON-RPC id 1, `protocolVersion: "2025-03-26"`) → grab session
   header
2. `notifications/initialized` (no id)
3. `tools/call` as needed

Any MCP SDK client works too — the manual recipe above is the floor, not the
recommendation: point the SDK at `<base>/` with the Bearer token and it handles
framing/session for you.

## Write tools

- `memory_store(content, artefact_id?, mime)` — store a document; returns
  `{accepted, ulid, artefact_id}`. Omit `artefact_id` for a stable
  content-derived id; pass an existing one to add a version. `accepted: true`
  is structural fail-loud — the write-intent reached the bus, or you get an
  error, never a silent drop. `mime`: `text/plain`, `text/markdown`,
  `application/json`.
- `memory_remember(text, tags?, artefact_id?, artefact_version?)` — store a hint.
- Read back to verify: `memory_get(artefact_id, version)` — **`version` is a
  required argument**, there is no implicit-latest.

## Security notes

- The OOB pattern is the one Google deprecated in 2022 for phishing resistance;
  it is acceptable here only because every login already requires personal
  passphrase + TOTP (see the comment block in `redirect_policy.py`). Don't
  paste authorization codes anywhere except the client that generated the
  authorize URL, and don't reuse a code (single-use).
- Standing content rules still apply to whatever you write: named-agent
  reports never go to ARB Memory; pseudonymise. The token authenticates you;
  it doesn't change what belongs in the store.
- Tokens/verifiers: file mode 600, outside any git checkout.

## Alternatives, for completeness

- **Claude with the claude.ai connector**: use the connector; none of this is
  needed.
- **Client with a browser + loopback listener**: normal `authorization_code`
  redirect to `http://127.0.0.1:<port>/callback/...` works; OOB is for clients
  that can't or don't want to run one.
- **Local read-only, no OAuth at all**: `tools/arb-memory-local` (stdio MCP,
  read-only Postgres role) — reads without touching the door.
- **Visibility plane**: `mint_visibility_token()` (`src/arb_memory/visibility.py`)
  mints long-lived `vis-…` bearer tokens for the visibility API only — not a
  memory write path.
