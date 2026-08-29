# ARB Memory Phase 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the read-only public MCP door for ARB Memory — our own OAuth 2.1 authorization server on the `mcp` 1.28 SDK provider interface — validated entirely on local infra, with go-live held for Mark.

**Architecture:** Three container entrypoints (one image) for `memory`/`audit`/`mcp`. The `mcp` host is a **read-only** MCP server: it reads Postgres directly under a **`SELECT`-only DB role** and has **no Valkey client**. Auth is our own OAuth AS; the SDK serves S256-PKCE + the metadata/route envelope, and **the provider builds everything else** (audience-binding, auth-code single-use+binding, refresh rotation, the redirect_uri allowlist). The redirect_uri allowlist (authorize-time) is the central control against the DCR-phishing attack.

**Tech Stack:** Python 3.14, `mcp` 1.28 (FastMCP + `mcp.server.auth`), Starlette, psycopg 3, pyotp (TOTP), Postgres+pgvector, Docker Compose.

## Global Constraints

- **Read-only door:** the `mcp` package imports `store` (read) ONLY — no import of `store.write_*`/`store.upsert_*`/the embed-insert path, and **no `redis`/Valkey client anywhere under `src/arb_memory/mcp/`**.
- **DB role:** the MCP host connects as role **`arbmem_mcp`**: `SELECT` only on `hints`,`artefacts`; full DML only on schema `mcp_auth`. Enforcement is the database, not code.
- **Tokens & codes:** access/refresh tokens and auth codes are stored as **sha256 hashes**, never plaintext. Auth codes are **one-use**, bound to `client_id`/`redirect_uri`/`resource`/`code_challenge`. Refresh tokens **rotate** (reuse → reject + revoke chain). Tokens are **audience-bound** (RFC 8707) to our resource.
- **redirect_uri allowlist (pinned):** scheme `https`; host ∈ {`claude.ai`,`chatgpt.com`}; path: claude.ai exact `/api/mcp/auth_callback`; chatgpt.com exact `/connector_platform_oauth_redirect` OR prefix `/connector/oauth/`. No wildcards, no other host, no loopback (Claude Code uses the bus). Enforced at **authorize-time** AND DCR-time.
- **Proxy-trust:** all issuer/resource/redirect/PRM/ASM values derive from a required `ARB_MEMORY_MCP_PUBLIC_BASE_URL`; inbound `Host`/`X-Forwarded-*` are never trusted.
- **PKCE:** S256 only; plain rejected.
- **Secrets** live in gitignored env (`.env.arb-memory`): `ARB_MEMORY_MCP_LOGIN_SECRET`, `ARB_MEMORY_MCP_TOTP_SECRET`, `OPENAI_API_KEY`, DSNs.
- **Test DB/redis:** tests use the local pgvector DSN (`ARB_MEMORY_DSN`) + redis db 15 (never db 12). The OAuth security tests are the **merge gate**.
- **Connector-compat is a pre-go-live canary**, not local DoD — local proves the OAuth state machine + protocol conformance only.

---

## Plan v2 — folded plan-panel fixes (4/4 PLAN-HOLES: cold-Opus + agy + codex + M3)

