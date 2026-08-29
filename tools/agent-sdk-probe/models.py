"""Model matrix for the probe. Secret-free: keys read from env by name."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


class MissingKeyError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelSpec:
    name: str
    base_url: str
    # model_id is passed directly as the requested model. Leave "" for providers
    # that route via Claude tier-lane mapping instead (extra_env sets
    # ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL and the SDK uses its default
    # model, which the provider maps) — e.g. z.ai/GLM.
    model_id: str
    key_env: str
    auth_style: str = "x-api-key"
    # Extra child-env vars (k,v) merged into the spike/mutation subprocess env —
    # a hashable tuple so ModelSpec stays a frozen dataclass.
    extra_env: tuple[tuple[str, str], ...] = ()


# Endpoints resolved from pi's provider catalog + vendor Claude-Code docs (2026-06-18).
MODELS: list[ModelSpec] = [
    ModelSpec("minimax-m3", "https://api.minimax.io/anthropic", "MiniMax-M3", "AGENT_SDK_MINIMAX_KEY"),
    ModelSpec("kimi", "https://api.kimi.com/coding/", "kimi-for-coding", "AGENT_SDK_KIMI_KEY"),
    # GLM/z.ai: passing model="glm-5.2" directly is the WRONG shape (it 529-loops);
    # z.ai expects Claude tier-lane mapping (the working Claude Code config). So we
    # use the SDK default model and map the lanes to GLM (opus/sonnet -> glm-5.2[1m],
    # haiku -> glm-4.5-air for cheap background calls). auth via ANTHROPIC_AUTH_TOKEN.
    ModelSpec(
        "glm-5.2",
        "https://api.z.ai/api/anthropic",
        "",  # use SDK default model; lanes below route it to GLM
        "AGENT_SDK_GLM_KEY",
        "auth-token",
        (
            ("ANTHROPIC_DEFAULT_OPUS_MODEL", "glm-5.2[1m]"),
            ("ANTHROPIC_DEFAULT_SONNET_MODEL", "glm-5.2[1m]"),
            ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "glm-4.5-air"),
            ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "1000000"),
        ),
    ),
]


def load_key(spec: ModelSpec) -> str:
    val = os.environ.get(spec.key_env)
    if not val:
        raise MissingKeyError(f"env var {spec.key_env} is unset; source envs/agent-sdk-models-dev.env")
    return val
