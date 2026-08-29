"""Shared ACP engine base (Agent Client Protocol) — the transport every ACP
adapter runs on.

`_acp.py` was extracted from cursor_acp so that "grok_acp (and any future ACP
engine) reuses the panel-reviewed allow-option selection instead of forking it".
Only that one function was ever shared; the rest of the client — process
lifecycle, JSON-RPC id allocation, the stdout reader thread, request/notify,
client-message dispatch, the prompt loop — was forked five ways (cline 697,
devin 667, cursor 591, gemini 466, grok 865 lines; difflib line ratio 0.85
between cline and devin). This module holds that machinery once.

Layering, and why it is split rather than one class:

* :class:`AcpEngineBase` — the transport plus the common prompt loop.
  Everything the generic-acp family (omp / opencode / kimi-code / mini-agent /
  dsh, all on ``generic_acp.GenericAcpEngine``) shares with the policy-driving
  engines.
* :class:`HealthReportingAcpEngine` — adds ``is_healthy``. Deliberately NOT on
  the transport base: ``engine_pool.py`` consults ``is_healthy`` only when the
  engine defines it (engine_pool.py:164), and the generic-acp family has never
  declared one, so hoisting it would silently opt those seats into pool-side
  health quarantine — a behaviour change, not a refactor.
* :class:`DenyBudgetAcpEngine` — the turn-scoped approval deny budget and the
  policy-threaded permission responder shared by devin-acp and cline-acp.

Permission *decisions* stay in `_acp.py` (``_select_allow_option``,
``TurnPolicyPermissionMixin``): they are security-reviewed, and are reused
rather than reimplemented here.

Spawning stays on `_stdio`: ``scrubbed_child_env`` + ``start_stderr_drain`` are
the only way a child is created, and `tests/test_engine_spawn_env_ast_guard.py`
enforces the env choke.

Noted, deliberately NOT fixed by this refactor: `_acp.py`'s bare
``except Exception`` in ``_cancel_stale_permission_asks`` (``queue.Empty`` is
the only expected exit).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
from typing import Any, Callable

from agent_redis_bridge.local_memory_mcp import local_memory_mcp_servers

from ._acp import _select_allow_option
from ._stdio import scrubbed_child_env, start_stderr_drain
from .base import EngineError, ProgressCallback, TurnResult, engine_init_timeout

logger = logging.getLogger(__name__)


class AcpEngineBase:
    """Stdio JSON-RPC client for an ACP agent.

    Subclasses supply only what is genuinely engine-specific: the command line
    (:meth:`command_args`), the post-spawn handshake (:meth:`_start_handshake`),
    model resolution, :meth:`set_session_mode_for_policy`, and the session-update
    normaliser (:meth:`_normalize_session_update`).
    """

    # --- identity -----------------------------------------------------------
    #: lowercase name used in error text, log lines and the stderr-drain label.
    engine_label: str = "acp"
    #: capitalised name used in the "<Name> ACP …" messages.
    display_name: str = "ACP"
    #: default argv[0]; overridable per construction via ``command=``.
    default_command: str = "acp"
    #: the engine module's logger, so ``assertLogs("…engines.<engine>_acp")``
    #: keeps working for messages emitted by shared code.
    logger: logging.Logger = logger

    # --- capability declarations (see engines/base.py AgentEngine) -----------
    supports_thread_resume: bool = False
    supports_continuation: bool = False

    # --- knobs --------------------------------------------------------------
    #: `session/initialize` clientCapabilities; cursor adds a `_meta` entry.
    initialize_client_capabilities: dict[str, Any] = {
        "auth": {"terminal": False},
        "fs": {"readTextFile": False, "writeTextFile": False},
        "terminal": False,
    }
    #: message for :meth:`steer`; ``None`` derives it from ``display_name``.
    steer_error: str | None = None
    #: grok-acp drops malformed stdout JSON silently; the others log it.
    log_malformed_stdout_json: bool = True
    #: devin/cline mark the engine unhealthy when the child dies mid-turn;
    #: cursor leaves the flag alone (``is_healthy`` already ANDs ``poll()``).
    unhealthy_on_turn_process_death: bool = False

    def __init__(
        self,
        *,
        cwd: str,
        model: str | None,
        command: str | None = None,
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        self.cwd = cwd
        self.model = model
        self._init_timeout = engine_init_timeout()
        self.command = self.default_command if command is None else command
        self.popen_factory = popen_factory
        self.process: subprocess.Popen[str] | None = None
        self.reader_thread: threading.Thread | None = None
        self.stderr_thread: threading.Thread | None = None
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.next_id = 1
        self.send_lock = threading.Lock()
        self.session_id: str | None = None
        self.active_prompt_id: int | str | None = None
        self.healthy = True
        self._progress_seq = 0

    # ------------------------------------------------------------------ hooks
    def command_args(self) -> list[str]:
        raise NotImplementedError

    def _popen_extra_kwargs(self) -> dict[str, Any]:
        """Per-engine additions to the child spawn (encoding/errors/cwd/…)."""
        return {}

    def _start_handshake(self) -> None:
        """Everything after the child is spawned and the reader is running."""
        raise NotImplementedError

    def set_session_mode_for_policy(self, policy: str) -> None:
        raise NotImplementedError

    def _normalize_session_update(
        self, update: dict[str, Any], tool_titles: dict[str, str] | None
    ) -> tuple[str, dict[str, Any]] | None:
        raise NotImplementedError

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self.process = self.popen_factory(
            self.command_args(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=scrubbed_child_env(),
            **self._popen_extra_kwargs(),
        )
        self.reader_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader_thread.start()
        self.stderr_thread = start_stderr_drain(self.process, self.engine_label)
        self._start_handshake()

    def _initialize(self) -> dict[str, Any]:
        return self.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientInfo": {"name": "agent-redis-bridge", "version": "0.1.0"},
                "clientCapabilities": self.initialize_client_capabilities,
            },
            timeout=self._init_timeout,
        )

    def _new_session(self, *, timeout: int = 30) -> dict[str, Any]:
        """Issue ``session/new`` and adopt the returned sessionId, or raise."""
        response = self.request(
            "session/new",
            {"cwd": self.cwd, "mcpServers": local_memory_mcp_servers()},
            timeout=timeout,
        )
        session_id = response.get("sessionId")
        if not isinstance(session_id, str):
            raise EngineError("session/new did not return sessionId")
        self.session_id = session_id
        return response

    def stop(self) -> None:
        process = self.process
        if process is None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def steer(self, message: str) -> str:
        raise EngineError(
            self.steer_error
            or f"{self.display_name} ACP does not support mid-prompt steer"
        )

    def interrupt(self) -> str:
        if self.session_id is None:
            raise EngineError("ACP session not started")
        self.notify("session/cancel", {"sessionId": self.session_id})
        return self.session_id

    # ------------------------------------------------------------- transport
    def _send(self, payload: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise EngineError(f"{self.display_name} ACP process is not running")
        with self.send_lock:
            self.process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            self.process.stdin.flush()

    def _next_request_id(self) -> int:
        with self.send_lock:
            request_id = self.next_id
            self.next_id += 1
            return request_id

    def send_request_no_wait(self, method: str, params: dict[str, Any]) -> int:
        request_id = self._next_request_id()
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return request_id

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _read_stdout(self) -> None:
        assert self.process is not None
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    if self.log_malformed_stdout_json:
                        self.logger.warning(
                            f"{self.engine_label} stdout: dropped malformed JSON line: %r",
                            line[:200],
                        )
                    continue
                if isinstance(value, dict):
                    self.messages.put(value)
        except Exception:  # noqa: BLE001 - a dead reader must mark the engine, not die silently
            self.logger.exception(
                f"{self.engine_label} stdout reader died; marking engine unhealthy"
            )
            self.healthy = False

    def _get_message(self, *, timeout: float) -> dict[str, Any] | None:
        try:
            return self.messages.get(timeout=timeout)
        except queue.Empty:
            return None

    def _process_exited(self) -> bool:
        return self.process is not None and self.process.poll() is not None

    def _last_chance_message_after_process_exit(self) -> dict[str, Any] | None:
        """Drain a final line the child wrote just before exiting.

        Joins the reader thread rather than sleeping a fixed grace: once the
        child is gone the reader is at EOF by definition, so the join is bounded
        by how fast the OS flushes the pipe, not by a guessed constant. Landed
        in cursor_acp and copied into cline_acp/devin_acp/grok_acp; grok was the
        one ACP engine whose ``request()`` had no liveness path at all.
        """
        if not self._process_exited():
            return None
        if self.reader_thread is not None and self.reader_thread is not threading.current_thread():
            self.reader_thread.join(timeout=1.0)
        return self._get_message(timeout=0)

    def _request_process_exit_error(self, method: str) -> EngineError:
        """The error ``request()`` raises once the child is known dead."""
        return EngineError(f"{self.engine_label} process exited unexpectedly")

    def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: int,
        allow_empty_result: bool = False,
    ) -> dict[str, Any]:
        request_id = self.send_request_no_wait(method, params)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self._get_message(timeout=max(0.1, min(2, deadline - time.monotonic())))
            if message is None:
                message = self._last_chance_message_after_process_exit()
                if message is None:
                    if self._process_exited():
                        raise self._request_process_exit_error(method)
                    continue
            # Same per-side id-namespace rule as the prompt loop below: a message
            # with "method" is the AGENT's request regardless of id (audit CUR-1).
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise EngineError(f"{method} failed: {message['error']}")
                result = message.get("result")
                if not isinstance(result, dict):
                    # Void ACP methods (set_mode/set_model/authenticate) return a
                    # null/empty result — especially as an idempotent no-op when the
                    # value is already set. Callers that expect no payload pass
                    # allow_empty_result=True; treat null as success ({}).
                    if allow_empty_result:
                        return {}
                    raise EngineError(f"{method} returned non-object result")
                return result
            # policy defaults to None here => _respond_to_client_request denies any
            # ask unconditionally (fail-closed floor: no authorizing turn is in
            # scope during a handshake/config request; GROK-1 D2).
            self._handle_client_message(message, on_event=None, chunks=[], tool_titles={})
        raise EngineError(f"{method} timed out after {timeout}s")

    # ------------------------------------------------------- progress schema
    def _next_progress_seq(self) -> int:
        self._progress_seq += 1
        return self._progress_seq

    def _with_progress_schema(self, event_name: str, data: dict[str, Any]) -> dict[str, Any]:
        turn_id = str(self.active_prompt_id)
        item_id = data.get("tool_call_id")
        if not isinstance(item_id, str):
            suffix = "text" if event_name == "model_text" else "thinking" if event_name == "model_thinking" else event_name
            item_id = f"{turn_id}:{suffix}"
        enriched = dict(data)
        enriched.update(
            {
                "turn_id": turn_id,
                "item_id": item_id,
                "seq": self._next_progress_seq(),
            }
        )
        if "kind" not in enriched:
            enriched["kind"] = event_name
        return enriched

    # --------------------------------------------------- inbound dispatching
    def _dispatch_client_request(
        self,
        message: dict[str, Any],
        *,
        policy: str | None,
        on_event: ProgressCallback | None,
    ) -> None:
        """Route a server-initiated request to this engine's responder.

        A hook rather than a direct call because the responders differ in
        signature: cursor answers from ``self.policy``, devin/cline take the
        threaded ``policy``, grok takes ``policy`` and ``on_event``, and the
        security-reviewed ``TurnPolicyPermissionMixin`` (omp/opencode/dsh) takes
        neither — that mixin must be reused unmodified, so the base's default
        is the no-extra-argument form it expects.
        """
        self._respond_to_client_request(message)

    def _handle_client_message(
        self,
        message: dict[str, Any],
        *,
        on_event: ProgressCallback | None,
        chunks: list[str],
        tool_titles: dict[str, str],
        started_tool_calls: set[str] | None = None,
        completed_only_tool_calls: set[str] | None = None,
        policy: str | None = None,
    ) -> None:
        if "id" in message and isinstance(message.get("method"), str):
            self._dispatch_client_request(message, policy=policy, on_event=on_event)
            return

        if message.get("method") != "session/update":
            return
        params = message.get("params")
        if not isinstance(params, dict) or params.get("sessionId") != self.session_id:
            return
        update = params.get("update")
        if not isinstance(update, dict):
            return
        event = self._normalize_session_update(update, tool_titles)
        if event is None:
            return
        event_name, data = event
        if event_name == "model_text":
            delta = data.get("delta")
            if isinstance(delta, str):
                chunks.append(delta)
        if event_name == "command_started" and started_tool_calls is not None:
            # Cursor sends pending -> in_progress -> completed for one tool call;
            # both pending and in_progress normalize to command_started. Emit it
            # once per tool_call_id so downstream sees a single start (matching
            # the one command_finished). Keyed on first-seen, so a tool that
            # skips a phase still surfaces a start. (Was duplicated verbatim in
            # devin_acp/cline_acp, uncommented; de-duplicated here.)
            tool_call_id = data.get("tool_call_id")
            if isinstance(tool_call_id, str):
                if tool_call_id in started_tool_calls:
                    return
                started_tool_calls.add(tool_call_id)
        if event_name == "command_finished" and completed_only_tool_calls is not None:
            tool_call_id = data.get("tool_call_id")
            if (
                isinstance(tool_call_id, str)
                and (started_tool_calls is None or tool_call_id not in started_tool_calls)
            ):
                completed_only_tool_calls.add(tool_call_id)
        if on_event is not None:
            data = self._with_progress_schema(event_name, data)
            on_event(event_name, data)

    # -------------------------------------------------------------- the turn
    def _prepare_turn(self, policy: str) -> None:
        """Everything between "session is up" and sending the prompt."""
        self.set_session_mode_for_policy(policy)

    def _turn_usage(self, result: dict[str, Any]) -> Any:
        """Usage payload carried on ``turn_completed``."""
        return result.get("usage")

    def _turn_interrupt_result(self, *, chunks: list[str]) -> TurnResult | None:
        """Checked after every handled mid-turn message; non-None ends the turn."""
        return None

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

        self._prepare_turn(policy)
        prompt_id = self.send_request_no_wait(
            "session/prompt",
            {"sessionId": self.session_id, "prompt": [{"type": "text", "text": task}]},
        )
        self.active_prompt_id = prompt_id
        if on_event is not None:
            on_event("turn_started", {"turn_id": str(prompt_id), "session_id": self.session_id})

        chunks: list[str] = []
        tool_titles: dict[str, str] = {}
        started_tool_calls: set[str] = set()
        completed_only_tool_calls: set[str] = set()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self._get_message(timeout=max(0.1, min(5, deadline - time.monotonic())))
            if message is None:
                # The prompt response can race the process exit — drain the
                # reader's final flush before declaring a crash.
                message = self._last_chance_message_after_process_exit()
                if message is None:
                    if self._process_exited():
                        self.active_prompt_id = None
                        if self.unhealthy_on_turn_process_death:
                            self.healthy = False
                        return TurnResult(
                            ok=False,
                            result="".join(chunks).strip(),
                            error=f"{self.engine_label} process exited unexpectedly",
                        )
                    continue
            # JSON-RPC ids are PER-SIDE namespaces: the agent's own outbound requests
            # (session/request_permission, ...) count independently and can collide with
            # our prompt id after enough permissioned calls. A message with "method" is a
            # REQUEST regardless of id — only a method-less message is the prompt response.
            # (Root cause of every long-turn "null/malformed prompt result", 2026-07-07.)
            if message.get("id") == prompt_id and "method" not in message:
                if "error" in message:
                    self.active_prompt_id = None
                    return TurnResult(
                        ok=False,
                        result="".join(chunks).strip(),
                        error=str(message["error"]),
                    )
                result = message.get("result")
                if not isinstance(result, dict):
                    self.active_prompt_id = None
                    if on_event is not None:
                        on_event(
                            "turn_completed",
                            {"turn_id": str(prompt_id), "ok": False, "stop_reason": None, "usage": None},
                        )
                    text = "".join(chunks).strip()
                    return TurnResult(
                        ok=False,
                        result=text,
                        error=f"{self.engine_label} ACP returned a null/malformed prompt result",
                    )
                stop_reason = result.get("stopReason")
                # "refusal" included per the cursor-acp hardening precedent (panel-reviewed,
                # docs/superpowers/specs/2026-07-01-cursor-acp-hardening-design.md): a refused
                # turn is a failed turn, not a success with refusal prose as the result.
                ok = stop_reason not in {"cancelled", "failed", "error", "refusal"}
                self.active_prompt_id = None
                if on_event is not None:
                    on_event(
                        "turn_completed",
                        {
                            "turn_id": str(prompt_id),
                            "ok": ok,
                            "stop_reason": stop_reason,
                            "usage": self._turn_usage(result),
                        },
                    )
                text = "".join(chunks).strip()
                return TurnResult(
                    ok=ok,
                    result=text,
                    error=None if ok else f"{self.display_name} ACP stopReason={stop_reason}",
                    stop_reason=stop_reason if isinstance(stop_reason, str) else None,
                    tool_calls=len(started_tool_calls) + len(completed_only_tool_calls),
                )

            self._handle_client_message(
                message,
                on_event=on_event,
                chunks=chunks,
                tool_titles=tool_titles,
                started_tool_calls=started_tool_calls,
                completed_only_tool_calls=completed_only_tool_calls,
                policy=policy,
            )
            interrupted = self._turn_interrupt_result(chunks=chunks)
            if interrupted is not None:
                return interrupted

        if on_event is not None:
            on_event("turn_timeout", {"timeout": timeout})
        self.active_prompt_id = None
        try:
            self.notify("session/cancel", {"sessionId": self.session_id})
        except Exception:
            pass
        self.healthy = False
        return TurnResult(ok=False, result="".join(chunks).strip(), error=f"turn timed out after {timeout}s")


class HealthReportingAcpEngine(AcpEngineBase):
    """:class:`AcpEngineBase` plus the pooling health predicate.

    Separate from the transport base on purpose. ``engine_pool.release`` only
    consults ``is_healthy`` when the engine defines it (engine_pool.py:164), and
    the generic-acp family (omp / opencode / kimi-code / mini-agent / dsh)
    never has — declaring it there would change how those seats are recycled. This
    class is what cursor / devin / cline / grok mix in.
    """

    def is_healthy(self) -> bool:
        # reader_thread liveness matters: a dead reader with a live child is a
        # DEAF engine — recycling it poisons the next task (audit CUR-2; grok
        # carried the same check as "cursor CUR-2 parity").
        return (
            self.healthy
            and self.process is not None
            and self.process.poll() is None
            and (self.reader_thread is None or self.reader_thread.is_alive())
        )


class DenyBudgetAcpEngine(HealthReportingAcpEngine):
    """The devin-acp / cline-acp shape: policy-threaded asks + a deny budget.

    Authorization is turn-scoped — threaded through the call chain (grok D2
    parity), never engine state — and the budget bounds deny-looping under
    non-trusted policies.
    """

    unhealthy_on_turn_process_death = True

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # Permission authorization is turn-scoped (threaded through the call
        # chain, grok D2 parity) — never engine state. The budget bounds
        # deny-looping under non-trusted policies.
        self.deny_budget = int(os.environ.get("BRIDGE_APPROVAL_DENY_BUDGET", "10"))
        self._deny_count = 0
        self._last_denied_title: str | None = None

    def _config_option(self, config_id: str) -> dict[str, Any] | None:
        for option in self.config_options:
            if option.get("id") == config_id:
                return option
        return None

    def _prepare_turn(self, policy: str) -> None:
        self._deny_count = 0
        self._last_denied_title = None
        try:
            self.set_session_mode_for_policy(policy)
        except EngineError as exc:
            self.logger.warning(f"{self.engine_label} set session mode failed: %s", exc)

    def _dispatch_client_request(
        self,
        message: dict[str, Any],
        *,
        policy: str | None,
        on_event: ProgressCallback | None,
    ) -> None:
        self._respond_to_client_request(message, policy=policy)

    def _respond_to_client_request(
        self, message: dict[str, Any], *, policy: str | None = None
    ) -> None:
        """Answer a server-initiated request. Authorization comes ONLY from the
        threaded ``policy`` (grok D2 parity): the turn loop passes the active
        turn's policy; ``request()`` passes None, which always denies."""
        method = message.get("method")
        request_id = message.get("id")
        raw_params = message.get("params")
        params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}

        if method == "session/request_permission":
            ask_session = params.get("sessionId")
            if ask_session != self.session_id:
                # An ask for a session we do not own is structurally
                # unauthorizable — deny fail-closed regardless of policy
                # (grok D3b parity).
                self.logger.warning(
                    f"{self.engine_label} permission ask for non-current session %r "
                    "(current %r); denied fail-closed",
                    ask_session,
                    self.session_id,
                )
                result = {"outcome": {"outcome": "cancelled"}}
            elif policy != "trusted":
                if policy is not None:
                    self._deny_count += 1
                    self._last_denied_title = _ask_title(params)
                else:
                    self.logger.warning(
                        f"{self.engine_label} permission ask outside an authorizing "
                        "turn; denied fail-closed"
                    )
                result = {"outcome": {"outcome": "cancelled"}}
            else:
                option_id = _select_allow_option(params)
                if option_id is not None:
                    result = {"outcome": {"outcome": "selected", "optionId": option_id}}
                else:
                    result = {"outcome": {"outcome": "cancelled"}}
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

    def _turn_interrupt_result(self, *, chunks: list[str]) -> TurnResult | None:
        if self._deny_count <= self.deny_budget:
            return None
        # The model is deny-looping instead of concluding; every ask
        # was still answered fail-closed above. Cancel and end the
        # turn legibly instead of burning the whole timeout.
        self.active_prompt_id = None
        try:
            self.notify("session/cancel", {"sessionId": self.session_id})
        except Exception:
            pass
        self.healthy = False
        return TurnResult(
            ok=False,
            result="".join(chunks).strip(),
            error=(
                f"approval deny budget exhausted ({self._deny_count} denials); "
                f"last: {self._last_denied_title or '<unknown tool>'}"
            ),
        )


