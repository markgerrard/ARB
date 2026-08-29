"""The generic ACP engine — the stdio Agent Client Protocol client every
non-policy-driving adapter subclasses.

This class was called ``GeminiAcpEngine`` and lived in ``gemini_acp.py`` until
2026-08-29. That name had been wrong for a while: Google retired the `gemini`
CLI in 2026-07, the `gemini-acp` engine has been RETIRED in the support-tier
table ever since, and yet this class is the LIVE base of five shipping seats —
omp-acp, opencode-acp, kimi-code-acp, mini-agent-acp and dsh-acp. A deprecated
adapter's name on the base meant an omp seat announced itself as "Gemini" in its
error text and drained its child's stderr under a `[gemini-stderr]` prefix.

So the generic transport lives here under a generic name, and
``gemini_acp.GeminiAcpEngine`` is now a thin shim over it that carries the
gemini CLI's own command line and identity plus the deprecation notice. Nothing
importing the old name breaks.

What this class owns, and why it is not simply ``AcpEngineBase``: the pieces of
the ACP client this family diverges on. Each is kept here rather than taken from
the shared base because changing it would change how those five seats behave —

* ``run_turn_with_progress``: the ``_await_or_detect_death`` liveness shape, a
  non-dict prompt result treated as normal termination, no deny budget, and no
  ``stop_reason`` on the returned TurnResult.
* ``request()``: no ``allow_empty_result``, and liveness routed through
  ``_await_or_detect_death`` so the error names the child's own exit code.
* ``_read_stdout()``: this family has never had the CUR-2 exception guard, and
  has no ``is_healthy`` for its ``healthy = False`` to feed.
* ``_respond_to_client_request()``: cancels every permission ask.
  ``_acp.TurnPolicyPermissionMixin`` is what omp/opencode/dsh mix in over it.

Subclasses supply ``command_args()`` and their own ``engine_label`` /
``display_name``; ``session/set_mode`` defaults to gemini's ``yolo`` / ``default``
pair because kimi-code depends on it (see kimi_code_acp's module docstring).
"""
from __future__ import annotations

import json
import time
from typing import Any

from ._acp_base import AcpEngineBase
from .base import EngineError, ProgressCallback, TurnResult


