"""dispatch_seat typed tool — lens required in the schema, dispatcher injected.

`lens` being schema-required makes a lens-less dispatch unrepresentable at the
tool-call layer (the structural fix for lens collapse). The handler refuses
whitespace-only values with specific problem codes: a blank lens is lens
collapse wearing a satisfied schema.

Dispatch logic stays OUTSIDE the orch process: the handler calls whatever
dispatcher object it was built with. StubSeatDispatcher serves tests and the
stub-seat test-drive; a subprocess dispatcher over scripts/agent-dispatch is
the live path.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from claude_agent_sdk import SdkMcpTool, tool

DISPATCH_SEAT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "seat": {
            "type": "string",
            "description": "Bridge seat id to dispatch to (e.g. codex-sol, agy).",
        },
        "lens": {
            "type": "string",
            "description": (
                "Review/work lens this dispatch is scoped to (e.g. correctness, "
                "security, repro). Required: a dispatch without a lens is not "
                "representable."
            ),
        },
        "task": {
            "type": "string",
            "description": "The task brief for the seat.",
        },
    },
    "required": ["seat", "lens", "task"],
}


@dataclass(frozen=True)
class DispatchResult:
    """Structured outcome of one seat dispatch."""

    status: str
    seat: str
    reply: str = ""


class SeatDispatcher(Protocol):
    def dispatch(self, seat: str, lens: str, task: str) -> DispatchResult: ...


@dataclass
class StubSeatDispatcher:
    """Records calls and returns a canned result — the stub seat."""

    result: DispatchResult = field(
        default_factory=lambda: DispatchResult(status="completed", seat="stub", reply="")
    )
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    def dispatch(self, seat: str, lens: str, task: str) -> DispatchResult:
        self.calls.append((seat, lens, task))
        return self.result


def _error(code: str, detail: str = "") -> dict[str, Any]:
    text = f"{code}: {detail}" if detail else code
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def build_dispatch_seat_tool(dispatcher: SeatDispatcher) -> SdkMcpTool[Any]:
    @tool("dispatch_seat", "Dispatch a task to a bridge seat under a named lens.", DISPATCH_SEAT_SCHEMA)
    async def dispatch_seat(args: dict[str, Any]) -> dict[str, Any]:
        for name, code in (
            ("seat", "dispatch-seat-blank"),
            ("lens", "dispatch-lens-blank"),
            ("task", "dispatch-task-blank"),
        ):
            if not str(args.get(name, "")).strip():
                return _error(code, f"{name!r} must be a non-blank string")
        try:
            result = dispatcher.dispatch(args["seat"], args["lens"], args["task"])
        except Exception as exc:
            return _error("dispatch-failed", str(exc))
        return {"content": [{"type": "text", "text": json.dumps(asdict(result))}]}

    return dispatch_seat
