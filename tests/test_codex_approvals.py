"""CDX-1: every server-initiated codex request gets a response (design v1.2).

Verification obligations V2/V3/V4/V7 from
docs/superpowers/specs/2026-07-08-cdx1-approval-handling-design.md.
Wire shapes pinned by the V1 probe (docs/superpowers/probes/2026-07-08-cdx1-v1-probe/):
method item/commandExecution/requestApproval, availableDecisions in-request,
cancel=deny / accept=allow, error => no execution.

Fixture discipline: the fake records what the ENGINE writes to stdin; it never
answers on the engine's behalf. Deleting the handler leaves the ask unanswered
=> the response assertions go red (deny-proof, fixture cannot short-circuit).
"""
from __future__ import annotations

import queue
from typing import Any

import pytest

from agent_redis_bridge.engines.codex import CodexEngine


APPROVAL_REQUEST = {
    "method": "item/commandExecution/requestApproval",
    "id": 0,
    "params": {
        "threadId": "thread-1",
        "turnId": "turn-1",
        "itemId": "call_abc",
        "reason": "wants to write outside the sandbox",
        "command": "/bin/zsh -lc 'touch /tmp/x'",
        "cwd": "/tmp",
        "availableDecisions": [
            "accept",
            {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["touch"]}},
            "cancel",
        ],
    },
}

TURN_COMPLETED = {
    "method": "turn/completed",
    "params": {"turn": {"id": "turn-1", "status": "completed"}},
}

MODEL_TEXT = {
    "method": "item/agentMessage/delta",
    "params": {"turnId": "turn-1", "itemId": "text-1", "delta": "done"},
}


class ApprovalFakeCodex(CodexEngine):
    """Real turn loop + real _respond path; fake transport."""

    def __init__(self) -> None:
        super().__init__(
            cwd="/tmp",
            model="gpt-5.5",
            approval_policy="on-request",
            sandbox="read-only",
        )
        self.thread_id = "thread-1"
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.sent: list[dict[str, Any]] = []
        self.turn_start_response: dict[str, Any] = {"turn": {"id": "turn-1"}}

    def request(self, method: str, params: dict[str, Any], *, timeout: int) -> dict[str, Any]:
        if method == "turn/start":
            return self.turn_start_response
        return {}

    def _send(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)

    def _get_message(self, *, timeout: float) -> dict[str, Any] | None:
        try:
            return self.messages.get(timeout=min(timeout, 0.05))
        except queue.Empty:
            return None


class RawRequestFakeCodex(CodexEngine):
    """Keeps the REAL request() wait loop (for the inter-turn V3 case)."""

    def __init__(self) -> None:
        super().__init__(
            cwd="/tmp",
            model="gpt-5.5",
            approval_policy="on-request",
            sandbox="read-only",
        )
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.sent: list[dict[str, Any]] = []

    def _send(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)

    def _get_message(self, *, timeout: float) -> dict[str, Any] | None:
        try:
            return self.messages.get(timeout=min(timeout, 0.05))
        except queue.Empty:
            return None


def _responses_to(engine: CodexEngine, request_id: int) -> list[dict[str, Any]]:
    return [
        p for p in engine.sent
        if p.get("id") == request_id and "method" not in p
    ]


def _run(engine: ApprovalFakeCodex, *, policy: str, timeout: int = 5):
    events: list[tuple[str, dict[str, Any]]] = []
    result = engine.run_turn_with_progress(
        "task", timeout=timeout, policy=policy,
        on_event=lambda name, data: events.append((name, data)),
    )
    return result, events


# --- V2: deny-proof ---------------------------------------------------------

def test_human_policy_approval_ask_is_denied_with_cancel_and_turn_completes():
    engine = ApprovalFakeCodex()
    engine.messages.put(dict(APPROVAL_REQUEST))
    engine.messages.put(dict(MODEL_TEXT))
    engine.messages.put(dict(TURN_COMPLETED))

    result, events = _run(engine, policy="human")

    responses = _responses_to(engine, 0)
    assert responses, "approval ask was never answered (CDX-1 regression)"
    assert responses[0]["result"] == {"decision": "cancel"}  # pinned by probe run-C
    denied = [d for n, d in events if n == "command_denied"]
    assert len(denied) == 1
    assert denied[0]["command"] == "/bin/zsh -lc 'touch /tmp/x'"
    assert denied[0]["deny_count"] == 1
    assert denied[0]["deny_budget"] == engine.deny_budget
    assert result.ok  # deny-and-continue: turn ends normally


