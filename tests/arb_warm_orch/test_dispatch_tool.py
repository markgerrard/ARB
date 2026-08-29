"""Warm-orch test-drive slice: dispatch_seat typed tool (buzz control-plane brief).

The tool is the structural fix for lens collapse: `lens` is REQUIRED in the
JSON schema, so a dispatch without a lens is unrepresentable at the tool-call
layer rather than discouraged in prose. Handlers call an injected dispatcher —
dispatch logic stays external to the orch process (orchestrator-non-finality).
Stub seat first; the subprocess dispatcher against scripts/agent-dispatch is a
later slice. One test per named behavior, red-first.
"""
from __future__ import annotations

import asyncio
import json

from arb_warm_orch.dispatch import (
    DispatchResult,
    StubSeatDispatcher,
    build_dispatch_seat_tool,
)


def _tool(dispatcher=None):
    return build_dispatch_seat_tool(dispatcher or StubSeatDispatcher())


def _call(tool, args):
    return asyncio.run(tool.handler(args))


# ---------------------------------------------------------------- schema


def test_tool_is_named_dispatch_seat():
    assert _tool().name == "dispatch_seat"


def test_schema_requires_seat_lens_and_task():
    schema = _tool().input_schema
    assert set(schema["required"]) == {"seat", "lens", "task"}


def test_schema_declares_string_types_for_all_three():
    schema = _tool().input_schema
    for field in ("seat", "lens", "task"):
        assert schema["properties"][field]["type"] == "string"


# ---------------------------------------------------------------- handler


def test_handler_invokes_dispatcher_with_seat_lens_task():
    stub = StubSeatDispatcher()
    tool = build_dispatch_seat_tool(stub)
    _call(tool, {"seat": "codex-sol", "lens": "correctness", "task": "review X"})
    assert stub.calls == [("codex-sol", "correctness", "review X")]


def test_handler_returns_dispatcher_result_as_json_text():
    stub = StubSeatDispatcher(
        result=DispatchResult(status="completed", seat="codex-sol", reply="LGTM")
    )
    out = _call(build_dispatch_seat_tool(stub), {"seat": "codex-sol", "lens": "risk", "task": "t"})
    payload = json.loads(out["content"][0]["text"])
    assert payload["status"] == "completed"
    assert payload["reply"] == "LGTM"


# ------------------------------------------------- blank-field refusals
#
# The JSON schema makes the keys required; it cannot make them non-blank.
# The handler refuses whitespace-only values with a SPECIFIC problem code
# (refusal-is-ambient-assert-the-code): a blank lens is lens collapse
# wearing a satisfied schema.


def test_handler_refuses_blank_lens_with_specific_code():
    out = _call(_tool(), {"seat": "s", "lens": "   ", "task": "t"})
    assert out["is_error"] is True
    assert "dispatch-lens-blank" in out["content"][0]["text"]


def test_handler_refuses_blank_seat_with_specific_code():
    out = _call(_tool(), {"seat": "", "lens": "correctness", "task": "t"})
    assert out["is_error"] is True
    assert "dispatch-seat-blank" in out["content"][0]["text"]


def test_handler_refuses_blank_task_with_specific_code():
    out = _call(_tool(), {"seat": "s", "lens": "correctness", "task": "\n"})
    assert out["is_error"] is True
    assert "dispatch-task-blank" in out["content"][0]["text"]


def test_blank_field_refusal_never_reaches_dispatcher():
    stub = StubSeatDispatcher()
    tool = build_dispatch_seat_tool(stub)
    _call(tool, {"seat": "s", "lens": " ", "task": "t"})
    assert stub.calls == []


# ---------------------------------------------------------------- errors


def test_dispatcher_failure_surfaces_as_error_with_specific_code():
    class ExplodingDispatcher:
        def dispatch(self, seat, lens, task):
            raise RuntimeError("bus unreachable")

    out = _call(build_dispatch_seat_tool(ExplodingDispatcher()), {
        "seat": "s", "lens": "correctness", "task": "t",
    })
    assert out["is_error"] is True
    text = out["content"][0]["text"]
    assert "dispatch-failed" in text
    assert "bus unreachable" in text
