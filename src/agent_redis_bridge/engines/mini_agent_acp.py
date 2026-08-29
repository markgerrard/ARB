"""Mini-Agent ACP engine — `mini-agent-acp` over stdio (JSON-RPC v1).

MiniMax's official agent (https://github.com/MiniMax-AI/Mini-Agent),
installed via `uv tool install git+https://github.com/MiniMax-AI/Mini-Agent.git`.
Speaks the same ACP wire protocol as gemini-acp.

Differences from gemini-acp:

1. Command shape: `mini-agent-acp` (a single binary, no `--acp` flag).
2. No session/set_mode — mini-agent's agentCapabilities don't advertise
   sessionModes; the agent runs in its configured posture.
3. Auth is config-file based at `~/.mini-agent/config/config.yaml`. No
   device-code flow. Set `api_key`, `api_base`, `model` (e.g.
   `MiniMax-M3`) before launching the bridge — no per-launch env-var
   plumbing required by the engine itself.

`loadSession: false` per the agentCapabilities response — each turn
starts fresh; the engine doesn't persist conversation state across
bridge turns. That matches the bridge's per-request semantics anyway.
"""

from __future__ import annotations

import subprocess
from typing import Callable

from .generic_acp import GenericAcpEngine


class MiniAgentAcpEngine(GenericAcpEngine):
    supports_thread_resume = False
    engine_label = "mini-agent"
    display_name = "mini-agent"
    def __init__(
        self,
        *,
        cwd: str,
        model: str | None,
        command: str = "mini-agent-acp",
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        super().__init__(
            cwd=cwd,
            model=model,
            command=command,
            popen_factory=popen_factory,
        )

    def command_args(self) -> list[str]:
        return [self.command]

    def set_session_mode_for_policy(self, policy: str) -> None:
        # mini-agent doesn't expose session/set_mode. Mode posture is set
        # via config.yaml + the system prompt at ~/.mini-agent/config/.
        return None
