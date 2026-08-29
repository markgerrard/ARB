"""kimi-code ACP engine — `kimi acp` over stdio (JSON-RPC v1).

Speaks the same Agent Client Protocol as gemini-acp. Differences:

- Command shape: `kimi acp` (not `gemini --acp`).
- Kimi's session/new exposes configOptions advertising 4 modes
  (default, plan, auto, yolo) but does NOT declare them in
  agentCapabilities.sessionModes. The base class still sends
  `session/set_mode` with `modeId: yolo` when policy is "trusted",
  which kimi accepts (verified empirically 2026-06-04). Without
  setting yolo, kimi gates every tool call behind a
  `session/request_permission` round-trip; the default
  `_respond_to_client_request` cancels them, producing empty results.

The user must have run `kimi login` once on this host before the bridge
can spawn `kimi acp` headlessly — without it, the agent advertises
authMethods and the bridge's initialize handshake will hang waiting for
a session that can't start.
"""

from __future__ import annotations

import subprocess
from typing import Callable

from .generic_acp import GenericAcpEngine


class KimiCodeAcpEngine(GenericAcpEngine):
    supports_thread_resume = False
    engine_label = "kimi-code"
    display_name = "kimi-code"
    def __init__(
        self,
        *,
        cwd: str,
        model: str | None,
        command: str = "kimi",
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        super().__init__(
            cwd=cwd,
            model=model,
            command=command,
            popen_factory=popen_factory,
        )

    def command_args(self) -> list[str]:
        return [self.command, "acp"]
