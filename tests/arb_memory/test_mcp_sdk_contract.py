import importlib.metadata
import inspect

from mcp.server.auth.provider import (
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RegistrationError,
    TokenError,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull


def test_mcp_sdk_128_auth_contract_objects_instantiate():
    assert importlib.metadata.version("mcp").startswith("1.28.")

    client_registration = ClientRegistrationOptions(
        enabled=True,
        valid_scopes=["memory.read"],
        default_scopes=["memory.read"],
    )
    auth_settings = AuthSettings(
        issuer_url="https://mem.example.com",
        resource_server_url="https://mem.example.com",
        client_registration_options=client_registration,
        required_scopes=["memory.read"],
    )
    assert auth_settings.client_registration_options is client_registration

    client = OAuthClientInformationFull(
        client_id="client-1",
        redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope="memory.read",
    )
    assert str(client.redirect_uris[0]) == "https://claude.ai/api/mcp/auth_callback"

    params = AuthorizationParams(
        state="state-1",
        scopes=["memory.read"],
        code_challenge="challenge-1",
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
        redirect_uri_provided_explicitly=True,
        resource="https://mem.example.com",
    )
    assert params.code_challenge == "challenge-1"

    provider_methods = {
        name for name, value in inspect.getmembers(OAuthAuthorizationServerProvider)
        if inspect.isfunction(value)
    }
    assert {
        "authorize",
        "exchange_authorization_code",
        "load_access_token",
        "exchange_refresh_token",
    }.issubset(provider_methods)
    assert AuthorizeError("access_denied").error == "access_denied"
    assert TokenError("invalid_grant").error == "invalid_grant"
    assert RegistrationError("invalid_redirect_uri").error == "invalid_redirect_uri"