def test_malformed_params_approval_ask_is_still_answered():
    # cold-Opus r1 P2-D: the injection must precede the params isinstance guard.
    engine = ApprovalFakeCodex()
    engine.messages.put({"method": "item/commandExecution/requestApproval", "id": 7, "params": None})
    engine.messages.put(dict(TURN_COMPLETED))

    _run(engine, policy="human")

    assert _responses_to(engine, 7), "malformed-params ask slipped the closed-world net"


# --- V3: unknown methods, both loops ----------------------------------------

def test_unknown_method_gets_32601_in_turn_loop(caplog):
    engine = ApprovalFakeCodex()
    engine.messages.put({"method": "codex/somethingNew", "id": 3, "params": {}})
    engine.messages.put(dict(TURN_COMPLETED))

    _run(engine, policy="human")

    responses = _responses_to(engine, 3)
    assert responses and responses[0]["error"]["code"] == -32601
    assert "codex/somethingNew" in caplog.text


def test_unknown_approval_shaped_method_takes_deny_path():
    # GLM r1 F1: rename-survivable — name contains "approval" => deny, not error.
    engine = ApprovalFakeCodex()
    engine.messages.put({"method": "item/patch/requestApprovalV9", "id": 4,
                         "params": {"turnId": "turn-1", "command": "rm -rf /"}})
    engine.messages.put(dict(TURN_COMPLETED))

    _run(engine, policy="human")

    responses = _responses_to(engine, 4)
    assert responses and "result" in responses[0]
    assert responses[0]["result"]["decision"] in {"cancel", "denied"}


def test_legacy_approval_methods_are_denied():
    engine = ApprovalFakeCodex()
    engine.messages.put({"method": "execCommandApproval", "id": 5,
                         "params": {"command": "ls"}})
    engine.messages.put({"method": "applyPatchApproval", "id": 6, "params": {}})
    engine.messages.put(dict(TURN_COMPLETED))

    _run(engine, policy="human")

    for rid in (5, 6):
        responses = _responses_to(engine, rid)
        assert responses and "result" in responses[0], f"legacy ask id={rid} unanswered"


def test_server_request_during_inter_turn_wait_is_denied(caplog):
    # V3 inter-turn arm: real request() loop; ask arrives while awaiting a response.
    engine = RawRequestFakeCodex()
    engine.messages.put(dict(APPROVAL_REQUEST))          # server ask, id 0 (own namespace)
    engine.messages.put({"id": 1, "result": {"ok": True}})  # the actual response

    result = engine.request("thread/resume", {"threadId": "t"}, timeout=5)

    assert result == {"ok": True}
    responses = _responses_to(engine, 0)
    # exclude our own outbound request (id 1 with method) — _responses_to already does
    assert responses and responses[0]["result"] == {"decision": "cancel"}
    assert "inter-turn" in caplog.text or "approval" in caplog.text.lower()


# --- V4: inertness ----------------------------------------------------------

def test_trusted_turn_without_asks_is_byte_identical():
    engine = ApprovalFakeCodex()
    engine.messages.put(dict(MODEL_TEXT))
    engine.messages.put(dict(TURN_COMPLETED))

    result, events = _run(engine, policy="trusted")

    assert [n for n, _ in events] == ["turn_started", "model_text", "turn_completed"]
    assert engine.sent == []  # no stdin writes beyond the (stubbed) turn/start
    assert result.ok


def test_trusted_turn_with_ask_allows_and_warns(caplog):
    # Trusted drift (Mark 2026-07-08): allow + WARNING, and no command_denied event.
    engine = ApprovalFakeCodex()
    engine.messages.put(dict(APPROVAL_REQUEST))
    engine.messages.put(dict(TURN_COMPLETED))

    result, events = _run(engine, policy="trusted")

    responses = _responses_to(engine, 0)
    assert responses and responses[0]["result"] == {"decision": "accept"}
    assert not [n for n, _ in events if n == "command_denied"]
    assert "trusted" in caplog.text.lower()
    assert result.ok


