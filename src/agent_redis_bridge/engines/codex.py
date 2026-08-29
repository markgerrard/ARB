from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
from typing import Any, Callable

from agent_redis_bridge.local_memory_mcp import local_memory_mcp_argv_safe_config

from ._stdio import scrubbed_child_env, start_stderr_drain
from .base import EngineError, TurnResult, engine_init_timeout


AppServerError = EngineError
CodexTurnResult = TurnResult

# Reasoning-effort levels the codex app-server accepts on thread/turn start
# (ReasoningEffort in the codex protocol). Ordered low→high for readability only.
CODEX_EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max", "ultra"})


def normalize_reasoning_effort(value: str | None) -> str | None:
    """Validate a per-dispatch reasoning-effort value; None/'' → None (use default)."""
    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    if normalized not in CODEX_EFFORT_LEVELS:
        raise ValueError(
            f"invalid reasoning effort {value!r}; expected one of {sorted(CODEX_EFFORT_LEVELS)}"
        )
    return normalized

LOGGER = logging.getLogger(__name__)

# Wire shapes pinned by the V1 probe (docs/superpowers/probes/2026-07-08-cdx1-v1-probe/):
# the current method carries availableDecisions ("accept"/"cancel"); the legacy pair
# predates that and used approved/denied. An errored ask does NOT execute (probe run-B),
# so the -32601 fallback for non-approval methods is fail-closed on the real binary.
KNOWN_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "execCommandApproval",
    "applyPatchApproval",
}
LEGACY_APPROVAL_METHODS = {"execCommandApproval", "applyPatchApproval"}


def _is_approval_shaped(method: str) -> bool:
    # Rename-survivable heuristic (design v1.2 D1): correctness never depends on
    # the enumerated list being complete.
    return method in KNOWN_APPROVAL_METHODS or "approval" in method.lower()


def _pick_decision(params: Any, method: str, *, allow: bool) -> str:
    offered: list[str] = []
    if isinstance(params, dict):
        offered = [d for d in params.get("availableDecisions") or [] if isinstance(d, str)]
    if allow:
        if "accept" in offered:
            return "accept"
        return "approved" if method in LEGACY_APPROVAL_METHODS else "accept"
    if "cancel" in offered:
        return "cancel"
    return "denied" if method in LEGACY_APPROVAL_METHODS else "cancel"


