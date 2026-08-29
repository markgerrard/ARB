from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_compose_shape_services_restartable_mcp_no_redis_cloudflared_egress_only():
    compose = yaml.safe_load((REPO_ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8"))

    services = compose["services"]
    # The public MCP door delegates writes to an internal writer; cloudflared remains the sole ingress.
    assert set(services) == {
        "memory",
        "audit",
        "audit-close-consumer",
        "eval",
        "transcript",
        "mcp",
        "cloudflared",
        "writer",
        "visibility",
        # Static-file installer host for ARB codex builds (0a8ff218): no ports,
        # reachable only via the tunnel like every other service.
        "arb-tools-static",
        # Served-hint record consumer (ARB-B21): drains arbmem:hint-reads into
        # Postgres under the shared eval-consumer role.
        "hint-reads",
    }
    for service in services.values():
        assert service["restart"] == "unless-stopped"

    # The MCP door never talks to the bus (read-only OAuth surface) — no Redis/Valkey env may leak in.
    mcp_env = services["mcp"].get("environment", {})
    if isinstance(mcp_env, list):
        keys = {item.split("=", 1)[0] for item in mcp_env}
    else:
        keys = set(mcp_env)
    assert not any("REDIS" in key or "VALKEY" in key for key in keys)
    healthcheck = " ".join(services["mcp"]["healthcheck"]["test"])
    assert "readiness" in healthcheck

    # cloudflared is egress-only and mcp publishes no host port — the tunnel is the sole ingress
    # (security review). A stray `ports:` on either would expose the origin directly, bypassing the edge.
    assert "ports" not in services["cloudflared"]
    assert "ports" not in services["mcp"]


def test_compose_journey_snapshot_volume_mounted_with_modes_and_env():
    compose = yaml.safe_load((REPO_ROOT / "deploy" / "docker-compose.yml").read_text(encoding="utf-8"))

    assert "arb_journey" in compose.get("volumes", {})
    services = compose["services"]
    memory = services["memory"]
    visibility = services["visibility"]
    assert "ARB_JOURNEY_SNAPSHOT_DIR" in memory["environment"]
    assert "ARB_JOURNEY_SNAPSHOT_DIR" in visibility["environment"]
    assert memory["environment"]["ARB_JOURNEY_SNAPSHOT_DIR"] == "/journey-snapshot"
    assert visibility["environment"]["ARB_JOURNEY_SNAPSHOT_DIR"] == "/journey-snapshot"
    assert "arb_journey:/journey-snapshot:rw" in memory["volumes"]
    assert "arb_journey:/journey-snapshot:ro" in visibility["volumes"]