# --- V7: deny budget, both D2a arms -----------------------------------------

def _flood(engine: ApprovalFakeCodex, n: int) -> None:
    for i in range(n):
        ask = dict(APPROVAL_REQUEST)
        ask["id"] = 100 + i
        engine.messages.put(ask)


def test_budget_exhaustion_grace_success(monkeypatch):
    monkeypatch.setenv("BRIDGE_APPROVAL_DENY_BUDGET", "2")
    monkeypatch.setenv("BRIDGE_APPROVAL_GRACE_S", "5")
    engine = ApprovalFakeCodex()
    _flood(engine, 3)  # third ask exceeds budget of 2
    engine.messages.put(dict(TURN_COMPLETED))  # arrives within grace

    result, events = _run(engine, policy="human")

    # every ask answered, including the budget-exceeding one (D1 unconditional)
    for rid in (100, 101, 102):
        assert _responses_to(engine, rid), f"ask id={rid} unanswered"
    assert not result.ok
    assert "deny budget exhausted" in (result.error or "")
    interrupts = [p for p in engine.sent if p.get("method") == "turn/interrupt"]
    assert interrupts, "budget exhaustion did not interrupt the turn"
    assert engine.is_healthy() in (True, False)  # engine object still coherent
    assert engine.healthy is False  # ENG-1 D7: deny-exhaust is unclean; retire path identical
    assert engine.active_turn_id is None
    denied = [d for n, d in events if n == "command_denied"]
    assert [d["deny_count"] for d in denied] == [1, 2, 3]


def test_budget_exhaustion_grace_expiry_quarantines(monkeypatch):
    monkeypatch.setenv("BRIDGE_APPROVAL_DENY_BUDGET", "1")
    monkeypatch.setenv("BRIDGE_APPROVAL_GRACE_S", "0.2")
    engine = ApprovalFakeCodex()
    _flood(engine, 2)
    # no turn/completed: codex is wedged

    result, _ = _run(engine, policy="human", timeout=30)

    assert not result.ok
    assert "deny budget exhausted" in (result.error or "")
    assert engine.healthy is False  # grace expiry: pool must quarantine
    assert engine.active_turn_id is None
    interrupts = [p for p in engine.sent if p.get("method") == "turn/interrupt"]
    assert interrupts


def test_budget_resets_between_turns(monkeypatch):
    monkeypatch.setenv("BRIDGE_APPROVAL_DENY_BUDGET", "2")
    engine = ApprovalFakeCodex()
    _flood(engine, 2)  # exactly at budget: no exhaustion
    engine.messages.put(dict(TURN_COMPLETED))
    result1, _ = _run(engine, policy="human")
    assert result1.ok

    _flood(engine, 2)
    engine.messages.put(dict(TURN_COMPLETED))
    result2, _ = _run(engine, policy="human")
    assert result2.ok, "deny count leaked across turns"


# --- D3: startup notice ------------------------------------------------------

def test_d3_notice_warns_when_human_sender_reachable():
    from agent_redis_bridge.bridge import codex_approval_path_notice
    level, msg = codex_approval_path_notice(
        engine="codex", bypass=False,
        sender_policies={"human-codexctl": "human", "claude-x": "trusted"},
        unknown_sender_policy="reject",
    )
    assert level == "warning" and "LIVE" in msg


def test_d3_notice_warns_on_unknown_sender_policy_human():
    # agy r2 P2: the fallback alone makes the path reachable with an empty roster.
    from agent_redis_bridge.bridge import codex_approval_path_notice
    level, _ = codex_approval_path_notice(
        engine="codex", bypass=False, sender_policies={}, unknown_sender_policy="human",
    )
    assert level == "warning"


