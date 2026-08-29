from __future__ import annotations

import logging
import subprocess
from typing import Any, Callable

from ._acp import _DENY_MARKERS, _select_allow_option
from ._acp_base import HealthReportingAcpEngine, normalize_acp_session_update
from .base import EngineError

logger = logging.getLogger("agent_redis_bridge.engines.cursor_acp")


class CursorAcpEngine(HealthReportingAcpEngine):
    supports_thread_resume = False
    """Bridge engine for Cursor CLI's `agent acp` JSON-RPC server.

    Communicates with the Cursor agent over stdio using the Agent Client
    Protocol (ACP).  Maps ACP notifications to bridge progress events so
    that streaming text, tool calls, and turn completion are observable
    through the Redis bus.

    The transport (spawn, reader thread, JSON-RPC ids, request/notify, the
    prompt loop) lives in `_acp_base.AcpEngineBase`; what stays here is the
    Cursor-specific surface — the `_meta.parameterizedModelPicker` capability,
    the `authenticate` handshake step, bracketed-modelId resolution, the `fast`
    config option, and cursor's own client methods (`cursor/ask_question`,
    `cursor/create_plan`).
    """

    engine_label = "cursor"
    display_name = "Cursor"
    default_command = "agent"
    logger = logger

    # Cursor keeps a stateful ACP session (self.session_id) and accepts
    # back-to-back session/prompt calls on it (live-verified), so the bridge's
    # drive-to-completion loop may re-prompt the same session.
    supports_continuation = True

    initialize_client_capabilities = {
        "auth": {"terminal": False},
        "fs": {"readTextFile": False, "writeTextFile": False},
        "_meta": {"parameterizedModelPicker": True},
        "terminal": False,
    }

    def __init__(
        self,
        *,
        cwd: str,
        model: str | None,
        fast: bool = False,
        command: str = "agent",
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        super().__init__(cwd=cwd, model=model, command=command, popen_factory=popen_factory)
        self.fast = fast
        self.available_models: list[dict[str, Any]] = []
        self.policy = "human"

    def _popen_extra_kwargs(self) -> dict[str, Any]:
        return {"encoding": "utf-8", "errors": "replace"}

    def command_args(self) -> list[str]:
        return [self.command, "acp"]

    def _start_handshake(self) -> None:
        self._initialize()

        self.request("authenticate", {"methodId": "cursor_login"}, timeout=self._init_timeout, allow_empty_result=True)

        response = self._new_session(timeout=30)

        models = response.get("models")
        if isinstance(models, dict):
            available = models.get("availableModels")
            if isinstance(available, list):
                self.available_models = [m for m in available if isinstance(m, dict)]

        if self.model:
            self._set_model(self.model)
        self._set_fast_mode(response)

    def _set_model(self, model: str) -> None:
        """Select *model* via a single session/set_model call.

        Resolves the operator-supplied value against the ``availableModels``
        list returned by session/new, then issues exactly one set_model. If the
        value can't be resolved, fail startup rather than silently running the
        wrong model.
        """
        if self.session_id is None:
            return
        model_id = self._resolve_model_id(model)
        if model_id is None:
            raise EngineError(f"could not resolve model {model!r} against availableModels")
        self.request(
            "session/set_model",
            {"sessionId": self.session_id, "modelId": model_id},
            timeout=self._init_timeout,
            allow_empty_result=True,
        )

    def _set_fast_mode(self, session_response: dict[str, Any]) -> None:
        if self.session_id is None:
            return
        config_options = session_response.get("configOptions")
        if not isinstance(config_options, list):
            return
        fast_option = next(
            (option for option in config_options if isinstance(option, dict) and option.get("id") == "fast"),
            None,
        )
        if fast_option is None:
            return
        try:
            self.request(
                "session/set_config_option",
                {"sessionId": self.session_id, "configId": "fast", "value": "true" if self.fast else "false"},
                timeout=self._init_timeout,
                allow_empty_result=True,
            )
        except EngineError as exc:
            logger.warning("session/set_config_option fast failed: %s", exc)

    def _resolve_model_id(self, model: str) -> str | None:
        """Resolve *model* to a full Cursor modelId using the session's
        availableModels (each entry is ``{modelId, name}`` where modelId is the
        bracketed id, e.g. ``claude-sonnet-4-6[thinking=true,...]``, and name is
        the bare label, e.g. ``claude-sonnet-4-6``).

        Match order: exact modelId, then exact name. If the value is already a
        bracketed id (contains ``[``) we honour it as-is even when not listed,
        so operators can pin a variant the server didn't advertise. Otherwise
        return None (caller keeps the default)."""
        for entry in self.available_models:
            if entry.get("modelId") == model:
                return model
        for entry in self.available_models:
            if entry.get("name") == model:
                model_id = entry.get("modelId")
                if isinstance(model_id, str):
                    return model_id
        if "[" in model:
            return model
        return None

    def _prepare_turn(self, policy: str) -> None:
        # Cursor answers client requests from self.policy rather than a threaded
        # argument, because cursor/create_plan is decided outside the permission
        # path too. The handshake default ("human") is what makes a pre-turn ask
        # cancel fail-closed.
        self.policy = policy
        super()._prepare_turn(policy)

    def _turn_usage(self, result: dict[str, Any]) -> Any:
        return result.get("usage") or (result.get("_meta") or {}).get("quota")

    def set_session_mode_for_policy(self, policy: str) -> None:
        if self.session_id is None:
            raise EngineError("ACP session not started")
        mode_id = "agent" if policy == "trusted" else "ask"
        self.request(
            "session/set_mode",
            {"sessionId": self.session_id, "modeId": mode_id},
            timeout=15,
            allow_empty_result=True,
        )

    def _normalize_session_update(self, update, tool_titles):
        return normalize_session_update(update, tool_titles)

    def _respond_to_client_request(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        request_id = message.get("id")

        if method == "session/request_permission":
            # Trusted bridge auto-approves, but must echo an optionId the server
            # actually offered (Cursor labels these per-tool; a hardcoded id can
            # be rejected and stall the turn). Pick the offered allow option.
            if self.policy != "trusted":
                result = {"outcome": {"outcome": "cancelled"}}
            else:
                params = message.get("params")
                option_id = _select_allow_option(params if isinstance(params, dict) else {})
                if option_id is not None:
                    result = {"outcome": {"outcome": "selected", "optionId": option_id}}
                else:
                    result = {"outcome": {"outcome": "cancelled"}}
        elif method == "cursor/ask_question":
            # Cancel blocking questions in headless mode.
            result = {"outcome": {"outcome": "cancelled"}}
        elif method == "cursor/create_plan":
            result = {"outcome": {"outcome": "accepted" if self.policy == "trusted" else "cancelled"}}
        else:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"client method not supported: {method}"},
                }
            )
            return

        self._send({"jsonrpc": "2.0", "id": request_id, "result": result})


def normalize_session_update(
    update: dict[str, Any],
    tool_titles: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Cursor's ACP session/update vocabulary — the standard shape, with every
    thought chunk emitted (cursor does not fragment them the way devin/cline do)
    and an untitled session_info_update surfaced as an unknown update."""
    return normalize_acp_session_update(update, tool_titles)
