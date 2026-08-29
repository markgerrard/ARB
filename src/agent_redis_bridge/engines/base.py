from __future__ import annotations

import os
from dataclasses import dataclass
import os
from typing import Any, Callable, Protocol


ProgressCallback = Callable[[str, dict[str, Any]], None]


class EngineError(RuntimeError):
    pass


def engine_init_timeout(default: int = 60) -> int:
    """Start/initialize handshake budget (seconds). DSP-1: 15s sat ~2s above codex's
    normal first-after-idle startup; 60 costs nothing unless a start is genuinely wedged."""
    try:
        return int(os.environ.get("BRIDGE_ENGINE_INIT_TIMEOUT_S", str(default)))
    except ValueError:
        return default


def parse_tool_allowlist(
    csv: str | None,
    *,
    surface_name: str = "BRIDGE_PI_TOOLS",
    fallback_hint: str,
) -> tuple[str, ...]:
    """Parse a comma-separated tool allowlist, refusing a DEGENERATE value.

    Returns ``()`` for an unset surface (``None`` / ``""``), which legitimately
    means "use the engine's own default toolset". Any OTHER value that yields no
    tool names — ``","``, ``"   "`` — is an operator typo, and is refused here
    rather than silently falling back to the FULL toolset.

    The condition is ``csv not in (None, "")``, deliberately NOT the
    ``csv and csv.strip()`` form this replaces: the latter SKIPS a whitespace-only
    value (``"   "`` is truthy but ``.strip()`` is falsy), so it failed OPEN on
    exactly the typo it existed to catch. That shape shipped in pi_sdk and was
    absent entirely from pi_rpc; one shared implementation is what keeps the three
    call sites (pi-sdk, pi-rpc, omp-acp) from drifting apart again.
    """
    parsed = tuple(t.strip() for t in (csv or "").split(",") if t.strip())
    if csv not in (None, "") and not parsed:
        raise EngineError(
            f"{surface_name}={csv!r} parses to empty tool list. "
            "Set a real comma-separated tool list (e.g. 'read,grep,find,ls') or "
            f"unset {surface_name} to accept {fallback_hint}."
        )
    return parsed


@dataclass(frozen=True)
class TurnResult:
    ok: bool
    result: str
    error: str | None = None
    # Set by the completion gate (Bridge.process_request): the post-turn
    # working-tree decision — {state, head_before, head_after, dirty_files}.
    completion: dict[str, Any] | None = None
    # Drive-to-completion loop inputs (only read when the engine declares
    # supports_continuation). stop_reason is the ACP stopReason ("end_turn",
    # "cancelled", "refusal", …); tool_calls is how many distinct tool calls
    # ran this turn (0 + empty output on end_turn => likely a limit/refusal).
    stop_reason: str | None = None
    tool_calls: int = 0
    # Engine conversation/thread/session id that served the request, when known.
    thread_id: str | None = None


class AgentEngine(Protocol):
    # Engines that keep a reusable session and can be re-prompted with carried
    # context set this True. Default False => the drive-to-completion loop is
    # skipped (single bounce) for engines that can't safely continue.
    supports_continuation: bool = False
    # Explicit capability for caller-supplied thread/session re-entry. This is
    # intentionally distinct from the bridge's internal drive-to-completion loop.
    supports_thread_resume: bool = False
    # Engines that consume BRIDGE_ROLE_PROFILE_FILE natively set this True so
    # the bridge does not also wrap the turn prompt with the same role profile.
    consumes_role_profile: bool = False

    def start(self) -> None: ...

    def run_turn_with_progress(
        self,
        task: str,
        *,
        timeout: int,
        policy: str,
        on_event: ProgressCallback | None,
    ) -> TurnResult: ...

    def steer(self, message: str) -> str: ...

    def interrupt(self) -> str: ...

    def stop(self) -> None: ...