def test_d3_notice_info_when_compliant_and_none_for_bypass_or_other_engines():
    from agent_redis_bridge.bridge import codex_approval_path_notice
    level, _ = codex_approval_path_notice(
        engine="codex", bypass=False,
        sender_policies={"a": "trusted", "b": "reject"}, unknown_sender_policy="reject",
    )
    assert level == "info"
    assert codex_approval_path_notice(
        engine="codex", bypass=True, sender_policies={"a": "human"}, unknown_sender_policy="human",
    ) is None
    assert codex_approval_path_notice(
        engine="agy-print", bypass=False, sender_policies={"a": "human"}, unknown_sender_policy="human",
    ) is None


# --- Impl-panel P2 remediations (run panel-cdx1impl-20260708T135206Z-db38be) --

def test_inter_turn_deny_does_not_count_against_budget():
    # M3 impl P2: design D2 — inter-turn denies are NOT budget-counted.
    engine = RawRequestFakeCodex()
    engine.messages.put(dict(APPROVAL_REQUEST))
    engine.messages.put({"id": 1, "result": {"ok": True}})

    engine.request("thread/resume", {"threadId": "t"}, timeout=5)

    assert engine._deny_count == 0


def test_inter_turn_unknown_non_approval_method_gets_32601(caplog):
    # GLM impl F1: the design's "both loops" claim, asserted for the -32601 arm.
    engine = RawRequestFakeCodex()
    engine.messages.put({"method": "codex/telemetryPing", "id": 9, "params": {}})
    engine.messages.put({"id": 1, "result": {"ok": True}})

    engine.request("thread/resume", {"threadId": "t"}, timeout=5)

    responses = _responses_to(engine, 9)
    assert responses and responses[0]["error"]["code"] == -32601


def test_ask_arriving_mid_grace_is_still_answered(monkeypatch):
    # cold-Opus impl P2: the D2a drain answers per D1 throughout the grace window.
    monkeypatch.setenv("BRIDGE_APPROVAL_DENY_BUDGET", "1")
    monkeypatch.setenv("BRIDGE_APPROVAL_GRACE_S", "5")
    engine = ApprovalFakeCodex()
    _flood(engine, 2)               # exceeds budget of 1 -> enters grace
    mid_grace_ask = dict(APPROVAL_REQUEST)
    mid_grace_ask["id"] = 200
    engine.messages.put(mid_grace_ask)   # arrives during the grace drain
    engine.messages.put(dict(TURN_COMPLETED))

    result, _ = _run(engine, policy="human")

    assert not result.ok and "deny budget exhausted" in (result.error or "")
    responses = _responses_to(engine, 200)
    assert responses and "result" in responses[0], "mid-grace ask left unanswered"
    assert engine.healthy is False  # ENG-1 D7: deny-exhaust is unclean


def test_stale_message_after_grace_completion_stays_unconsumed(monkeypatch):
    # GLM impl F2: V7's no-stale-leak obligation, asserted with an injected straggler.
    monkeypatch.setenv("BRIDGE_APPROVAL_DENY_BUDGET", "1")
    monkeypatch.setenv("BRIDGE_APPROVAL_GRACE_S", "5")
    engine = ApprovalFakeCodex()
    _flood(engine, 2)
    engine.messages.put(dict(TURN_COMPLETED))
    straggler = {"method": "item/agentMessage/delta",
                 "params": {"turnId": "turn-1", "itemId": "stale", "delta": "STALE"}}
    engine.messages.put(dict(straggler))

    result, _ = _run(engine, policy="human")
    assert not result.ok

    # turn N+1 gets a FRESH turn id (as real codex mints one per turn) — the
    # straggler carries turn-1 and must not surface in turn-2's output.
    engine.turn_start_response = {"turn": {"id": "turn-2"}}
    engine.messages.put({"method": "item/agentMessage/delta",
                         "params": {"turnId": "turn-2", "itemId": "text-2", "delta": "fresh"}})
    engine.messages.put({"method": "turn/completed",
                         "params": {"turn": {"id": "turn-2", "status": "completed"}}})
    result2, events2 = _run(engine, policy="human")
    assert result2.ok
    deltas = [d.get("delta") for n, d in events2 if n == "model_text"]
    assert deltas == ["fresh"]
