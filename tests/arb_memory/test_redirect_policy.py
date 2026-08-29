import pytest

from arb_memory.mcp.redirect_policy import OOB_REDIRECT_URI, connector_host_for_redirect_uris, is_allowed_redirect


@pytest.mark.parametrize(
    ("uri", "ok"),
    [
        ("https://claude.ai/api/mcp/auth_callback", True),
        ("https://chatgpt.com/connector_platform_oauth_redirect", True),
        ("https://chatgpt.com/connector/oauth/abc123", True),
        ("https://chatgpt.com/connector/oauth/", True),
        ("https://attacker.com/callback", False),
        ("http://claude.ai/api/mcp/auth_callback", False),
        ("https://claude.ai/evil", False),
        ("https://evil.chatgpt.com/connector/oauth/x", False),
        ("https://chatgpt.com.attacker.com/connector/oauth/x", False),
        ("https://chatgpt.com/connector/oauth/../../evil", False),
        ("https://chatgpt.com/connector/oauth/%2e%2e/evil", False),
        ("https://chatgpt.com/connector/oauth/%2E%2E/evil", False),
        ("https://chatgpt.com/connector/oauth/%2fevil", False),
        ("https://chatgpt.com/connector/oauth/%5cevil", False),
        ("https://chatgpt.com/connector/oauth/x?next=evil", False),
        ("https://chatgpt.com/connector/oauth/x#evil", False),
        ("https://user@claude.ai/api/mcp/auth_callback", False),
        ("https://claude.ai:8443/api/mcp/auth_callback", False),
        ("https://CLAUDE.AI/api/mcp/auth_callback", True),
        ("https://claude.ai/api/mcp/auth_callback/../evil", False),
        # --- RFC 8252 loopback native-client redirects (Codex, MCP Inspector, ...) ---
        # The real Codex CLI redirect: literal 127.0.0.1, ephemeral port, per-login random path.
        ("http://127.0.0.1:57944/callback/fXsTSGOZvP8x", True),
        ("http://127.0.0.1:1455/callback", True),
        ("http://127.0.0.1/callback", True),  # port optional
        ("http://[::1]:8080/callback", True),
        # localhost stays REJECTED (DNS-rebindable; RFC 8252 §8.3 prefers literal loopback IP)
        ("http://localhost:8080/callback", False),
        # not loopback: link-local / metadata SSRF target, 0.0.0.0, public IP, spoofed host
        ("http://169.254.169.254/callback", False),
        ("http://0.0.0.0:8080/callback", False),
        ("http://10.0.0.5:8080/callback", False),
        ("http://127.0.0.1.attacker.com:8080/callback", False),
        # loopback still gets the path-traversal + credential + query/fragment guards
        ("http://127.0.0.1:8080/callback/../evil", False),
        ("http://127.0.0.1:8080/callback%2e%2e/evil", False),
        ("http://user@127.0.0.1:8080/callback", False),
        ("http://127.0.0.1:8080/callback?next=evil", False),
        ("http://127.0.0.1:8080/callback#evil", False),
        # https to a non-allowlisted host (incl. loopback) is still rejected
        ("https://127.0.0.1:8080/callback", False),
        # --- Out-of-band (no loopback listener): code shown on the login-success page ---
        ("urn:ietf:wg:oauth:2.0:oob", True),
        ("urn:ietf:wg:oauth:2.0:oob:extra", False),  # exact match only, no prefix match
        ("URN:IETF:WG:OAUTH:2.0:OOB", False),  # exact match only, no case-folding for this sentinel
    ],
)
def test_redirect_allowlist(uri, ok):
    assert is_allowed_redirect(uri) is ok


def test_oob_redirect_uri_constant_matches_the_allowlisted_string():
    # Regression guard against the constant and the allowlist check drifting apart.
    assert is_allowed_redirect(OOB_REDIRECT_URI) is True


# Direct regression guard for the connector-host derivation added for ARB Messages' generalization
# (a low-maintenance allowlist needs a stable category, not a per-registration client_id).
@pytest.mark.parametrize(
    ("redirect_uris", "expected_host"),
    [
        (["https://claude.ai/api/mcp/auth_callback"], "claude.ai"),
        (["https://chatgpt.com/connector/oauth/abc123"], "chatgpt.com"),
        (["https://CLAUDE.AI/api/mcp/auth_callback"], "claude.ai"),  # case-insensitive, matches is_allowed_redirect
        (["http://127.0.0.1:57944/callback/fXsTSGOZvP8x"], "loopback"),
        (["http://[::1]:8080/callback"], "loopback"),
        (["https://attacker.com/callback"], None),  # never registered successfully, but defensive anyway
        ([], None),
        # A client that somehow ended up with multiple redirect_uris -- the first recognized one wins.
        (["https://attacker.com/callback", "https://claude.ai/api/mcp/auth_callback"], "claude.ai"),
    ],
)
def test_connector_host_for_redirect_uris(redirect_uris, expected_host):
    assert connector_host_for_redirect_uris(redirect_uris) == expected_host
