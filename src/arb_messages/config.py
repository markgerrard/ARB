from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class Settings:
    postgres_dsn: str
    # Values are CONNECTOR-HOST categories ("claude.ai", "chatgpt.com", "loopback" for native/CLI
    # clients like Codex), not per-registration OAuth client_ids. client_id is a fresh value
    # minted on every DCR re-authorization -- an allowlist keyed on it would need updating every
    # time a connector re-authorizes. connector_host is derived from the client's registered
    # redirect_uri (see arb_memory.mcp.redirect_policy.connector_host_for_redirect_uris), which
    # is a stable, cryptographically meaningful signal (an attacker can't redirect a real auth
    # code to their own server) rather than a self-reported one -- see door_tools.py's
    # _connector_host(). client_id itself remains the per-session identity used for
    # delivery/key-registration scoping (unchanged), since collapsing THAT to a shared category
    # would break isolation between concurrent sessions of the same connector.
    allowed_agents: frozenset[str]
    # Structural defense-in-depth (found by all three reviewers of the generalization pass):
    # once an agent passes the identity allowlist, capability is freeform text with no
    # structural check on it at all -- provider is the coarse-grained category (e.g.
    # "cloudflare", "azure", "digitalocean") an operator CAN allowlist without coupling to any
    # one provider's specific permission model. Denied before enqueueing, same layer as the
    # agent check -- Codex's own judgment on the actual freeform capability is still the real
    # backstop, this only bounds which CATEGORIES of request this deployment will broker at all.
    allowed_providers: frozenset[str]
    lease_seconds: int = 300
    messages_enabled: bool = True


_REQUIRED = (
    "ARB_MESSAGES_POSTGRES_DSN",
    "ARB_MESSAGES_ALLOWED_AGENTS",
    "ARB_MESSAGES_ALLOWED_PROVIDERS",
)


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    value = env.get(key)
    return int(value) if value else default


def _split_csv(value: str, *, lower: bool = False) -> frozenset[str]:
    items = []
    for raw in value.split(","):
        item = raw.strip()
        if item:
            items.append(item.lower() if lower else item)
    return frozenset(items)


def load_settings(env: Mapping[str, str]) -> Settings:
    missing = [key for key in _REQUIRED if not env.get(key)]
    if missing:
        raise ValueError(f"ARB Messages config missing: {', '.join(missing)}")

    allowed_agents = _split_csv(env["ARB_MESSAGES_ALLOWED_AGENTS"], lower=True)
    if not allowed_agents:
        raise ValueError("ARB_MESSAGES_ALLOWED_AGENTS must be non-empty")
    allowed_providers = _split_csv(env["ARB_MESSAGES_ALLOWED_PROVIDERS"], lower=True)
    if not allowed_providers:
        raise ValueError("ARB_MESSAGES_ALLOWED_PROVIDERS must be non-empty")

    return Settings(
        postgres_dsn=env["ARB_MESSAGES_POSTGRES_DSN"],
        allowed_agents=allowed_agents,
        allowed_providers=allowed_providers,
        lease_seconds=_int(env, "ARB_MESSAGES_LEASE_SECONDS", 300),
        messages_enabled=env.get("ARB_MESSAGES_ENABLED", "1") == "1",
    )
