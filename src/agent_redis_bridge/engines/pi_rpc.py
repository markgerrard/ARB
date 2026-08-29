from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
from typing import Any, Callable

from ._stdio import scrubbed_child_env, start_stderr_drain
from .base import EngineError, ProgressCallback, TurnResult, parse_tool_allowlist

logger = logging.getLogger(__name__)


# Pi may emit a human-readable preamble (ANSI-coloured banner, version
# string, motd) on stdout before the NDJSON stream starts. Strip ANSI
# escapes so the preserved prelude text is grep-friendly. Pattern
# mirrors the one in svkozak/pi-acp `src/pi-rpc/process.ts` (the
# upstream ACP adapter for Zed) — empirically vetted across pi releases.
_ANSI_ESCAPE = re.compile(
    r"[\x1b\x9b][\[\]()#;?]*"
    r"(?:[0-9]{1,4}(?:;[0-9]{0,4})*)?"
    r"[0-9A-ORZcf-nqry=><]"
)


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE.sub("", text)


VALID_THINKING_LEVELS = frozenset(
    {"off", "minimal", "low", "medium", "high", "xhigh"}
)


def normalize_pi_event(event: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    event_type = event.get("type")
    if event_type == "message_update":
        update = event.get("assistantMessageEvent")
        if isinstance(update, dict):
            inner_type = update.get("type")
            if inner_type == "text_delta":
                delta = update.get("delta")
                if isinstance(delta, str):
                    return ("model_text", {"delta": delta})
            if inner_type == "thinking_delta":
                # Pi's extended-thinking output (reasoning models like
                # MiniMax-M3, k2.6). Emit as `model_thinking` so the
                # bridge heartbeat fires — without this, reasoning-heavy
                # prompts look frozen at "starting" while the agent is
                # actively thinking. Verified empirically 2026-06-04
                # (4-min reviewer.md role-profile caused minimax to do
                # so many thinking_delta events that the bridge never
                # saw a `model_text` and status updated_at stayed
                # frozen for 8+ min).
                delta = update.get("delta")
                if isinstance(delta, str):
                    return ("model_thinking", {"delta": delta})
            if inner_type == "toolcall_start":
                # camelCase tool-call shapes emitted by pi 0.78+ (observed
                # specifically on minimax/MiniMax-M3). The tool metadata
                # lives inside the partial assistant message at
                # `content[contentIndex]` — reachable via the `message`
                # field or `partial` in the assistantMessageEvent.
                content_index = update.get("contentIndex")
                tool_call = None
                msg = event.get("message")
                if isinstance(msg, dict) and isinstance(content_index, int):
                    content = msg.get("content")
                    if isinstance(content, list) and 0 <= content_index < len(content):
                        tool_call = content[content_index]
                if tool_call is None:
                    partial = update.get("partial")
                    if isinstance(partial, dict) and isinstance(content_index, int):
                        content = partial.get("content")
                        if isinstance(content, list) and 0 <= content_index < len(content):
                            tool_call = content[content_index]
                if isinstance(tool_call, dict) and tool_call.get("type") == "toolCall":
                    tool_name = tool_call.get("name")
                    tool_call_id = tool_call.get("id")
                    args = tool_call.get("arguments") or {}
                    command = args.get("command") if isinstance(args.get("command"), str) else tool_name
                    return (
                        "command_started",
                        {
                            "command": command,
                            "status": "in_progress",
                            "exit_code": None,
                            "tool_call_id": tool_call_id,
                            "kind": tool_name,
                        },
                    )
            if inner_type == "toolcall_end":
                return None
    if event_type == "tool_execution_start":
        args = event.get("args")
        if not isinstance(args, dict):
            args = {}
        tool_name = event.get("toolName")
        command = args.get("command") if isinstance(args.get("command"), str) else tool_name
        return (
            "command_started",
            {
                "command": command,
                "status": "in_progress",
                "exit_code": None,
                "tool_call_id": event.get("toolCallId"),
                "kind": tool_name,
            },
        )
    if event_type == "tool_execution_end":
        is_error = bool(event.get("isError"))
        tool_name = event.get("toolName")
        return (
            "command_finished",
            {
                "command": tool_name,
                "status": "failed" if is_error else "completed",
                "exit_code": 1 if is_error else 0,
                "tool_call_id": event.get("toolCallId"),
                "kind": tool_name,
            },
        )
    return None


class PiRpcEngine:
    supports_thread_resume = False
    consumes_role_profile = True

    def __init__(
        self,
        *,
        cwd: str,
        model: str | None,
        pi_tools: str | None = None,
        command: str = "pi",
        role_profile_path: str | None = None,
        thinking_level: str | None = None,
        popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> None:
        self.cwd = cwd
        self.model = model
        self.pi_tools = pi_tools
        # pi-rpc previously kept ONLY the raw string: a degenerate value like
        # "   " is truthy, so it passed `--tools "   "` to pi AND satisfied the
        # `if not self.pi_tools` policy guard below — i.e. the seat looked
        # tool-restricted while pi fell back to its full toolset. Parse once,
        # refuse a degenerate value, and guard on the PARSED list (pi_sdk's
        # Tri-model P0 fix, which pi-rpc never received).
        self._tools_list: tuple[str, ...] = parse_tool_allowlist(
            pi_tools, fallback_hint="pi's full toolset (read/bash/edit/write)"
        )
        self.command = command
        # Pi's default system prompt is lean by design (the agent is a
        # neutral RPC tool). When dispatching for review work, pi-rpc
        # was empirically observed 2026-06-04 to soften severity labels
        # (`SHIP_WITH_NITS` vs `FIX_BEFORE_MERGE` for the same MiniMax-M3
        # model under mini-agent's richer harness). The role-profile
        # mechanism lets the operator append a reviewer-mode (or any
        # role-mode) system prompt via pi's native `--append-system-prompt`
        # flag, leveling the harness gap without changing the underlying
        # model. Resolution order: explicit constructor arg > env var
        # BRIDGE_ROLE_PROFILE_FILE > none.
        if role_profile_path is None:
            role_profile_path = os.environ.get("BRIDGE_ROLE_PROFILE_FILE") or None
        self.role_profile_path = role_profile_path
        self.role_profile_text: str | None = None
        if role_profile_path:
            try:
                with open(role_profile_path, encoding="utf-8") as f:
                    self.role_profile_text = f.read().strip() or None
            except OSError:
                # Silent fail — operator can grep stderr for the pi-stderr
                # absence of role-profile injection if expected.
                self.role_profile_text = None
        # Per-bridge thinking level. Pi accepts off/minimal/low/medium/high/xhigh
        # via the `set_thinking_level` RPC command (verified against
        # svkozak/pi-acp's pi-rpc command catalogue 2026-06-04). Higher
        # levels = more reasoning per turn; lower = faster/cheaper but
        # softer calibration. Resolution: constructor arg > env var
        # BRIDGE_PI_THINKING_LEVEL > pi's default (whatever the model
        # negotiates from get_state). Invalid values silently fall to None.
        if thinking_level is None:
            thinking_level = os.environ.get("BRIDGE_PI_THINKING_LEVEL") or None
        if thinking_level and thinking_level not in VALID_THINKING_LEVELS:
            logger.warning(
                f"[pi-rpc] invalid thinking_level={thinking_level!r}; "
                f"valid: {sorted(VALID_THINKING_LEVELS)}. Ignoring.",
            )
            thinking_level = None
        self.thinking_level = thinking_level
        self.popen_factory = popen_factory
        self.process: subprocess.Popen[bytes] | None = None
        self.reader_thread: threading.Thread | None = None
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        # Human-readable preamble (banner, version, motd) pi may emit on
        # stdout before NDJSON starts. Captured here for operator
        # visibility; not surfaced as protocol events.
        self.prelude_lines: list[str] = []
        self.next_id = 1
        self.send_lock = threading.Lock()
        self.active_prompt_id: int | str | None = None
        self.healthy = True
        # Prompt-ack watchdog: pi must emit *some* output (the `response`
        # ack, at minimum) shortly after a prompt is sent. A wedge (e.g. the
        # 2026-06-04 minimax-under-daemon hang: pi idle in kevent, no TCP, no
        # ack) otherwise looks like an infinite turn — the loop would wait the
        # full turn timeout (up to 5400s). If nothing arrives within this
        # window we abort and fail the turn fast, marking the engine unhealthy
        # so the pool respawns it. Override with BRIDGE_PI_ACK_TIMEOUT.
        try:
            self.ack_timeout = float(os.environ.get("BRIDGE_PI_ACK_TIMEOUT", "") or 30.0)
        except ValueError:
            self.ack_timeout = 30.0
        # Deduplicate tool-call events when both camelCase (toolcall_*)
        # and snake_case (tool_execution_*) shapes arrive for the same id.
        self._seen_tool_starts: set[str] = set()

    def start(self, probe_timeout: int = 10) -> None:
        # Surface spawn-time failures with operator-actionable messages
        # instead of opaque "failed readiness probe". The two common
        # cases are ENOENT (pi not installed / not on PATH) and EACCES
        # (binary present but not executable). Other spawn errors are
        # wrapped but pass through their underlying message.
        try:
            self.process = self.popen_factory(
                self.command_args(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                bufsize=-1,
                cwd=self.cwd,
                env=scrubbed_child_env(),
            )
        except FileNotFoundError as exc:
            raise EngineError(
                f"pi executable not found (tried `{self.command}`). "
                "Install via `npm install -g @earendil-works/pi-coding-agent` "
                "or set `command` to the absolute path."
            ) from exc
        except PermissionError as exc:
            raise EngineError(
                f"pi executable found but not executable (tried `{self.command}`). "
                "Check file permissions or PATH."
            ) from exc
        except OSError as exc:
            raise EngineError(f"pi spawn failed: {exc}") from exc
        self.reader_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader_thread.start()
        # Drain pi's stderr so the pipe buffer (~16-64 KB) never blocks
        # the subprocess on diagnostic / progress writes. Bridge log
        # captures pi's stderr prefixed with `[pi-stderr]`.
        self.stderr_thread = start_stderr_drain(self.process, "pi")
        probe_id = self._next_request_id()
        self._send({"id": probe_id, "type": "get_state"})
        deadline = time.monotonic() + probe_timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise EngineError("pi --mode rpc failed readiness probe")
            message = self._get_message(timeout=max(0.1, min(1, deadline - time.monotonic())))
            if message is None:
                continue
            if message.get("type") == "response" and message.get("id") == probe_id:
                if message.get("success") is False:
                    raise EngineError("pi --mode rpc failed readiness probe")
                # Apply per-bridge thinking level once the session is live.
                # Best-effort; if pi rejects the call we don't fail the
                # whole start() — operator sees a [pi-rpc] warning and
                # the engine continues with pi's default.
                if self.thinking_level:
                    self._apply_thinking_level(self.thinking_level, probe_timeout=5)
                return
        raise EngineError("pi --mode rpc failed readiness probe")

    def _apply_thinking_level(self, level: str, *, probe_timeout: int) -> None:
        """Best-effort `set_thinking_level` after the start probe.

        Pi accepts the command per svkozak/pi-acp's command catalogue;
        we don't propagate failures because thinking level is a
        calibration knob, not a correctness invariant.
        """
        request_id = self._next_request_id()
        self._send(
            {"id": request_id, "type": "set_thinking_level", "level": level}
        )
        deadline = time.monotonic() + probe_timeout
        while time.monotonic() < deadline:
            message = self._get_message(
                timeout=max(0.1, min(1, deadline - time.monotonic()))
            )
            if message is None:
                continue
            if (
                message.get("type") == "response"
                and message.get("id") == request_id
            ):
                if message.get("success") is False:
                    logger.warning(
                        f"[pi-rpc] set_thinking_level={level!r} rejected by pi: "
                        f"{message.get('error')}. Continuing with default.",
                    )
                return
        # Timed out waiting for response — don't fail, just warn.
        logger.warning(
            f"[pi-rpc] set_thinking_level={level!r} response timeout "
            f"({probe_timeout}s); continuing with whatever pi did.",
        )

    def command_args(self) -> list[str]:
        # `--no-themes`: themes are irrelevant in rpc mode and can be
        # noisy/slow to load (per svkozak/pi-acp's launch args
        # 2026-06-04). Pure speed/robustness win.
        args = [self.command, "--mode", "rpc", "--no-session", "--no-themes"]
        if self._tools_list:
            args.extend(["--tools", ",".join(self._tools_list)])
        if self.model:
            args.extend(["--model", self.model])
        if self.role_profile_text:
            args.extend(["--append-system-prompt", self.role_profile_text])
        return args

    def steer(self, message: str) -> str:
        steer_id = self._next_request_id()
        self._send({"id": steer_id, "type": "steer", "message": message})
        return str(steer_id)

    def interrupt(self) -> str:
        self._send({"type": "abort"})
        return str(self.active_prompt_id or "pi-rpc")

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

    def run_turn_with_progress(
        self,
        task: str,
        *,
        timeout: int = 3600,
        policy: str = "trusted",
        on_event: ProgressCallback | None,
    ) -> TurnResult:
        self._drain()
        self._seen_tool_starts.clear()
        if not self._tools_list and policy != "trusted":
            return TurnResult(ok=False, result="", error="non-trusted turn refused by full-tools pi instance")
        prompt_id = self._next_request_id()
        self.active_prompt_id = prompt_id
        self._send({"id": prompt_id, "type": "prompt", "message": task})
        if on_event is not None:
            on_event("turn_started", {"turn_id": str(prompt_id)})

        chunks: list[str] = []
        prompt_sent = time.monotonic()
        ack_deadline = prompt_sent + self.ack_timeout
        seen_any = False
        deadline = prompt_sent + timeout
        while time.monotonic() < deadline:
            # Until pi produces its first output, poll in slices no longer than
            # the remaining ack window so the watchdog can fire promptly.
            poll = min(5.0, deadline - time.monotonic())
            if not seen_any:
                poll = min(poll, ack_deadline - time.monotonic())
            message = self._get_message(timeout=max(0.05, poll))
            if message is None:
                if not seen_any and time.monotonic() >= ack_deadline:
                    # Pi never acknowledged the prompt — treat as a wedge.
                    # Abort (best-effort) and fail fast rather than blocking the
                    # whole turn timeout; mark unhealthy so the pool respawns.
                    if on_event is not None:
                        on_event("turn_timeout", {"timeout": self.ack_timeout, "reason": "no_ack"})
                    try:
                        self._send({"type": "abort"})
                    finally:
                        self.healthy = False
                    self.active_prompt_id = None
                    return TurnResult(
                        ok=False,
                        result="".join(chunks).strip(),
                        error=f"pi did not acknowledge prompt within {self.ack_timeout:.0f}s (wedge); turn aborted",
                    )
                continue
            seen_any = True
            if message.get("type") == "response" and message.get("id") == prompt_id:
                if message.get("success") is False:
                    self.active_prompt_id = None
                    return TurnResult(
                        ok=False,
                        result="".join(chunks).strip(),
                        error=str(message.get("error") or "prompt rejected"),
                    )
                continue
            if message.get("type") == "agent_end":
                text = self._fetch_last_text(deadline)
                self.active_prompt_id = None
                if on_event is not None:
                    on_event("turn_completed", {"ok": True, "kind": "turn_completed"})
                result = text.strip() or "".join(chunks).strip() or f"pi-rpc prompt {prompt_id} completed."
                return TurnResult(ok=True, result=result)
            error = self._turn_ending_error(message)
            if error is not None:
                self.active_prompt_id = None
                return TurnResult(ok=False, result="".join(chunks).strip(), error=error)
            self._handle_client_message(message, on_event=on_event, chunks=chunks)

        if on_event is not None:
            on_event("turn_timeout", {"timeout": timeout})
        try:
            self._send({"type": "abort"})
        finally:
            self.healthy = False
        self.active_prompt_id = None
        return TurnResult(ok=False, result="".join(chunks).strip(), error=f"turn timed out after {timeout}s")

    def is_healthy(self) -> bool:
        return self.healthy

    def _drain(self) -> None:
        while True:
            try:
                self.messages.get_nowait()
            except queue.Empty:
                return

    def _fetch_last_text(self, deadline: float) -> str:
        request_id = self._next_request_id()
        self._send({"id": request_id, "type": "get_last_assistant_text"})
        while time.monotonic() < deadline:
            message = self._get_message(timeout=max(0.1, min(2, deadline - time.monotonic())))
            if message is None:
                continue
            if message.get("type") == "response" and message.get("id") == request_id:
                data = message.get("data")
                if isinstance(data, dict) and isinstance(data.get("text"), str) and data["text"]:
                    return data["text"]
        return ""

    def _handle_client_message(
        self,
        message: dict[str, Any],
        *,
        on_event: ProgressCallback | None = None,
        chunks: list[str] | None = None,
    ) -> None:
        if message.get("type") == "extension_ui_request":
            self._handle_extension_ui_request(message)
            return
        event = normalize_pi_event(message)
        if event is None:
            return
        event_name, data = event
        if event_name == "command_started":
            tool_call_id = data.get("tool_call_id")
            if isinstance(tool_call_id, str) and tool_call_id in self._seen_tool_starts:
                return
            if isinstance(tool_call_id, str):
                self._seen_tool_starts.add(tool_call_id)
        if event_name == "model_text":
            delta = data.get("delta")
            if isinstance(delta, str):
                if chunks is not None:
                    chunks.append(delta)
        if on_event is not None:
            on_event(event_name, data)

    def _handle_extension_ui_request(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        request_id = message.get("id")
        if method == "confirm":
            self._send({"type": "extension_ui_response", "id": request_id, "confirmed": False})
        elif method in {"select", "input", "editor"}:
            self._send({"type": "extension_ui_response", "id": request_id, "cancelled": True})

    def _turn_ending_error(self, message: dict[str, Any]) -> str | None:
        if message.get("type") == "message_update":
            update = message.get("assistantMessageEvent")
            if isinstance(update, dict) and update.get("type") == "error":
                return str(update.get("reason") or "message error")
        if message.get("type") == "auto_retry_end" and message.get("success") is False:
            return str(message.get("finalError") or "auto retry failed")
        return None

    def _send(self, payload: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise EngineError("pi-rpc process is not running")
        line = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        # Bypass Python's BufferedWriter via raw os.write() on the
        # underlying file descriptor when available. One kernel syscall,
        # no Python IO layer that could potentially deadlock with pi's
        # stdin reader. Verified equivalent to .write/.flush during
        # 2026-06-04 wedge investigation; kept as defensive minimum.
        # Falls back to .write/.flush for test doubles and platforms
        # where fileno() is unavailable.
        try:
            fd = self.process.stdin.fileno()
        except (AttributeError, OSError, ValueError):
            fd = -1
        with self.send_lock:
            if fd >= 0:
                remaining = line
                while remaining:
                    n = os.write(fd, remaining)
                    if n <= 0:
                        break
                    remaining = remaining[n:]
            else:
                self.process.stdin.write(line)
                self.process.stdin.flush()

    def _next_request_id(self) -> int:
        with self.send_lock:
            request_id = self.next_id
            self.next_id += 1
            return request_id

    def _read_stdout(self) -> None:
        assert self.process is not None
        assert self.process.stdout is not None
        # NDJSON-stream-only mode begins after pi's initial banner.
        # Until we've seen our first valid JSON message, treat
        # parse-failures as preamble lines (ANSI-stripped + preserved on
        # `self.prelude_lines` for operator diagnosis). After NDJSON has
        # started, unparseable lines are ignored (probably stray writes
        # or corruption).
        ndjson_started = False
        for raw in self.process.stdout:
            raw = raw.rstrip(b"\n")
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            if not raw:
                continue
            try:
                decoded = raw.decode("utf-8", errors="replace")
            except Exception:
                continue
            try:
                value = json.loads(decoded)
            except json.JSONDecodeError:
                if not ndjson_started:
                    cleaned = _strip_ansi(decoded).rstrip()
                    if cleaned:
                        self.prelude_lines.append(cleaned)
                        logger.info(
                            f"[pi-prelude] {cleaned}"
                        )
                continue
            ndjson_started = True
            if isinstance(value, dict):
                self.messages.put(value)

    def _get_message(self, *, timeout: float) -> dict[str, Any] | None:
        try:
            return self.messages.get(timeout=timeout)
        except queue.Empty:
            return None
