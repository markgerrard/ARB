from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import queue
import re
import subprocess
import socket
import threading
import time
from typing import Any, Callable, Mapping

from .base import EngineError, TurnResult, engine_init_timeout
from ._stdio import resolve_child_env, scrubbed_child_env


LOGGER = logging.getLogger(__name__)
INTERPRETER_VERSION = "0.0.21"
MAX_FRAME_BYTES = 1024 * 1024
HARNESS_NAMES = frozenset({"zcode", "kimi-cli"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TOOL_METHODS = frozenset(
    {
        "tool/request",
        "tool/call",
        "item/toolCall",
        "item/toolUse",
        "shell/execute",
        "file/read",
        "file/write",
        "code/execute",
    }
)
_TRUSTED_ACK_SOURCES = frozenset({"runtime", "provider", "harness"})
_UNTRUSTED_ACK_SOURCES = frozenset({"request", "request-echo", "config", "config-echo", "echo", "prose", "model-prose"})


def decode_frame(frame: bytes, *, max_bytes: int = MAX_FRAME_BYTES) -> dict[str, Any]:
    """Decode one bounded app-server JSON frame."""
    payload = frame.rstrip(b"\r\n")
    if len(payload) > max_bytes:
        raise EngineError(f"app-server frame oversized: {len(payload)} > {max_bytes} bytes")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EngineError(f"app-server frame malformed: {exc}") from exc
    if not isinstance(value, dict):
        raise EngineError("app-server frame malformed: expected an object")
    if "error" in value:
        raise EngineError(f"app-server error frame: {value['error']}")
    return value


def _configured_binary(binary: str | Path | None) -> Path:
    configured = binary if binary is not None else os.environ.get("OI_INTERPRETER_BIN")
    if not configured:
        raise EngineError("explicit OI_INTERPRETER_BIN is required; PATH lookup is forbidden")
    path = Path(configured).expanduser()
    if not path.is_absolute():
        raise EngineError("OI_INTERPRETER_BIN must be an absolute path")
    if path.is_symlink():
        raise EngineError("OI_INTERPRETER_BIN symlink is forbidden")
    try:
        realpath = path.resolve(strict=True)
    except OSError as exc:
        raise EngineError(f"OI_INTERPRETER_BIN is not a readable file: {path}") from exc
    if realpath != path:
        raise EngineError("OI_INTERPRETER_BIN symlink is forbidden")
    if not path.is_file() or not os.access(path, os.X_OK):
        raise EngineError(f"OI_INTERPRETER_BIN is not executable: {path}")
    return path


def verify_interpreter_binary(
    binary: str | Path | None = None,
    *,
    expected_sha256: str | None = None,
    expected_version: str = INTERPRETER_VERSION,
) -> dict[str, str]:
    """Verify the externally installed executable before an app-server launch."""
    path = _configured_binary(binary)
    digest_pin = expected_sha256 or os.environ.get("OI_INTERPRETER_SHA256")
    if not digest_pin:
        raise EngineError("OI_INTERPRETER_SHA256 manifest pin is required")
    if not _SHA256_RE.fullmatch(digest_pin):
        raise EngineError("OI_INTERPRETER_SHA256 must be 64 lowercase hexadecimal characters")

    try:
        # Preflight is a real child process: must use the same scrub choke as the
        # app-server Popen so lane-writer / bus creds never inherit into --version.
        result = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
            shell=False,
            env=scrubbed_child_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EngineError(f"unable to verify interpreter version: {exc}") from exc
    version = result.stdout.strip()
    if result.returncode != 0 or version != f"interpreter {expected_version}":
        raise EngineError(
            f"Open Interpreter version mismatch: expected {expected_version}, got {version or '<empty>'}"
        )

    try:
        actual_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise EngineError(f"unable to hash Open Interpreter executable: {exc}") from exc
    if actual_sha256 != digest_pin:
        raise EngineError(
            f"Open Interpreter sha256 mismatch: expected {digest_pin}, got {actual_sha256}"
        )
    return {
        "path": str(path),
        "realpath": str(path.resolve()),
        "version": expected_version,
        "sha256": actual_sha256,
    }


class CellToolPlaneBroker:
    """Typed bridge to the cell tool plane; it never executes a host tool itself."""

    def __init__(self, handler: Callable[[dict[str, Any]], Any] | None = None, completion_provider: Callable[[], Mapping[str, Any]] | None = None, receipt_chain: Any = None, socket_gid: int | None = None) -> None:
        if handler is not None and not callable(handler):
            raise EngineError("cell tool-plane broker handler must be callable")
        if completion_provider is not None and not callable(completion_provider):
            raise EngineError("cell completion provider must be callable")
        self._handler = handler
        self._completion_provider = completion_provider
        self._receipt_chain = receipt_chain
        self.socket_gid = socket_gid

    @classmethod
    def from_git_service(cls, service: Any) -> "CellToolPlaneBroker":
        handler = getattr(service, "handle", None)
        if not callable(handler):
            raise EngineError("scored cell Git service must expose handle")
        receipt_chain = getattr(service, "receipt_chain", None)
        if receipt_chain is None:
            raise EngineError("scored cell Git service must own an authenticated receipt chain")
        provider = getattr(service, "completion_projection", None)
        if not callable(provider):
            raise EngineError("scored cell Git service must expose an authenticated completion provider")
        tool_gid = getattr(service, "tool_gid", None)
        if isinstance(tool_gid, bool) or not isinstance(tool_gid, int) or tool_gid <= 0:
            raise EngineError("scored cell Git service must expose the provisioned tool-plane GID")
        return cls(
            lambda params: handler(params, actor="tool"),
            provider,
            receipt_chain,
            socket_gid=tool_gid,
        )

    @classmethod
    def from_endpoint(
        cls,
        endpoint: str,
        *,
        socket_gid: int,
        identity: Mapping[str, str],
    ) -> "CellToolPlaneBroker":
        """Bind the engine to the distinct scored tool-broker process.

        The Git capability never enters this process.  The controller gives it to
        the tool process through an inherited descriptor; this client carries only
        bounded tool requests and the authenticated completion projection.
        """

        if not isinstance(endpoint, str) or not os.path.isabs(endpoint):
            raise EngineError("cell tool-plane endpoint must be absolute")
        if isinstance(socket_gid, bool) or not isinstance(socket_gid, int) or socket_gid <= 0:
            raise EngineError("cell tool-plane endpoint requires the provisioned GID")
        if set(identity) != {"cell_id", "attempt_id"} or not all(
            isinstance(value, str) and value for value in identity.values()
        ):
            raise EngineError("cell tool-plane endpoint identity is malformed")

        def call(op: str, payload: Mapping[str, Any] | None = None) -> Any:
            request = {"protocol": "implbench-tool-plane-v1", "op": op}
            if payload is not None:
                request["payload"] = dict(payload)
            encoded = json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
            if len(encoded) > 1024 * 1024:
                raise EngineError("cell tool-plane request exceeds its bound")
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                    connection.settimeout(30.0)
                    connection.connect(endpoint)
                    connection.sendall(encoded)
                    chunks: list[bytes] = []
                    size = 0
                    while True:
                        chunk = connection.recv(min(65536, 1024 * 1024 + 1 - size))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        size += len(chunk)
                        if size > 1024 * 1024:
                            raise EngineError("cell tool-plane response exceeds its bound")
                        if b"\n" in chunk:
                            break
                raw = b"".join(chunks)
                if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
                    raise EngineError("cell tool-plane response is not one bounded frame")
                response = json.loads(raw)
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise EngineError("cell tool-plane RPC is unavailable") from exc
            if not isinstance(response, dict) or set(response) != {"ok", "result", "error"}:
                raise EngineError("cell tool-plane response is malformed")
            if response["ok"] is not True:
                raise EngineError(str(response.get("error") or "cell tool-plane request failed"))
            return response["result"]

        ping = call("ping")
        if (
            not isinstance(ping, dict)
            or ping.get("protocol") != "implbench-tool-plane-v1"
            or ping.get("identity") != dict(identity)
            or ping.get("socket_gid") != socket_gid
            or not isinstance(ping.get("pid"), int)
            or ping["pid"] == os.getpid()
        ):
            raise EngineError("cell tool-plane process evidence is invalid")
        receipt_identity = type("RemoteReceiptIdentity", (), {"identity": dict(identity)})()
        return cls(
            lambda params: call("tool", params),
            lambda: call("completion"),
            receipt_identity,
            socket_gid=socket_gid,
        )

    @property
    def is_bound(self) -> bool:
        return self._handler is not None

    @property
    def is_authenticated(self) -> bool:
        return self.is_bound and self._receipt_chain is not None and self._completion_provider is not None

    def handle_tool_request(self, params: dict[str, Any]) -> Any:
        if not isinstance(params, dict):
            raise EngineError("cell tool-plane request must be an object")
        if self._handler is None:
            raise EngineError("cell tool-plane broker is not bound")
        return self._handler(dict(params))

    def completion_projection(self) -> dict[str, Any]:
        if self._completion_provider is None:
            raise EngineError("cell completion projection is not bound")
        value = self._completion_provider()
        if not isinstance(value, Mapping):
            raise EngineError("cell completion projection must be an object")
        return dict(value)

    def clear(self) -> None:
        """Retire all cell-scoped capabilities after a scored turn."""

        self._handler = None
        self._completion_provider = None
        self._receipt_chain = None
        self.socket_gid = None


class OpenInterpreterEngine:
    supports_thread_resume = False
    """Bounded one-turn adapter for the separately installed Interpreter binary."""

    supports_continuation = False
    consumes_role_profile = False

    def __init__(
        self,
        *,
        cwd: str,
        provider: str | None = None,
        model: str | None = None,
        harness: str | None = None,
        tool_broker: Any = None,
        binary: str | Path | None = None,
        expected_sha256: str | None = None,
        max_frame_bytes: int = MAX_FRAME_BYTES,
        process_env: Mapping[str, str] | None = None,
    ) -> None:
        if not provider or not model or not harness:
            raise EngineError("explicit provider, model, and harness are required")
        if harness not in HARNESS_NAMES:
            raise EngineError(f"harness must be one of {sorted(HARNESS_NAMES)}")
        if not isinstance(tool_broker, CellToolPlaneBroker) or not tool_broker.is_bound:
            raise EngineError("a bound typed cell tool-plane broker is required")
        self.cwd = str(Path(cwd).resolve())
        self.provider = provider
        self.model = model
        self.harness = harness
        self.tool_broker = tool_broker
        self.binary = binary
        self.expected_sha256 = expected_sha256
        self.max_frame_bytes = max_frame_bytes
        self.process_env = dict(process_env) if process_env is not None else None
        self.process: subprocess.Popen[str] | None = None
        self.reader_thread: threading.Thread | None = None
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.send_lock = threading.Lock()
        self.next_id = 1
        self.thread_id: str | None = None
        self.active_turn_id: str | None = None
        self.healthy = True
        self._interrupted = False
        self._control_ack_received = False
        self._verified_pin: dict[str, str] | None = None
        self._reasoning_effort = "medium"
        self.provenance: dict[str, Any] = {
            "engine": "openinterpreter",
            "provider": provider,
            "model": model,
            "harness": harness,
            "version": INTERPRETER_VERSION,
            "verified_via": None,
            "controls": {},
        }

    @property
    def retire_after_turn(self) -> bool:
        raw = os.environ.get("BRIDGE_INTERPRETER_RETIRE_AFTER_TURN", "1").lower()
        return raw not in {"0", "false", "no"}

    def command_args(self) -> list[str]:
        self._verified_pin = verify_interpreter_binary(
            self.binary,
            expected_sha256=self.expected_sha256,
        )
        self.provenance.update(
            {
                "version": self._verified_pin["version"],
                "sha256": self._verified_pin["sha256"],
                "binary_realpath": self._verified_pin["realpath"],
            }
        )
        return [self._verified_pin["path"], "app-server", "--listen", "stdio://"]

    def popen_kwargs(self) -> dict[str, Any]:
        return {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
            "shell": False,
            "cwd": self.cwd,
            # Always set env= through the shared choke point — never inherit the
            # daemon's full environment (scored process_env merges are scrubbed too).
            "env": resolve_child_env(self.process_env),
        }

    def start(self) -> None:
        self.process = subprocess.Popen(self.command_args(), **self.popen_kwargs())
        self.reader_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader_thread.start()
        initialize = self.request(
            "initialize",
            {
                "provider": self.provider,
                "model": self.model,
                "harness": self.harness,
                "cwd": self.cwd,
                "capabilities": {"toolBroker": True},
            },
            timeout=engine_init_timeout(),
        )
        for key in ("controlAck", "runtimeAck"):
            if key in initialize:
                self._record_control_ack(initialize[key])
        response = self.request("thread/start", {"cwd": self.cwd}, timeout=30)
        thread = response.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            raise EngineError("thread/start did not return thread.id")
        self.thread_id = thread["id"]

    def is_healthy(self) -> bool:
        return (
            self.healthy
            and self.process is not None
            and self.process.poll() is None
            and (self.reader_thread is None or self.reader_thread.is_alive())
        )

    def run_turn_with_progress(
        self,
        task: str,
        *,
        timeout: int = 3600,
        policy: str = "trusted",
        on_event: Callable[[str, dict[str, Any]], None] | None,
    ) -> TurnResult:
        del policy
        if self.thread_id is None:
            raise EngineError("app-server context is not started")
        self.healthy = False
        self._interrupted = False
        response = self.request(
            "turn/start",
            {
                "threadId": self.thread_id,
                "input": [{"type": "text", "text": task}],
                "provider": self.provider,
                "model": self.model,
                "harness": self.harness,
                "reasoning": self._reasoning_effort,
            },
            timeout=30,
        )
        turn = response.get("turn")
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str) or not turn_id:
            raise EngineError("turn/start did not return turn.id")
        self.active_turn_id = turn_id
        if on_event:
            on_event("turn_started", {"turn_id": turn_id})

        chunks: list[str] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self._get_message(timeout=max(0.1, min(2.0, deadline - time.monotonic())))
            if message is None:
                if self.process is not None and self.process.poll() is not None:
                    self.healthy = False
                    self.active_turn_id = None
                    return TurnResult(
                        ok=False,
                        result="".join(chunks).strip(),
                        error=f"interpreter process exited unexpectedly (code {self.process.returncode})",
                    )
                continue
            if "__frame_error__" in message:
                self.active_turn_id = None
                self.healthy = False
                return TurnResult(ok=False, result="".join(chunks).strip(), error=message["__frame_error__"])
            if "id" in message and isinstance(message.get("method"), str):
                self.handle_server_request(message)
                continue
            params = message.get("params")
            if not isinstance(params, dict):
                continue
            method = message.get("method")
            if method in {"item/agentMessage/delta", "message/delta"} and self._matches_turn(params, turn_id):
                delta = params.get("delta")
                if isinstance(delta, str):
                    chunks.append(delta)
                    if on_event:
                        on_event("model_text", {"delta": delta, "turn_id": turn_id})
            elif method == "control/ack":
                self._record_control_ack(params)
            elif method == "turn/completed" and self._matches_turn(params, turn_id):
                try:
                    result = self.consume_terminal(message)
                    if not self._control_ack_received:
                        raise EngineError("missing independent runtime control acknowledgement")
                except EngineError as exc:
                    self.active_turn_id = None
                    self.healthy = False
                    return TurnResult(ok=False, result="".join(chunks).strip(), error=str(exc))
                self.active_turn_id = None
                self.healthy = True
                if on_event:
                    on_event("turn_completed", {"turn_id": turn_id, "ok": True})
                return result or TurnResult(ok=True, result="".join(chunks).strip())

        self.active_turn_id = None
        self.healthy = False
        if on_event:
            on_event("turn_timeout", {"timeout": timeout})
        return TurnResult(ok=False, result="".join(chunks).strip(), error=f"turn timed out after {timeout}s")

    def run_turn(self, task: str, *, timeout: int = 3600) -> TurnResult:
        return self.run_turn_with_progress(task, timeout=timeout, policy="trusted", on_event=None)

    def consume_terminal(self, message: dict[str, Any]) -> TurnResult | None:
        method = message.get("method")
        params = message.get("params")
        if method == "control/ack":
            self._record_control_ack(params)
            return None
        if method != "turn/completed" or not isinstance(params, dict):
            return None
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else params
        status = turn.get("status")
        if status in {"errored", "failed", "error"}:
            detail = turn.get("error") or params.get("error") or "unknown interpreter turn error"
            raise EngineError(f"Open Interpreter turn failed: {detail}")
        text = turn.get("text") or turn.get("output") or ""
        return TurnResult(ok=True, result=text if isinstance(text, str) else "")

    def handle_server_request(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params")
        if method == "control/ack":
            self._record_control_ack(params if isinstance(params, dict) else {})
            return
        is_tool_method = method in _TOOL_METHODS or (
            isinstance(method, str) and method.startswith("tool/")
        )
        if not is_tool_method:
            self._send({"id": request_id, "error": {"code": -32601, "message": f"unsupported method: {method}"}})
            return
        if not isinstance(params, dict):
            self._send({"id": request_id, "error": {"code": -32602, "message": "tool params must be an object"}})
            return
        try:
            if hasattr(self.tool_broker, "handle_tool_request"):
                result = self.tool_broker.handle_tool_request({"method": method, "params": params})
            elif hasattr(self.tool_broker, "handle"):
                result = self.tool_broker.handle(params)
            elif callable(self.tool_broker):
                result = self.tool_broker(params)
            else:
                raise EngineError("injected tool broker has no request handler")
        except Exception as exc:  # broker errors are returned to the app-server, never executed here
            self._send({"id": request_id, "error": {"code": -32001, "message": str(exc)}})
            return
        self._send({"id": request_id, "result": result})

    def _record_control_ack(self, ack: Any) -> None:
        if not isinstance(ack, dict):
            raise EngineError("control acknowledgement must be an object")
        if set(ack) != {"provider", "model", "harness", "reasoning", "source"}:
            raise EngineError("control acknowledgement fields are not exact")
        source = ack.get("source")
        if source not in _TRUSTED_ACK_SOURCES or source in _UNTRUSTED_ACK_SOURCES:
            raise EngineError("control acknowledgement must come from runtime, provider, or harness")
        if (ack.get("provider"), ack.get("model"), ack.get("harness")) != (
            self.provider,
            self.model,
            self.harness,
        ):
            raise EngineError("control acknowledgement provider/model/harness mismatch")
        reasoning = ack.get("reasoning")
        if not isinstance(reasoning, dict) or set(reasoning) != {"requested", "effective", "verified_via"}:
            raise EngineError("control acknowledgement reasoning fields are not exact")
        if reasoning["requested"] != "medium" or reasoning["effective"] != "medium":
            raise EngineError("control acknowledgement reasoning must be medium")
        verified_via = reasoning["verified_via"]
        if not isinstance(verified_via, str) or not verified_via or verified_via in _UNTRUSTED_ACK_SOURCES:
            raise EngineError("control acknowledgement reasoning source is untrusted")
        self.provenance["verified_via"] = source
        self._control_ack_received = True
        self.provenance["controls"] = {
            "reasoning": dict(reasoning),
        }
        self.provenance["runtime_ack"] = {
            "provider": self.provider,
            "model": self.model,
            "harness": self.harness,
            "source": source,
        }

    def steer(self, message: str) -> str:
        del message
        raise EngineError("Open Interpreter scored turns do not support continuation or steering")

    def interrupt(self) -> str:
        if self.thread_id is None or self.active_turn_id is None:
            raise EngineError("no active interpreter turn to interrupt")
        self._send({"id": self._next_request_id(), "method": "turn/interrupt", "params": {"threadId": self.thread_id, "turnId": self.active_turn_id}})
        self._interrupted = True
        return self.active_turn_id

    def reset_context(self) -> str:
        if self.process is None:
            raise EngineError("app-server is not running")
        response = self.request("thread/start", {"cwd": self.cwd}, timeout=30)
        thread = response.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            raise EngineError("thread/start did not return thread.id")
        self.thread_id = thread["id"]
        return self.thread_id

    def set_turn_reasoning_effort(self, effort: str | None) -> None:
        if effort != "medium":
            raise EngineError("Open Interpreter scored reasoning effort must be medium")
        self._reasoning_effort = effort

    def resume_thread(self, thread_id: str) -> str:
        del thread_id
        raise EngineError("Open Interpreter does not support resume/fork warm-thread paths")

    def fork_thread(self, base_thread_id: str) -> str:
        del base_thread_id
        raise EngineError("Open Interpreter does not support resume/fork warm-thread paths")

    def stop(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        except OSError:
            self.healthy = False

    def request(self, method: str, params: dict[str, Any], *, timeout: int) -> dict[str, Any]:
        request_id = self._next_request_id()
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self._get_message(timeout=max(0.1, min(2.0, deadline - time.monotonic())))
            if message is None:
                continue
            if "__frame_error__" in message:
                raise EngineError(message["__frame_error__"])
            if "id" in message and isinstance(message.get("method"), str):
                self.handle_server_request(message)
                continue
            if message.get("id") != request_id or "method" in message:
                continue
            if "error" in message:
                raise EngineError(f"{method} failed: {message['error']}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise EngineError(f"{method} returned non-object result")
            return result
        raise EngineError(f"{method} timed out after {timeout}s")

    def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            for line in self.process.stdout:
                try:
                    self.messages.put(decode_frame(line.encode("utf-8"), max_bytes=self.max_frame_bytes))
                except EngineError as exc:
                    self.healthy = False
                    self.messages.put({"__frame_error__": str(exc)})
                    return
        except Exception as exc:  # pragma: no cover - defensive reader boundary
            self.healthy = False
            self.messages.put({"__frame_error__": f"app-server reader failed: {exc}"})

    def _get_message(self, *, timeout: float) -> dict[str, Any] | None:
        try:
            return self.messages.get(timeout=timeout)
        except queue.Empty:
            return None

    def _send(self, payload: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise EngineError("app-server is not running")
        with self.send_lock:
            try:
                self.process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
                self.process.stdin.flush()
            except OSError as exc:
                self.healthy = False
                raise EngineError(f"app-server stdin write failed: {exc}") from exc

    def _next_request_id(self) -> int:
        with self.send_lock:
            value = self.next_id
            self.next_id += 1
            return value

    @staticmethod
    def _matches_turn(params: dict[str, Any], turn_id: str) -> bool:
        turn = params.get("turn")
        return (isinstance(turn, dict) and turn.get("id") == turn_id) or params.get("turnId") == turn_id


InterpreterEngine = OpenInterpreterEngine
