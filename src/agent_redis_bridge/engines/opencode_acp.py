"""opencode ACP engine — `opencode acp` over stdio (JSON-RPC).

opencode (sst/opencode) is a TypeScript coding agent with first-class ACP
support, so it lands on the same `GenericAcpEngine` base as kimi/mini-agent/dsh.

Differences from the base:

- Command shape: `opencode acp` (not `gemini --acp`).
- Session modes are `build` (full access) | `plan` (read-only analysis), NOT
  gemini/kimi's `yolo`/`default`. opencode returns `modes: null` from
  `session/new` and declares no `agentCapabilities.sessionModes`, so the modes
  are not discoverable from the handshake — the same "accepts set_mode without
  advertising it" shape kimi-code-acp documents. Sending `yolo` fails with
  `Invalid params: mode not found: yolo`; `build` is accepted (verified
  empirically 2026-08-02).
- `session/set_model` IS supported and takes a `provider/model` id (e.g.
  `opencode/big-pickle`), so the base's model call is inherited unchanged. An
  unknown id is rejected at start with `Invalid params: model not found: …`,
  which is a loud failure rather than a silent fallback.

**This engine has no tool-allowlist surface.** Unlike the pi-family engines
(including omp-acp) there is no `--tools` flag on the `acp` path; opencode's
read-only posture is MODE-based (`plan`), which `readonly_gate.py` does not
model. An opencode seat therefore cannot be certified read-only by that gate and
is deliberately absent from its allowlist-engine set — see
`readonly_gate.py` and the `seat_posture_v` migration its docstring names.

`consumes_role_profile` is left at the base default (False): there is no
system-prompt injection flag on `opencode acp`, so the bridge prepends the role
profile to the task text instead (Bridge.role_profile_for_turn).

The user must have authenticated opencode once on this host (`opencode auth
login`, stored under `~/.local/share/opencode`) before the bridge can spawn
`opencode acp` headlessly.
"""

from __future__ import annotations

import subprocess
from typing import Callable

from ._acp import TurnPolicyPermissionMixin
from .base import EngineError
from .generic_acp import GenericAcpEngine


class OpencodeAcpEngine(TurnPolicyPermissionMixin, GenericAcpEngine):
    supports_thread_resume = False
    engine_label = "opencode"
    display_name = "opencode"

    def __init__(
        self,
        *,
        cwd: str,
        model: str | None,
        command: str = "opencode",
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

    def set_session_mode_for_policy(self, policy: str) -> None:
        if self.session_id is None:
            raise EngineError("ACP session not started")
        # opencode ships two agents/modes: build (full access) and plan
        # (read-only analysis). "yolo" is rejected outright.
        mode_id = "build" if policy == "trusted" else "plan"
        self.request(
            "session/set_mode",
            {"sessionId": self.session_id, "modeId": mode_id},
            timeout=15,
        )
