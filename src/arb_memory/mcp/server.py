from __future__ import annotations

import json
import logging
import os
from urllib.parse import parse_qs, urlencode, urlsplit

from arb_files.mcp.door_wire import register_file_tools
from arb_email.mcp.door_wire import register_email_tool
from arb_messages.mcp.door_wire import register_messages_tools
from arb_messages.mcp.fulfillment_wire import register_fulfillment_tools

from arb_memory.mcp import oauth_store

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from arb_memory.mcp.config import Settings, load_settings
from arb_memory.mcp.login import login_routes
from arb_memory.mcp.oauth import ArbMemoryOAuthProvider
from arb_memory.mcp.tools import MemoryTools

_trace_logger = logging.getLogger("arb_memory.mcp.trace")


class _RequestTraceASGI:
    """Pure-ASGI request tracer for connector debugging — gated by ARB_MEMORY_MCP_TRACE (off by default).

    Logs one structured line per request: method, path, status, and the headers that decide MCP connector
    behaviour — whether a bearer reached the transport, the Accept/Content-Type negotiation, the
    Mcp-Session-Id / Mcp-Protocol-Version lifecycle, UA, and CF-Ray — plus the response content-type and
    session id. NEVER logs the Authorization value or any token (presence-only). Pure ASGI so it does not
    buffer the response body (safe for SSE streams). Recommended by the Phase 3 connector-canary panel to
    turn guessing into observation of what claude.ai actually sends.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not os.environ.get("ARB_MEMORY_MCP_TRACE"):
            return await self.app(scope, receive, send)
        req = {k.decode().lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        captured = {}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                captured["status"] = message["status"]
                captured["headers"] = {
                    k.decode().lower(): v.decode("latin-1") for k, v in message.get("headers", [])
                }
            await send(message)

        await self.app(scope, receive, send_wrapper)
        resp = captured.get("headers", {})
        _trace_logger.warning(
            "MCPTRACE %s %s -> %s | auth=%s accept=%r req_ct=%r sid=%r mpv=%r ua=%r cf=%s "
            "|| resp_ct=%r resp_sid=%r www_auth=%s",
            scope.get("method"), scope.get("path"), captured.get("status"),
            "yes" if req.get("authorization") else "no",
            req.get("accept"), req.get("content-type"),
            req.get("mcp-session-id"), req.get("mcp-protocol-version"),
            (req.get("user-agent") or "")[:60], req.get("cf-ray", ""),
            resp.get("content-type"), resp.get("mcp-session-id"),
            "yes" if resp.get("www-authenticate") else "no",
        )


class _PinnedBaseURLMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, public_base_url: str):
        super().__init__(app)
        self.public_base_url = public_base_url.rstrip("/")

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if not request.url.path.startswith("/.well-known/oauth-"):
            return response
        content_type = response.headers.get("content-type", "")
        if "application/json" not in content_type:
            return response
        body = b"".join([chunk async for chunk in response.body_iterator])
        payload = json.loads(body)
        payload = self._pin_base_url(payload)
        # Advertise public-client auth ("none"). Our DCR registers claude.ai as a PUBLIC client
        # (token_endpoint_auth_method="none", no secret), but the SDK hardcodes the authorization-server
        # metadata's token_endpoint_auth_methods_supported to secret-based methods only. A strict OAuth
        # client can register, obtain a token, then refuse to proceed because the server's metadata
        # claims it doesn't support the client's own ("none") auth method — the exact post-/token abort
        # our connector canary hit. A known-working public MCP connector advertises "none".
        if isinstance(payload, dict) and isinstance(payload.get("token_endpoint_auth_methods_supported"), list):
            methods = payload["token_endpoint_auth_methods_supported"]
            if "none" not in methods:
                payload["token_endpoint_auth_methods_supported"] = methods + ["none"]
        headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in {"content-length", "content-type"}
        }
        return JSONResponse(payload, status_code=response.status_code, headers=headers)

    def _pin_base_url(self, value):
        if isinstance(value, str):
            return self.public_base_url if value == f"{self.public_base_url}/" else value
        if isinstance(value, list):
            return [self._pin_base_url(item) for item in value]
        if isinstance(value, dict):
            return {key: self._pin_base_url(item) for key, item in value.items()}
        return value


class _TokenResourceMiddleware:
    def __init__(self, app, *, public_base_url: str, conn_factory=None):
        self.app = app
        self.public_base_url = public_base_url.rstrip("/")
        self.conn_factory = conn_factory

    def _ensure_refresh_client_id(self, form, body):
        # The MCP SDK's client-auth requires a resolvable client_id in the /token form BEFORE the
        # refresh grant runs; a public connector (auth_method=none) that omits client_id on refresh,
        # or echoes a pruned one, gets 401'd and is forced to fully re-authorize. The refresh token
        # already identifies its client in our store, so inject that client_id when the form's is
        # absent or unresolvable. (Resolving a client from a presented *active* refresh token is
        # OAuth-correct for public clients; rotation + reuse-revocation in exchange_refresh_token
        # still apply, so this is not an auth bypass.)
        if self.conn_factory is None:
            return body
        refresh_token = (form.get("refresh_token") or [""])[0]
        if not refresh_token:
            return body
        client_id = (form.get("client_id") or [""])[0]
        conn = self.conn_factory()
        if client_id and oauth_store.get_client(conn, client_id) is not None:
            return body  # present and resolvable — respect the client's stated identity
        row = oauth_store.get_refresh_token(conn, refresh_token)
        if row is None:
            return body  # unknown/inactive token — let the SDK reject it (400 invalid_grant)
        # Inject ONLY for a public (no-secret) client. A confidential client must authenticate the
        # normal way (client_id + secret); never auto-resolve its identity from a bearer refresh token,
        # or a stolen refresh token alone could stand in for client credentials. (DCR pins every client
        # to auth_method=none today, so this is defense-in-depth against a future confidential client.)
        bound_client = oauth_store.get_client(conn, row["client_id"])
        if bound_client is None or bound_client.get("client_secret_hash") is not None:
            return body
        new_form = {key: list(values) for key, values in form.items()}
        new_form["client_id"] = [row["client_id"]]
        return urlencode(new_form, doseq=True).encode("utf-8")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") != "/token" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        body = b""
        more_body = True
        while more_body:
            message = await receive()
            body += message.get("body", b"")
            more_body = message.get("more_body", False)

        form = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        grant_type = (form.get("grant_type") or [""])[0]
        resource = (form.get("resource") or [""])[0].rstrip("/")
        if grant_type == "refresh_token":
            body = self._ensure_refresh_client_id(form, body)
        if grant_type == "authorization_code" and resource != self.public_base_url:
            response = JSONResponse(
                {
                    "error": "invalid_target",
                    "error_description": "token request resource must match this resource server",
                },
                status_code=400,
                headers={
                    "Cache-Control": "no-store",
                    "Pragma": "no-cache",
                },
            )
            await response(scope, receive, send)
            return

        sent = False

        async def replay_receive():
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay_receive, send)


class PinnedBaseFastMCP(FastMCP):
    def __init__(self, *args, public_base_url: str, conn_factory=None, **kwargs):
        self._public_base_url = public_base_url.rstrip("/")
        self._conn_factory = conn_factory
        super().__init__(*args, **kwargs)

    def _advertise_resource_scopes(self, app):
        """Advertise the full valid_scopes in the protected-resource metadata (RFC 9728).

        The SDK sets the protected-resource scopes_supported = required_scopes, which we keep minimal
        at memory.read (so reads aren't forced to carry write; write is enforced per-tool). But
        RFC-9728 clients (claude.ai) request exactly the scopes the *resource* advertises — so without
        memory.write here, those clients never request it and their tokens are read-only ("memory.write
        scope required" at the tool gate). Re-advertise valid_scopes at the resource; required_scopes
        (the transport gate) stays read.
        """
        auth = getattr(self.settings, "auth", None)
        if auth is None:
            return
        cro = getattr(auth, "client_registration_options", None)
        full = (cro.valid_scopes if cro else None) or auth.required_scopes
        if not full:
            return
        from mcp.server.auth.routes import create_protected_resource_routes

        new_routes = create_protected_resource_routes(
            resource_url=auth.resource_server_url,
            authorization_servers=[auth.issuer_url],
            scopes_supported=list(full),
        )
        new_paths = {r.path for r in new_routes}
        app.router.routes[:] = [
            r for r in app.router.routes if getattr(r, "path", None) not in new_paths
        ]
        app.router.routes.extend(new_routes)

    def streamable_http_app(self):
        app = super().streamable_http_app()
        self._advertise_resource_scopes(app)
        app.add_middleware(_PinnedBaseURLMiddleware, public_base_url=self._public_base_url)
        app.add_middleware(
            _TokenResourceMiddleware,
            public_base_url=self._public_base_url,
            conn_factory=self._conn_factory,
        )
        # Outermost (added last) so it observes every request/response, incl. those short-circuited by
        # auth/transport-security. No-op unless ARB_MEMORY_MCP_TRACE is set.
        app.add_middleware(_RequestTraceASGI)
        return app

    def sse_app(self, mount_path: str | None = None):
        # FastMCP.run_sse_async calls self.sse_app(mount_path) positionally — the override MUST accept
        # and forward it, or the server crash-loops at startup (TypeError) and cloudflared 502s.
        app = super().sse_app(mount_path)
        self._advertise_resource_scopes(app)
        app.add_middleware(_PinnedBaseURLMiddleware, public_base_url=self._public_base_url)
        app.add_middleware(
            _TokenResourceMiddleware,
            public_base_url=self._public_base_url,
            conn_factory=self._conn_factory,
        )
        app.add_middleware(_RequestTraceASGI)
        return app


def build_server(
    *,
    settings: Settings | None = None,
    provider: ArbMemoryOAuthProvider | None = None,
    conn_factory=None,
    embed=None,
    http_client=None,
) -> FastMCP:
    settings = settings or load_settings()
    provider = provider or ArbMemoryOAuthProvider(settings=settings, conn_factory=conn_factory)
    if http_client is None:
        import httpx

        http_client = httpx.AsyncClient(timeout=10.0)
    writer_url = os.environ.get("ARB_MEMORY_MCP_WRITER_URL")
    writer_token = os.environ.get("ARB_MEMORY_WRITER_TOKEN")
    tools = MemoryTools(
        settings,
        conn_factory=conn_factory,
        embed=embed,
        writer_url=writer_url,
        writer_token=writer_token,
        http_client=http_client,
    )
    # The token middleware needs a conn_factory to resolve a public client from its refresh token.
    # run_mcp() passes only `provider` (which carries its own conn_factory), not `conn_factory`, so
    # derive from the provider when not given explicitly — otherwise the middleware no-ops in prod
    # (dormant fix: tests inject conn_factory directly and would never catch it).
    middleware_conn_factory = conn_factory or getattr(provider, "conn_factory", None)
    # DNS-rebinding protection: the SDK only auto-allows the Host when bound to localhost. We bind
    # 0.0.0.0 inside the container (so cloudflared can reach mcp:8000), so the SDK leaves
    # transport_security=None -> the streamable-HTTP transport rejects the real public Host header
    # with 421 "Invalid Host header", which a connector surfaces as a generic "authorization failed"
    # AFTER a fully successful OAuth round-trip. Allow the public host (cloudflared preserves it
    # verbatim; verified live) + a port-wildcard for robustness. Origin is unset by server-side
    # connectors and absent-Origin passes, so allowed_origins stays empty. (ai-brain hit this exact
    # 421 and fixed it the same way; Phase 3 connector canary.)
    public_host = urlsplit(settings.public_base_url).netloc
    server = PinnedBaseFastMCP(
        "arb-memory",
        public_base_url=settings.public_base_url,
        conn_factory=middleware_conn_factory,
        # Serve the streamable-HTTP transport at the ROOT, not /mcp. The protected-resource metadata
        # advertises `resource: <public_base_url>` (the bare origin), and per the MCP auth spec a client
        # uses that advertised resource URL as the server endpoint — so claude.ai POSTs to the origin,
        # which 404s if the transport is at /mcp. Aligning the transport to the advertised resource is
        # the fix: the connector URL is just the origin, no path. (Phase 3 connector canary: after OAuth
        # succeeded, the client still failed because it POSTed `/` -> 404, never /mcp.)
        streamable_http_path="/",
        # STATEFUL by design (SDK default — stateless_http/json_response intentionally NOT set). We tried
        # stateless+json as a connector-canary follow-up (hypothesis: it would smooth ChatGPT's
        # distributed backend, whose cross-region requests caused a few interspersed 400s). EMPIRICALLY
        # WRONG and reverted: in stateless mode the server returns no Mcp-Session-Id (resp_sid=None), and
        # ChatGPT's connector REQUIRES the session id — its requests still returned 200/202 but it could
        # no longer maintain the connection and broke. The 400s under stateful were benign self-healing
        # cross-region races, not a sign ChatGPT wanted stateless. All three clients work stateful; do not
        # re-flip without a full three-connector re-verify (ChatGPT is the canary that fails).
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[public_host, f"{public_host}:*"],
        ),
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=settings.public_base_url,
            resource_server_url=settings.public_base_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=["memory.read", "memory.write", "files.read", "files.write", "email.send", "messages.request", "messages.fulfill"],
                # email.send/messages.fulfill are in default_scopes too: claude.ai auto-requests
                # advertised scopes but ChatGPT only requests its DCR-registered (default) set, so
                # a new scope must be a default to reach ChatGPT (see memory
                # chatgpt-connector-scope-grant). A plain reconnect is insufficient for
                # ChatGPT either way -- it needs a full disconnect + fresh DCR registration to pick
                # up a newly-added default.
                #
                # messages.fulfill was originally kept OUT of default_scopes (it's an operator-only
                # capability -- claim ANY pending request in the whole queue, deliver a sealed
                # reply that looks legitimate to whoever is polling, deny/fail arbitrary rows --
                # unlike messages.request, which is scoped to the caller's own identity). That
                # protected against auto-granting it to every connecting client, but it ALSO means
                # ChatGPT (which only ever picks up default_scopes, never requests a
                # valid-but-not-default scope explicitly like claude.ai does) could never receive
                # it through ordinary reconnection -- the only path left was an out-of-band grant
                # via oauth_store.put_client. Operator decision (2026-07-02, explicit, after this
                # tradeoff was surfaced): default-grant it instead, judged acceptable for this
                # single-operator deployment where every token issuance already requires a
                # personal passphrase + TOTP regardless of which client requests it. Known
                # consequence, not an oversight: every future claude.ai session gets this scope
                # too (claude.ai requests every advertised scope), not just the intended Codex/
                # ChatGPT operator client -- a compromised/misbehaving session of any kind
                # inherits it.
                default_scopes=["memory.read", "memory.write", "files.read", "files.write", "email.send", "messages.request", "messages.fulfill"],
            ),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=["memory.read"],
        ),
    )

    async def memory_search(query: str, k: int = 8) -> list[dict]:
        """Semantically search stored memories and documents. Returns up to `k` ranked hits, each
        with the matched memory text and its linked document (artefact), if any."""
        return await tools.memory_search(query, k)

    async def memory_get(artefact_id: str, version: int) -> dict | None:
        """Fetch a stored document (artefact) verbatim by its `artefact_id` and `version`.
        `content_hash` is not a digest of `content` alone — it is
        `sha256(b"arbmem:artefact:v1\\0" + content_mime + b"\\0" + kind + b"\\0" + payload)` where `kind` is
        `b"text"` with `payload` = the UTF-8 bytes of `content`, or `b"binary"` with `payload` = the raw bytes,
        and each `\\0` is one NUL byte."""
        return await tools.memory_get(artefact_id, version)

    async def memory_recent(limit: int = 10) -> list[dict]:
        """List the most recently stored documents (artefacts), newest first."""
        return await tools.memory_recent(limit)

    async def memory_related(
        artefact_id: str, version: int | None = None, k: int = 5, threshold: float = 0.35
    ) -> list[dict]:
        """Artefacts similar to `artefact_id` (pgvector min-pairwise hint distance),
        nearest-first. Default reads the latest version's live hints; passing an explicit
        `version` switches to the as-written view of that version's hints (including
        hints retired by later versions)."""
        return await tools.memory_related(artefact_id, version, k, threshold)

    async def memory_references(artefact_id: str, version: int | None = None) -> dict:
        """Explicit citations, both directions: `references` = ids this artefact's body
        cites (the given version's body; latest if omitted); `referenced_by` = latest
        artefacts citing this id. `version` affects ONLY the outgoing direction —
        backlinks always scan the latest corpus."""
        return await tools.memory_references(artefact_id, version)

    async def memory_store(
        content: str,
        artefact_id: str | None = None,
        mime: str = "text/plain",
        await_result: bool = False,
        expected_version: int | None = None,
    ) -> dict:
        """Store a document (artefact); returns its `artefact_id` and `version`. The document is
        auto-indexed, so it is also findable via memory_search. Omit `artefact_id` for a stable
        content-derived id; pass an explicit `artefact_id` to add a new version of an existing
        document. `mime`: text/plain, text/markdown, or application/json.
        When `expected_version` is set, `await_result` is required and the write is refused if
        the head is not that version (optimistic concurrency). The stored document's
        `content_hash` (returned by a subsequent `memory_get`) is
        `sha256(b"arbmem:artefact:v1\\0" + mime + b"\\0" + b"text" + b"\\0" + content.encode("utf-8"))` for text
        writes (`\\0` = one NUL byte), and an omitted `artefact_id` is derived as `art-` plus that hash's first 16
        hex characters."""
        return await tools.memory_store(
            content,
            artefact_id=artefact_id,
            mime=mime,
            await_result=await_result,
            expected_version=expected_version,
        )

    async def memory_remember(
        text: str,
        tags: list[str] | None = None,
        artefact_id: str | None = None,
        artefact_version: int | None = None,
        await_result: bool = False,
    ) -> dict:
        """Store a searchable memory (a short note). For a free-standing note, pass neither
        `artefact_id` nor `artefact_version`. To anchor the note to a stored document, pass BOTH
        `artefact_id` AND `artefact_version` — supplying only one is rejected, and the referenced
        document must already exist. Optional `tags` are stored as metadata."""
        return await tools.memory_remember(
            text,
            tags=tags,
            artefact_id=artefact_id,
            artefact_version=artefact_version,
            await_result=await_result,
        )

    server.add_tool(memory_search, name="memory_search")
    server.add_tool(memory_get, name="memory_get")
    server.add_tool(memory_recent, name="memory_recent")
    server.add_tool(memory_related, name="memory_related")
    server.add_tool(memory_references, name="memory_references")
    server.add_tool(memory_store, name="memory_store")
    server.add_tool(memory_remember, name="memory_remember")
    register_file_tools(server, os.environ)
    register_email_tool(server, os.environ)
    register_messages_tools(server, os.environ)
    register_fulfillment_tools(server, os.environ)
    server._custom_starlette_routes.extend(
        login_routes(provider, settings=settings, conn_factory=conn_factory)
    )
    return server
