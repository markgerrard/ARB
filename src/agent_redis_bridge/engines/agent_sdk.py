from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    project_key_for_directory,
)
from claude_agent_sdk.types import (
    TERMINAL_TASK_STATUSES,
    PermissionResultAllow,
    PermissionResultDeny,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
)
from claude_agent_sdk.types import ToolPermissionContext

from agent_redis_bridge.local_memory_mcp import local_memory_mcp_config

from ._stdio import is_bus_credential, is_gate_daemon_credential
from .agent_sdk_loop import LoopThread
from .agent_sdk_mediation import KNOWN_TOOLS, MUTATING, decide, gated_option_kwargs, parse_ceiling
from .agent_sdk_models import SENSITIVE_PREFIXES, auth_var, isolated_env, resolve, subscription_env
from .agent_sdk_session import FileSessionStore, ScrubbedSessionStore, scrub
from .base import EngineError, ProgressCallback, TurnResult


def assert_no_live_bus_credentials(final_env: dict[str, str]) -> None:
    """Fail-closed spawn gate: the SDK child's merged env must carry no live
    bus or gate-daemon credential. The overlay blanks them (agent_sdk_models);
    this assertion is the proof at spawn time, so a regression in either layer
    refuses the launch instead of leaking the bus/gate to a tool-bearing child.
    """
    live = sorted(
        name
        for name, value in final_env.items()
        if value
        and (is_bus_credential(name) or is_gate_daemon_credential(name))
    )
    if live:
        raise EngineError(
            "agent-sdk child env bus/gate-daemon credentials not neutralized: "
            + ", ".join(live)
        )


ClientFactory = Callable[..., Any]
LOGGER = logging.getLogger(__name__)
LOCAL_MEMORY_MCP_AGENT_SDK_TOOLS = frozenset(
    {
        "mcp__arb-memory-local__memory_search",
        "mcp__arb-memory-local__memory_get",
        "mcp__arb-memory-local__memory_recent",
    }
)

# Tool-input keys, in priority order, whose value is the most useful one-line label for a tool call
# (e.g. the file for Read/Edit, the pattern for Grep). Used by _tool_command_label.
_TOOL_LABEL_KEYS = ("file_path", "path", "notebook_path", "pattern", "command", "url", "query")


def _tool_command_label(name: str, tool_input: Any) -> str:
    """Readable command label for a tool call so the transcript shows e.g. ``Read(src/foo.py)`` /
    ``Grep(pattern)`` instead of a bare tool name. Falls back to the bare name when there is no
    salient scalar argument. (The agent-sdk SDK carries args in ToolUseBlock.input, unlike codex's
    flat command string — without this the arb-watch transcript renders a detail-less bullet.)"""
    name = name or "tool"
    if not isinstance(tool_input, dict) or not tool_input:
        return name

    def _cap(value: Any) -> str:
        # Cap EVERY branch (not just the fallback) — a long Bash command / URL is both transcript
        # bloat and maximal exposure of any inline secret (the named-key branch was uncapped).
        text = str(value).strip()
        return text if len(text) <= 120 else text[:117] + "…"

    for key in _TOOL_LABEL_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return f"{name}({_cap(value)})"
    for value in tool_input.values():
        if isinstance(value, (str, int, float)) and str(value).strip():
            return f"{name}({_cap(value)})"
    return name


def _tool_result_text(content: Any, *, cap: int = 16000) -> str:
    """Flatten a ToolResultBlock.content (str | list of text/image blocks) to display text for the
    transcript's ``⎿`` output line. Full output (operator decision: parity with codex) but capped
    defensively so a Read of a huge file can't tee megabytes onto the bus. Goes to the TRANSCRIPT
    only — ``content`` is not in the eval allowlist, so this does not reach the eval tee."""
    if content is None:
        return ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text", "")) if item.get("type") == "text"
                             else f"[{item.get('type', 'content')}]")
            else:
                parts.append(str(getattr(item, "text", "") or getattr(item, "type", item)))
        text = "\n".join(p for p in parts if p)
    else:
        text = str(content)
    text = text.strip()
    if len(text) > cap:
        text = text[:cap] + f"\n…[truncated {len(text) - cap} chars]"
    return text
# These limits are per bridge process. A cross-process Redis-backed cap is
# future hardening if subscription seats are scaled out across processes/hosts.
_SUBSCRIPTION_OPUS_SEMAPHORE = threading.BoundedSemaphore(1)
_SUBSCRIPTION_IMPLEMENTOR_SEMAPHORE = threading.BoundedSemaphore(2)


class _SubscriptionSlot:
    def __init__(self, semaphore: threading.BoundedSemaphore) -> None:
        self._semaphore = semaphore
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._semaphore.release()


def _seat_enabled() -> bool:
    return os.environ.get("SEAT_ENABLED", "1").lower() not in {"0", "false", "no"}