class GenericAcpEngine(AcpEngineBase):
    supports_thread_resume = False

    # engine_label / display_name / default_command are deliberately NOT set
    # here: they are per-adapter identity, and inheriting a concrete engine's
    # name is exactly the defect this rename fixed. Every concrete subclass sets
    # them; command_args() stays abstract on AcpEngineBase.

    def _start_handshake(self) -> None:
        self._initialize()
        self.start_session()

    def start_session(self) -> str:
        self._new_session(timeout=30)

        if self.model:
            self.request("session/set_model", {"sessionId": self.session_id, "modelId": self.model}, timeout=self._init_timeout)
        return self.session_id

    def run_turn_with_progress(
        self,
        task: str,
        *,
        timeout: int = 3600,
        policy: str = "trusted",
        on_event: ProgressCallback | None,
    ) -> TurnResult:
        if self.session_id is None:
            raise EngineError("ACP session not started")

        self.set_session_mode_for_policy(policy)
        prompt_id = self.send_request_no_wait(
            "session/prompt",
            {"sessionId": self.session_id, "prompt": [{"type": "text", "text": task}]},
        )
        self.active_prompt_id = prompt_id
        if on_event is not None:
            on_event("turn_started", {"turn_id": str(prompt_id), "session_id": self.session_id})

        chunks: list[str] = []
        tool_titles: dict[str, str] = {}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # A child that dies mid-turn otherwise burns the whole turn timeout
            # (up to an hour) before anyone notices; surface the exit code now.
            #
            # RETURN, do not propagate: the raise is right for request() (no
            # partial result exists there), but inside the turn loop it would
            # discard everything `chunks` has streamed AND skip the terminal
            # progress event, so bridge.py's `turn_completed` handler never
            # advances the turn index and the reply carries result="". The
            # timeout path below and the ACP siblings that shipped this fix
            # first (cursor_acp / grok_acp / cline_acp / devin_acp) all preserve
            # the streamed prefix on an abnormal exit; this keeps the whole
            # family on one contract. Panel finding P1-1, run
            # panel-omp-opencode-arc-20260803T125825Z-570c21.
            try:
                message = self._await_or_detect_death(
                    "session/prompt", deadline=deadline, poll_cap=5
                )
            except EngineError as exc:
                self.active_prompt_id = None
                if on_event is not None:
                    on_event(
                        "turn_completed",
                        {
                            "turn_id": str(prompt_id),
                            "ok": False,
                            "stop_reason": "process_exited",
                            "usage": None,
                        },
                    )
                return TurnResult(ok=False, result="".join(chunks).strip(), error=str(exc))
            if message is None:
                continue
            # JSON-RPC ids are PER-SIDE namespaces: the agent's own outbound requests
            # (session/request_permission, ...) can collide with our prompt id. A
            # message with "method" is a REQUEST regardless of id — only a method-less
            # message is the prompt response (cursor-acp eee0b15; audit LT-1; the
            # non-dict branch below otherwise reports a FALSE successful completion).
            if message.get("id") == prompt_id and "method" not in message:
                if "error" in message:
                    self.active_prompt_id = None
                    return TurnResult(ok=False, result="".join(chunks).strip(), error=str(message["error"]))
                result = message.get("result")
                if not isinstance(result, dict):
                    # Some ACP servers (kimi-code observed 2026-06-04) return a
                    # non-dict (null / string / list) at session/prompt close
                    # instead of {stopReason, usage, ...}. Treat that as
                    # "agent terminated normally — use what streamed in".
                    self.active_prompt_id = None
                    if on_event is not None:
                        on_event("turn_completed", {"turn_id": str(prompt_id), "ok": True, "stop_reason": None, "usage": None})
                    text = "".join(chunks).strip() or f"ACP prompt {prompt_id} completed (no result body)."
                    return TurnResult(ok=True, result=text)
                stop_reason = result.get("stopReason")
                # "refusal" included per the cursor-acp hardening precedent (panel-reviewed,
                # docs/superpowers/specs/2026-07-01-cursor-acp-hardening-design.md): a refused
                # turn is a failed turn, not a success with refusal prose as the result.
                # This class serves the live kimi-code/mini-agent seats.
                ok = stop_reason not in {"cancelled", "failed", "error", "refusal"}
                if on_event is not None:
                    on_event(
                        "turn_completed",
                        {
                            "turn_id": str(prompt_id),
                            "ok": ok,
                            "stop_reason": stop_reason,
                            "usage": result.get("usage") or result.get("_meta", {}).get("quota"),
                        },
                    )
                self.active_prompt_id = None
                return TurnResult(
                    ok=ok,
                    result="".join(chunks).strip() or f"{self.display_name} ACP prompt {prompt_id} completed.",
                    error=None if ok else f"{self.display_name} ACP stopReason={stop_reason}",
                )
            self._handle_client_message(message, on_event=on_event, chunks=chunks, tool_titles=tool_titles)

        if on_event is not None:
            on_event("turn_timeout", {"timeout": timeout})
        self.active_prompt_id = None
        return TurnResult(ok=False, result="".join(chunks).strip(), error=f"turn timed out after {timeout}s")

    def set_session_mode_for_policy(self, policy: str) -> None:
        if self.session_id is None:
            raise EngineError("ACP session not started")
        mode_id = "yolo" if policy == "trusted" else "default"
        self.request("session/set_mode", {"sessionId": self.session_id, "modeId": mode_id}, timeout=15)

    def reset_context(self) -> str:
        return self.start_session()

    def _dead_child_error(self, method: str) -> EngineError | None:
        """An EngineError naming the child's exit code, or None if it is alive.

        Callers MUST only consult this when the message queue has come up empty:
        a child that wrote a complete reply and then exited is a normal, healthy
        shape, and checking liveness before draining would turn it into a false
        failure.
        """
        process = self.process
        if process is None:
            return None
        exit_code = process.poll()
        if exit_code is None:
            return None
        return EngineError(
            f"{method} failed: {self.command!r} exited with code {exit_code} "
            "before answering. Its own diagnostics are in the bridge log under "
            "the engine's stderr prefix — a non-zero code here is normally a "
            "spawn-flag or auth error, not a hang."
        )

    def _await_or_detect_death(
        self, method: str, *, deadline: float, poll_cap: float = 2
    ) -> dict[str, Any] | None:
        """One queue read, upgrading a dead child from 'silence' to a failure.

        Without this the loops below wait out the FULL timeout when the CLI has
        already exited — an ACP agent that dies at spawn (bad flag, missing auth)
        reported `initialize timed out after 60s` instead of its own error. Live
        specimen 2026-08-03: `omp --tools read,grep,find,ls` (pi's vocabulary;
        omp has no find/ls) exits rc=2 instantly and the seat waited the full 60s.
        """
        message = self._get_message(timeout=max(0.1, min(poll_cap, deadline - time.monotonic())))
        if message is not None:
            return message
        error = self._dead_child_error(method)
        if error is None:
            return None
        # Grace drain: the reader thread may still be flushing a final line the
        # child wrote just before exiting. Only fail if nothing arrives.
        message = self._get_message(timeout=0.5)
        if message is not None:
            return message
        raise error

    def request(self, method: str, params: dict[str, Any], *, timeout: int) -> dict[str, Any]:
        """This family's request loop.

        Not the base's: this one has no ``allow_empty_result`` (a non-dict result
        is always an error here) and it routes liveness through
        ``_await_or_detect_death``, which reports the child's own exit code
        instead of a bare "process exited unexpectedly".
        """
        request_id = self.send_request_no_wait(method, params)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self._await_or_detect_death(method, deadline=deadline)
            if message is None:
                continue
            # Per-side id namespaces: "method" present ⇒ agent request, not our
            # response (audit LT-1) — fall through to _handle_client_message.
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise EngineError(f"{method} failed: {message['error']}")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise EngineError(f"{method} returned non-object result")
                return result
            self._handle_client_message(message, on_event=None, chunks=[], tool_titles={})
        raise EngineError(f"{method} timed out after {timeout}s")

    def _respond_to_client_request(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        request_id = message.get("id")
        if method == "session/request_permission":
            result: dict[str, Any] = {"outcome": {"outcome": "cancelled"}}
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

    def _read_stdout(self) -> None:
        """Kept rather than inherited: this family's reader has never had the
        exception guard the cursor/grok reader grew (audit CUR-2), and it has no
        ``is_healthy`` for the guard's ``healthy = False`` to feed. Adopting the
        hardened base reader here would change how omp/opencode/kimi/mini-agent/
        dsh seats behave, which is out of scope for a transport extraction."""
        assert self.process is not None
        assert self.process.stdout is not None
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                self.messages.put(value)

    def _normalize_session_update(self, update, tool_titles):
        return normalize_session_update(update, tool_titles)


def normalize_session_update(
    update: dict[str, Any],
    tool_titles: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any]] | None:
    update_type = update.get("sessionUpdate")
    # Permissive parse — kimi-code-acp emits sessionUpdate types this fn
    # historically didn't recognize (verified empirically 2026-06-04: 1555
    # tool-call events surfaced fine, but the closing session/prompt
    # response shape diverged from gemini's contract). Surface unknown
    # update types as a generic "session_update_unknown" event so the
    # bridge log makes them visible to operators rather than silently
    # dropping them. The bridge's `[turn-event]` logger then prints the
    # type, letting us decode new ACP servers iteratively.
    if update_type == "agent_message_chunk":
        content = update.get("content")
        if isinstance(content, dict) and content.get("type") == "text" and isinstance(content.get("text"), str):
            return "model_text", {"delta": content["text"]}
        # fall through to generic emission so non-text chunks surface
    if update_type == "agent_thought_chunk":
        # Kimi-code-acp's extended-thinking output. Same shape as
        # agent_message_chunk but semantically reasoning, not the
        # response text. Emit as `model_thinking` so the bridge's
        # heartbeat triggers and the operator sees the agent is alive
        # during a long reasoning phase. Verified empirically 2026-06-04
        # (300 thought chunks before first text delta on a typical
        # review-shaped prompt).
        content = update.get("content")
        if isinstance(content, dict) and content.get("type") == "text" and isinstance(content.get("text"), str):
            return "model_thinking", {"delta": content["text"]}
    if update_type == "available_commands_update":
        # Informational — agent declares which slash-commands are
        # available in this session. Emit a one-shot event for the
        # log; doesn't affect the prompt's text content.
        commands = update.get("availableCommands")
        return "available_commands", {"commands": commands if isinstance(commands, list) else []}
    if update_type in {"tool_call", "tool_call_update"}:
        tool_call_id = update.get("toolCallId")
        title = update.get("title")
        if isinstance(tool_titles, dict) and isinstance(tool_call_id, str) and isinstance(title, str):
            tool_titles[tool_call_id] = title
        command = title
        if not isinstance(command, str) and isinstance(tool_titles, dict) and isinstance(tool_call_id, str):
            command = tool_titles.get(tool_call_id)
        status = update.get("status")
        data = {
            "command": command,
            "status": status,
            "exit_code": 0 if status == "completed" else None,
            "tool_call_id": tool_call_id,
        }
        if status == "in_progress":
            return "command_started", data
        if status in {"completed", "failed"}:
            if status == "failed":
                data["exit_code"] = 1
            return "command_finished", data
    # Unknown sessionUpdate type — emit a generic event so the bridge log
    # surfaces what's flowing. Useful when integrating a new ACP server
    # whose update vocabulary diverges from this family's.
    if isinstance(update_type, str):
        return "session_update_unknown", {"sessionUpdate": update_type}
    return None
