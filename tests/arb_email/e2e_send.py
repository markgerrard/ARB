from __future__ import annotations

import os
from pathlib import Path

import anyio

from arb_email.client import EmailClient
from arb_email.config import load_settings
from arb_email.mcp.door_tools import EmailTools


def _load_env_file(path: Path) -> dict[str, str]:
    env = dict(os.environ)
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        env[key] = value.strip().strip("'\"")
    return env


class _NoNetworkClient:
    def send(self, *_args, **_kwargs):
        raise AssertionError("non-allowlisted recipient should reject before send")


def _settings_for_validation(env: dict[str, str]):
    validation_env = {
        **env,
        "ARB_EMAIL_POSTMARK_TOKEN": env.get("ARB_EMAIL_POSTMARK_TOKEN", "validation-token"),
        "ARB_EMAIL_SEND_ENABLED": env.get("ARB_EMAIL_SEND_ENABLED", "1"),
    }
    return load_settings(validation_env)


def _assert_non_allowlisted_rejects_before_network(env: dict[str, str]) -> None:
    settings = _settings_for_validation(env)
    events = []
    tools = EmailTools(
        _NoNetworkClient(),
        settings,
        require_scope=lambda _scope: None,
        actor=lambda: "e2e-validation",
        audit_sink=events.append,
    )
    async def call():
        return await tools.email_send(
            "E2E validation",
            text_body="x",
            to="nobody@example.com",
        )

    try:
        anyio.run(call)
    except ValueError:
        pass
    else:
        raise AssertionError("non-allowlisted recipient unexpectedly allowed")
    assert events and events[0]["op"] == "email_send_denied"


def main() -> None:
    env = _load_env_file(Path("envs/arb-email.env"))
    _assert_non_allowlisted_rejects_before_network(env)
    if env.get("ARB_EMAIL_E2E") != "1":
        print("ARB_EMAIL_E2E not set; real Postmark send skipped")
        return

    settings = load_settings(env)
    client = EmailClient(settings)
    tools = EmailTools(
        client,
        settings,
        require_scope=lambda _scope: None,
        actor=lambda: "e2e",
    )

    async def send_real():
        return await tools.email_send(
            "ARB Email E2E",
            text_body="ARB Email E2E validation",
            to=settings.default_to,
        )

    out = anyio.run(send_real)
    assert out["sent"] is True
    assert out["message_id"]
    print("ARB Email E2E send ok")


if __name__ == "__main__":
    main()
