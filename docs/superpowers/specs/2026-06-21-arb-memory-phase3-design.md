# ARB Memory — Phase 3: deploy + public MCP-OAuth door [design v2 — post-panel]

**Status:** DESIGN v2 (folded the 5-reviewer design panel + Mark's calls; GLM pending). Phase 3 of ARB
Memory (Workflow B). Builds on Phases 0+1+2 on `feat/arb-memory`.
**Architecture of record:** `docs/decisions/arb-memory-architecture.md` §5/§6/§7/§8a (kept in sync).

> **v2 changelog (what the panel moved):** the public door is now read-only **enforced by a Postgres
> SELECT-only role** (not a grep) and carries **no Valkey client at all**; the **redirect_uri allowlist** is
> the central control (defeats the DCR-phishing attack 2FA can't); the **SDK-vs-provider responsibility split
> is corrected** (the SDK does NOT enforce audience-binding / auth-code single-use / refresh rotation / redirect
> policy — all provider code); **proxy-trust** (fixed `PUBLIC_BASE_URL`); **login is a real CSRF/session route**;
> **token hashing**; **readiness ≠ liveness**; **embed bounded**; connector-compat is a **pre-go-live canary**,
> not local DoD. Claude Code is **not** a door client — it reaches memory via the **bus** (Mark).

## 0. Scope — IS / IS NOT
**IS:** the deploy + public-access layer, **built + validated on local infra** (branch DoD): a **read-only**
MCP server; an **OAuth 2.1 authorization server** (our own, on the mcp-SDK provider interface); **three
container entrypoints** (one image); a **docker-compose** topology + local overlay; **end-to-end local
validation** of the OAuth state machine + read path; a **go-live runbook** for Mark's hands.
**IS NOT:** no cloud go-live (DO provisioning, CF tunnel, DNS, secret provisioning, reboot-survival — Mark's
hands, documented); **no external write path** (no `memory_capture`, no Valkey client in the MCP host); **no
Claude Code door surface** (Claude Code is a bus client); no loopback redirect handling.

## 1. The threat the door must survive (panel-derived)
The OAuth server is the **sole gate** between the public internet and a personal memory store. The panel's
load-bearing finding (M3): **the redirect_uri phishing attack** — an attacker self-registers a client with
`redirect_uri=https://attacker.com/...`, phishes the user into authenticating at our *legitimate* login page
(2FA passes — the user really is authenticating), and the auth code is redirected to the attacker, who
exchanges it for a valid (audience-bound) token and reads the whole store. **2FA and audience-binding do not
stop this.** The defense is a **pinned redirect_uri allowlist enforced at authorize-time** (§4b). Read-only
keeps the blast radius at *recall, not corruption* — but for a memory store, full recall is the product, so
the allowlist is not optional.

## 2. Connector requirements (verified by search; Nov-2025 MCP spec)
claude.ai (web/Desktop/mobile/Cowork) and ChatGPT redirect the user to **our** authorize URL — so a 2FA login
page does **not** block them (it is just our IdP page). Required or we are unreachable:
- **RFC 9728 PRM** at `/.well-known/oauth-protected-resource` (unauth); **RFC 8414 ASM** at
  `/.well-known/oauth-authorization-server` with `code_challenge_methods_supported: ["S256"]`.
- **DCR (RFC 7591)** and/or **CIMD** (client_id is a URL whose metadata lists the connector's redirect_uris).
- **PKCE S256 only**; **RFC 8707 `resource`** on authorize+token, tokens audience-bound; **401 +
  `WWW-Authenticate`** → PRM.
- **Pinned connector callbacks** (§4b): claude.ai `https://claude.ai/api/mcp/auth_callback`;
  ChatGPT `https://chatgpt.com/connector_platform_oauth_redirect` (legacy) and
  `https://chatgpt.com/connector/oauth/{callback_id}` (production — variable last segment).

**SDK coverage — CORRECTED (cold-Opus, source-verified against mcp 1.28).** The SDK gives **only**: S256-PKCE
verification (`authorize.py`), the route + well-known metadata envelope, bearer-token plumbing. The SDK does
**NOT** enforce — these are **provider code we write and adversarially test**:
| Requirement | SDK | Provider must build |
|---|---|---|
| S256 PKCE verify | ✅ | — |
| PRM/ASM metadata routes | ✅ (from our config) | — |
| **RFC 8707 audience binding** | ❌ (`token.py` drops `resource`; `bearer_auth.py` doesn't check aud) | mint tokens bound to our resource; reject wrong-aud at validation |
| **Auth-code single-use + binding** | ❌ | one-use; bound to client_id/redirect_uri/resource/code_challenge |
| **Refresh rotation** | ❌ | rotate atomically; reject reused refresh |
| **Redirect_uri allowlist** | ❌ (`validate_redirect_uri` = exact string) | the §4b host+path policy |

## 3. The MCP server — read-only, structurally
Tools (reads only; served on a **SELECT-only** Postgres role; query embed bounded, §5c):
- `memory_search(query, k)` → `store.retrieve`; `memory_get(artefact_id, version)` → `store.fetch_artefact`;
  `memory_recent(limit)`.
- **No `memory_capture`. No Valkey client in the MCP host at all** (reads are direct Postgres). The door has
  no write path of any kind.

### 3a. Read-only enforced by the database, not a grep (3/3 converged; closes O2)
The MCP host connects under a dedicated role **`arbmem_mcp`** with:
- `SELECT` only on `hints`, `artefacts` (and the views it reads);
- `SELECT/INSERT/UPDATE/DELETE` only on the **`mcp_auth` schema** (its own OAuth state, §4c).

So a SQL-injection, raw query, ORM bypass, or future import **cannot** write `hints`/`artefacts` — the
credential lacks the privilege. **Negative integration tests** prove `INSERT`/`UPDATE` on the memory tables
fail with `permission denied` under the `arbmem_mcp` role. The old symbol-grep guard is dropped (hollow:
same process legitimately writes `mcp_auth`, and grep misses raw SQL). **DO-compatible:** roles/schemas/grants
are standard Postgres DDL; the one real DO connection test (§5b) verifies the grants apply on DO managed PG.

## 4. OAuth provider (our own AS — the panel findings are the build checklist)
`ArbMemoryOAuthProvider(OAuthAuthorizationServerProvider)`, state in schema `mcp_auth` (§4c).

### 4a. Login is a real route with a session state machine (cold-Opus O7; codex)
`provider.authorize` returns a redirect to **`GET /login`** (not "inside authorize"). The login route:
- renders a form with a **CSRF token**; **`POST /login`** verifies **passphrase (constant-time) + TOTP**
  (RFC 6238);
- **rate-limits** failed attempts (global + per login-session; not per-IP — the CF tunnel masks source IP);
- sets a **`Secure`/`HttpOnly`/`SameSite=Lax`** session cookie; binds the pending **authorize-state** to the
  login session; only on success issues a one-use auth code and completes the connector redirect.

### 4b. redirect_uri allowlist — THE central control (M3 P0; authorize-time enforced)
At **authorize-time** (load-bearing) AND at DCR-time, `redirect_uri` must match the pinned policy:
- scheme `https`; host ∈ {`claude.ai`, `chatgpt.com`};
- path: claude.ai exact `…/api/mcp/auth_callback`; ChatGPT exact `…/connector_platform_oauth_redirect` OR
  prefix `…/connector/oauth/` (variable callback_id);
- **no wildcards, no other host.** A DCR-registered `https://attacker.com/...` is **rejected at authorize**.
**Test:** register `attacker.com` via DCR → authorize with it → rejected (proves the phishing defense).
(Claude Code loopback is intentionally **not** allowed — Claude Code uses the bus.)

### 4c. Tokens, codes, DCR — hygiene (codex; cold-Opus O8)
- **Store token *hashes*** (sha256), never plaintext; validate by hash. Access TTL 1 h; **refresh rotation**
  (atomic; a reused refresh token is rejected + the chain revoked); auth-code TTL 10 min, **one-use**, bound
  to client_id/redirect_uri/resource/code_challenge; revocation supported. **Tokens audience-bound** (RFC 8707).
- **DCR hardening:** open registration creates a client but **grants nothing without the login gate** (test:
  register → fail to get any token without the 2FA secret). **Global registration cap + TTL/GC on unused
  clients + metadata size cap** (not IP-based — CF masks IP); registered redirect_uris validated under §4b.

### 4d. Proxy-trust (codex P0)
A required **`ARB_MEMORY_MCP_PUBLIC_BASE_URL`** drives all issuer/resource/redirect/PRM/ASM values. Inbound
`Host` / `X-Forwarded-*` are **never** trusted for metadata. Test: hostile `Host`/`X-Forwarded-Host` does not
change issued metadata or accepted redirects.

## 5. Containers, conn-layer, readiness
### 5a. Entrypoints + image (§6)
`python -m arb_memory <memory|audit|mcp>` (`run.py`), **one image** (`deploy/Dockerfile`), three compose
services, `restart: unless-stopped`. memory needs PG(write role)+Valkey+OpenAI; **mcp needs PG(`arbmem_mcp`
read role)+OpenAI only — no Valkey**; audit needs PG+Valkey.

### 5b. Generic code, DO's shape baked in (Mark)
Connection layer takes full conn config from env **day one** — `sslmode`, pool params env-driven. Local
validation uses local values (`sslmode=disable`, trivial pool); go-live flips to DO SSL + pooling. **One real
DO SSL+pooled connection test** (and a `arbmem_mcp`-role grant check) is a pre-go-live runbook step. The
schema 4c role/grants are DO-managed-PG compatible.

### 5c. Readiness ≠ liveness (codex P2; O3); embed bounded (cold-Opus O9)
- **memory:** query round-trips. **audit:** group draining. **mcp:** memory read answers AND `mcp_auth`
  reachable — but as **readiness** (degraded + backoff on a transient PG blip), **never liveness/restart**
  (anti-flap).
- The public search **embed is bounded**: per-token rate-limit + query-length cap, so a forged/abusive token
  can't run an unbounded OpenAI cost-DoS, and the embed call is decoupled from the readiness signal.

## 6. Repo structure (§7)
```
src/arb_memory/mcp/{server.py, tools.py, oauth.py, oauth_store.py, login.py, redirect_policy.py}
src/arb_memory/run.py                     # memory|audit|mcp entrypoints
src/arb_memory/schema.sql                 # + mcp_auth schema + arbmem_mcp role/grants (additive, DO-compat)
deploy/{Dockerfile, docker-compose.yml, docker-compose.local.yml, cloudflared/config.example.yml, README.md}
```
The MCP package imports `store` (read) only — no import of the write/embed-insert path or any Valkey writer.

## 7. Tests + review
- **Read-only (structural):** under `arbmem_mcp`, `INSERT/UPDATE` on `hints`/`artefacts` → `permission denied`.
- **OAuth adversarial battery (merge gate):** forged/expired/**wrong-audience** token rejected; **`resource`
  required + audience-bound** (RFC 8707); **PKCE plain rejected**, S256 ok; **auth-code replay / wrong-verifier
  / wrong-redirect / wrong-client / wrong-resource** rejected (one-use + binding); **refresh reuse** rejected
  (rotation); **redirect allowlist** — DCR-registered `attacker.com` rejected at authorize (the phishing test);
  **DCR** register-then-fail-without-2FA + storage caps; **login** CSRF + rate-limit + cookie flags +
  passphrase-only-insufficient; **proxy-trust** hostile `Host`/`X-Forwarded-*`; **401** correct
  `WWW-Authenticate`→PRM.
- **Local compose e2e:** trio green; simulated MCP client runs DCR→login(2FA,CSRF)→authorize(S256,resource)→
  token(hashed,aud-bound)→authenticated read; readiness per-dependency.
- **Connector-compat = pre-go-live CANARY (codex, honesty):** local proves the OAuth state machine + protocol
  conformance; the real claude.ai-mobile/Cowork/ChatGPT handshake is verified live at go-live (Mark's hands,
  runbook), **not** claimed as local done.
- **Review:** the 5-reviewer panel on this v2 design, then plan, then code, **plus a dedicated adversarial
  OAuth-security pass** before merge.

## 8. Opens (post-panel)
- **CLOSED O1** (auth non-blocking): pinned connector callbacks (§4b) + 2FA-is-just-our-login-page (§2).
- **CLOSED O2** (single-writer): DB `arbmem_mcp` SELECT-only role (§3a).
- **CLOSED O3** (readiness): readiness≠liveness, degraded+backoff (§5c).
- **CLOSED O4** (RFC 8707/S256): SDK does S256; provider builds+tests audience-binding (§2 table).
- **CLOSED O5** (DCR): caps + allowlist make registration non-load-bearing (§4c).
- **CLOSED O6** (scope): read-only v1; capture deferred as a future separately-reviewed surface.
- **CLOSED O7/O8/O9** (cold-Opus): login CSRF/session route (§4a); token hash + code one-use + refresh rotation
  (§4c); embed bounding (§5c).
- **REMAINING for the v2 panel:** is the §4b path-prefix match for ChatGPT's variable `connector/oauth/{id}`
  tight enough (no open-redirect via a crafted path under `chatgpt.com`)? Is CIMD (client_id-URL metadata)
  needed in v1 or is DCR sufficient for both connectors? Any GLM finding (pending).