def subscription_certifier_audit_event(
    *,
    orchestrator_identity: str | None,
    orchestrator_model: str | None,
    seat_model: str | None,
) -> dict[str, Any]:
    """Build the non-blocking subscription-certifier audit heuristic.

    Detection depends on sender/model name shape and an orchestrator-supplied
    `orchestrator_model`. Missing or inaccurate model declarations can
    false-negative; misclassification only affects the surfaced audit flag.
    """
    identity = (orchestrator_identity or "").lower()
    model = (orchestrator_model or "").lower()
    # Any subscription Opus seat, not just 4.8 — the flag exists to surface an Opus seat being
    # driven by an Opus orchestrator (a decorrelation concern), and that is model-generation
    # agnostic. Pinning the literal "opus-4.8" silently false-negatived every later Opus seat.
    is_bridge_opus = (seat_model or "").startswith("opus-")
    is_claude_code_opus = "claude" in identity and "opus" in model
    return {
        "orchestrator_identity": orchestrator_identity,
        "orchestrator_model": orchestrator_model,
        "seat_model": seat_model,
        "bridge_opus_inside_claude_code_opus": is_bridge_opus and is_claude_code_opus,
    }


class AgentSdkEngine:
    supports_thread_resume = True
    consumes_role_profile = True

    def __init__(
        self,
        *,
        cwd: str,
        model: str | None,
        tool_ceiling: str | None,
        key: str,
        session_root: str | Path,
        oneshot: bool = False,
        role_profile: str | None = None,
        agent_id: str | None = None,
        bare: bool = False,
        startup_probe: bool = True,
        background_grace: float | None = None,
        live_smoke_test: bool = False,
        client_factory: ClientFactory = ClaudeSDKClient,
    ) -> None:
        self.cwd = cwd
        self.model_name = model or "minimax-m3"
        self.spec = resolve(self.model_name)
        self.ceiling = parse_ceiling(tool_ceiling)
        self._local_memory_mcp_config = local_memory_mcp_config()
        if self._local_memory_mcp_config is not None:
            self.ceiling = frozenset((*self.ceiling, *LOCAL_MEMORY_MCP_AGENT_SDK_TOOLS))
        self.key = key
        self.session_root = Path(session_root)
        self._cold_reviewer = self._is_reviewer_subscription()
        self.oneshot = oneshot or self._cold_reviewer
        self.supports_continuation = not self.oneshot
        self.supports_thread_resume = not self.oneshot
        self.role_profile = role_profile
        self.agent_id = agent_id or f"agent-sdk-{self.spec.slug}"
        self.bare = bare
        self.config_dir = self.session_root / self.agent_id / "claude-config" / uuid.uuid4().hex
        self.startup_probe = startup_probe
        self.live_smoke_test = live_smoke_test
        self.client_factory = client_factory
        self.loop_thread = LoopThread()
        self.client: Any | None = None
        self.healthy = True
        self._turn_policy = "trusted"
        self._turn_on_event: ProgressCallback | None = None
        self._active_future = None
        self._active_turn = False
        # Background-task ids started this turn and not yet terminal (see _run_turn).
        self._pending_tasks: set[str] = set()
        # Ceiling on how long one bridge turn waits for a background task's completion after
        # an interim ResultMessage. The deadline is also clamped to the loop-thread coroutine's
        # start + max(turn_timeout - 60, turn_timeout / 2), which normally expires ahead of the
        # caller's hard timeout (the two clocks differ by loop scheduling and client.query()
        # latency). A slow model turn after the task completes is bounded by the turn timeout.
        self.background_grace = (
            float(background_grace)
            if background_grace is not None
            else float(os.environ.get("BRIDGE_AGENT_SDK_BACKGROUND_GRACE_SECS", "1500"))
        )
        self._interim_text = ""
        self._hold_keepalive_secs = 60.0
        self._last_session_id: str | None = None if self._cold_reviewer else self._load_last_session_id()
        self._options: ClaudeAgentOptions | None = None
        self._gate_records: list[tuple[str, bool, str]] = []
        self._progress_seq = 0
        self._orchestrator_identity: str | None = None
        self._orchestrator_model: str | None = None
        # Retire the engine after every dispatch so the pool never re-serves the
        # accumulating ClaudeSDKClient conversation (the sonnet wiki-gate seat
        # stacked 15 unrelated dispatches into one 1.76MB session on 2026-07-07 —
        # cross-dispatch contamination inside a review gate). Long-lived seats
        # opt out with BRIDGE_AGENT_SDK_RETIRE_AFTER_TURN=0 (launchd plist, not
        # the seat env file). Explicit resume_thread() continuation is unchanged.
        raw_retire = os.environ.get("BRIDGE_AGENT_SDK_RETIRE_AFTER_TURN")
        self.retire_after_turn = str(raw_retire).lower() not in {"0", "false"}

    def _next_progress_seq(self) -> int:
        self._progress_seq += 1
        return self._progress_seq

    def _is_reviewer_subscription(self) -> bool:
        return self.spec.subscription and self.spec.reviewer

    def set_turn_audit_context(self, *, orchestrator_identity: str | None, orchestrator_model: str | None) -> None:
        self._orchestrator_identity = orchestrator_identity
        self._orchestrator_model = orchestrator_model

    @property
    def session_id(self) -> str | None:
        return self._last_session_id

    def _last_session_id_path(self) -> Path:
        return self.session_root / self.agent_id / "last-session-id"

    def _load_last_session_id(self) -> str | None:
        try:
            value = self._last_session_id_path().read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        return value or None

    def _persist_last_session_id(self, session_id: str) -> None:
        path = self._last_session_id_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{session_id}\n", encoding="utf-8")

    def start(self) -> None:
        self.loop_thread.start()
        options = self._build_options()
        self._options = options
        self.client = self.client_factory(options=options)
        self.loop_thread.submit(self.client.connect()).result(timeout=30)
        if self.startup_probe:
            self.assert_serveable()
        if self.live_smoke_test:
            self._run_live_startup_smoke_test()

    def _system_prompt(self) -> str:
        # A raw-string system_prompt REPLACES Claude Code's default prompt, which is
        # what normally injects the working-directory line. So we must announce the
        # cwd ourselves: a seat whose Write/Edit need absolute paths and that has no
        # shell to run `pwd` is otherwise blind to where it is and writes outside the
        # worktree (observed live on a no-Bash haiku implementor). Keep the role
        # profile; append the cwd anchor.
        anchor = (
            f"Your current working directory is: {self.cwd}\n"
            "When creating or editing files, use absolute paths under this directory. "
            "You have no shell, so this is the only source of your working directory."
        )
        return f"{self.role_profile}\n\n{anchor}" if self.role_profile else anchor

    def _build_options(self, *, explicit_resume: bool = False) -> ClaudeAgentOptions:
        if self.spec.subscription:
            return self._build_subscription_options(explicit_resume=explicit_resume)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        env = isolated_env(self.spec, self.key, base=dict(os.environ), config_dir=self.config_dir)
        assert_no_live_bus_credentials({**dict(os.environ), **env})
        store = ScrubbedSessionStore(
            FileSessionStore(self.session_root, self.agent_id),
            secrets=[self.key],
            var_names=[self.spec.key_env, auth_var(self.spec.auth_style)],
        )
        kwargs = gated_option_kwargs()
        return ClaudeAgentOptions(
            **kwargs,
            can_use_tool=self._gate,
            cwd=self.cwd,
            model=self.spec.model_id or None,
            env=env,
            session_store=store,
            session_store_flush="batched",
            system_prompt=self._system_prompt(),
            include_hook_events=False,
            mcp_servers=local_memory_mcp_agent_sdk_servers(self._local_memory_mcp_config),
            # Auto-resuming the persisted session on a fresh engine would silently
            # rebuild the accumulated conversation retirement exists to shed (and
            # exercises resume-at-connect, which the subscription comment below
            # documents as crash-prone, on every dispatch). Explicit resume_thread()
            # still resumes; the id is still loaded/persisted for observability.
            resume=self._last_session_id if explicit_resume or not self.retire_after_turn else None,
            stderr=self._handle_stderr,
        )

    def _build_subscription_options(self, *, explicit_resume: bool = False) -> ClaudeAgentOptions:
        if self.bare:
            raise EngineError("agent-sdk subscription seats cannot launch with --bare")
        self.config_dir.mkdir(parents=True, exist_ok=True)
        base = dict(os.environ)
        env = subscription_env(base=base, config_dir=self.config_dir)
        final_env = {**base, **env}
        token = final_env.get("CLAUDE_CODE_OAUTH_TOKEN", "")
        if not token:
            raise EngineError("agent-sdk subscription seat requires CLAUDE_CODE_OAUTH_TOKEN")
        shadow_keys = sorted(name for name, value in final_env.items() if value and name.startswith(SENSITIVE_PREFIXES))
        if shadow_keys:
            raise EngineError(
                "agent-sdk subscription shadow environment keys not neutralized: " + ", ".join(shadow_keys)
            )
        assert_no_live_bus_credentials(final_env)
        store = ScrubbedSessionStore(
            FileSessionStore(self.session_root, self.agent_id),
            secrets=[token],
            var_names=["CLAUDE_CODE_OAUTH_TOKEN"],
        )
        kwargs = gated_option_kwargs()
        return ClaudeAgentOptions(
            **kwargs,
            can_use_tool=self._gate,
            cwd=self.cwd,
            model=self.spec.model_id or None,
            env=env,
            session_store=store,
            session_store_flush="batched",
            system_prompt=self._system_prompt(),
            include_hook_events=False,
            mcp_servers=local_memory_mcp_agent_sdk_servers(self._local_memory_mcp_config),
            # Subscription seats never auto-resume at connect (fresh context per
            # engine is the retire-after-turn contract). Explicit resume_thread()
            # continuation DOES resume: the SDK materializes the transcript from
            # session_store into a temp CLAUDE_CONFIG_DIR (session_resume.py), so
            # it works across processes — live-proven 2026-07-10. The store key
            # includes the cwd's project key, so resume requires the same cwd;
            # resume_thread pre-checks that and fails loud on a miss.
            resume=self._last_session_id if explicit_resume else None,
            stderr=self._handle_stderr,
        )

    async def _prompt_stream(self, task: str) -> AsyncIterator[dict[str, Any]]:
        yield {
            "type": "user",
            "message": {"role": "user", "content": task},
            "parent_tool_use_id": None,
            "session_id": "default",
        }

    async def _gate(self, tool_name: str, tool_input: dict[str, Any], context: Any):
        try:
            allow, reason = decide(tool_name, ceiling=self.ceiling, policy=self._turn_policy)
        except Exception as exc:  # noqa: BLE001 - permission callbacks must fail closed
            allow, reason = False, f"gate error: {exc}"
        self._gate_records.append((tool_name, allow, reason))
        event = "agent_sdk_tool_allowed" if allow else "agent_sdk_tool_denied"
        try:
            if self._turn_on_event is not None:
                turn_id = self._last_session_id or "agent-sdk"
                tool_call_id = getattr(context, "tool_use_id", None)
                self._turn_on_event(
                    "tool_permission_decided",
                    self._scrub_payload(
                        {
                            "command": tool_name,
                            "status": "allowed" if allow else "denied",
                            "exit_code": 0 if allow else 1,
                            "tool_call_id": tool_call_id,
                            "kind": event,
                            "reason": reason,
                            "turn_id": turn_id,
                            "item_id": tool_call_id if isinstance(tool_call_id, str) else f"{turn_id}:{event}",
                            "seq": self._next_progress_seq(),
                        }
                    ),
                )
        except Exception as exc:  # noqa: BLE001 - telemetry must never break the gate's contract
            # The permission callback's whole design is "always returns an
            # explicit Allow/Deny". An observability failure here used to raise
            # out of the callback, handing the CLI an error-response whose
            # allow/deny interpretation we cannot verify (audit ASK-2).
            LOGGER.warning(f"[agent-sdk] gate telemetry failed for {tool_name}: {exc}")
        if allow:
            return PermissionResultAllow()
        return PermissionResultDeny(message=reason)

    def assert_serveable(self) -> None:
        options = self._options
        if options is None or options.can_use_tool is None:
            raise EngineError("agent-sdk can_use_tool gate is not wired")
        if options.allowed_tools != [] or options.setting_sources != [] or options.permission_mode != "default":
            raise EngineError("agent-sdk SDK options are not fail-closed")
        if not self.ceiling:
            raise EngineError("agent-sdk tool ceiling is empty")
        self._gate_records.clear()
        previous_policy = self._turn_policy
        self._turn_policy = "trusted"
        try:
            self._assert_connected_gate_wired(options)
            self.loop_thread.submit(self._run_startup_gate_checks(options)).result(timeout=10)
        finally:
            self._turn_policy = previous_policy

    def _assert_connected_gate_wired(self, options: ClaudeAgentOptions) -> None:
        query = getattr(self.client, "_query", None)
        if query is None or getattr(query, "can_use_tool", None) is not options.can_use_tool:
            self.healthy = False
            raise EngineError("agent-sdk gate callback not wired into the connected client")

    def _startup_allow_tool(self) -> str:
        known_ceiling = sorted(self.ceiling & KNOWN_TOOLS)
        allow_tool = next((tool for tool in known_ceiling if tool not in MUTATING), None)
        if allow_tool is not None:
            return allow_tool
        if known_ceiling:
            return known_ceiling[0]
        raise EngineError("agent-sdk ceiling has no known tool to probe")

    async def _run_startup_gate_checks(self, options: ClaudeAgentOptions) -> None:
        assert options.can_use_tool is not None
        allow_tool = self._startup_allow_tool()
        allow_context = ToolPermissionContext(tool_use_id="startup-allow")
        allow_result = await options.can_use_tool(allow_tool, {}, allow_context)
        if not isinstance(allow_result, PermissionResultAllow):
            raise EngineError(f"agent-sdk startup gate denied in-ceiling tool {allow_tool}")

        context = ToolPermissionContext(tool_use_id="startup-deny")
        result = await options.can_use_tool("ARB_STARTUP_DENY_SENTINEL", {}, context)
        if not isinstance(result, PermissionResultDeny):
            raise EngineError("agent-sdk startup guard did not observe sentinel denial")

    @staticmethod
    def _startup_probe_prompt() -> str:
        # READ-ONLY by design. The live smoke-test only needs the model to
        # round-trip and hit the can_use_tool gate (the deterministic deny-proof is
        # done separately in _run_startup_gate_checks). It must NOT instruct a write:
        # a Write-capable implementor's gate correctly ALLOWS writes, so a write
        # probe lands a file in the pooled engine's cwd — the base checkout — on
        # every boot (litter, not a containment break). A harmless directory listing
        # exercises the gate without touching the filesystem.
        return (
            "Startup self-test: call the LS tool once to list your current working directory, then stop. "
            "Use only that read-only tool."
        )

    async def _run_startup_probe(self) -> None:
        assert self.client is not None
        await self.client.query(self._prompt_stream(self._startup_probe_prompt()))
        async for message in self.client.receive_response():
            if isinstance(message, ResultMessage):
                return
        self.healthy = False
        raise EngineError("agent-sdk startup probe ended without ResultMessage")

    def _run_live_startup_smoke_test(self) -> None:
        records_before = len(self._gate_records)
        healthy_before = self.healthy
        try:
            self.loop_thread.submit(self._run_startup_probe()).result(timeout=60)
            if len(self._gate_records) > records_before:
                LOGGER.info(
                    "[agent-sdk] live-model startup smoke test fired and hit the gate; "
                    "deterministic gate already passed",
                )
            else:
                LOGGER.info(
                    "[agent-sdk] live-model startup smoke test did not fire cleanly - "
                    "deterministic gate passed, seat is serving; investigate only if recurring",
                )
        except Exception as exc:
            LOGGER.info(
                "[agent-sdk] live-model startup smoke test did not fire cleanly - "
                f"deterministic gate passed, seat is serving; investigate only if recurring: {exc}",
            )
        finally:
            self.healthy = healthy_before

    def run_turn_with_progress(
        self,
        task: str,
        *,
        timeout: int = 3600,
        policy: str = "trusted",
        on_event: ProgressCallback | None,
    ) -> TurnResult:
        if self.spec.subscription and not _seat_enabled():
            return TurnResult(ok=False, result="", error="agent-sdk subscription seat disabled by SEAT_ENABLED")
        if self.client is None:
            raise EngineError("agent-sdk client not started")
        if policy != "trusted" and self.ceiling.intersection(MUTATING):
            return TurnResult(ok=False, result="", error="non-trusted mutation turn refused")
        slot: _SubscriptionSlot | None = None
        if self.spec.subscription:
            try:
                slot = self._acquire_subscription_slot()
            except EngineError as exc:
                return TurnResult(ok=False, result="", error=str(exc))
        self._turn_policy = policy
        self._turn_on_event = on_event
        self._active_turn = True
        if self.spec.subscription:
            audit_payload = self._subscription_audit_payload()
            if on_event is not None:
                on_event("agent_sdk_subscription_audit", audit_payload)
            else:
                LOGGER.info("agent_sdk_subscription_audit %s", json.dumps(audit_payload, sort_keys=True))
        # Reset on the caller thread, ordered before any loop-thread mutation, so a timeout
        # can never read a stale id left by a previous turn.
        self._pending_tasks = set()
        self._interim_text = ""
        # The hold must expire before the hard timeout so an interim result is returned
        # instead of the total-loss timeout path.
        hold_grace = min(self.background_grace, max(timeout - 60, timeout * 0.5))
        future = self.loop_thread.submit(
            self._run_turn(task, on_event=on_event, hold_grace=hold_grace, turn_budget=max(timeout - 60, timeout * 0.5))
        )
        self._active_future = future
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError:
            self.healthy = False
            future.cancel()
            pending = sorted(self._pending_tasks)
            stopped: list[str] = []
            failed: list[str] = []
            reap_incomplete: list[str] = []
            if pending:
                try:
                    stopped, failed = self.loop_thread.submit(self._reap_pending()).result(timeout=20)
                except Exception:
                    reap_incomplete = pending
            self._disconnect(timeout=10)
            if on_event is not None:
                on_event(
                    "turn_timeout",
                    {
                        "timeout": timeout,
                        "pending_tasks": pending,
                        "stop_requested_tasks": stopped,
                        "stop_failed_tasks": failed,
                        "reap_incomplete_tasks": reap_incomplete,
                    },
                )
            error = f"turn timed out after {timeout}s"
            if stopped:
                error += f"; stop requested for background tasks: {', '.join(stopped)}"
            if failed:
                error += f"; stop_task failed for: {', '.join(failed)}"
            if reap_incomplete:
                error += f"; reap did not complete for: {', '.join(reap_incomplete)}"
            return TurnResult(ok=False, result=self._interim_text, error=error)
        finally:
            self._active_turn = False
            self._active_future = None
            self._turn_on_event = None
            if slot is not None:
                slot.release()

    def _acquire_subscription_slot(self) -> _SubscriptionSlot:
        if not self.spec.subscription:
            raise EngineError("agent-sdk subscription slot requested for non-subscription model")
        semaphore = (
            _SUBSCRIPTION_OPUS_SEMAPHORE
            if self.spec.reviewer
            else _SUBSCRIPTION_IMPLEMENTOR_SEMAPHORE
        )
        if not semaphore.acquire(blocking=False):
            raise EngineError("agent-sdk subscription concurrency limit reached")
        return _SubscriptionSlot(semaphore)

    def _subscription_audit_payload(self) -> dict[str, Any]:
        event = subscription_certifier_audit_event(
            orchestrator_identity=self._orchestrator_identity,
            orchestrator_model=self._orchestrator_model,
            seat_model=self.spec.name,
        )
        event.update(
            {
                "kind": "agent_sdk_subscription_audit",
                "turn_id": self._last_session_id or "agent-sdk",
                "item_id": f"{self.agent_id}:agent_sdk_subscription_audit",
                "seq": self._next_progress_seq(),
            }
        )
        return event

    def _emit_tool_result(self, block: ToolResultBlock, turn_id: str, on_event: ProgressCallback | None) -> None:
        """Emit a tool result as command_output so arb-watch renders the ⎿ output line under the
        matching ⏺ tool call (same item_id = tool_use_id). command_output is codex's proven, served
        kind. Result content goes in `content` (transcript-only — not eval-allowlisted)."""
        if on_event is None:
            return
        on_event(
            "command_output",
            self._scrub_payload(
                {
                    "command": block.tool_use_id,
                    "content": _tool_result_text(block.content),
                    "status": "failed" if block.is_error else "completed",
                    "exit_code": 1 if block.is_error else 0,
                    "tool_call_id": block.tool_use_id,
                    "turn_id": turn_id,
                    # DISTINCT :output item_id (codex's pattern) — the gateway merges frames by
                    # item_id, so reusing the command_started id (tool_use_id) collapses the output
                    # INTO the started frame (which arb-watch renders without content). A separate
                    # item_id keeps it its own command_output frame → arb-watch renders the ⎿ line.
                    "item_id": f"{block.tool_use_id}:output",
                    "kind": "command_output",
                    "seq": self._next_progress_seq(),
                }
            ),
        )
        on_event(
            "command_finished",
            self._scrub_payload({
                "command": block.tool_use_id,
                "status": "failed" if block.is_error else "completed",
                "exit_code": 1 if block.is_error else 0,
                "tool_call_id": block.tool_use_id,
                "turn_id": turn_id,
                "item_id": f"{block.tool_use_id}:finished",
                "kind": "command_finished",
                "seq": self._next_progress_seq(),
            }),
        )

    async def _reap_pending(self) -> tuple[list[str], list[str]]:
        # stop_task() is a control request the CLI ACKs; whether the OS child is gone is not
        # established here (the 'stopped' task_notification is not awaited). Report only what
        # was requested and what the request itself failed on.
        assert self.client is not None
        stopped: list[str] = []
        failed: list[str] = []
        for task_id in sorted(self._pending_tasks):
            try:
                await asyncio.wait_for(self.client.stop_task(task_id), timeout=5)
                stopped.append(task_id)
            except Exception:
                LOGGER.warning("agent-sdk stop_task(%s) failed", task_id)
                failed.append(task_id)
        self._pending_tasks = set()
        return stopped, failed

    async def _run_turn(
        self,
        task: str,
        *,
        on_event: ProgressCallback | None,
        hold_grace: float = 1500.0,
        turn_budget: float | None = None,
    ) -> TurnResult:
        assert self.client is not None
        await self.client.query(self._prompt_stream(task))
        turn_id = self._last_session_id or "agent-sdk"
        if on_event is not None:
            on_event("turn_started", {"turn_id": turn_id})
        chunks: list[str] = []
        tool_ids: set[str] = set()
        saw_result = False
        final_result: ResultMessage | None = None
        hold_deadline: float | None = None
        grace_expired = False
        interim_results = 0
        stream = self._message_stream().__aiter__()
        loop = asyncio.get_running_loop()
        turn_started_at = loop.time()
        # The next-message read lives in its own task so a timed wait can expire WITHOUT
        # cancelling it: cancelling __anext__ on a real async generator finishes the generator,
        # which would misreport a long hold as the stream ending.
        pending_read: asyncio.Future[Any] | None = None
        stream_error: BaseException | None = None

        try:
            while True:
                if pending_read is None:
                    pending_read = asyncio.ensure_future(stream.__anext__())
                if hold_deadline is not None and not self._pending_tasks:
                    # The task(s) completed: the hold is over and the rest of the turn is
                    # bounded by the caller's turn timeout only (re-panel cold-Opus P1-1).
                    hold_deadline = None
                if hold_deadline is None:
                    done, _ = await asyncio.wait({pending_read})
                else:
                    remaining = hold_deadline - loop.time()
                    if remaining <= 0 and not pending_read.done():
                        grace_expired = True
                        break
                    if remaining <= 0:
                        remaining = 0.0
                    # Wake at least every 60s while holding so the bridge's stall watch sees
                    # progress (the hold is otherwise silent — cold-Opus P2-6).
                    done, _ = await asyncio.wait({pending_read}, timeout=min(remaining, self._hold_keepalive_secs))
                    if not done:
                        if hold_deadline - loop.time() <= 0 and not pending_read.done():
                            grace_expired = True
                            break
                        if on_event is not None:
                            on_event(
                                "turn_continued",
                                {
                                    "turn_id": turn_id,
                                    "pending_tasks": sorted(self._pending_tasks),
                                    "keepalive": True,
                                    "seq": self._next_progress_seq(),
                                },
                            )
                        continue
                try:
                    message = pending_read.result()
                except StopAsyncIteration:
                    pending_read = None
                    break
                pending_read = None
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            chunks.append(block.text)
                            if on_event is not None:
                                on_event(
                                    "model_text",
                                    self._scrub_payload(
                                        {
                                            "delta": block.text,
                                            "turn_id": turn_id,
                                            "item_id": f"{turn_id}:text",
                                            "kind": "model_text",
                                            "seq": self._next_progress_seq(),
                                        }
                                    ),
                                )
                        elif isinstance(block, ThinkingBlock):
                            if on_event is not None:
                                on_event(
                                    "model_thinking",
                                    self._scrub_payload(
                                        {
                                            "delta": block.thinking,
                                            "turn_id": turn_id,
                                            "item_id": f"{turn_id}:thinking",
                                            "kind": "model_thinking",
                                            "seq": self._next_progress_seq(),
                                        }
                                    ),
                                )
                        elif isinstance(block, ToolUseBlock):
                            if block.id not in tool_ids:
                                tool_ids.add(block.id)
                                if on_event is not None:
                                    on_event(
                                        "command_started",
                                        self._scrub_payload(
                                            {
                                                # Label (name + salient arg) goes in `command` ONLY — NOT
                                                # `tool_name`. The eval tee allowlists `tool_name` (eval is
                                                # contracted to exclude raw args), so a labelled tool_name
                                                # would leak Bash args into eval. The transcript still renders
                                                # the label via transcript_flusher's `tool_name or command`.
                                                "command": _tool_command_label(block.name, block.input),
                                                "status": "in_progress",
                                                "exit_code": None,
                                                "tool_call_id": block.id,
                                                "kind": "command_started",
                                                "turn_id": turn_id,
                                                "item_id": block.id,
                                                "seq": self._next_progress_seq(),
                                            }
                                        ),
                                    )
                        elif isinstance(block, ToolResultBlock):
                            self._emit_tool_result(block, turn_id, on_event)
                elif isinstance(message, UserMessage):
                    # Tool RESULTS come back in a UserMessage (the SDK auto-runs the tool and returns its
                    # output here), NOT in an AssistantMessage — so without this branch the output never
                    # reaches the transcript (the AssistantMessage ToolResultBlock branch is dead for
                    # built-in tools). Emit command_output → arb-watch renders the ⎿ line.
                    for block in getattr(message, "content", None) or []:
                        if isinstance(block, ToolResultBlock):
                            self._emit_tool_result(block, turn_id, on_event)
                elif isinstance(message, TaskStartedMessage):
                    self._pending_tasks.add(message.task_id)
                elif isinstance(message, (TaskNotificationMessage, TaskUpdatedMessage)):
                    # Terminal state may arrive as EITHER message (the SDK documents that the
                    # notification is sometimes suppressed and only the task_updated patch lands).
                    if (message.status or "") in TERMINAL_TASK_STATUSES:
                        self._pending_tasks.discard(message.task_id)
                elif isinstance(message, ResultMessage):
                    saw_result = True
                    final_result = message
                    if not self._cold_reviewer:
                        self._last_session_id = message.session_id
                        self._persist_last_session_id(message.session_id)
                    self._interim_text = message.result or "".join(chunks).strip()
                    if self._pending_tasks and not message.is_error:
                        # Background tasks still running. Observed once (macOS, SDK 0.2.117, ARB
                        # Memory art-d17b2c72afaf7b15 v2): the CLI re-invokes the model in this same
                        # session when they finish, so this ResultMessage is interim. Not reproduced
                        # on Linux. Keep listening (idle, no token spend) until hold_grace expires;
                        # if the re-invocation never comes, the interim result is returned and the
                        # stragglers are stop-requested.
                        interim_results += 1
                        if hold_deadline is None:
                            hold_deadline = loop.time() + hold_grace
                            if turn_budget is not None:
                                hold_deadline = min(hold_deadline, turn_started_at + turn_budget)
                        if on_event is not None:
                            on_event(
                                "turn_continued",
                                {
                                    "turn_id": message.session_id,
                                    "pending_tasks": sorted(self._pending_tasks),
                                    "seq": self._next_progress_seq(),
                                },
                            )
                        continue
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A transport/error-envelope exception must not skip the reap below; it is
            # re-raised after cleanup so the caller sees the same failure as before.
            stream_error = exc
        finally:
            if pending_read is not None and not pending_read.done():
                pending_read.cancel()

        # Exits that leave background tasks pending reap them here, on the loop thread: grace
        # expiry, an is_error interim result, the stream ending early, or a stream exception
        # (caught above). Cancellation (the hard turn timeout) reaps from the caller thread.
        abandoned = sorted(self._pending_tasks)
        stopped: list[str] = []
        failed: list[str] = []
        if abandoned:
            stopped, failed = await self._reap_pending()
        if abandoned:
            # Any abandoned task leaves work in flight in this session (at minimum the CLI's
            # later 'stopped' notification, per client.stop_task's contract), and the next
            # prompt's stream would read it. healthy=False means: the pool will not re-serve
            # this engine (engine_pool.release) and the bridge will not re-prompt it in-task
            # (drive_to_completion / _commit_message check is_healthy). No disconnect here.
            self.healthy = False

        if stream_error is not None:
            self.healthy = False
            raise stream_error

        if not saw_result or final_result is None:
            self.healthy = False
            return TurnResult(ok=False, result="".join(chunks).strip(), error="stream ended without result")

        ok = not final_result.is_error
        error: str | None = None if ok else "; ".join(final_result.errors or []) or final_result.subtype
        if abandoned and not grace_expired and ok:
            # Stream ended while tasks were pending: the interim text is real but the turn did
            # not complete, so do not report it as success (agy P2-1).
            ok = False
            error = f"stream ended with background tasks pending: {', '.join(abandoned)}"
        elif abandoned and not ok:
            error = f"{error}; background tasks pending at error: {', '.join(abandoned)}"
        if failed:
            ok = False
            error = f"{error + '; ' if error else ''}stop_task failed for: {', '.join(failed)}"
        stop_reason = final_result.stop_reason or final_result.subtype
        if grace_expired:
            # Truncation must be caller-visible (re-panel P2-6) but `error` is contractually
            # set only when ok=false (SPEC.md § reply); carry it in stop_reason instead.
            stop_reason = f"background_hold_expired:{','.join(abandoned) or 'none'}"
        result_text = final_result.result or "".join(chunks).strip()
        if on_event is not None:
            on_event(
                "turn_completed",
                {
                    "turn_id": final_result.session_id,
                    "ok": ok,
                    "stop_reason": stop_reason,
                    # usage covers the LAST CLI turn only; interim_results says how many
                    # earlier ResultMessages this bridge turn absorbed (cold-Opus P2-7).
                    "usage": final_result.usage,
                    "interim_results": interim_results,
                    "abandoned_tasks": abandoned,
                    "stop_requested_tasks": stopped,
                    "stop_failed_tasks": failed,
                    "hold_grace_expired": grace_expired,
                },
            )
        return TurnResult(
            ok=ok,
            result=result_text,
            error=error,
            stop_reason=stop_reason,
            tool_calls=len(tool_ids),
            thread_id=final_result.session_id,
        )

    def steer(self, message: str) -> str:
        # Pre-fix the non-oneshot branch RETURNED a string, which handle_control
        # used as a turn id in a steer_sent milestone — a silent drop reported
        # as success (audit ASK-3, panel-confirmed). Every engine that cannot
        # deliver a steer raises; the bridge then emits steer_failed.
        raise EngineError("agent-sdk does not support mid-turn steer")

    def resume_thread(self, thread_id: str) -> str:
        if self.oneshot:
            raise EngineError("agent-sdk one-shot does not support thread resume")
        if not thread_id:
            raise EngineError("agent-sdk resume requires a thread id")
        try:
            uuid.UUID(thread_id)
        except (AttributeError, TypeError, ValueError) as exc:
            # Broad on purpose: a non-str thread_id raises TypeError/AttributeError
            # from uuid.UUID, and this guard must stay legible, never crash raw.
            raise EngineError(
                f"thread-resume-unavailable: session {thread_id} is not a valid UUID"
            ) from exc
        store = FileSessionStore(self.session_root, self.agent_id)
        key = {"project_key": project_key_for_directory(self.cwd), "session_id": thread_id}
        if self.loop_thread.loop is not None:
            entries = self.loop_thread.submit(store.load(key)).result(timeout=15)
        else:
            entries = asyncio.run(store.load(key))
        if not entries:
            raise EngineError(
                f"thread-resume-unavailable: session {thread_id} not in the session store "
                f"for cwd {self.cwd}"
            )
        self._last_session_id = thread_id
        self._persist_last_session_id(thread_id)
        if self.client is not None:
            try:
                self._disconnect(timeout=10)
                options = self._build_options(explicit_resume=True)
                self._options = options
                self.client = self.client_factory(options=options)
                self.loop_thread.submit(self.client.connect()).result(timeout=30)
            except Exception:
                self.healthy = False
                raise
        return thread_id

    def interrupt(self) -> str:
        if self.client is None or not self._active_turn:
            return "agent-sdk"
        future = self.loop_thread.submit(self.client.interrupt())
        try:
            # Bounded wait: fire-and-forget silently dropped a failed SDK
            # interrupt while the bridge reported cancel_sent (audit ASK-6).
            future.result(timeout=10)
        except Exception as exc:  # noqa: BLE001 - includes FutureTimeoutError
            raise EngineError(f"agent-sdk interrupt failed: {exc}") from exc
        return self._last_session_id or "agent-sdk"

    def stop(self) -> None:
        future = self._active_future
        if future is not None:
            future.cancel()
        self._disconnect(timeout=10)
        self.loop_thread.stop(timeout=5)

    def _message_stream(self) -> AsyncIterator[Any]:
        # receive_response() terminates at the first ResultMessage, which is wrong once a
        # background task is pending. Prefer the open-ended receive_messages() (real
        # ClaudeSDKClient); fall back for minimal fakes that only implement receive_response().
        assert self.client is not None
        stream = getattr(self.client, "receive_messages", None)
        if stream is None:
            return self.client.receive_response()
        return stream()

    def _disconnect(self, *, timeout: float) -> None:
        if self.client is None or self.loop_thread.loop is None:
            return
        try:
            self.loop_thread.submit(self.client.disconnect()).result(timeout=timeout)
        except Exception:
            self.healthy = False

    def is_healthy(self) -> bool:
        return self.healthy

    def _handle_stderr(self, line: str) -> None:
        secrets, var_names = self._scrub_material()
        print(scrub(line, secrets, var_names), flush=True)

    def _scrub_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        text = json.dumps(payload, ensure_ascii=False)
        secrets, var_names = self._scrub_material()
        cleaned = scrub(text, secrets, var_names)
        return json.loads(cleaned)

    def _scrub_material(self) -> tuple[list[str], list[str]]:
        # Include lane-writer / gate-daemon secret values and variable names so
        # stderr and structured transcripts never retain them (Stage 1d-i).
        gate_var_names = [
            "ARB_GATE_READER_DSN",
            "ARB_GATE_READER_ROLE",
            "ARB_GATE_LANE_WRITER_DSN",
            "ARB_GATE_LANE_WRITER_ROLE",
            "ARB_GATE_LANE_WRITER_CONSUMER_ID",
            "ARB_GATE_LANE_WRITER_LANE",
            "ARB_MEMORY_REDIS_URL",
            "ARB_AUDIT_REDIS_URL",
        ]
        gate_secrets = [
            os.environ.get(name, "") for name in gate_var_names if os.environ.get(name)
        ]
        if self.spec.subscription:
            secrets = [
                self.key,
                os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
                *gate_secrets,
            ]
            var_names = ["CLAUDE_CODE_OAUTH_TOKEN", *gate_var_names]
            return secrets, var_names
        return (
            [self.key, *gate_secrets],
            [self.spec.key_env, auth_var(self.spec.auth_style), *gate_var_names],
        )


def local_memory_mcp_agent_sdk_servers(config: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if config is None:
        return {}
    return {
        "arb-memory-local": {
            "command": config["command"],
            "args": config["args"],
            "env": config["env"],
        }
    }
