from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from typing import Any, Callable

from agent_redis_bridge.local_memory_mcp import local_memory_mcp_servers

from ._acp import _select_allow_option
from ._acp_base import HealthReportingAcpEngine
from .base import EngineError, ProgressCallback, TurnResult

logger = logging.getLogger(__name__)


class GrokAcpEngine(HealthReportingAcpEngine):
    supports_thread_resume = False
    """
    ACP client engine for Grok Build (grok agent stdio).

    Follows the same pattern as GenericAcpEngine. Launches Grok as an ACP
    server over stdio and drives it via the Agent Client Protocol.

    v1 goals:
    - Basic prompt → streaming text + tool visibility
    - Trusted / human policy mapping (yolo vs default)
    - Minimal but safe handling of Grok's x.ai/* extensions (don't crash)
    - Same normalized event surface as the rest of the bridge

    Later iterations can add deeper x.ai/* support (worktrees, fs, hunk tracker,
    rich search, session fork/rewind, etc.).

    The ACP transport comes from `_acp_base`; grok keeps its own prompt loop
    because of the D3a affirmative health marking, the D3b per-dispatch session
    rotation and the D3 deny-budget grace drain.
    """

    engine_label = "grok"
    display_name = "Grok"
    default_command = "grok"
    logger = logger

    # Grok's reader drops malformed stdout JSON silently (unlike cursor/devin/cline).
    log_malformed_stdout_json = False

    # Grok may support mid-turn steering via x.ai/ extensions or a standard method.
    # For v1 we declare it unsupported (same as Gemini).
    steer_error = "Grok ACP v1 does not support mid-prompt steer"

    def __init__(
        self,
        *,
        cwd: str,
        model: str | None,
        command: str = "grok",
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        super().__init__(cwd=cwd, model=model, command=command, popen_factory=popen_factory)
        # GROK-1 D2/D3: per-turn approval deny budget (env names shared with codex).
        self.deny_budget = int(os.environ.get("BRIDGE_APPROVAL_DENY_BUDGET", "10"))
        self.approval_grace_s = float(os.environ.get("BRIDGE_APPROVAL_GRACE_S", "10"))
        self._deny_count = 0
        self._last_denied_title: str | None = None
        # GROK-1 D3a: affirmative health. False during a turn; True ONLY after a
        # cleanly-received, non-error, uninterrupted terminal response.
        self._interrupted = False
        self._turns_served = 0
        # Retire the engine after every dispatch so the pool never re-serves the
        # accumulating ACP session (live-proven leak 2026-07-10: a self-contained
        # probe recalled a prior dispatch's review-brief title). grok-acp has no
        # session resume, so an explicit --thread-id continuation fails legibly
        # at pool-affinity acquire (thread-affinity-miss) once the session's
        # engine is retired. Long-lived seats opt out with
        # BRIDGE_GROK_RETIRE_AFTER_TURN=0 (launchd plist, not env file).
        raw_retire = os.environ.get("BRIDGE_GROK_RETIRE_AFTER_TURN")
        self.retire_after_turn = str(raw_retire).lower() not in {"0", "false"}

    def command_args(self) -> list[str]:
        # Primary way to run Grok Build as an ACP server.
        # Add --yolo or other flags here later if we decide on launch-time policy.
        return [self.command, "agent", "stdio"]

    def _start_handshake(self) -> None:
        # Grok supports the standard ACP initialize + session/new flow.
        # We declare modest client capabilities (similar to Gemini).
        # Grok will advertise a large set of x.ai/* extensions; we can ignore most for v1.
        self._initialize()
        self._new_session(timeout=30)
        self._pin_model()

    def _pin_model(self) -> None:
        """Pin the CURRENT session to self.model, or raise.

        The method is snake_case `session/set_model` (verified live 2026-08-12:
        camelCase `session/setModel` returns -32601 Method not found). A pin that
        silently no-ops is worse than a dead seat — an operator believes the seat
        is pinned when it is actually running the CLI config default. So: fail
        loudly, and verify the server echoes back the model we asked for.

        Called from EVERY path that creates a session, not just start(). The pin
        is per-session: `session/set_model` takes a sessionId, so a session
        created later carries no pin from an earlier one. `_rotate_session_if_reused`
        opens a fresh session per dispatch, so pinning only in start() would hold
        the pin for dispatch 1 and silently drop it from dispatch 2 onward — the
        same "operator believes it is pinned" failure this code exists to prevent,
        one layer down.
        """
        if not self.model:
            return
        resp = self.request(
            "session/set_model",
            {"sessionId": self.session_id, "modelId": self.model},
            timeout=self._init_timeout,
        )
        # Response shape: {"_meta": {"model": {"Ok": "<model-id>"}}}
        got = (
            (resp or {})
            .get("_meta", {})
            .get("model", {})
            .get("Ok")
        )
        if got != self.model:
            raise EngineError(
                f"grok session/set_model did not pin the requested model: "
                f"asked {self.model!r}, server reported {got!r}"
            )

    def _request_process_exit_error(self, method: str) -> EngineError:
        """Name grok's own exit code rather than the family's generic message.

        A grok CLI that dies at spawn (bad flag, expired auth) used to sit in
        `request()` for the full BRIDGE_ENGINE_INIT_TIMEOUT_S and report
        "initialize timed out after 60s" — the exact defect the ACP liveness
        backlog item was filed against. That item was closed claiming grok
        "already checks poll()"; grok checked only in its TURN loop, never in
        `request()`, so `initialize` stayed exposed. Panel finding P1-2, run
        panel-omp-opencode-arc-20260803T125825Z-570c21.
        """
        code = self.process.poll() if self.process is not None else None
        return EngineError(
            f"{method} failed: grok exited with code {code} before answering. "
            "Its own diagnostics are in the bridge log under the engine's stderr "
            "prefix — a non-zero code here is normally a spawn-flag or auth error, "
            "not a hang."
        )

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

        # D3a affirmative marking: unhealthy until this turn PROVES a clean end.
        self.healthy = False
        self._interrupted = False
        self._rotate_session_if_reused()
        self.set_session_mode_for_policy(policy)
        self._deny_count = 0
        self._last_denied_title = None
        prompt_id = self.send_request_no_wait(
            "session/prompt",
            {"sessionId": self.session_id, "prompt": [{"type": "text", "text": task}]},
        )
        self.active_prompt_id = prompt_id
        self._turns_served += 1
        if on_event is not None:
            on_event("turn_started", {"turn_id": str(prompt_id), "session_id": self.session_id})

        chunks: list[str] = []
        tool_titles: dict[str, str] = {}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self._get_message(timeout=max(0.1, min(5, deadline - time.monotonic())))
            if message is None:
                if self._process_exited():
                    self.active_prompt_id = None
                    return TurnResult(
                        ok=False,
                        result="".join(chunks).strip(),
                        error="grok process exited unexpectedly",
                    )
                continue
            # JSON-RPC ids are PER-SIDE namespaces: a message with "method" is the
            # agent's own REQUEST regardless of id — only a method-less message is
            # the prompt response (cursor-acp eee0b15; audit LT-1).
            if message.get("id") == prompt_id and "method" not in message:
                if "error" in message:
                    self.active_prompt_id = None
                    return TurnResult(ok=False, result="".join(chunks).strip(), error=str(message["error"]))
                result = message.get("result")
                if not isinstance(result, dict):
                    raise EngineError("session/prompt returned non-object result")
                stop_reason = result.get("stopReason")
                # "refusal" included per the cursor-acp hardening precedent (panel-reviewed):
                # a refused turn is a failed turn, not a success.
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
                if not self._interrupted:
                    # Clean, uninterrupted terminal response: the ONLY place
                    # healthy flips back to True (D3a).
                    self.healthy = True
                self.active_prompt_id = None
                return TurnResult(
                    ok=ok,
                    result="".join(chunks).strip() or f"Grok ACP prompt {prompt_id} completed.",
                    error=None if ok else f"Grok ACP stopReason={stop_reason}",
                )
            self._handle_client_message(
                message, on_event=on_event, chunks=chunks, tool_titles=tool_titles, policy=policy
            )
            if self._deny_count > self.deny_budget:
                return self._exhaust_deny_budget(
                    deadline=deadline,
                    prompt_id=prompt_id,
                    policy=policy,
                    on_event=on_event,
                    chunks=chunks,
                    tool_titles=tool_titles,
                )

        if on_event is not None:
            on_event("turn_timeout", {"timeout": timeout})
        self.active_prompt_id = None
        return TurnResult(ok=False, result="".join(chunks).strip(), error=f"turn timed out after {timeout}s")

    def _rotate_session_if_reused(self) -> None:
        """v1.3 D3b: a non-retiring engine keeps its warm process but NEVER reuses
        a session across dispatches — fresh session/new per dispatch (probe run H:
        isolated context; asks carry the new sessionId, so the D3b gate correlates).
        self.session_id flips ONLY after a successful response (build pin 1); on
        failure the engine is quarantined rather than falling back to the old
        session (fail-closed)."""
        if self.retire_after_turn or self._turns_served == 0:
            return
        old_session = self.session_id
        try:
            response = self.request(
                "session/new",
                {"cwd": self.cwd, "mcpServers": local_memory_mcp_servers()},
                timeout=30,
            )
        except EngineError as exc:
            self.healthy = False
            raise EngineError(f"session rotation failed; engine quarantined: {exc}") from exc
        new_session = response.get("sessionId")
        if not isinstance(new_session, str):
            self.healthy = False
            raise EngineError("session rotation returned no sessionId; engine quarantined")
        self.session_id = new_session
        # Re-pin: the new session inherits nothing from the old one. Quarantine on
        # failure, exactly as the rotation itself does — an unpinned session is a
        # seat serving a model nobody chose.
        try:
            self._pin_model()
        except EngineError as exc:
            self.healthy = False
            raise EngineError(f"model re-pin after session rotation failed; engine quarantined: {exc}") from exc
        logger.info(f"[grok-acp] rotated session {old_session!r} -> {new_session!r} (fresh context per dispatch)")

    def _exhaust_deny_budget(
        self,
        *,
        deadline: float,
        prompt_id: int,
        policy: str,
        on_event: ProgressCallback | None,
        chunks: list[str],
        tool_titles: dict[str, str],
    ) -> TurnResult:
        """D3: the model is deny-looping instead of concluding. Interrupt, drain
        for a bounded grace answering every ask per D2 throughout, return legibly.
        Both arms leave healthy=False: an interrupted turn is an UNCLEAN end and
        the session is never reused (v1.3 D3b)."""
        error = (
            f"approval deny budget exhausted ({self._deny_count} denials); "
            f"last: {self._last_denied_title or '<unknown tool>'}"
        )
        try:
            self.interrupt()
        except EngineError:
            pass  # dead engine can't be interrupted; drain below settles the exit
        grace_deadline = time.monotonic() + max(
            0.0, min(self.approval_grace_s, deadline - time.monotonic())
        )
        while time.monotonic() < grace_deadline:
            message = self._get_message(
                timeout=max(0.05, min(0.5, grace_deadline - time.monotonic()))
            )
            if message is None:
                if self._process_exited():
                    break
                continue
            if message.get("id") == prompt_id and "method" not in message:
                self.active_prompt_id = None
                return TurnResult(ok=False, result="".join(chunks).strip(), error=error)
            self._handle_client_message(
                message, on_event=on_event, chunks=chunks, tool_titles=tool_titles, policy=policy
            )
        self.active_prompt_id = None
        return TurnResult(ok=False, result="".join(chunks).strip(), error=error)

    def set_session_mode_for_policy(self, policy: str) -> None:
        """
        Map the bridge's sender policy to Grok's ACP permission modes.

        Preferred Grok modes (in rough order of "how much it trusts the user"):
            - "yolo" or "bypassPermissions"  → full auto-approve (trusted machine peers)
            - "acceptEdits"                  → ask on edits but allow reads
            - "default"                      → normal cautious behavior
            - "plan"                         → planning only, no execution

        We try the most permissive sensible mode for "trusted" and fall back gracefully.
        """
        if self.session_id is None:
            raise EngineError("ACP session not started")

        if policy == "trusted":
            candidates = ["yolo", "bypassPermissions", "acceptEdits"]
        else:
            candidates = ["default", "acceptEdits"]

        last_error = None
        for mode_id in candidates:
            try:
                self.request(
                    "session/set_mode",
                    {"sessionId": self.session_id, "modeId": mode_id},
                    timeout=12,
                )
                logger.info(f"[grok-acp] Successfully set session mode to '{mode_id}' for policy='{policy}'")
                return
            except EngineError as e:
                last_error = e
                continue

        logger.warning(f"[grok-acp] WARNING: Could not set a preferred mode for policy='{policy}'. "
                       f"Last error: {last_error}. Continuing with Grok defaults.")

    def interrupt(self) -> str:
        # Marked BEFORE the session check: _exhaust_deny_budget swallows the
        # EngineError from a dead engine, and the turn must still count as an
        # unclean end (D3a).
        self._interrupted = True
        if self.session_id is None:
            raise EngineError("ACP session not started")
        self.notify("session/cancel", {"sessionId": self.session_id})
        return self.session_id

    def _dispatch_client_request(
        self,
        message: dict[str, Any],
        *,
        policy: str | None,
        on_event: ProgressCallback | None,
    ) -> None:
        self._respond_to_client_request(message, policy=policy, on_event=on_event)

    def _respond_to_client_request(
        self,
        message: dict[str, Any],
        *,
        policy: str | None = None,
        on_event: ProgressCallback | None = None,
    ) -> None:
        """Answer a server-initiated request. Authorization comes ONLY from the
        threaded ``policy`` (GROK-1 v1.3 D2): the turn loop passes the active
        turn's policy; ``request()`` passes None, which always denies."""
        method = message.get("method")
        request_id = message.get("id")
        raw_params = message.get("params")
        params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}

        if method == "session/request_permission":
            ask_session = params.get("sessionId")
            if ask_session != self.session_id:
                # GROK-1 v1.3 D3b: a stale ask from an abandoned session is
                # structurally unauthorizable — deny regardless of policy.
                logger.warning(
                    f"[grok-acp] permission ask for non-current session {ask_session!r} "
                    f"(current {self.session_id!r}); denied fail-closed"
                )
                self._send({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"outcome": {"outcome": "cancelled"}},
                })
                return
            if policy == "trusted":
                option_id = _select_allow_option(params)
                if option_id is not None:
                    logger.debug(
                        f"[grok-acp] trusted allow: {self._ask_title(params)!r} via option {option_id!r}"
                    )
                    self._send({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"outcome": {"outcome": "selected", "optionId": option_id}},
                    })
                    return
                logger.warning(
                    "[grok-acp] permission ask offered no allow option (or params malformed); "
                    "cancelled despite trusted policy (fail-closed floor)"
                )
                self._send({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"outcome": {"outcome": "cancelled"}},
                })
                return
            self._deny_ask(request_id, params, policy=policy, on_event=on_event)
            return

        # Unknown client methods: JSON-RPC -32601 (probe run D: error => no execution).
        self._send({
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"client method not supported in grok-acp: {method}"},
        })

    @staticmethod
    def _ask_title(params: dict[str, Any]) -> str | None:
        tool_call = params.get("toolCall")
        if isinstance(tool_call, dict) and isinstance(tool_call.get("title"), str):
            return tool_call["title"]
        return None

    def _deny_ask(
        self,
        request_id: Any,
        params: dict[str, Any],
        *,
        policy: str | None,
        on_event: ProgressCallback | None,
    ) -> None:
        title = self._ask_title(params)
        if policy is not None:
            # In-turn denies are budget-counted regardless of on_event (V3d);
            # inter-turn (policy=None) denies are timeout-bounded anomalies (D2).
            self._deny_count += 1
            self._last_denied_title = title
        self._send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"outcome": {"outcome": "cancelled"}},
        })
        if policy is None:
            logger.warning(
                "[grok-acp] permission ask outside an authorizing turn; denied fail-closed"
            )
            return
        if on_event is None:
            logger.warning(
                f"[grok-acp] denied permission ask (no progress callback): {title!r} "
                f"deny_count={self._deny_count}/{self.deny_budget}"
            )
            return
        tool_call = params.get("toolCall")
        tool_call_id = tool_call.get("toolCallId") if isinstance(tool_call, dict) else None
        turn_id = str(self.active_prompt_id)
        on_event(
            "command_denied",
            {
                "command": title,
                "turn_id": turn_id,
                "item_id": tool_call_id if isinstance(tool_call_id, str) else f"{turn_id}:approval",
                "kind": "command_denied",
                "seq": self._next_progress_seq(),
                "deny_count": self._deny_count,
                "deny_budget": self.deny_budget,
            },
        )

    def _normalize_session_update(self, update, tool_titles):
        return normalize_session_update(update, tool_titles)


