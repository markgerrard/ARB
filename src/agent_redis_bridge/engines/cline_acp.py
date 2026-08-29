from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from typing import Any, Callable

from ._acp_base import DenyBudgetAcpEngine, normalize_acp_session_update
from .base import EngineError

logger = logging.getLogger("agent_redis_bridge.engines.cline_acp")


class ClineAcpEngine(DenyBudgetAcpEngine):
    supports_thread_resume = False
    """Bridge engine for the Cline CLI's `--acp` JSON-RPC server.

    Communicates with Cline over stdio using the Agent Client Protocol.
    Cline exposes `session/new` with Devin-style `configOptions` (provider /
    model selects) plus `modes` (plan / act) and `models.availableModels`.

    The model pin is load-bearing: an ACP session boots on whatever model the
    interactive CLI last used — NOT the seat's intended model, and possibly a
    different vendor's — and the argv `-m` flag does not reach ACP sessions
    (probed 2026-08-01, cline 3.0.48). `start()` therefore hard-fails unless
    an explicit model resolves, applies, and reads back verbatim. See
    docs/superpowers/specs/2026-08-01-cline-acp-seat-design.md.

    The ACP transport, the prompt loop, the turn-scoped deny budget and the
    policy-threaded permission responder live in `_acp_base`.
    """

    engine_label = "cline"
    display_name = "Cline"
    default_command = "cline"
    logger = logger

    supports_continuation = False

    def __init__(
        self,
        *,
        cwd: str,
        model: str | None,
        command: str = "cline",
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        super().__init__(cwd=cwd, model=model, command=command, popen_factory=popen_factory)
        self.config_options: list[dict[str, Any]] = []
        self.available_models: list[dict[str, Any]] = []
        # Cline keeps sticky shared state under ~/.cline; retire the engine
        # after every dispatch so a pooled engine never reuses a process or
        # session across dispatches.
        self.retire_after_turn = True

    def _popen_extra_kwargs(self) -> dict[str, Any]:
        return {"encoding": "utf-8", "errors": "replace", "cwd": self.cwd}

    def command_args(self) -> list[str]:
        return [self.command, "--acp"]

    def _start_handshake(self) -> None:
        self._initialize()

        response = self._new_session(timeout=30)

        config_options = response.get("configOptions")
        if isinstance(config_options, list):
            self.config_options = [o for o in config_options if isinstance(o, dict)]
        models = response.get("models")
        if isinstance(models, dict):
            entries = models.get("availableModels")
            if isinstance(entries, list):
                self.available_models = [m for m in entries if isinstance(m, dict)]

        # The model pin is required, applied, and read back — never fail-open
        # onto the session default (a different vendor's model; see class doc).
        if not self.model:
            raise EngineError(
                "cline seat requires an explicit --model: the ACP session default is "
                "whatever the interactive CLI last used, not a seat-stable pin"
            )
        self._set_model(self.model)

    def _resolve_model_id(self, model: str) -> str | None:
        """Resolve *model* against the "model" config option, then the
        session's availableModels. Match order: exact value/modelId, then
        display name."""
        model_option = self._config_option("model")
        if isinstance(model_option, dict):
            options = model_option.get("options")
            if isinstance(options, list):
                for entry in options:
                    if isinstance(entry, dict) and entry.get("value") == model:
                        return model
                for entry in options:
                    if isinstance(entry, dict) and entry.get("name") == model:
                        value = entry.get("value")
                        if isinstance(value, str):
                            return value
        for entry in self.available_models:
            if entry.get("modelId") == model:
                return model
        for entry in self.available_models:
            if entry.get("name") == model:
                model_id = entry.get("modelId")
                if isinstance(model_id, str):
                    return model_id
        return None

    def _set_model(self, model: str) -> None:
        if self.session_id is None:
            raise EngineError("ACP session not started")
        model_id = self._resolve_model_id(model)
        if model_id is None:
            raise EngineError(
                f"could not resolve model {model!r} against cline configOptions/availableModels"
            )
        response = self.request(
            "session/set_config_option",
            {"sessionId": self.session_id, "configId": "model", "value": model_id},
            timeout=self._init_timeout,
            allow_empty_result=True,
        )
        current: Any = None
        echoed = response.get("configOptions")
        if isinstance(echoed, list):
            for option in echoed:
                if isinstance(option, dict) and option.get("id") == "model":
                    current = option.get("currentValue")
        if current != model_id:
            raise EngineError(
                f"model set read-back mismatch: requested {model_id!r}, session reports "
                f"{current!r} — refusing to run on the wrong model"
            )

    def set_session_mode_for_policy(self, policy: str) -> None:
        if self.session_id is None:
            raise EngineError("ACP session not started")
        # Cline modes:
        #   - act: make changes to the codebase (tool calls raise permission
        #     asks, which the trusted policy answers via the allow option)
        #   - plan: explore without modifying files
        # Non-trusted policies get plan mode AND fail-closed ask denial.
        mode_id = "act" if policy == "trusted" else "plan"
        self.request(
            "session/set_mode",
            {"sessionId": self.session_id, "modeId": mode_id},
            timeout=15,
            allow_empty_result=True,
        )

    def _normalize_session_update(self, update, tool_titles):
        return normalize_session_update(update, tool_titles)


def normalize_session_update(
    update: dict[str, Any],
    tool_titles: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Normalize Cline ACP session/update events to the bridge's neutral vocabulary.

    Observed vocabulary (probe 2026-08-01, cline 3.0.48): agent_message_chunk,
    tool_call, tool_call_update, session_info_update. Thought chunks and
    available-commands updates are in cline's upstream vocabulary and handled
    for parity with the other ACP engines. An untitled session_info_update is
    cline's bare `{updatedAt}` heartbeat and is dropped rather than surfaced as
    unknown-update noise."""
    return normalize_acp_session_update(
        update,
        tool_titles,
        suppress_short_thoughts=True,
        drop_untitled_session_info=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Standalone tester for ClineAcpEngine (cline --acp)")
    parser.add_argument(
        "prompt",
        nargs="?",
        default="Say hello from the Cline ACP bridge and run `pwd && echo --- && git status --short | head -5`",
        help="Task/prompt to send to Cline",
    )
    parser.add_argument("--cwd", default=".", help="Working directory for the Cline session")
    parser.add_argument("--model", default="deepseek/deepseek-v4-flash", help="Model pin (required by the engine)")
    parser.add_argument(
        "--policy",
        default="trusted",
        choices=["trusted", "human"],
        help="Sender policy mapping (act+allow vs plan+deny)",
    )
    parser.add_argument("--timeout", type=int, default=180, help="Turn timeout in seconds")
    args = parser.parse_args()

    def on_event(event: str, data: dict[str, Any]) -> None:
        if event == "model_text":
            sys.stdout.write(data.get("delta", ""))
            sys.stdout.flush()
        elif event == "model_thinking":
            pass
        elif event == "turn_completed":
            print(f"\n[turn_completed ok={data.get('ok')} stop_reason={data.get('stop_reason')}]")
        else:
            print(f"[{event}] {data}", file=sys.stderr)

    engine = ClineAcpEngine(cwd=args.cwd, model=args.model)
    try:
        engine.start()
        result = engine.run_turn_with_progress(args.prompt, timeout=args.timeout, policy=args.policy, on_event=on_event)
        print("\n[result]", "ok" if result.ok else f"error: {result.error}")
        print(result.result)
    finally:
        engine.stop()