def _ask_title(params: dict[str, Any]) -> str | None:
    tool_call = params.get("toolCall")
    if isinstance(tool_call, dict) and isinstance(tool_call.get("title"), str):
        return tool_call["title"]
    return None


def normalize_acp_session_update(
    update: dict[str, Any],
    tool_titles: dict[str, str] | None = None,
    *,
    suppress_short_thoughts: bool = False,
    drop_untitled_session_info: bool = False,
) -> tuple[str, dict[str, Any]] | None:
    """Normalize a standard ACP ``session/update`` to the bridge's vocabulary.

    Shared verbatim by cursor-acp, devin-acp and cline-acp, which had three
    copies of this function differing only in the two flags below. grok-acp and
    generic-acp keep their own (grok dedups tool events and emits nothing for
    unknown types; generic-acp's predates the diff-path and `pending`
    handling).

    *suppress_short_thoughts* — devin/cline drop tiny ``agent_thought_chunk``
    fragments to reduce event spam. *drop_untitled_session_info* — cline sees a
    bare ``{updatedAt}`` heartbeat and drops it rather than emitting
    unknown-update noise.
    """
    update_type = update.get("sessionUpdate")

    if update_type == "agent_message_chunk":
        content = update.get("content")
        if isinstance(content, dict) and content.get("type") == "text" and isinstance(content.get("text"), str):
            return "model_text", {"delta": content["text"]}

    if update_type == "agent_thought_chunk":
        content = update.get("content")
        if isinstance(content, dict) and content.get("type") == "text" and isinstance(content.get("text"), str):
            text = content["text"]
            if not suppress_short_thoughts:
                return "model_thinking", {"delta": text}
            # Suppress tiny thought fragments to reduce event spam; emit when
            # the chunk is meaningful or ends a sentence.
            if len(text.strip()) > 8 or any(c in text for c in ".!?"):
                return "model_thinking", {"delta": text}
            return None

    if update_type == "available_commands_update":
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
        diff_path = None
        content = update.get("content")
        if isinstance(content, list):
            for entry in content:
                if isinstance(entry, dict) and entry.get("type") == "diff" and isinstance(entry.get("path"), str):
                    # Cursor may include multiple diff entries; one bridge event uses the first as its summary.
                    diff_path = entry["path"]
                    break
        if isinstance(diff_path, str):
            command = f"{command}: {diff_path}" if isinstance(command, str) else diff_path
        status = update.get("status")
        data: dict[str, Any] = {
            "command": command,
            "status": status,
            "exit_code": 0 if status == "completed" else None,
            "tool_call_id": tool_call_id,
        }
        if isinstance(diff_path, str):
            data["path"] = diff_path
        if status == "pending":
            return "command_started", data
        if status == "in_progress":
            return "command_started", data
        if status in {"completed", "failed"}:
            if status == "failed":
                data["exit_code"] = 1
            return "command_finished", data

    if update_type == "session_info_update":
        title = update.get("title")
        if isinstance(title, str):
            return "session_info", {"title": title}
        if drop_untitled_session_info:
            # Observed cline shape: a bare {updatedAt} heartbeat — no payload the
            # bridge uses, so drop it instead of emitting unknown-update noise.
            return None

    if isinstance(update_type, str):
        return "session_update_unknown", {"sessionUpdate": update_type}

    return None
