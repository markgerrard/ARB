"""oh-my-pi (`omp`) ACP engine — `omp acp` over stdio (JSON-RPC).

oh-my-pi is a FORK of pi (`@earendil-works/pi-coding-agent`), so this engine is a
cousin of `pi_rpc.py` / `pi_sdk.py`. It deliberately does NOT reuse either:

- pi's `--mode rpc` NDJSON is a private wire that omp is already extending (its
  `ready` frame advertises `supportedProtocolVersions [1,2]`), and omp republishes
  daily — it was at 17.2.4 / 571 versions against pi's 0.83.0 when this landed.
- omp's TypeScript SDK has diverged from the surface `tools/pi-sdk-host/host.mjs`
  drives (`ModelRuntime` is gone; tool construction moved to `createTools` /
  `BashTool`), so the pi-sdk host is not a drop-in either.

ACP is the versioned, standard seam, and it costs nothing that matters here:
omp's differentiators are agent-side (29 built-in tools vs pi's 7, subagents,
LSP/DAP, persistent eval) and reach through the ACP transport unchanged —
verified empirically 2026-08-02 by running `eval` (real Python; the returned
SHA-256 digest matched an independently computed one) and `task` (a real
subagent spawn) through this engine's ACP session.

Differences from the `GenericAcpEngine` base:

- Command shape: `omp [flags] acp` (flags precede the subcommand — that is the
  form proven below; `omp acp --flags` also parses but was never behaviourally
  verified, so it is not the shape used here).
- **`session/set_model` is unsupported** — omp answers `Unknown ACP ext method`
  and the base's `start_session()` would turn that into an `EngineError` and fail
  `start()`. The model is pinned at spawn via `--model` instead, so this class
  suppresses the base's set_model call.
- Session modes are `default` | `plan` (NOT gemini/kimi's `yolo`); `plan` is
  omp's engine-enforced read-only mode, used for non-trusted policies.
- `--tools` (the allowlist) and `--append-system-prompt` (the role profile) are
  honoured on the `acp` subcommand — both verified behaviourally, the allowlist
  by a control/limited pair in which the control wrote a file and the
  `--tools read,grep` run could not and reported only read/grep available.

Auth lives in omp's own store (`~/.omp/agent/`), separate from pi's
`~/.pi/agent/auth.json`; omp also inherits credentials from other tools' dotfiles
on first run. Authenticate before the first dispatch.
"""

from __future__ import annotations

import subprocess
from typing import Callable

from ._acp import TurnPolicyPermissionMixin
from ._stdio import scrubbed_child_env
from .base import EngineError, parse_tool_allowlist
from .generic_acp import GenericAcpEngine


class OmpAcpEngine(TurnPolicyPermissionMixin, GenericAcpEngine):
    supports_thread_resume = False
    engine_label = "omp"
    display_name = "omp"
    # omp takes the role profile as a spawn flag (`--append-system-prompt`), so
    # the bridge must NOT also prepend it to the task text — see
    # Bridge.role_profile_for_turn (bridge.py:3769).
    consumes_role_profile = True

    def __init__(
        self,
        *,
        cwd: str,
        model: str | None,
        pi_tools: str | None = None,
        append_system_prompt: str | None = None,
        command: str = "omp",
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        preflight_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        super().__init__(
            cwd=cwd,
            model=model,
            command=command,
            popen_factory=popen_factory,
        )
        # Comma-separated allowlist, same shape and same env var as the pi
        # engines (`--pi-tools` / BRIDGE_PI_TOOLS), which is what lets
        # readonly_gate certify an omp seat. The degenerate-value refusal lives
        # in the shared helper so pi-sdk/pi-rpc/omp-acp cannot drift apart.
        self.pi_tools = pi_tools
        self._tools_list: tuple[str, ...] = parse_tool_allowlist(
            pi_tools,
            fallback_hint=(
                "omp's full 29-tool default surface, including bash/write/edit "
                "and the host- and network-reaching browser/computer/github"
            ),
        )
        self.append_system_prompt = append_system_prompt
        self._preflight_runner = preflight_runner

    def start(self) -> None:
        self._preflight_spawn_flags()
        super().start()

    def _preflight_spawn_flags(self) -> None:
        """Validate the spawn flags with omp's OWN validator before handshaking.

        omp rejects a bad flag by exiting rc=2 *immediately* — but the ACP base's
        `initialize` request has no liveness check, so it waits out the full
        `BRIDGE_ENGINE_INIT_TIMEOUT_S` (60s default) and reports
        ``initialize timed out after 60s``: an operator config error disguised as
        a wedge, with omp's actual explanation left in the stderr drain.

        The trap this exists for is real and easy to hit: **omp's tool vocabulary
        is NOT pi's.** The canonical pi reviewer allowlist ``read,grep,find,ls``
        dies here, because omp has no ``find`` and no ``ls`` — its equivalents are
        ``glob`` and ``read`` (which reads directories too). Verified 2026-08-02:
        ``omp --tools read,grep,find,ls --version`` → rc=2, "Unknown tool in
        --tools: ls".

        Running omp's own validator (rather than a hardcoded name list here)
        means custom/MCP tool names a seat legitimately declares are never
        false-refused — omp decides, we only surface the verdict.
        """
        # command_args() always appends "acp" last; swap it for --version so omp
        # parses and validates the flags, then exits instead of serving.
        argv = self.command_args()[:-1] + ["--version"]
        try:
            proc = self._preflight_runner(
                argv,
                capture_output=True,
                text=True,
                timeout=self._init_timeout,
                env=scrubbed_child_env(),
            )
        except FileNotFoundError as exc:
            raise EngineError(
                f"omp executable not found (tried `{self.command}`). Install via "
                "`brew install can1357/tap/omp` (the standalone build; the bun "
                "install route needs bun >= 1.3.14) or set `command` to an "
                "absolute path."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise EngineError(
                f"omp did not answer `--version` within {self._init_timeout}s; "
                "refusing to start rather than wedge the ACP handshake."
            ) from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise EngineError(
                f"omp rejected its spawn flags (exit {proc.returncode}): "
                f"{detail[0] if detail else '<no output>'}"
            )

    def command_args(self) -> list[str]:
        args = [self.command]
        if self._tools_list:
            args.extend(["--tools", ",".join(self._tools_list)])
        if self.append_system_prompt:
            args.extend(["--append-system-prompt", self.append_system_prompt])
        if self.model:
            args.extend(["--model", self.model])
        args.append("acp")
        return args

    def start_session(self) -> str:
        """Create the ACP session WITHOUT the base's `session/set_model` call.

        omp rejects that method (`Unknown ACP ext method: session/set_model`,
        verified 2026-08-02), which the base would raise as an EngineError and
        fail start(). `self.model` is still carried — `command_args` pins it at
        spawn via `--model` — so it is only suppressed across the super() call.
        """
        model, self.model = self.model, None
        try:
            return super().start_session()
        finally:
            self.model = model

    def set_session_mode_for_policy(self, policy: str) -> None:
        if self.session_id is None:
            raise EngineError("ACP session not started")
        # omp's session/new advertises exactly two modes: "default" (standard
        # ACP headless) and "plan" ("Read-only planning mode that drafts a plan
        # to a markdown file before any code changes"). There is no "yolo".
        mode_id = "default" if policy == "trusted" else "plan"
        self.request(
            "session/set_mode",
            {"sessionId": self.session_id, "modeId": mode_id},
            timeout=15,
        )