class CodexEngine:
    supports_thread_resume = True
    # ENG-1 D10 tripwire: the bridge's drive_to_completion re-prompts the SAME
    # engine with no resume/fork between attempts; per-dispatch thread rotation
    # (D2) would destroy the dispatch's own context on attempt 2. Enabling
    # continuation requires resetting _thread_turns per continuation attempt.
    supports_continuation = False

    def __init__(
        self,
        *,
        cwd: str,
        model: str | None,
        approval_policy: str,
        sandbox: str,
        bypass_approvals_and_sandbox: bool = False,
        default_effort: str | None = None,
    ) -> None:
        self.cwd = cwd
        self.model = model
        self._init_timeout = engine_init_timeout()
        self.approval_policy = approval_policy
        self.sandbox = sandbox
        self.bypass_approvals_and_sandbox = bypass_approvals_and_sandbox
        # Seat default reasoning effort. codex STICKS the last effort on a warm thread,
        # so every turn must carry an explicit value — the per-dispatch override if set,
        # else this default — or a prior --effort dispatch leaks into later ones.
        # The per-turn RPC param WINS over a config.toml model_reasoning_effort, so a
        # seat pin holds regardless of host-wide config. (Corrected 2026-08-03: this
        # note previously claimed the opposite. Probed on codex-cli 0.146.0 — with
        # config.toml pinned "low", a turn/start carrying effort="xhigh" was accepted
        # and persisted as xhigh on the thread row in ~/.codex/state_5.sqlite.)
        self._default_effort = normalize_reasoning_effort(
            default_effort if default_effort is not None else os.environ.get("BRIDGE_CODEX_DEFAULT_EFFORT")
        ) or "medium"
        # CDX-1 (design v1.2 D2): per-turn approval deny budget + D2a grace window.
        self.deny_budget = int(os.environ.get("BRIDGE_APPROVAL_DENY_BUDGET", "10"))
        self.approval_grace_s = float(os.environ.get("BRIDGE_APPROVAL_GRACE_S", "10"))
        self._deny_count = 0
        self._last_denied_command: str | None = None
        self.process: subprocess.Popen[str] | None = None
        self.reader_thread: threading.Thread | None = None
        self.stderr_thread: threading.Thread | None = None
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.next_id = 1
        self.send_lock = threading.Lock()
        self.thread_id: str | None = None
        self.active_turn_id: str | None = None
        self._progress_seq = 0
        self.healthy = True
        self._reasoning_effort: str | None = None
        # Retire the engine after every dispatch so the pool never re-serves the
        # ever-growing app-server thread (one 2026-07-08 thread served 24
        # unrelated dispatches, 22M cumulative tokens — cross-dispatch
        # contamination for panel seats). Explicit thread_id /
        # fork_from_thread_id continuations survive retirement: codex resumes
        # threads from ~/.codex/sessions rollouts on a fresh process
        # (live-proven 2026-07-10). Long-lived seats opt out with
        # BRIDGE_CODEX_RETIRE_AFTER_TURN=0.
        raw_retire = os.environ.get("BRIDGE_CODEX_RETIRE_AFTER_TURN")
        self._retire_after_turn_env = str(raw_retire).lower() not in {"0", "false"}
        # ENG-1 D9: process-lifetime bound for warm (retire=0) seats. 0 = unlimited.
        self._max_process_turns = int(os.environ.get("BRIDGE_CODEX_MAX_PROCESS_TURNS", "20"))
        # ENG-1 D2/D7 state: per-thread turn count (reset on any thread install),
        # per-process turn count (never reset), interrupt latch for D7.
        self._thread_turns = 0
        self._process_turns = 0
        self._interrupted = False

    @property
    def retire_after_turn(self) -> bool:
        """ENG-1 D9: env flag, or the process-turns cap on warm seats. Read-only
        by design — the pool reads this dynamically at release, so a capped
        engine retires itself with zero pool changes."""
        if self._retire_after_turn_env:
            return True
        cap = self._max_process_turns
        return cap > 0 and self._process_turns >= cap

    def set_turn_reasoning_effort(self, effort: str | None) -> None:
        """Per-dispatch reasoning-effort override; None clears back to the seat default."""
        self._reasoning_effort = normalize_reasoning_effort(effort)

    def _effective_effort(self) -> str:
        return self._reasoning_effort or self._default_effort

    def _next_progress_seq(self) -> int:
        self._progress_seq += 1
        return self._progress_seq

    def start(self) -> None:
        self.process = subprocess.Popen(
            self.command_args(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=scrubbed_child_env(),
        )
        self.reader_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader_thread.start()
        self.stderr_thread = start_stderr_drain(self.process, "codex")

        self.request(
            "initialize",
            {
                "clientInfo": {"name": "agent-redis-bridge", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
            timeout=self._init_timeout,
        )
        self.notify("initialized", {})

        self.start_thread()

    def start_thread(self) -> str:
        response = self.request("thread/start", self.thread_start_params(), timeout=30)
        thread = response.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str) or not thread["id"]:
            raise AppServerError("thread/start did not return thread.id")
        self.thread_id = thread["id"]
        self._thread_turns = 0
        return self.thread_id

    def command_args(self) -> list[str]:
        command = ["codex"]
        if self.bypass_approvals_and_sandbox:
            command.append("--dangerously-bypass-approvals-and-sandbox")
        command.extend(["app-server", "--listen", "stdio://"])
        config_override = local_memory_mcp_codex_override()
        if config_override is not None:
            command.extend(["-c", config_override])
        if os.environ.get("AGENT_BRIDGE_CODEX_DISABLE_AUTO_MEMORY") == "1":
            # Panel-seat exclusion rule (agent-memory-seeds README): reviewer
            # seats stay COLD — the operator's interactive config may enable
            # auto_memory globally; a seat must not inherit it.
            command.extend(["-c", "features.auto_memory=false"])
        return command

    def thread_start_params(self) -> dict[str, Any]:
        sandbox = "danger-full-access" if self.bypass_approvals_and_sandbox else self.sandbox
        return {
            "cwd": self.cwd,
            "model": self.model,
            "approvalPolicy": self.approval_policy,
            "sandbox": sandbox,
            "sessionStartSource": "startup",
            "effort": self._effective_effort(),
        }

    def turn_params(self, task: str, *, policy: str) -> dict[str, Any]:
        return {
            "threadId": self.thread_id,
            "cwd": self.cwd,
            "model": self.model,
            "approvalPolicy": self.approval_policy_for_policy(policy),
            "input": [{"type": "text", "text": task}],
            "effort": self._effective_effort(),
        }

    def is_healthy(self) -> bool:
        # Without this, engine_pool.release() treated codex as unconditionally
        # healthy and recycled dead subprocesses forever (audit CDX-3).
        return (
            self.healthy
            and self.process is not None
            and self.process.poll() is None
            and (self.reader_thread is None or self.reader_thread.is_alive())
        )

    def _process_exited(self) -> bool:
        return self.process is not None and self.process.poll() is not None

    def _last_chance_message(self) -> dict[str, Any] | None:
        # The child may have flushed a final message right before exiting;
        # drain it before declaring the death, so a raced turn/completed wins.
        return self._get_message(timeout=0.05)

    def run_turn(self, task: str, *, timeout: int = 3600) -> TurnResult:
        return self.run_turn_with_progress(task, timeout=timeout, policy="trusted", on_event=None)

    def _rotate_thread_if_reused(self) -> None:
        """ENG-1 D2 (grok D3b mirror): a non-retiring engine keeps its warm
        process but NEVER reuses a thread across dispatches. thread_id flips
        only on a successful thread/start; any failure quarantines (D3)."""
        if self.retire_after_turn or self._thread_turns == 0:
            return
        old_thread = self.thread_id
        try:
            self.start_thread()
        except AppServerError as exc:
            self.healthy = False
            raise AppServerError(f"thread rotation failed; engine quarantined: {exc}") from exc
        LOGGER.info(
            f"[codex] rotated thread {old_thread!r} -> {self.thread_id!r} (fresh context per dispatch)"
        )

    def run_turn_with_progress(
        self,
        task: str,
        *,
        timeout: int = 3600,
        policy: str = "trusted",
        on_event: Callable[[str, dict[str, Any]], None] | None,
    ) -> TurnResult:
        # ENG-1 D7: affirmative health — a warm process must EARN reuse each
        # turn. True again only on a clean, uninterrupted "completed" terminal.
        self.healthy = False
        self._interrupted = False

        if self.thread_id is None:
            raise AppServerError("app-server thread not started")

        self._rotate_thread_if_reused()
        # ENG-1 R1 NORMATIVE: dirty BEFORE the send — a lost turn/start response
        # can leave a server-side-accepted turn; attempted == dirty.
        self._thread_turns += 1
        self._process_turns += 1

        response = self.request(
            "turn/start",
            self.turn_params(task, policy=policy),
            timeout=30,
        )
        turn = response.get("turn")
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str):
            raise AppServerError("turn/start did not return turn.id")

        self.active_turn_id = turn_id
        self._deny_count = 0
        self._last_denied_command = None
        if on_event is not None:
            on_event("turn_started", {"turn_id": turn_id})

        chunks: list[str] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # Cap the wait so a mid-turn child death is noticed within ~2s
            # instead of one full 5s poll (matches request()'s cadence).
            remaining = max(0.1, min(2, deadline - time.monotonic()))
            message = self._get_message(timeout=remaining)
            if message is None:
                if self._process_exited():
                    message = self._last_chance_message()
                    if message is None:
                        self.healthy = False
                        self.active_turn_id = None
                        return TurnResult(
                            ok=False,
                            result="".join(chunks).strip(),
                            error=f"codex process exited unexpectedly (code {self.process.returncode})",
                        )
                else:
                    continue

            method = message.get("method")
            # CDX-1 D1: server-initiated requests are routed BEFORE the params
            # guard below — a malformed-params ask must still be answered.
            if "id" in message and isinstance(method, str):
                self._respond_to_server_request(
                    message, policy=policy, on_event=on_event, turn_id=turn_id
                )
                if self._deny_count > self.deny_budget:
                    return self._exhaust_deny_budget(
                        deadline=deadline, turn_id=turn_id, policy=policy,
                        on_event=on_event, chunks=chunks,
                    )
                continue

            params = message.get("params")
            if not isinstance(params, dict):
                continue

            if method == "item/agentMessage/delta" and params.get("turnId") == turn_id:
                delta = params.get("delta")
                if isinstance(delta, str):
                    chunks.append(delta)
                    if on_event is not None:
                        item_id = params.get("itemId")
                        on_event(
                            "model_text",
                            {
                                "delta": delta,
                                "turn_id": turn_id,
                                "item_id": item_id if isinstance(item_id, str) else f"{turn_id}:text",
                                "kind": "model_text",
                                "seq": self._next_progress_seq(),
                            },
                        )
            elif method == "item/completed" and params.get("turnId") == turn_id:
                item = params.get("item")
                if isinstance(item, dict) and item.get("type") == "agentMessage":
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        chunks = [text]
                elif isinstance(item, dict) and item.get("type") == "commandExecution":
                    if on_event is not None:
                        item_id = item.get("id") or params.get("itemId")
                        on_event(
                            "command_finished",
                            {
                                "command": item.get("command"),
                                "status": item.get("status"),
                                "exit_code": item.get("exitCode"),
                                "duration_ms": item.get("durationMs"),
                                "turn_id": turn_id,
                                "item_id": item_id if isinstance(item_id, str) else f"{turn_id}:command",
                                "kind": "command_finished",
                                "seq": self._next_progress_seq(),
                            },
                        )
                        # Codex delivers command output in the completed item's
                        # `aggregatedOutput` (it does NOT stream `outputDelta`), so emit
                        # it here as command_output under a distinct `:output` item id so
                        # it renders as its own ⎿ line instead of coalescing into the command.
                        output = item.get("aggregatedOutput")
                        if isinstance(output, str) and output.strip():
                            base_id = item_id if isinstance(item_id, str) else f"{turn_id}:command"
                            on_event(
                                "command_output",
                                {
                                    "delta": output,
                                    "turn_id": turn_id,
                                    "item_id": f"{base_id}:output",
                                    "kind": "command_output",
                                    "seq": self._next_progress_seq(),
                                },
                            )
            elif method == "item/started" and params.get("turnId") == turn_id:
                item = params.get("item")
                if isinstance(item, dict) and item.get("type") == "commandExecution":
                    if on_event is not None:
                        item_id = item.get("id") or params.get("itemId")
                        on_event(
                            "command_started",
                            {
                                "command": item.get("command"),
                                "turn_id": turn_id,
                                "item_id": item_id if isinstance(item_id, str) else f"{turn_id}:command",
                                "kind": "command_started",
                                "seq": self._next_progress_seq(),
                            },
                        )
            elif method == "item/commandExecution/outputDelta" and params.get("turnId") == turn_id:
                delta = params.get("delta")
                if isinstance(delta, str) and on_event is not None:
                    item_id = params.get("itemId")
                    on_event(
                        "command_output",
                        {
                            "delta": delta,
                            "item_id": item_id if isinstance(item_id, str) else f"{turn_id}:command_output",
                            "turn_id": turn_id,
                            "kind": "command_output",
                            "seq": self._next_progress_seq(),
                        },
                    )
            elif method == "turn/completed" and self._turn_matches(params, turn_id):
                result = "".join(chunks).strip()
                turn_payload = params.get("turn")
                status = turn_payload.get("status") if isinstance(turn_payload, dict) else None
                ok = status not in {"errored", "failed"} if status is not None else True
                if status == "completed" and not self._interrupted:
                    # ENG-1 D7 allowlist: the ONLY place healthy flips back True.
                    self.healthy = True
                else:
                    LOGGER.warning(
                        f"[codex] non-clean terminal status={status!r} interrupted={self._interrupted} — quarantining warm process"
                    )
                if on_event is not None:
                    on_event("turn_completed", {"turn_id": turn_id, "ok": ok})
                self.active_turn_id = None
                return TurnResult(ok=ok, result=result or f"Codex turn {turn_id} completed.")

        if on_event is not None:
            on_event("turn_timeout", {"timeout": timeout})
        self.active_turn_id = None
        self.healthy = False
        return TurnResult(ok=False, result="", error=f"turn timed out after {timeout}s")

    def _respond_to_server_request(
        self,
        message: dict[str, Any],
        *,
        policy: str | None,
        on_event: Callable[[str, dict[str, Any]], None] | None,
        turn_id: str | None = None,
    ) -> None:
        """CDX-1 D1: any id+method message is a server-initiated request and is
        ALWAYS answered — deny for approval-shaped, -32601 otherwise. Never owns
        control flow (D2a.1): it counts denials and answers; callers own exits.
        policy=None means "no authorizing context" (inter-turn) => deny."""
        method = message["method"]
        request_id = message.get("id")
        params = message.get("params")

        if not _is_approval_shaped(method):
            LOGGER.warning(f"codex server request unsupported; answering -32601: {method}")
            self._send({
                "id": request_id,
                "error": {"code": -32601, "message": f"client method not supported: {method}"},
            })
            return

        if policy == "trusted":
            # Trusted drift (design v1.2 D2, operator decision 2026-07-08):
            # trusted => "never", so an ask here means config drift — allow, loudly.
            LOGGER.warning(
                f"codex approval ask on a trusted-policy turn (approvalPolicy drift?); allowing: {method}"
            )
            decision = _pick_decision(params, method, allow=True)
            self._send({"id": request_id, "result": {"decision": decision}})
            return

        command = params.get("command") if isinstance(params, dict) else None
        if policy is not None:
            # D2: the budget counts ACTIVE-TURN denials only; inter-turn denies
            # (policy=None) are timeout-bounded anomalies outside the budget.
            self._deny_count += 1
            self._last_denied_command = command if isinstance(command, str) else None
        decision = _pick_decision(params, method, allow=False)
        self._send({"id": request_id, "result": {"decision": decision}})
        if policy is None:
            LOGGER.warning(
                f"codex approval ask during inter-turn wait; denied fail-closed: {method}"
            )
        if on_event is not None:
            params_turn = params.get("turnId") if isinstance(params, dict) else None
            item_id = params.get("itemId") if isinstance(params, dict) else None
            event_turn = params_turn if isinstance(params_turn, str) else turn_id
            on_event(
                "command_denied",
                {
                    "command": command if isinstance(command, str) else None,
                    "turn_id": event_turn,
                    "item_id": item_id if isinstance(item_id, str) else f"{event_turn}:approval",
                    "kind": "command_denied",
                    "seq": self._next_progress_seq(),
                    "deny_count": self._deny_count,
                    "deny_budget": self.deny_budget,
                },
            )

    def _exhaust_deny_budget(
        self,
        *,
        deadline: float,
        turn_id: str,
        policy: str,
        on_event: Callable[[str, dict[str, Any]], None] | None,
        chunks: list[str],
    ) -> TurnResult:
        """D2a: interrupt, bounded grace drain answering per D1 throughout, then
        return legibly — healthy on observed completion, quarantined otherwise."""
        error = (
            f"approval deny budget exhausted ({self._deny_count} denials); "
            f"last: {self._last_denied_command or '<unknown command>'}"
        )
        try:
            self.interrupt()
        except AppServerError:
            pass  # a dead engine can't be interrupted; grace drain settles health below
        grace_deadline = time.monotonic() + max(
            0.0, min(self.approval_grace_s, deadline - time.monotonic())
        )
        completed = False
        while time.monotonic() < grace_deadline:
            message = self._get_message(timeout=max(0.05, min(0.5, grace_deadline - time.monotonic())))
            if message is None:
                if self._process_exited():
                    break
                continue
            if "id" in message and isinstance(message.get("method"), str):
                self._respond_to_server_request(
                    message, policy=policy, on_event=on_event, turn_id=turn_id
                )
                continue
            if message.get("method") == "turn/completed" and self._turn_matches(
                message.get("params") or {}, turn_id
            ):
                completed = True
                break
        if not completed:
            self.healthy = False  # engine_pool.release() quarantines (CDX-3)
        self.active_turn_id = None
        if on_event is not None:
            on_event("turn_completed", {"turn_id": turn_id, "ok": False})
        return TurnResult(ok=False, result="".join(chunks).strip(), error=error)

    def steer(self, message: str) -> str:
        if self.thread_id is None or self.active_turn_id is None:
            raise AppServerError("no active turn to steer")

        self.send_request_no_wait(
            "turn/steer",
            {
                "threadId": self.thread_id,
                "expectedTurnId": self.active_turn_id,
                "input": [{"type": "text", "text": message}],
            },
        )
        return self.active_turn_id

    def reset_context(self) -> str:
        return self.start_thread()

    def resume_thread(self, thread_id: str) -> str:
        self.request("thread/resume", {"threadId": thread_id}, timeout=30)
        self.thread_id = thread_id
        self._thread_turns = 0
        return self.thread_id

    def fork_thread(self, base_thread_id: str) -> str:
        response = self.request("thread/fork", {"threadId": base_thread_id}, timeout=30)
        thread = response.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str) or not thread["id"]:
            raise AppServerError("thread/fork did not return thread.id")
        self.thread_id = thread["id"]
        self._thread_turns = 0
        return self.thread_id

    def interrupt(self) -> str:
        if self.thread_id is None or self.active_turn_id is None:
            raise AppServerError("no active turn to interrupt")

        self.send_request_no_wait(
            "turn/interrupt",
            {
                "threadId": self.thread_id,
                "turnId": self.active_turn_id,
            },
        )
        self._interrupted = True
        return self.active_turn_id

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

    def request(self, method: str, params: dict[str, Any], *, timeout: int) -> dict[str, Any]:
        request_id = self._next_request_id()
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            message = self._get_message(timeout=max(0.1, min(2, deadline - time.monotonic())))
            if message is None:
                if self._process_exited():
                    message = self._last_chance_message()
                    if message is None:
                        self.healthy = False
                        raise AppServerError(
                            f"codex process exited unexpectedly (code {self.process.returncode}) during {method}"
                        )
                else:
                    continue
            # Per-side id namespaces: "method" present ⇒ server-initiated request,
            # never this call's response (same rule as the ACP engines).
            if isinstance(message.get("method"), str) and "id" in message:
                # CDX-1 D1: no per-dispatch policy in scope here — deny fail-closed.
                self._respond_to_server_request(message, policy=None, on_event=None)
                continue
            if message.get("id") != request_id or "method" in message:
                continue
            if "error" in message:
                raise AppServerError(f"{method} failed: {message['error']}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise AppServerError(f"{method} returned non-object result")
            return result

        raise AppServerError(f"{method} timed out after {timeout}s")

    def send_request_no_wait(self, method: str, params: dict[str, Any]) -> int:
        request_id = self._next_request_id()
        self._send({"id": request_id, "method": method, "params": params})
        return request_id

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"method": method, "params": params})

    def _send(self, payload: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise AppServerError("app-server is not running")

        with self.send_lock:
            try:
                self.process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
                self.process.stdin.flush()
            except OSError as exc:
                # A write to a CLOSED pipe (child crashed/exited) raises raw
                # BrokenPipeError, which handle_control's EngineError catch never
                # saw — a late steer/cancel against a dead engine crashed the
                # whole daemon (audit CDX-2).
                self.healthy = False
                raise AppServerError(f"app-server stdin write failed: {exc}") from exc

    def _next_request_id(self) -> int:
        with self.send_lock:
            request_id = self.next_id
            self.next_id += 1
            return request_id

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
                    LOGGER.warning(f"codex stdout: dropped malformed JSON line: {line[:200]!r}")
                    continue
                if isinstance(value, dict):
                    self.messages.put(value)
        except Exception:  # noqa: BLE001 - a dead reader must mark the engine, not die silently
            LOGGER.exception("codex stdout reader died; marking engine unhealthy")
            self.healthy = False

    def _get_message(self, *, timeout: float) -> dict[str, Any] | None:
        try:
            return self.messages.get(timeout=timeout)
        except queue.Empty:
            return None

    @staticmethod
    def _turn_matches(params: dict[str, Any], turn_id: str) -> bool:
        turn = params.get("turn")
        if isinstance(turn, dict) and turn.get("id") == turn_id:
            return True
        return params.get("turnId") == turn_id

    @staticmethod
    def approval_policy_for_policy(policy: str) -> str:
        if policy == "trusted":
            return "never"
        if policy == "human":
            return "on-request"
        return "on-request"


CodexAppServer = CodexEngine


def local_memory_mcp_codex_override() -> str | None:
    config = local_memory_mcp_argv_safe_config()
    if config is None:
        return None
    env = config["env"]
    env_items = ", ".join(f"{key}={_toml_string(value)}" for key, value in env.items())
    return (
        "mcp_servers.arb-memory-local="
        f"{{command={_toml_string(config['command'])}, args=[], env={{{env_items}}}}}"
    )


def _toml_string(value: object) -> str:
    return json.dumps(str(value))
