from __future__ import annotations

from urllib.parse import unquote, urlsplit


_RULES = {
    "claude.ai": lambda path: path == "/api/mcp/auth_callback",
    "chatgpt.com": lambda path: (
        path == "/connector_platform_oauth_redirect"
        or path.startswith("/connector/oauth/")
    ),
}

# RFC 8252 §7.3/§8.3: native/CLI apps (Codex `codex mcp login`, MCP Inspector, etc.) receive the auth
# code on a LOOPBACK redirect — http to a literal loopback IP on an ephemeral port, with a client-chosen
# path. Safe on a public authorization server because the code can only be delivered to a listener on the
# requester's own machine, and we require PKCE. Restricted to literal loopback IPs only — "localhost" is
# deliberately excluded (DNS-rebindable to a non-loopback address). Observed: Codex uses
# http://127.0.0.1:<ephemeral>/callback/<random>.
_LOOPBACK_HOSTS = {"127.0.0.1", "::1"}

# Out-of-band redirect: for a client that can't run a loopback listener (or doesn't want to), the
# authorization code is displayed on the login-success page instead of delivered via redirect, and
# the human copies it into the client manually. The exact sentinel value (not a real URI scheme) is
# the historical convention used by Google's classic installed-app OOB flow — reused here as a
# recognizable, standards-precedented marker rather than inventing a new one. Security note: this
# is the pattern Google deprecated in 2022 for phishing resistance (a malicious page could harvest a
# code shown on a legitimate-looking prompt). Accepted here because every flow already requires a
# personal passphrase + TOTP regardless of redirect_uri (login.py) — a materially higher bar than
# the consumer-OAuth case that motivated Google's deprecation — but it is not zero risk, and this is
# not a substitute for that judgement call.
OOB_REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"


def _path_is_safe(raw_path: str) -> bool:
    # No percent-encoding or backslashes in the raw path, and no traversal segment after decoding.
    if "%" in raw_path or "\\" in raw_path:
        return False
    path = unquote(raw_path)
    if "\\" in path or ".." in path.split("/"):
        return False
    return True


def connector_host_for_redirect_uris(redirect_uris: list[str]) -> str | None:
    """Derive a stable connector-category identifier from a client's REGISTERED redirect_uris.

    Unlike client_id (a fresh value minted per DCR registration, churning on every
    re-authorization), the redirect host is stable across re-registrations of the "same"
    logical connector, and it's a cryptographically meaningful signal, not a self-reported one:
    completing an OAuth flow with redirect_uri=https://claude.ai/... requires the authorization
    code to be delivered to claude.ai's own server (an attacker registering a client with that
    same redirect_uri could never see the code themselves), and this MCP server's login flow
    additionally requires a personal passphrase + TOTP for every single flow regardless of which
    client initiated it. Used for a low-maintenance connector-category allowlist (e.g.
    "claude.ai,chatgpt.com") distinct from client_id (which stays the per-session identity used
    for delivery/key-registration scoping -- collapsing that to a shared category value would
    break isolation between concurrent sessions of the same connector, which register distinct
    keys and must not share one).
    """
    for uri in redirect_uris:
        try:
            parsed = urlsplit(uri)
        except ValueError:
            continue
        if parsed.scheme == "http" and parsed.hostname in _LOOPBACK_HOSTS:
            return "loopback"
        if parsed.scheme == "https" and parsed.hostname in _RULES:
            return parsed.hostname
    return None


def is_allowed_redirect(uri: str) -> bool:
    # Exact-string sentinel, checked before any URL parsing -- it isn't a URL in the http(s)/loopback
    # sense at all, and urlsplit's behaviour on a bare URN-shaped string isn't something to depend on.
    if uri == OOB_REDIRECT_URI:
        return True

    try:
        parsed = urlsplit(uri)
    except ValueError:
        return False

    try:
        port = parsed.port
    except ValueError:
        return False

    # Never accept embedded credentials, query, or fragment on a REGISTERED redirect URI.
    if parsed.username or parsed.password:
        return False
    if parsed.query or parsed.fragment:
        return False

    host = parsed.hostname or ""

    # Loopback native-client redirect: http + literal loopback IP + any (ephemeral) port + safe path.
    if parsed.scheme == "http" and host in _LOOPBACK_HOSTS:
        return _path_is_safe(parsed.path)

    # Hosted-connector redirect: https, no port, known host, and the host's exact path rule.
    if parsed.scheme == "https" and port is None:
        rule = _RULES.get(host)
        if rule is None:
            return False
        if not _path_is_safe(parsed.path):
            return False
        return rule(unquote(parsed.path))

    return False
