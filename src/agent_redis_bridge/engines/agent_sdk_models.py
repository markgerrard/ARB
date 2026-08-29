from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ._stdio import is_bus_credential, is_gate_daemon_credential


SENSITIVE_PREFIXES = ("ANTHROPIC_", "AGENT_SDK_")
SENSITIVE_ENV_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "AGENT_SDK_MINIMAX_KEY",
        "AGENT_SDK_KIMI_KEY",
        "AGENT_SDK_GLM_KEY",
    }
)


@dataclass(frozen=True)
class ModelSpec:
    name: str
    slug: str
    base_url: str
    model_id: str
    key_env: str
    auth_style: str = "x-api-key"
    lane_env: dict[str, str] = field(default_factory=dict)
    subscription: bool = False
    reviewer: bool = False


MODELS: dict[str, ModelSpec] = {
    "minimax-m3": ModelSpec(
        "minimax-m3",
        "m3",
        "https://api.minimax.io/anthropic",
        "MiniMax-M3",
        "AGENT_SDK_MINIMAX_KEY",
    ),
    "kimi": ModelSpec(
        "kimi",
        "kimi",
        "https://api.kimi.com/coding/",
        "kimi-for-coding",
        "AGENT_SDK_KIMI_KEY",
    ),
    "glm-5.2": ModelSpec(
        "glm-5.2",
        "glm",
        "https://api.z.ai/api/anthropic",
        "",
        "AGENT_SDK_GLM_KEY",
        "auth-token",
        {
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.2",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5.2",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "glm-4.5-air",
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1000000",
        },
    ),
    "opus-4.8": ModelSpec(
        "opus-4.8",
        "opus48",
        "",
        "claude-opus-4-8",
        "CLAUDE_CODE_OAUTH_TOKEN",
        subscription=True,
        reviewer=True,
    ),
    "opus-5": ModelSpec(
        "opus-5",
        "opus5",
        "",
        "claude-opus-5",
        "CLAUDE_CODE_OAUTH_TOKEN",
        subscription=True,
        reviewer=True,
    ),
    # Fable 5 (subscription) — authoring / deep-reasoning seat. Not a certifier
    # (reviewer=False) so it can run concurrent with the opus certifier slot.
    "fable-5": ModelSpec(
        "fable-5",
        "fable5",
        "",
        "claude-fable-5",
        "CLAUDE_CODE_OAUTH_TOKEN",
        subscription=True,
    ),
    "sonnet-5": ModelSpec(
        "sonnet-5",
        "sonnet5",
        "",
        "claude-sonnet-5",
        "CLAUDE_CODE_OAUTH_TOKEN",
        subscription=True,
    ),
    "haiku-4.5": ModelSpec(
        "haiku-4.5",
        "haiku45",
        "",
        "claude-haiku-4-5-20251001",
        "CLAUDE_CODE_OAUTH_TOKEN",
        subscription=True,
    ),
}


def resolve(name: str) -> ModelSpec:
    return MODELS[name]


def auth_var(auth_style: str) -> str:
    return "ANTHROPIC_AUTH_TOKEN" if auth_style == "auth-token" else "ANTHROPIC_API_KEY"


def isolated_env(spec: ModelSpec, key: str, *, base: dict[str, str], config_dir: str | Path) -> dict[str, str]:
    # The SDK merges parent os.environ before options.env, so sensitive inherited
    # keys must be explicitly overwritten rather than omitted from this overlay.
    # Bus credentials get the same blank-out: the agent-sdk analogue of
    # scrubbed_child_env for Popen engines.
    env = {
        name: ""
        for name in base
        if name.startswith(SENSITIVE_PREFIXES)
        or is_bus_credential(name)
        or is_gate_daemon_credential(name)
    }
    env["ANTHROPIC_BASE_URL"] = spec.base_url
    env[auth_var(spec.auth_style)] = key
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env.update(spec.lane_env)
    for name, value in base.items():
        if not name.startswith(SENSITIVE_PREFIXES) and name not in env:
            env[name] = value
    return env


def subscription_env(*, base: dict[str, str], config_dir: str | Path | None = None) -> dict[str, str]:
    # The SDK merges parent os.environ before options.env; subscription launches
    # must therefore overwrite every provider/shadow key by prefix, not omit it.
    # Bus and gate-daemon credentials are blanked for the same merge reason.
    env = {
        name: ""
        for name in base
        if name.startswith(SENSITIVE_PREFIXES)
        or is_bus_credential(name)
        or is_gate_daemon_credential(name)
    }
    for name, value in base.items():
        if not name.startswith(SENSITIVE_PREFIXES) and name not in env:
            env[name] = value
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return env
