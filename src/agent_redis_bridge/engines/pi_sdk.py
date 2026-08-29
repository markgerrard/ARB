"""Pi-SDK engine: wraps the Node `tools/pi-sdk-host/host.mjs` harness.

The harness drives `@earendil-works/pi-coding-agent` via its TypeScript SDK
and exposes a JSON-RPC-over-stdio protocol modelled on codex's app-server
wire shape (see codex.py). This engine is a sister to `pi_rpc.py` — both
talk to pi, but pi-sdk goes through the SDK while pi-rpc parses
`pi --mode rpc` NDJSON. The SDK path lets us drop the NDJSON parser, ANSI
banner stripping, camelCase/snake_case tool-event dedup, prompt-ack
watchdog, and the get_last_assistant_text heuristic that pi_rpc needed.

Protocol contract (mirrors tools/pi-sdk-host/host.mjs):
- `initialize` (request) at start
- `thread/start` (request) at start, returns {thread:{id}} used for all turns
- One turn per `run_turn_with_progress` call: send `turn/start`, drain
  `turn/*` notifications, return on the matching `turn/completed`.
- `turn/abort` aborts the active turn. The bridge MUST wait for the
  `turn/completed` notification (NOT just the abort response) before
  starting another turn — pi's `session.abort()` resolves when the abort
  signal is delivered, not when the in-flight prompt() has settled.
- Tool execution notifications carry SDK-provided `args` on
  `turn/toolStarted` and `result` on `turn/toolFinished`; this engine maps
  them to codex-parity `command_started`, `command_output`, and
  `command_finished` progress events for transcript capture.
- `shutdown` (request) on stop() for graceful tear-down; SIGTERM falls back.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from agent_redis_bridge.local_memory_mcp import local_memory_mcp_servers

from ._stdio import resolve_child_env, start_stderr_drain
from .base import (
    EngineError,
    ProgressCallback,
    TurnResult,
    engine_init_timeout,
    parse_tool_allowlist,
)
from .openinterpreter import CellToolPlaneBroker
from .pi_broker_mcp import PiSdkBrokerAdapter

logger = logging.getLogger(__name__)


# Path to the Node harness. Resolution order:
#   1. BRIDGE_PI_SDK_HOST env var (absolute path).
#   2. Default location relative to this file's repo root.
def _default_host_path() -> str:
    here = Path(__file__).resolve()
    # src/agent_redis_bridge/engines/pi_sdk.py → repo root is parents[3]
    return str(here.parents[3] / "tools" / "pi-sdk-host" / "host.mjs")


VALID_THINKING_LEVELS = frozenset(
    {"off", "minimal", "low", "medium", "high", "xhigh"}
)


class PiSdkEngine:
    supports_thread_resume = False
    # ENG-1 D10 tripwire: the bridge's drive_to_completion re-prompts the SAME
    # engine with no resume/fork between attempts; per-dispatch thread rotation
    # (D2) would destroy the dispatch's own context on attempt 2. Enabling
    # continuation requires resetting _thread_turns per continuation attempt.
    supports_continuation = False
    consumes_role_profile = True

    def __init__(
        self,
        *,
        cwd: str,
        model: str | None,
        pi_tools: str | None = None,
        thinking_level: str | None = None,
        append_system_prompt: str | None = None,
        tool_broker: CellToolPlaneBroker | None = None,
        scored: bool = False,
        host_script_path: str | None = None,
        node_command: str = "node",
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        process_env: Mapping[str, str] | None = None,
    ) -> None:
        self.cwd = cwd
        self.model = model
        self.tool_broker = tool_broker
        self.scored = bool(scored)
        if self.scored:
            if not isinstance(tool_broker, CellToolPlaneBroker) or not tool_broker.is_authenticated:
                raise EngineError("scored Pi SDK requires an authenticated cell broker")
            socket_gid = getattr(tool_broker, "socket_gid", None)
            if isinstance(socket_gid, bool) or not isinstance(socket_gid, int) or socket_gid <= 0:
                raise EngineError("scored Pi SDK requires the provisioned tool-plane GID")
            self.scored_broker = PiSdkBrokerAdapter(
                tool_broker,
                cwd=cwd,
                node_command=node_command,
                socket_gid=getattr(tool_broker, "socket_gid", None),
            )
        else:
            self.scored_broker = None
        self._init_timeout = engine_init_timeout()
        # Comma-separated tool list (same shape as pi_rpc's pi_tools). Empty
        # / None means "use pi's defaults", which is the FULL toolset
        # (read/bash/edit/write). For reviewer-style seats, callers set
        # BRIDGE_PI_TOOLS=read,grep,find,ls in the per-seat env file.
        #
        # Parse once into a tuple of tool names so the policy guard (in
        # run_turn_with_progress) and the thread/start payload (in start())
        # share a single source of truth — otherwise a degenerate string
        # like "," is truthy on `pi_tools` (bypasses the guard) but parses
        # to [] in start() (harness falls back to full tools). Tri-model
        # P0 (agy + opus).
        self.pi_tools = pi_tools
        # Empty parse from a supplied value means operator typo; refuse to start
        # rather than silently expand to full tools. Shared with pi_rpc/omp_acp —
        # the local copy this replaced skipped a whitespace-only value and so
        # failed OPEN (see parse_tool_allowlist).
        self._tools_list: tuple[str, ...] = parse_tool_allowlist(
            pi_tools, fallback_hint="pi's full toolset (read/bash/edit/write)"
        )
        # Validate thinking_level: pi accepts off|minimal|low|medium|high|xhigh.
        if thinking_level is None:
            thinking_level = os.environ.get("BRIDGE_PI_THINKING_LEVEL") or None
        if thinking_level and thinking_level not in VALID_THINKING_LEVELS:
            logger.warning(
                f"[pi-sdk] invalid thinking_level={thinking_level!r}; "
                f"valid: {sorted(VALID_THINKING_LEVELS)}. Ignoring.",
            )
            thinking_level = None
        self.thinking_level = thinking_level
        self.effective_reasoning_effort = thinking_level
        # appendSystemPrompt mirrors pi_rpc's role-profile injection. Resolution:
        # explicit ctor arg > BRIDGE_ROLE_PROFILE_FILE env > none. Same env var
        # as pi_rpc so the same per-seat env files work unchanged.
        if append_system_prompt is None:
            profile_path = os.environ.get("BRIDGE_ROLE_PROFILE_FILE") or None
            if profile_path:
                try:
                    with open(profile_path, encoding="utf-8") as f:
                        append_system_prompt = f.read().strip() or None
                except OSError:
                    append_system_prompt = None
        self.append_system_prompt = append_system_prompt
        self.host_script_path = host_script_path or os.environ.get(
            "BRIDGE_PI_SDK_HOST"
        ) or _default_host_path()
        self.node_command = node_command
        self.popen_factory = popen_factory
        self.process_env = dict(process_env) if process_env is not None else None

        self.process: subprocess.Popen[str] | None = None
        self.reader_thread: threading.Thread | None = None
        self.stderr_thread: threading.Thread | None = None
        self.responses: dict[int, dict[str, Any]] = {}
        self.notifications: queue.Queue[dict[str, Any]] = queue.Queue()
        self.next_id = 1
        # Split locks (agy P1.1 / opus P2-2): _stdin_lock serialises
        # stdin.write+flush AND next_id allocation (both write-side
        # operations on the same wire); _responses_lock is a brief mutex
        # around the responses dict so the reader thread isn't blocked by
        # a slow stdin.write on the main thread.
        self._stdin_lock = threading.Lock()
        self._responses_lock = threading.Lock()
        self.thread_id: str | None = None
        self._thread_params: dict[str, Any] = {}
        self.active_turn_id: str | None = None
        self.healthy = True
        # Wedge watchdog (audit PSK-3): pi_rpc aborts a turn that produces no
        # output within BRIDGE_PI_ACK_TIMEOUT; pi_sdk lacked the equivalent, so
        # an unauthenticated/misconfigured provider wedge held the pool slot
        # for the full turn timeout with only a passive stall_detected.
        try:
            self.ack_timeout = float(os.environ.get("BRIDGE_PI_ACK_TIMEOUT", "") or 30.0)
        except ValueError:
            self.ack_timeout = 30.0
        try:
            raw_retire = os.environ.get("BRIDGE_PI_RETIRE_AFTER_TURN")
            self._retire_after_turn_env = str(raw_retire).lower() not in {"0", "false"}
        except Exception:
            self._retire_after_turn_env = True
        # ENG-1 D9: process-lifetime bound for warm (retire=0) seats. 0 = unlimited.
        self._max_process_turns = int(os.environ.get("BRIDGE_PI_MAX_PROCESS_TURNS", "20"))
        # ENG-1 D2/D7 state: per-thread turn count, per-process turn count,
        # interrupt latch, affinity request, and sticky quarantine.
        self._thread_turns = 0
        self._process_turns = 0
        self._interrupted = False
        self._turn_affinity_requested = False
        self._quarantine_after_turn = False
        # Retiring pi-sdk hosts after each dispatch sheds SDK session history.
        # Explicit thread continuations against a retired host fail with the
        # existing thread-affinity-miss; long-lived seats can opt out with
        # BRIDGE_PI_RETIRE_AFTER_TURN=0.
        self._progress_seq = 0

    @property
    def retire_after_turn(self) -> bool:
        """ENG-1 D9: env flag, or the process-turns cap on warm seats. Read-only
        by design — the pool reads this dynamically at release, so a capped
        engine retires itself with zero pool changes."""
        if self._retire_after_turn_env:
            return True
        cap = self._max_process_turns
        return cap > 0 and self._process_turns >= cap

    def _next_progress_seq(self) -> int:
        self._progress_seq += 1
        return self._progress_seq

    @staticmethod
    def _json_compact(value: Any) -> str:
        try:
            return json.dumps(value, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return str(value)

    @classmethod
    def _tool_command(cls, tool_name: Any, args: Any) -> str:
        name = tool_name if isinstance(tool_name, str) and tool_name else "<unknown>"
        if isinstance(args, dict):
            command = args.get("command")
            if isinstance(command, str) and command:
                return command
            if args:
                return f"{name} {cls._json_compact(args)}"
        elif args not in (None, ""):
            return f"{name} {cls._json_compact(args)}"
        return name

    @classmethod
    def _tool_result_text(cls, result: Any) -> str:
        if result is None:
            return ""
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            for key in ("content", "output", "stdout", "stderr", "text", "message"):
                value = result.get(key)
                if isinstance(value, str) and value:
                    return value
                if isinstance(value, list):
                    text = cls._tool_result_text(value)
                    if text:
                        return text
            return cls._json_compact(result)
        if isinstance(result, list):
            parts: list[str] = []
            for item in result:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if isinstance(text, str):
                        parts.append(text)
                    else:
                        parts.append(cls._json_compact(item))
                else:
                    parts.append(cls._json_compact(item))
            return "\n".join(part for part in parts if part)
        return cls._json_compact(result)

    def thread_start_params(self) -> dict[str, Any]:
        # thread/start: pi's tools list is comma-separated in our env files
        # ("read,grep,find,ls") but the SDK expects an array. None means
        # "use pi's defaults" (full toolset).
        params: dict[str, Any] = {"cwd": self.cwd}
        if self.model is not None:
            params["model"] = self.model
        if self.thinking_level is not None:
            params["thinkingLevel"] = self.thinking_level
        if self.append_system_prompt is not None:
            params["appendSystemPrompt"] = self.append_system_prompt
        if self.scored_broker is not None:
            params["tools"] = []
            params["mcpServers"] = [self.scored_broker.server_spec()]
            return params
        if self._tools_list:
            params["tools"] = list(self._tools_list)
        mcp_servers = local_memory_mcp_servers()
        if mcp_servers:
            params["mcpServers"] = [
                {**spec, "name": "arb-memory-local"} for spec in mcp_servers
            ]
        return params

    # ------------------------------------------------------------------
    # Engine protocol
    # ------------------------------------------------------------------

    def start(self, probe_timeout: int | None = None) -> None:
        probe_timeout = self._init_timeout if probe_timeout is None else probe_timeout
        self._quarantine_after_turn = False
        self._thread_turns = 0
        try:
            if self.scored_broker is not None:
                self.scored_broker.start()
            host_path = self.host_script_path
            if not Path(host_path).is_file():
                raise EngineError(
                    f"pi-sdk host script not found at {host_path}. "
                    "Set BRIDGE_PI_SDK_HOST or run tools/pi-sdk-host/install.sh."
                )
            self.process = self.popen_factory(
                self.command_args(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=self.cwd,
                # Single choke point: scrub daemon env OR scrub a scored process_env
                # merge so {**os.environ, **provider} cannot reintroduce bus/gate secrets.
                env=resolve_child_env(self.process_env),
            )
        except FileNotFoundError as exc:
            self.stop()
            raise EngineError(
                f"node executable not found (tried `{self.node_command}`). "
                "Install Node 20+ or set node_command to the absolute path."
            ) from exc
        except OSError as exc:
            self.stop()
            raise EngineError(f"pi-sdk host spawn failed: {exc}") from exc
        except Exception:
            self.stop()
            raise

        self.reader_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader_thread.start()
        # Drain pi-sdk-host's stderr so the pipe buffer never blocks the
        # subprocess on diagnostic writes. Bridge log captures host stderr
        # prefixed with `[pi-sdk-host-stderr]`.
        self.stderr_thread = start_stderr_drain(self.process, "pi-sdk-host")

        # initialize round-trip
        self.request(
            "initialize",
            {
                "clientInfo": {"name": "agent-redis-bridge", "version": "0.1.0"},
                "capabilities": {},
            },
            timeout=probe_timeout,
        )

        start_params = self.thread_start_params()
        response = self.request("thread/start", start_params, timeout=30)
        thread = response.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            raise EngineError("thread/start did not return thread.id")
        self.thread_id = thread["id"]
        effective_params = thread.get("params") if isinstance(thread, dict) else None
        self._thread_params = dict(effective_params) if isinstance(effective_params, dict) else dict(start_params)

    def _rotate_thread_if_reused(self, affinity_requested: bool) -> None:
        if self.retire_after_turn or self._thread_turns == 0 or affinity_requested:
            return
        old_thread_id = self.thread_id
        try:
            response = self.request(
                "thread/rotate",
                {
                    "threadId": old_thread_id,
                    **({"thinkingLevel": self.thinking_level} if self.thinking_level is not None else {}),
                },
                timeout=30,
            )
            thread = response.get("thread")
            thread_id = thread.get("id") if isinstance(thread, dict) else None
            if not isinstance(thread_id, str) or not thread_id:
                raise EngineError("thread/rotate did not return thread.id")
        except EngineError as exc:
            self.healthy = False
            raise EngineError(f"thread rotation failed; engine quarantined: {exc}") from exc
        self.thread_id = thread_id
        effective_params = thread.get("params") if isinstance(thread, dict) else None
        if isinstance(effective_params, dict):
            self._thread_params = dict(effective_params)
        elif self.thinking_level is not None:
            self._thread_params["thinkingLevel"] = self.thinking_level
        effective_level = self._thread_params.get("thinkingLevel")
        if isinstance(effective_level, str):
            self.thinking_level = effective_level
            self.effective_reasoning_effort = effective_level
        self._thread_turns = 0
        self._quarantine_after_turn = response.get("oldDisposed") is False
        if self._quarantine_after_turn:
            logger.warning(
                "[pi-sdk] thread rotation left old thread undisposed; "
                "quarantining engine after this turn"
            )

    def command_args(self) -> list[str]:
        return [self.node_command, self.host_script_path]

    def reset_context(self) -> str:
        if self.process is None or self.thread_id is None:
            raise EngineError("pi-sdk context is not started")
        rotate_params: dict[str, Any] = {"threadId": self.thread_id}
        if self.thinking_level is not None:
            rotate_params["thinkingLevel"] = self.thinking_level
        response = self.request("thread/rotate", rotate_params, timeout=30)
        thread = response.get("thread")
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str) or not thread_id:
            raise EngineError("thread/rotate did not return thread.id")
        self.thread_id = thread_id
        effective_params = thread.get("params") if isinstance(thread, dict) else None
        if isinstance(effective_params, dict):
            self._thread_params = dict(effective_params)
        elif self.thinking_level is not None:
            self._thread_params["thinkingLevel"] = self.thinking_level
        self.active_turn_id = None
        self._thread_turns = 0
        return thread_id

    def set_turn_reasoning_effort(self, effort: str | None) -> None:
        if effort != "medium":
            raise EngineError("pi-sdk scored reasoning effort must be medium")
        self.thinking_level = effort
        self.effective_reasoning_effort = effort

    def run_turn_with_progress(
        self,
        task: str,
        *,
        timeout: int = 3600,
        policy: str = "trusted",
        on_event: ProgressCallback | None,
    ) -> TurnResult:
        self.healthy = False
        self._interrupted = False
        affinity_requested = self._turn_affinity_requested
        self._turn_affinity_requested = False
        if self.thread_id is None:
            raise EngineError("pi-sdk thread not started")
        # Mirror pi_rpc's policy guard: a full-tools instance refuses
        # non-trusted turns. Guard on the PARSED list so a degenerate
        # config string ("," etc.) can't bypass — parse_tool_allowlist
        # it to () which trips the guard correctly. Tri-model P0 fix.
        if not self._tools_list and policy != "trusted":
            return TurnResult(
                ok=False,
                result="",
                error="non-trusted turn refused by full-tools pi-sdk instance",
            )
        self._drain_pending()
        self._rotate_thread_if_reused(affinity_requested)
        self._thread_turns += 1
        self._process_turns += 1

        try:
            response = self.request(
                "turn/start",
                {"threadId": self.thread_id, "message": task},
                timeout=30,
            )
        except EngineError as exc:
            # turn/start failed before the harness ack'd. Most likely the
            # Node process is wedged or has died — mark unhealthy so the
            # engine pool respawns rather than reusing this instance.
            # Tri-model P1 (codex + agy).
            self.healthy = False
            return TurnResult(
                ok=False,
                result="",
                error=f"pi-sdk turn/start failed: {exc}",
            )
        turn = response.get("turn")
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str):
            self.healthy = False
            raise EngineError("turn/start did not return turn.id")
        self.active_turn_id = turn_id

        if on_event is not None:
            on_event("turn_started", {"turn_id": turn_id})

        chunks: list[str] = []
        deadline = time.monotonic() + timeout
        ack_deadline = time.monotonic() + self.ack_timeout
        seen_any = False
        while time.monotonic() < deadline:
            remaining = max(0.1, min(5, deadline - time.monotonic()))
            if not seen_any:
                # Wake at the ack deadline so the watchdog fires promptly.
                remaining = max(0.1, min(remaining, ack_deadline - time.monotonic() + 0.05))
            note = self._get_notification(timeout=remaining)
            if note is None:
                if not seen_any and time.monotonic() >= ack_deadline:
                    self.healthy = False
                    self.active_turn_id = None
                    return TurnResult(
                        ok=False,
                        result="",
                        error=(
                            f"pi-sdk produced no output within {self.ack_timeout:.0f}s "
                            "of turn start (wedge); turn aborted"
                        ),
                    )
                continue
            method = note.get("method")
            params = note.get("params")
            if not isinstance(params, dict):
                continue
            if params.get("turnId") != turn_id:
                # Notification for a stale turn; pi-sdk-host should never
                # emit one because we subscribe per-turn, but be defensive.
                continue
            if method != "turn/started":
                # turn/started is emitted by host.mjs unconditionally BEFORE
                # session.prompt() — the wedge point — so it is not evidence
                # of model output and must not disarm the ack watchdog
                # (cold-Opus panel P1, 2026-07-08).
                seen_any = True

            if method == "turn/textDelta":
                delta = params.get("delta")
                # Skip empty strings — harness shouldn't emit them but a
                # future SDK version might, and a noisy log isn't worth
                # propagating zero-byte progress events.
                if isinstance(delta, str) and delta:
                    chunks.append(delta)
                    if on_event is not None:
                        on_event(
                            "model_text",
                            {
                                "delta": delta,
                                "turn_id": turn_id,
                                "item_id": f"{turn_id}:text",
                                "kind": "model_text",
                                "seq": self._next_progress_seq(),
                            },
                        )
            elif method == "turn/thinkingDelta":
                delta = params.get("delta")
                if isinstance(delta, str) and delta and on_event is not None:
                    on_event(
                        "model_thinking",
                        {
                            "delta": delta,
                            "turn_id": turn_id,
                            "item_id": f"{turn_id}:thinking",
                            "kind": "model_thinking",
                            "seq": self._next_progress_seq(),
                        },
                    )
            elif method == "turn/toolStarted":
                if on_event is not None:
                    tool_call_id = params.get("toolCallId")
                    tool_name = params.get("toolName")
                    command = self._tool_command(tool_name, params.get("args"))
                    on_event(
                        "command_started",
                        {
                            "command": command,
                            "content": command,
                            "status": "in_progress",
                            "exit_code": None,
                            "tool_call_id": tool_call_id,
                            "tool_name": tool_name,
                            "kind": "command_started",
                            "turn_id": turn_id,
                            "item_id": tool_call_id if isinstance(tool_call_id, str) else f"{turn_id}:command",
                            "seq": self._next_progress_seq(),
                        },
                    )
            elif method == "turn/toolFinished":
                is_error = bool(params.get("isError"))
                if on_event is not None:
                    tool_call_id = params.get("toolCallId")
                    tool_name = params.get("toolName")
                    command = self._tool_command(tool_name, None)
                    base_id = tool_call_id if isinstance(tool_call_id, str) else f"{turn_id}:command"
                    output = self._tool_result_text(params.get("result"))
                    if output:
                        on_event(
                            "command_output",
                            {
                                "delta": output,
                                "command": command,
                                "tool_name": tool_name,
                                "tool_call_id": tool_call_id,
                                "kind": "command_output",
                                "turn_id": turn_id,
                                "item_id": f"{base_id}:output",
                                "seq": self._next_progress_seq(),
                            },
                        )
                    on_event(
                        "command_finished",
                        {
                            "command": command,
                            "content": "failed" if is_error else "completed",
                            "status": "failed" if is_error else "completed",
                            "exit_code": 1 if is_error else 0,
                            "tool_call_id": tool_call_id,
                            "tool_name": tool_name,
                            "kind": "command_finished",
                            "turn_id": turn_id,
                            "item_id": base_id,
                            "seq": self._next_progress_seq(),
                        },
                    )
            elif method in {"turn/autoRetry", "turn/compaction"}:
                if on_event is not None:
                    payload = dict(params)
                    payload["turn_id"] = turn_id
                    payload["seq"] = self._next_progress_seq()
                    on_event(
                        "engine_retrying" if method == "turn/autoRetry" else "engine_compacting",
                        payload,
                    )
            elif method == "turn/completed":
                ok = bool(params.get("ok"))
                final_text = params.get("finalText") or ""
                # pi-sdk-host already harvested canonical finalText from
                # agent_end.messages; the chunks we collected from
                # text_delta are only a fallback when the harness never
                # received agent_end (early reject / network drop).
                result = final_text.strip() or "".join(chunks).strip()
                err = params.get("error") if not ok else None
                stop_reason = params.get("stopReason") if isinstance(params.get("stopReason"), str) else None
                tool_calls = params.get("toolCalls")
                try:
                    tool_calls_int = int(tool_calls) if tool_calls is not None else 0
                except (TypeError, ValueError):
                    tool_calls_int = 0
                self.active_turn_id = None
                if on_event is not None:
                    on_event("turn_completed", {"turn_id": turn_id, "ok": ok})
                clean_terminal = (
                    ok  # redundant today (completeTurn: error-in-params iff not-ok) — final-review defense-in-depth, 2 seats
                    and stop_reason in {"stop", "toolUse"}
                    and "error" not in params
                    and not self._interrupted
                    and not self._quarantine_after_turn
                )
                if clean_terminal:
                    self.healthy = True
                else:
                    logger.warning(
                        "[pi-sdk] non-clean terminal stopReason=%r interrupted=%s "
                        "quarantine=%s error_field=%s",
                        stop_reason,
                        self._interrupted,
                        self._quarantine_after_turn,
                        "error" in params,
                    )
                if not ok:
                    return TurnResult(
                        ok=False,
                        result=result,
                        error=str(err or "turn failed"),
                        stop_reason=stop_reason,
                        tool_calls=tool_calls_int,
                    )
                return TurnResult(
                    ok=True,
                    result=result or f"pi-sdk turn {turn_id} completed.",
                    stop_reason=stop_reason,
                    tool_calls=tool_calls_int,
                )
            # turn/started is decorative; ignore.

        # Timeout: ask harness to abort and wait briefly for the abort to
        # land, but don't block the bridge longer than needed. Mark
        # unhealthy so the engine pool respawns the harness.
        if on_event is not None:
            on_event("turn_timeout", {"timeout": timeout})
        try:
            self.send_request_no_wait("turn/abort", {"threadId": self.thread_id})
        except EngineError:
            pass
        # Drain remaining notifications best-effort so a delayed
        # turn/completed doesn't leak into a subsequent turn.
        abort_deadline = time.monotonic() + 5
        while time.monotonic() < abort_deadline:
            note = self._get_notification(timeout=1.0)
            if note is None:
                continue
            if (
                note.get("method") == "turn/completed"
                and isinstance(note.get("params"), dict)
                and note["params"].get("turnId") == turn_id
            ):
                break
        self.healthy = False
        self.active_turn_id = None
        return TurnResult(
            ok=False,
            result="".join(chunks).strip(),
            error=f"turn timed out after {timeout}s",
        )

    def steer(self, message: str) -> str:
        # The pi SDK supports session.steer() but host.mjs does not expose it
        # yet. Pre-fix this returned the turn id WITHOUT sending anything, so
        # the bridge emitted steer_sent for a message the model never saw —
        # and the old comment claimed pi_rpc does the same, which is false
        # (pi_rpc._send's steer actually goes over the wire). Raise so the
        # bridge reports steer_failed until real steer support exists
        # (audit PSK-2, panel-confirmed).
        raise EngineError("pi-sdk does not support mid-turn steer")

    def interrupt(self) -> str:
        if self.thread_id is None or self.active_turn_id is None:
            raise EngineError("no active turn to interrupt")
        self.send_request_no_wait("turn/abort", {"threadId": self.thread_id})
        self._interrupted = True
        return self.active_turn_id

    def set_turn_thread_affinity(self, requested: bool) -> None:
        self._turn_affinity_requested = bool(requested)

    def stop(self) -> None:
        process = self.process
        try:
            if process is None:
                return
            # Try graceful shutdown first; codex-style SIGTERM/SIGKILL fallback
            # if the harness doesn't exit on its own.
            try:
                self.send_request_no_wait("shutdown", {})
            except (EngineError, OSError, ValueError):
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        finally:
            if self.scored_broker is not None:
                self.scored_broker.stop()

    @property
    def scored_broker_identity(self) -> dict[str, str] | None:
        if self.tool_broker is None:
            return None
        receipt_chain = getattr(self.tool_broker, "_receipt_chain", None)
        identity = getattr(receipt_chain, "identity", None)
        return dict(identity) if isinstance(identity, dict) else None

    def is_healthy(self) -> bool:
        # Treat a Popen with a returncode set as unhealthy so the engine
        # pool respawns rather than re-using a dead harness. The
        # self.healthy flag covers our own diagnoses (timeout, bad
        # response shape, turn/start failure); poll() catches the case
        # where the Node process crashed silently between turns.
        if not self.healthy:
            return False
        if self.process is None:
            return False
        return self.process.poll() is None

    # ------------------------------------------------------------------
    # JSON-RPC plumbing
    # ------------------------------------------------------------------

    def request(self, method: str, params: dict[str, Any], *, timeout: int) -> dict[str, Any]:
        request_id = self._next_request_id()
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._responses_lock:
                resp = self.responses.pop(request_id, None)
            if resp is not None:
                if "error" in resp:
                    raise EngineError(f"{method} failed: {resp['error']}")
                result = resp.get("result")
                if not isinstance(result, dict):
                    raise EngineError(f"{method} returned non-object result")
                return result
            # Detect process death promptly so request() doesn't block
            # the full timeout when the harness has crashed (codex P1.4).
            if self.process is not None and self.process.poll() is not None:
                self.healthy = False
                raise EngineError(
                    f"pi-sdk-host exited (returncode={self.process.returncode}) "
                    f"while waiting for {method} response"
                )
            time.sleep(0.05)
        raise EngineError(f"{method} timed out after {timeout}s")

    def send_request_no_wait(self, method: str, params: dict[str, Any]) -> int:
        request_id = self._next_request_id()
        self._send({"id": request_id, "method": method, "params": params})
        return request_id

    def _send(self, payload: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise EngineError("pi-sdk-host process is not running")
        line = json.dumps(payload, separators=(",", ":")) + "\n"
        with self._stdin_lock:
            self.process.stdin.write(line)
            self.process.stdin.flush()

    def _next_request_id(self) -> int:
        with self._stdin_lock:
            request_id = self.next_id
            self.next_id += 1
            return request_id

    def _drain_pending(self) -> None:
        # Drop any leftover notifications AND any stale responses (e.g.
        # the response to a previous turn's send_request_no_wait("turn/abort")
        # that arrived after request() returned). Without this the responses
        # dict grows by one per timeout/abort across the engine's lifetime
        # (codex P2.2 / agy P1.3 / opus P1-5).
        while True:
            try:
                self.notifications.get_nowait()
            except queue.Empty:
                break
        with self._responses_lock:
            self.responses.clear()

    def _get_notification(self, *, timeout: float) -> dict[str, Any] | None:
        try:
            return self.notifications.get(timeout=timeout)
        except queue.Empty:
            return None

    def _read_stdout(self) -> None:
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
            if not isinstance(value, dict):
                continue
            if "id" in value and ("result" in value or "error" in value):
                # CPython dict __setitem__ is atomic, but we still take a
                # brief lock so request() doesn't see a torn read between
                # `responses.pop()` and a concurrent write of an unrelated
                # id. Locking only here (not on stdin writes) avoids the
                # deadlock risk agy P1.1 flagged.
                with self._responses_lock:
                    self.responses[value["id"]] = value
            elif "method" in value:
                self.notifications.put(value)
            # Anything else (no id, no method) is undefined — drop.
