from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from typing import Any, Callable

from ._acp_base import DenyBudgetAcpEngine, normalize_acp_session_update
from .base import EngineError

logger = logging.getLogger("agent_redis_bridge.engines.devin_acp")


class DevinAcpEngine(DenyBudgetAcpEngine):
    supports_thread_resume = False
    """Bridge engine for Devin CLI's `acp` JSON-RPC server.

    Communicates with the Devin agent over stdio using the Agent Client
    Protocol (ACP). Devin exposes a stateful ACP session with `session/new`,
    `session/prompt`, and `session/set_config_option` for model/mode selection.

    v1 goals:
    - Basic prompt -> streaming text + tool visibility
    - Trusted/human policy mapping via Devin's `bypass` and `accept-edits` modes
    - Minimal handling of cognition.ai/* vendor extensions (ignore without crashing)
    - Same normalized event surface as the rest of the bridge

    The ACP transport, the prompt loop, the turn-scoped deny budget and the
    policy-threaded permission responder all live in `_acp_base`; what stays
    here is Devin's configOptions-based model selection and mode mapping.
    """

    engine_label = "devin"
    display_name = "Devin"
    default_command = "devin"
    logger = logger

    supports_continuation = False

    def __init__(
        self,
        *,
        cwd: str,
        model: str | None,
        command: str = "devin",
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        super().__init__(cwd=cwd, model=model, command=command, popen_factory=popen_factory)
        self.config_options: list[dict[str, Any]] = []
        # Retire the engine after every dispatch so the pooled engine never
        # reuses a Devin process/session across dispatches.
        self.retire_after_turn = True

    def _popen_extra_kwargs(self) -> dict[str, Any]:
        return {"encoding": "utf-8", "errors": "replace"}

    def command_args(self) -> list[str]:
        return [self.command, "acp"]

    def _start_handshake(self) -> None:
        self._initialize()

        response = self._new_session(timeout=30)

        config_options = response.get("configOptions")
        if isinstance(config_options, list):
            self.config_options = [o for o in config_options if isinstance(o, dict)]

        if self.model:
            # Slice 1h (ARB-B17, owner-ruled 2026-08-01): a configured model that
            # cannot be applied is a NAMED refusal — silently continuing on the
            # session default lets the running family diverge from the configured
            # family with no observable signal (same class as the cline hard-fail).
            try:
                self._set_model(self.model)
            except EngineError as exc:
                raise EngineError(
                    f"configured model {self.model!r} could not be applied; "
                    f"refusing to run on the session-default family: {exc}"
                ) from exc

    def _resolve_model_id(self, model: str) -> str | None:
        """Resolve *model* against the Devin "model" config option options.

        Match order: exact option value, then exact option name."""
        model_option = self._config_option("model")
        if not isinstance(model_option, dict):
            return None
        options = model_option.get("options")
        if isinstance(options, list):
            for entry in options:
                if not isinstance(entry, dict):
                    continue
                if entry.get("value") == model:
                    return model
            for entry in options:
                if not isinstance(entry, dict):
                    continue
                if entry.get("name") == model:
                    value = entry.get("value")
                    if isinstance(value, str):
                        return value
        return None

    def _set_model(self, model: str) -> None:
        if self.session_id is None:
            return
        model_id = self._resolve_model_id(model)
        if model_id is None:
            raise EngineError(f"could not resolve model {model!r} against Devin configOptions")
        self.request(
            "session/set_config_option",
            {"sessionId": self.session_id, "configId": "model", "value": model_id},
            timeout=self._init_timeout,
            allow_empty_result=True,
        )

    def set_session_mode_for_policy(self, policy: str) -> None:
        if self.session_id is None:
            raise EngineError("ACP session not started")
        # Devin modes:
        #   - bypass: auto-approve all tool calls (trusted bridge peer)
        #   - accept-edits: code mode with per-tool permission requests
        #   - ask: answer questions without code changes
        #   - plan: plan changes before implementing
        # The bridge's policy maps to how permission requests are handled.
        mode_id = "bypass" if policy == "trusted" else "accept-edits"
        self.request(
            "session/set_config_option",
            {"sessionId": self.session_id, "configId": "mode", "value": mode_id},
            timeout=15,
            allow_empty_result=True,
        )

    def _normalize_session_update(self, update, tool_titles):
        return normalize_session_update(update, tool_titles)


def normalize_session_update(
    update: dict[str, Any],
    tool_titles: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Normalize Devin ACP session/update events to the bridge's neutral vocabulary."""
    return normalize_acp_session_update(update, tool_titles, suppress_short_thoughts=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Standalone tester for DevinAcpEngine (devin acp)")
    parser.add_argument(
        "prompt",
        nargs="?",
        default="Say hello from the Devin ACP bridge and run `pwd && echo --- && git status --short | head -5`",
        help="Task/prompt to send to Devin",
    )
    parser.add_argument("--cwd", default=".", help="Working directory for the Devin session")
    parser.add_argument("--model", default=None, help="Optional model override")
    parser.add_argument(
        "--policy",
        default="trusted",
        choices=["trusted", "human"],
        help="Sender policy mapping (bypass vs accept-edits)",
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

    engine = DevinAcpEngine(cwd=args.cwd, model=args.model)
    try:
        engine.start()
        result = engine.run_turn_with_progress(args.prompt, timeout=args.timeout, policy=args.policy, on_event=on_event)
        print("\n[result]", "ok" if result.ok else f"error: {result.error}")
        print(result.result)
    finally:
        engine.stop()