**Cross-cutting test discipline (applies to EVERY security task — the panel's central finding):**
1. **Drive the provider methods DIRECTLY in tests, never only through the SDK HTTP/token flow.** cold-Opus
   source-verified `mcp/server/auth/handlers/token.py`: the SDK rejects wrong `client_id`/`redirect_uri`/PKCE
   verifier/expiry **before** `provider.exchange_authorization_code` runs. So a provider doing zero one-use +
   zero audience binding passes an end-to-end battery green. Each provider invariant (one-use, audience-bind,
   refresh-rotate, redirect-allowlist) gets a **unit test calling the provider method with crafted inputs**.
2. **Seed adversarial rows directly** via `oauth_store` (bypassing DCR/mint) so the validation path is actually
   exercised: a foreign-`resource` access-token row (audience test), a malicious client row (phishing test).
3. **Assert SPECIFIC exceptions**, never `pytest.raises(Exception)` (passes on any crash). Use the SDK's
   typed auth errors / explicit return-None.
4. **Rate-limit tests create FRESH sessions** and still hit the limiter (a per-session counter is not a limiter).

**Per-task fixes folded below (also reflected inline where it changes code):**
- **T0 (NEW):** an SDK contract test pinning the exact `mcp==1.28` classes (`AuthorizationParams`,
  `AuthSettings`, `ClientRegistrationOptions`, `OAuthClientInformationFull`, the provider Protocol) — imported
  and instantiated with real objects — so later tasks build on verified constructors, not assumed ones.
- **T1:** assert `current_user='arbmem_mcp'` + `has_table_privilege(...,'INSERT')` is false; test raw-SQL writes
  to `hints`, `artefacts`, AND `audit_events` separately; use a valid row shape so a failure is *permission*,
  not a constraint.
- **T3:** **`unquote()` the path before any check** and reject `%2e%2e`/encoded-slash/backslash; deny cases for
  percent-encoded traversal, userinfo, explicit port, **query/fragment**, mixed-case host, and
  `chatgpt.com.attacker.com` / `evil.chatgpt.com`.
- **T6:** a **global/per-client** login+token+DCR throttle (not only `login_sessions.fail_count`), tested by
  creating fresh sessions; rotate `session_id` on successful auth (anti-fixation).
- **T7:** **seed the malicious client directly with `oauth_store.put_client`** (bypassing DCR), call
  `provider.authorize` with the unauthorized redirect, assert the **specific** `AuthorizeError`; ALSO test the
  no-`redirect_uri`-param case (SDK falls back to `redirect_uris[0]`) so a single-registered-attacker-URI
  client can't slip through.
- **T8:** drive `provider.exchange_authorization_code` directly; **verify all bindings BEFORE `consume_code`**
  (a wrong verifier must NOT burn the code → then the correct verifier still succeeds); test PKCE-plain
  downgrade rejected.
- **T9:** drive `load_access_token`/`exchange_refresh_token` directly; **seed a foreign-`resource` token row**
  and assert `verify_token` → None; refresh-reuse asserts old refresh AND new refresh AND associated access
  tokens are ALL rejected (cascade revoke).
- **T11:** **drop the store-write symbol-grep** (hollow — `store` is one module the tools must import; the
  DB-role test in T1 is the real control). Keep only the **no-`redis`/`valkey` import** clause + a **fail-loud
  positive control** that the `arbmem_mcp` role actually denies a write.
- **T12:** hostile `Host`/`X-Forwarded-*` tested across **PRM, ASM issuer, the authorize→login URL, the minted
  token's `resource`/audience, AND the 401 `WWW-Authenticate`** — not PRM alone.

---

### Task 1: DB schema — `mcp_auth` schema + `arbmem_mcp` SELECT-only role

**Files:**
- Modify: `src/arb_memory/schema.sql` (append; additive)
- Test: `tests/arb_memory/test_mcp_role.py`

**Interfaces:**
- Produces: schema `mcp_auth` with tables `oauth_clients(client_id pk, client_secret_hash, redirect_uris jsonb, metadata jsonb, created_at, last_used_at)`, `auth_codes(code_hash pk, client_id, redirect_uri, resource, code_challenge, scopes, expires_at, used_at)`, `access_tokens(token_hash pk, client_id, resource, scopes, expires_at, revoked_at)`, `refresh_tokens(token_hash pk, client_id, access_token_hash, resource, scopes, expires_at, rotated_to, revoked_at)`, `login_sessions(session_id pk, csrf_token, authorize_state jsonb, verified_at, expires_at, fail_count)`; role `arbmem_mcp`.

- [ ] **Step 1: Write the failing test**
```python
# tests/arb_memory/test_mcp_role.py
import os, psycopg, pytest

def _mcp_dsn():
    # same DB, role swapped to arbmem_mcp (password from env or trust locally)
    return os.environ.get("ARB_MEMORY_MCP_DSN") or os.environ["ARB_MEMORY_DSN"].replace("arb_memory:", "arbmem_mcp:")

def test_mcp_role_cannot_write_memory(scratch):
    # scratch applied schema.sql (creates role + grants). Connect AS arbmem_mcp.
    with psycopg.connect(_mcp_dsn()) as c:
        c.execute("SELECT count(*) FROM hints")           # SELECT allowed
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("INSERT INTO hints (text, embedding) VALUES ('x', NULL)")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("UPDATE artefacts SET content='x'")

def test_mcp_role_can_write_its_own_auth_schema(scratch):
    with psycopg.connect(_mcp_dsn()) as c:
        c.execute("INSERT INTO mcp_auth.oauth_clients (client_id, redirect_uris, metadata) VALUES ('c1', '[]'::jsonb, '{}'::jsonb)")
        c.execute("DELETE FROM mcp_auth.oauth_clients WHERE client_id='c1'")
```

- [ ] **Step 2: Run → FAIL** (`pytest tests/arb_memory/test_mcp_role.py -v` → role/schema missing).

- [ ] **Step 3: Implement** — append to `schema.sql`:
```sql
CREATE SCHEMA IF NOT EXISTS mcp_auth;
CREATE TABLE IF NOT EXISTS mcp_auth.oauth_clients (client_id text PRIMARY KEY, client_secret_hash text, redirect_uris jsonb NOT NULL DEFAULT '[]', metadata jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now(), last_used_at timestamptz);
CREATE TABLE IF NOT EXISTS mcp_auth.auth_codes (code_hash text PRIMARY KEY, client_id text NOT NULL, redirect_uri text NOT NULL, resource text NOT NULL, code_challenge text NOT NULL, scopes jsonb NOT NULL DEFAULT '[]', expires_at timestamptz NOT NULL, used_at timestamptz);
CREATE TABLE IF NOT EXISTS mcp_auth.access_tokens (token_hash text PRIMARY KEY, client_id text NOT NULL, resource text NOT NULL, scopes jsonb NOT NULL DEFAULT '[]', expires_at timestamptz NOT NULL, revoked_at timestamptz);
CREATE TABLE IF NOT EXISTS mcp_auth.refresh_tokens (token_hash text PRIMARY KEY, client_id text NOT NULL, access_token_hash text, resource text NOT NULL, scopes jsonb NOT NULL DEFAULT '[]', expires_at timestamptz NOT NULL, rotated_to text, revoked_at timestamptz);
CREATE TABLE IF NOT EXISTS mcp_auth.login_sessions (session_id text PRIMARY KEY, csrf_token text NOT NULL, authorize_state jsonb NOT NULL, verified_at timestamptz, expires_at timestamptz NOT NULL, fail_count int NOT NULL DEFAULT 0);
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='arbmem_mcp') THEN CREATE ROLE arbmem_mcp LOGIN; END IF;
END $$;
GRANT USAGE ON SCHEMA public TO arbmem_mcp;
GRANT SELECT ON public.hints, public.artefacts TO arbmem_mcp;
GRANT USAGE ON SCHEMA mcp_auth TO arbmem_mcp;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA mcp_auth TO arbmem_mcp;
ALTER DEFAULT PRIVILEGES IN SCHEMA mcp_auth GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO arbmem_mcp;
```
(DO-managed-PG compatible: standard role/schema/grant DDL. The conftest `scratch` fixture must run as a superuser/owner so CREATE ROLE succeeds; if the local role lacks a password, set `ARB_MEMORY_MCP_DSN` for the test.)

- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** `feat(arb-memory-mcp): mcp_auth schema + arbmem_mcp SELECT-only role`.

---

### Task 2: Config + connection layer (PUBLIC_BASE_URL, SSL/pool env-driven)

**Files:** Create `src/arb_memory/mcp/__init__.py`, `src/arb_memory/mcp/config.py`; Test `tests/arb_memory/test_mcp_config.py`.

**Interfaces:**
- Produces: `Settings` (frozen dataclass) with `public_base_url: str`, `mcp_dsn: str` (SSL/pool from env), `login_secret: str`, `totp_secret: str`, `access_ttl=3600`, `refresh_ttl=2592000`, `code_ttl=600`, `login_ttl=300`, `dcr_global_cap=200`, `dcr_metadata_max_bytes=4096`, `search_max_query_chars=2000`, `search_rate_per_min=30`. `load_settings() -> Settings`. `mcp_connect() -> psycopg.Connection` honoring `sslmode`/pool params.

- [ ] **Step 1: Failing test** — `load_settings()` reads `ARB_MEMORY_MCP_PUBLIC_BASE_URL`; raises if missing; `mcp_dsn` carries `sslmode` when env sets it.
```python
def test_settings_require_public_base_url(monkeypatch):
    monkeypatch.delenv("ARB_MEMORY_MCP_PUBLIC_BASE_URL", raising=False)
    import arb_memory.mcp.config as cfg
    with pytest.raises(ValueError): cfg.load_settings()

def test_settings_carry_public_base_url(monkeypatch):
    monkeypatch.setenv("ARB_MEMORY_MCP_PUBLIC_BASE_URL", "https://mem.example.com")
    import arb_memory.mcp.config as cfg
    s = cfg.load_settings()
    assert s.public_base_url == "https://mem.example.com"
```
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** `config.py` — dataclass + `load_settings` (env reads, `ValueError` if `PUBLIC_BASE_URL` unset, strip trailing slash), `mcp_connect` (psycopg.connect with `ARB_MEMORY_MCP_DSN`; never derive URL from request).
- [ ] **Step 4: PASS.** **Step 5: Commit.**

---

### Task 3: redirect_uri allowlist policy (the central control)

**Files:** Create `src/arb_memory/mcp/redirect_policy.py`; Test `tests/arb_memory/test_redirect_policy.py`.

**Interfaces:**
- Produces: `is_allowed_redirect(uri: str) -> bool`.

- [ ] **Step 1: Failing test (the phishing-defense battery)**
```python
import pytest
from arb_memory.mcp.redirect_policy import is_allowed_redirect

@pytest.mark.parametrize("uri,ok", [
    ("https://claude.ai/api/mcp/auth_callback", True),
    ("https://chatgpt.com/connector_platform_oauth_redirect", True),
    ("https://chatgpt.com/connector/oauth/abc123", True),
    ("https://chatgpt.com/connector/oauth/", True),
    ("https://attacker.com/callback", False),            # the phishing target
    ("http://claude.ai/api/mcp/auth_callback", False),   # non-https
    ("https://claude.ai/evil", False),                   # right host, wrong path
    ("https://evil.chatgpt.com/connector/oauth/x", False),# subdomain spoof
    ("https://chatgpt.com.attacker.com/connector/oauth/x", False),
    ("https://chatgpt.com/connector/oauth/../../evil", False), # literal traversal
    ("https://chatgpt.com/connector/oauth/%2e%2e/evil", False),# ENCODED traversal (panel P0)
    ("https://chatgpt.com/connector/oauth/%2E%2E/evil", False),# encoded, upper
    ("https://chatgpt.com/connector/oauth/%2fevil", False),    # encoded slash
    ("https://chatgpt.com/connector/oauth/x?next=evil", False),# query smuggling
    ("https://chatgpt.com/connector/oauth/x#evil", False),     # fragment smuggling
    ("https://chatgpt.com.attacker.com/connector/oauth/x", False), # suffix spoof
    ("https://user@claude.ai/api/mcp/auth_callback", False),   # userinfo
    ("https://claude.ai:8443/api/mcp/auth_callback", False),   # explicit port
    ("https://CLAUDE.AI/api/mcp/auth_callback", True),         # host case-insensitive
    ("https://claude.ai/api/mcp/auth_callback/../evil", False),
    ("http://localhost:8080/callback", False),           # loopback NOT allowed
])
def test_redirect_allowlist(uri, ok):
    assert is_allowed_redirect(uri) is ok
```
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — `urlsplit`; require `scheme=="https"`, no userinfo/port; host lower-cased and exactly in `_RULES`; **reject any percent-encoding in the path** (`"%" in raw_path`) so an attacker can't smuggle `%2e%2e`/`%2f`; **reject query/fragment**; then `unquote` (defence-in-depth) and reject `..`; finally the host path rule.
```python
from urllib.parse import urlsplit, unquote
_RULES = {"claude.ai": lambda p: p == "/api/mcp/auth_callback",
          "chatgpt.com": lambda p: p == "/connector_platform_oauth_redirect" or p.startswith("/connector/oauth/")}
def is_allowed_redirect(uri: str) -> bool:
    try: u = urlsplit(uri)
    except ValueError: return False
    if u.scheme != "https" or u.username or u.password or u.port: return False
    if u.query or u.fragment: return False
    host = (u.hostname or "")            # urlsplit already lower-cases hostname
    if host not in _RULES: return False
    raw = u.path
    if "%" in raw or "\\" in raw: return False   # no encoded separators/traversal
    path = unquote(raw)
    if ".." in path.split("/"): return False
    return _RULES[host](path)
```
- [ ] **Step 4: PASS (all cases incl. encoded traversal).** **Step 5: Commit.**

---

### Task 4: oauth_store — hashed persistence for clients/codes/tokens/sessions

**Files:** Create `src/arb_memory/mcp/oauth_store.py`; Test `tests/arb_memory/test_oauth_store.py`.

**Interfaces (all take a `conn`):**
- Produces: `hash_token(secret: str) -> str` (sha256 hex); `put_code/get_code/consume_code` (one-use via `used_at`); `put_access_token/get_access_token`; `put_refresh_token/rotate_refresh_token/get_refresh_token`; `put_client/get_client/count_clients/gc_unused_clients`; `put_login_session/get_login_session/bump_fail`. Tokens/codes always stored by **hash**; lookups by hash.

- [ ] **Step 1: Failing tests** — (a) `put_access_token` stores a row whose PK is `hash_token(tok)` and the plaintext `tok` appears in **no** column (assert by scanning row values); (b) `consume_code` returns the row once then `None` (one-use); (c) `rotate_refresh_token(old)` marks old `rotated_to`, returns new, and a second `get_refresh_token(old)` is rejected as reused.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** with `hashlib.sha256(secret.encode()).hexdigest()`; `consume_code` does `UPDATE ... SET used_at=now() WHERE code_hash=%s AND used_at IS NULL RETURNING ...` (atomic one-use); `rotate_refresh_token` in one transaction.
- [ ] **Step 4: PASS.** **Step 5: Commit.**

---

### Task 5: login factors — passphrase (constant-time) + TOTP

**Files:** Add dep `pyotp`; Create `src/arb_memory/mcp/login_factors.py`; Test `tests/arb_memory/test_login_factors.py`.

**Interfaces:**
- Produces: `verify_passphrase(supplied: str, secret: str) -> bool` (`hmac.compare_digest`); `verify_totp(code: str, secret: str) -> bool` (`pyotp.TOTP(secret).verify(code, valid_window=1)`); `verify_two_factor(passphrase, totp, *, settings) -> bool` (BOTH required).

- [ ] **Step 1: Failing tests** — correct both → True; wrong TOTP → False; correct passphrase + empty TOTP → False (passphrase-only insufficient); `verify_passphrase` uses `hmac.compare_digest` (assert not `==`).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement.** Add `pyotp>=2.9` to `pyproject.toml`/requirements; install into `.venv`.
- [ ] **Step 4: PASS.** **Step 5: Commit.**

---

### Task 6: login route — CSRF + session + rate-limit + cookie flags

**Files:** Create `src/arb_memory/mcp/login.py`; Test `tests/arb_memory/test_login_route.py`.

**Interfaces:**
- Consumes: Task 4 (`put/get_login_session`, `bump_fail`), Task 5 (`verify_two_factor`), Task 2 (`Settings`).
- Produces: `login_routes(provider) -> list[starlette.routing.Route]` exposing `GET /login` and `POST /login`. On 2FA success, marks the login_session `verified_at` and redirects to the connector `redirect_uri` with a one-use code bound to the session's `authorize_state`.

- [ ] **Step 1: Failing tests (Starlette TestClient)** — (a) `GET /login?session=...` returns 200 with a hidden `csrf_token`; (b) `POST /login` without the matching CSRF → 403; (c) wrong 2FA increments `fail_count`; after `>=5` fails the session is locked (429/403); (d) the `Set-Cookie` has `Secure; HttpOnly; SameSite=Lax`; (e) success redirects to the session's bound `redirect_uri` (NOT a request-supplied one).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — server-side session (random `session_id` cookie, opaque, looked up in `mcp_auth.login_sessions`); CSRF token compared with `hmac.compare_digest`; rate-limit on `fail_count`; the redirect target comes from `authorize_state.redirect_uri` (already allowlist-validated in Task 7), never from the POST body.
- [ ] **Step 4: PASS.** **Step 5: Commit.**

---

### Task 7: provider.authorize — allowlist-enforced, redirects to login (PHISHING GATE)

**Files:** Create `src/arb_memory/mcp/oauth.py` (`ArbMemoryOAuthProvider`); Test `tests/arb_memory/test_oauth_authorize.py`.

**Interfaces:**
- Consumes: Task 3 (`is_allowed_redirect`), Task 4 (login_session + client), Task 2.
- Produces: `ArbMemoryOAuthProvider` implementing the SDK `OAuthAuthorizationServerProvider`. `authorize(client, params) -> str` returns a redirect URL to `GET /login` after creating a pending `login_session` carrying `authorize_state` (client_id, validated redirect_uri, resource, code_challenge, scopes). **Raises/redirects-error if `is_allowed_redirect(params.redirect_uri)` is False.**

- [ ] **Step 1: Failing test — THE phishing defense**
```python
from mcp.server.auth.errors import ...  # pin the SDK's typed authorize error in T0; use it below

async def test_authorize_rejects_attacker_redirect_seeded_directly(provider, conn):
    # Seed the malicious client DIRECTLY (bypass DCR — DCR would reject it, which would
    # make this test pass without ever exercising authorize). This isolates the
    # authorize-time gate. (panel: codex+agy P0 — the DCR-registered setup was hollow.)
    oauth_store.put_client(conn, client_id="evil", redirect_uris=["https://attacker.com/cb"], metadata={})
    client = await provider.get_client("evil")
    params = AuthorizationParams(redirect_uri="https://attacker.com/cb", code_challenge="x", ...)
    with pytest.raises(AuthorizeError):          # SPECIFIC error, not bare Exception
        await provider.authorize(client, params)

async def test_authorize_rejects_when_no_redirect_param_falls_back_to_attacker_uri(provider, conn):
    # SDK returns redirect_uris[0] when no redirect_uri param is supplied (cold-Opus P1).
    oauth_store.put_client(conn, client_id="evil2", redirect_uris=["https://attacker.com/cb"], metadata={})
    client = await provider.get_client("evil2")
    params = AuthorizationParams(redirect_uri=None, code_challenge="x", ...)
    with pytest.raises(AuthorizeError):
        await provider.authorize(client, params)

async def test_authorize_allows_pinned_connector_and_redirects_to_login(provider, conn):
    oauth_store.put_client(conn, client_id="claude", redirect_uris=["https://claude.ai/api/mcp/auth_callback"], metadata={})
    client = await provider.get_client("claude")
    params = AuthorizationParams(redirect_uri="https://claude.ai/api/mcp/auth_callback", code_challenge="x", ...)
    url = await provider.authorize(client, params)
    assert "/login" in url
```
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — validate redirect (Task 3) FIRST; on fail raise the SDK auth error (no code issued, no redirect to attacker); else create login_session + return `{public_base_url}/login?session=<id>`.
- [ ] **Step 4: PASS.** **Step 5: Commit** `feat(arb-memory-mcp): authorize-time redirect allowlist (phishing gate)`.

---

### Task 8: provider — auth-code exchange (one-use + binding + audience-bound mint)

**Files:** Modify `oauth.py`; Test `tests/arb_memory/test_oauth_code_exchange.py`.

**Interfaces:**
- Produces: `load_authorization_code`, `exchange_authorization_code` — verify code one-use (Task 4 `consume_code`), verify it is bound to the presented `client_id`/`redirect_uri`/`resource` and the PKCE S256 `code_challenge` (recompute from verifier), then mint access+refresh tokens **bound to `resource`** (RFC 8707), stored hashed.

- [ ] **Step 1: Failing tests (adversarial binding battery)** — replay same code twice → 2nd rejected; wrong PKCE verifier → rejected; wrong `redirect_uri` → rejected; wrong `client_id` → rejected; wrong/missing `resource` → rejected; success → access token row has `resource` == our resource.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — S256 check `base64url(sha256(verifier)) == code_challenge`; all binding comparisons constant-time where secret-ish; mint via Task 4.
- [ ] **Step 4: PASS.** **Step 5: Commit.**

---

### Task 9: provider — refresh rotation, revoke, verify_token (audience check)

**Files:** Modify `oauth.py`; Test `tests/arb_memory/test_oauth_tokens.py`.

**Interfaces:**
- Produces: `load_refresh_token`, `exchange_refresh_token` (rotate: issue new, mark old `rotated_to`, **reused old → reject + revoke chain**), `revoke_token`, `load_access_token`/`verify_token` (hash lookup + expiry + **audience == our resource** else reject).

- [ ] **Step 1: Failing tests** — refresh once → new pair; reuse old refresh → rejected AND the rotated-to chain revoked; revoked access token → `verify_token` None; a token whose `resource` != ours → `verify_token` None (audience confusion).
- [ ] **Step 2: Run → FAIL. Step 3: Implement. Step 4: PASS. Step 5: Commit.**

---

### Task 10: DCR hardening — register_client + caps + GC

**Files:** Modify `oauth.py`; Test `tests/arb_memory/test_dcr.py`.

**Interfaces:**
- Produces: `get_client`, `register_client` — validate each `redirect_uri` via Task 3 at registration; enforce `dcr_global_cap` (reject over cap), `dcr_metadata_max_bytes` (reject oversized), and `gc_unused_clients` (drop clients never used past a TTL).

- [ ] **Step 1: Failing tests** — **register-then-fail-without-2FA:** register a client, drive authorize→login, attempt token without the 2FA secret → no token issued; registering `attacker.com` redirect → rejected at DCR; exceeding `dcr_global_cap` → rejected; oversized metadata → rejected.
- [ ] **Step 2: FAIL. Step 3: Implement. Step 4: PASS. Step 5: Commit.**

---

### Task 11: MCP server + read tools (read-only, no Valkey, bounded embed)

**Files:** Create `src/arb_memory/mcp/tools.py`, `src/arb_memory/mcp/server.py`; Modify `src/arb_memory/store.py` (+`recent_artefacts`); Test `tests/arb_memory/test_mcp_tools.py`, `tests/arb_memory/test_mcp_readonly_import.py`.

**Interfaces:**
- Consumes: `store.retrieve/fetch_artefact/recent_artefacts`, Task 2 settings, the provider (Tasks 7-10).
- Produces: `build_server(settings, provider) -> FastMCP` registering tools `memory_search(query,k)`, `memory_get(artefact_id,version)`, `memory_recent(limit)`. `store.recent_artefacts(conn, limit) -> list[dict]`.

- [ ] **Step 1: Failing tests** — (a) `memory_search` returns hits via `store.retrieve`; (b) **import guard:** `tests/.../test_mcp_readonly_import.py` asserts no module under `arb_memory.mcp` imports `redis`/`store.write_artefact_and_hints`/`store.upsert_*` (scan `sys.modules`/source AST); (c) unauthenticated tool call → 401 with `WWW-Authenticate` pointing at PRM; (d) query longer than `search_max_query_chars` → rejected before embed; (e) the per-token search rate-limit triggers after `search_rate_per_min`.
- [ ] **Step 2: FAIL. Step 3: Implement** — `build_server` wires `AuthSettings(issuer_url=public_base_url, resource_server_url=public_base_url, client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=["memory.read"], default_scopes=["memory.read"]), revocation_options=RevocationOptions(enabled=True), required_scopes=["memory.read"])`; tools open a `mcp_connect()` (arbmem_mcp role) connection; embed bounded.
- [ ] **Step 4: PASS. Step 5: Commit.**

---

### Task 12: proxy-trust metadata (PRM/ASM from PUBLIC_BASE_URL)

**Files:** Modify `server.py`; Test `tests/arb_memory/test_proxy_trust.py`.

**Interfaces:** Produces: well-known PRM/ASM responses whose `resource`/`authorization_servers`/`issuer` derive solely from `settings.public_base_url`.

- [ ] **Step 1: Failing test** — request `/.well-known/oauth-protected-resource` with hostile `Host: attacker.com` and `X-Forwarded-Host: attacker.com` → the returned `resource`/`authorization_servers` still equal `public_base_url`, not the hostile header.
- [ ] **Step 2: FAIL. Step 3: Implement** — ensure FastMCP/AuthSettings use the fixed issuer; if the SDK echoes `Host`, add a Starlette middleware that pins the base URL. **Step 4: PASS. Step 5: Commit.**

---

### Task 13: entrypoints + readiness (≠ liveness)

**Files:** Create `src/arb_memory/run.py`, `src/arb_memory/mcp/health.py`; Test `tests/arb_memory/test_run_entrypoints.py`, `tests/arb_memory/test_mcp_health.py`.

**Interfaces:** Produces: `python -m arb_memory <memory|audit|mcp>` (argparse dispatch to the Phase-1 `MemoryConsumer`, Phase-2 `AuditConsumer`, Phase-3 server); `readiness()` (mcp: a memory read answers AND `mcp_auth` reachable → returns `{ready, degraded}`; transient PG error → `degraded=True`, NOT an exception) and a separate `liveness()` (process up).

- [ ] **Step 1: Failing tests** — `run.main(["mcp"])` dispatches to the server builder (monkeypatched); `readiness()` returns `degraded=True` (not raise) when the PG read errors; `liveness()` stays True during a PG blip (anti-flap).
- [ ] **Step 2: FAIL. Step 3: Implement. Step 4: PASS. Step 5: Commit.**

---

### Task 14: containers — Dockerfile + compose + local overlay

**Files:** Create `deploy/Dockerfile`, `deploy/docker-compose.yml`, `deploy/docker-compose.local.yml`, `deploy/cloudflared/config.example.yml`; Test `tests/arb_memory/test_compose_shape.py`.

**Interfaces:** Produces: one image, three services (`mcp`,`memory`,`audit`), `restart: unless-stopped`; the `mcp` service env has **no Valkey URL** and uses the `arbmem_mcp` DSN; healthchecks call `readiness()`.

- [ ] **Step 1: Failing test** — parse `docker-compose.yml` (yaml): 3 services; `mcp` service env contains no `*REDIS*`/`*VALKEY*` key; all have `restart: unless-stopped`; `mcp` healthcheck references readiness.
- [ ] **Step 2: FAIL. Step 3: Implement** Dockerfile (python:3.14-slim, install pkg, `ENTRYPOINT ["python","-m","arb_memory"]`); compose (prod-shaped, env from go-live); local overlay adds pgvector + valkey for memory/audit only. **Step 4: PASS. Step 5: Commit.**

---

### Task 15: local compose e2e — the definition of done

**Files:** Create `tests/arb_memory/test_phase3_e2e.py` (marked `@pytest.mark.e2e`, needs the local stack); Modify `deploy/README.md`.

**Interfaces:** Consumes everything. A simulated MCP client drives the full flow.

- [ ] **Step 1: Write the e2e** — bring up the local stack (or start the server in-process against local PG+redis15); simulate a connector: DCR register (pinned redirect) → authorize → `/login` GET (CSRF) → POST (passphrase+TOTP) → receive code → token exchange (S256 + resource) → call `memory_search` with the bearer token → assert a hit; assert an unauthenticated call is 401; assert an `attacker.com` DCR client cannot complete authorize. Label connector-compat (real claude.ai/ChatGPT) a **pre-go-live canary** in a docstring + README.
- [ ] **Step 2: Run → FAIL. Step 3: make it pass against the local stack. Step 4: PASS. Step 5: Commit.**

---

### Task 16: go-live runbook (Mark's hands — documented, not automated)

**Files:** Modify `deploy/README.md`; Modify `CHANGELOG.md`.

- [ ] **Step 1:** Write the runbook: provision DO managed pgvector; apply `schema.sql` (creates `arbmem_mcp` + grants — **set its password on DO**); the one **DO SSL+pooled+grant connection test**; create the CF tunnel (`cloudflared` config from the example) + DNS; set secrets (`PUBLIC_BASE_URL`, login/TOTP, OpenAI, DSNs); provision the TOTP (QR) once; `compose up`; **the connector canary** (add the server in claude.ai and ChatGPT, complete one real OAuth+2FA, run one `memory_search`); verify a real **reboot** brings the trio back. Add the Phase 3 CHANGELOG entry (what + why).
- [ ] **Step 2: Commit** `docs(arb-memory-p3): go-live runbook + CHANGELOG`.

---

## Self-Review

**Spec coverage:** §3a read-only role → T1,T11; §4b allowlist → T3,T7; §2 SDK-vs-provider (audience/code/refresh) → T8,T9; §4a login route → T5,T6; §4d proxy-trust → T2,T12; §4c hashing/DCR → T4,T10; §5c readiness/embed → T11,T13; §5a containers → T14; e2e → T15; runbook/canary → T16. All v2 sections covered.
**Placeholder scan:** no TBD/"handle errors" — each task carries concrete test + impl code or exact DDL/signatures.
**Type consistency:** `is_allowed_redirect`, `hash_token`, `mcp_connect`, `ArbMemoryOAuthProvider`, `build_server`, `readiness`, `store.recent_artefacts` used consistently across tasks.
**Note:** GLM design-panel findings (pending) remediate into the nearest open task when they land (likely T3/T7 redirect tightness or T10 DCR).