def normalize_session_update(
    update: dict[str, Any],
    tool_titles: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """
    Normalize Grok ACP session/update events to the bridge's neutral vocabulary.

    Grok emits the standard ones (agent_message_chunk, tool_call*, agent_thought_chunk)
    plus many x.ai/* rich updates. For v1 we focus on the common surface so the
    rest of the bridge (events, status, inbox) works without modification.

    Not the shared `normalize_acp_session_update`: grok dedups repeated tool
    events, has no diff-path enrichment or `pending`/`session_info_update`
    handling, and returns None for an unknown update type instead of a
    `session_update_unknown` event.
    """
    update_type = update.get("sessionUpdate")
    if update_type == "agent_message_chunk":
        content = update.get("content")
        if isinstance(content, dict) and content.get("type") == "text" and isinstance(content.get("text"), str):
            return "model_text", {"delta": content["text"]}

    # Grok emits very small agent_thought_chunk deltas.
    # We suppress tiny fragments here and only emit when we have a reasonable chunk.
    # The harness does additional accumulation on top for clean output.
    if update_type == "agent_thought_chunk":
        content = update.get("content")
        if isinstance(content, dict) and content.get("type") == "text" and isinstance(content.get("text"), str):
            text = content["text"]
            # Only emit thoughts that are somewhat meaningful (reduce spam)
            if len(text.strip()) > 8 or any(c in text for c in ".!?"):
                return "model_thinking", {"delta": text}

    if update_type in {"tool_call", "tool_call_update"}:
        tool_call_id = update.get("toolCallId")
        status = update.get("status")

        # Simple dedup: skip if we have the exact same tool + status recently
        # (Grok sometimes emits both tool_call and tool_call_update for the same event)
        if tool_titles is not None:
            dedup_key = f"{tool_call_id}:{status}"
            if tool_titles.get("_last_tool_event") == dedup_key:
                return None
            tool_titles["_last_tool_event"] = dedup_key

        title = update.get("title")
        if isinstance(tool_titles, dict) and isinstance(tool_call_id, str) and isinstance(title, str):
            tool_titles[tool_call_id] = title
        command = title
        if not isinstance(command, str) and isinstance(tool_titles, dict) and isinstance(tool_call_id, str):
            command = tool_titles.get(tool_call_id)

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

    # TODO(v2): handle plan entries, x.ai/* rich notifications (worktree progress, fs index, etc.)
    return None


# =============================================================================
# Standalone Test Harness
# =============================================================================
# Run this file directly to exercise GrokAcpEngine without the full Redis bridge.
#
# Examples:
#   python -m agent_redis_bridge.engines.grok_acp
#   python -m agent_redis_bridge.engines.grok_acp "Run git status and summarize"
#   python -m agent_redis_bridge.engines.grok_acp --policy human "Edit README.md"
#   python -m agent_redis_bridge.engines.grok_acp --cwd /some/other/repo "What is here?"
#

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Standalone tester for GrokAcpEngine (grok agent stdio)"
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        default="Say hello from Grok Build and run the command `pwd && echo '---' && git status --short | head -5`",
        help="Task/prompt to send to Grok",
    )
    parser.add_argument(
        "--cwd",
        default=".",
        help="Working directory for the Grok session (default: current dir)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Optional model override to pass to Grok",
    )
    parser.add_argument(
        "--policy",
        default="trusted",
        choices=["trusted", "human"],
        help="Sender policy mapping (affects yolo vs default mode)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="Turn timeout in seconds",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show more raw event data",
    )
    args = parser.parse_args()

    cwd = os.path.abspath(args.cwd)
    print(f"Starting GrokAcpEngine")
    print(f"  cwd    : {cwd}")
    print(f"  policy : {args.policy}")
    print(f"  prompt : {args.prompt[:80]}{'...' if len(args.prompt) > 80 else ''}")
    print("-" * 60)

    engine = GrokAcpEngine(cwd=cwd, model=args.model)

    # Thought accumulator using a list (mutable container) so we avoid nonlocal issues
    _thought_acc: list[str] = []

    def on_event(name: str, data: dict[str, Any]) -> None:
        if name == "model_text":
            delta = data.get("delta", "")
            if delta.startswith("[thinking]"):
                _thought_acc.append(delta)
            else:
                if _thought_acc:
                    print("".join(_thought_acc).strip())
                    _thought_acc.clear()
                sys.stdout.write(delta)
                sys.stdout.flush()
        elif name in ("command_started", "command_finished"):
            if _thought_acc:
                print("".join(_thought_acc).strip())
                _thought_acc.clear()
            print(f"\n[EVENT {name}] {data.get('command', '?')} (status={data.get('status')})")
        else:
            if args.verbose:
                print(f"\n[EVENT {name}] {data}")
            else:
                print(f"\n[EVENT {name}]")

    try:
        print("Calling engine.start() ...")
        engine.start()
        print("Engine started successfully. Running turn...\n")
        print(">>> GROK OUTPUT:\n")

        result = engine.run_turn_with_progress(
            args.prompt,
            timeout=args.timeout,
            policy=args.policy,
            on_event=on_event,
        )

        if _thought_acc:
            print("".join(_thought_acc).strip())
            _thought_acc.clear()

        print("\n" + "-" * 60)
        print("=== TURN RESULT ===")
        print(f"ok:    {result.ok}")
        print(f"error: {result.error}")
        if not result.ok:
            print(f"raw result: {result.result}")

    except KeyboardInterrupt:
        print("\n\n[Interrupted by user]")
        try:
            engine.interrupt()
        except Exception:
            pass
    except Exception as e:
        print(f"\n[ERROR] {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("\nStopping engine...")
        try:
            engine.stop()
        except Exception:
            pass
        print("Done.")
