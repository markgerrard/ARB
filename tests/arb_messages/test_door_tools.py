import asyncio
import base64
import json
from types import SimpleNamespace

import pytest
from arb_messages import store
import arb_messages.mcp.door_tools as door_tools
from arb_messages.config import Settings
from arb_messages.mcp.door_tools import MessagesTools

def settings(**overrides):
    base = dict(postgres_dsn="unused-in-these-tests", allowed_agents=frozenset({"claude.ai"}),
                allowed_providers=frozenset({"cloudflare"}))
    base.update(overrides)
    return Settings(**base)

class FakeConnFactory:
    def __init__(self): self.enqueued = []
    def __call__(self): return self
    def __enter__(self): return self
    def __exit__(self, *a): pass

def test_scope_denial_emits_audit_and_never_enqueues():
    # Plan-review r2 (agy-print P1): request_id is now the first positional parameter --
    # all messages_request call sites in these tests updated accordingly.
    audits = []
    tools = MessagesTools(FakeConnFactory(), settings(),
        require_scope=lambda s: (_ for _ in ()).throw(PermissionError("no scope")),
        actor=lambda: "agent-a", connector_host=lambda: "claude.ai", audit_sink=audits.append)
    with pytest.raises(PermissionError):
        asyncio.run(tools.messages_request("r1", "zone_dns_edit", "cloudflare"))
    assert any(a.get("event") == "denied" for a in audits)

def test_missing_client_id_fails_closed_not_mcp_fallback():
    tools = MessagesTools(FakeConnFactory(), settings(), require_scope=lambda s: None,
        actor=lambda: None)  # simulates absent client_id
    with pytest.raises(PermissionError):
        asyncio.run(tools.messages_register_key("some-pubkey-b64"))

def test_agent_id_always_from_actor_never_a_parameter():
    import inspect
    sig = inspect.signature(MessagesTools.messages_request)
    assert "agent_id" not in sig.parameters
    sig2 = inspect.signature(MessagesTools.messages_register_key)
    assert "agent_id" not in sig2.parameters
    sig3 = inspect.signature(MessagesTools.messages_poll)
    assert "agent_id" not in sig3.parameters

# Plan-review r4 (codex P2): the door's actor lower-casing (§ Interface, _actor()) was specified
# but never directly tested -- an implementation could still pass every other test while missing
# it, and Settings.allowed_agents is lower-cased at config load, so a mixed-case real OAuth
# client_id would otherwise be spuriously denied.
def test_actor_mixed_case_client_id_is_normalized_to_lowercase():
    tools = MessagesTools(FakeConnFactory(), settings(),
        require_scope=lambda s: None, actor=lambda: "Agent-A")
    assert tools._actor() == "agent-a"

def test_connector_not_in_allowlist_denied():
    tools = MessagesTools(FakeConnFactory(), settings(), require_scope=lambda s: None,
        actor=lambda: "agent-a", connector_host=lambda: "some-other-connector")
    with pytest.raises(ValueError):
        asyncio.run(tools.messages_request("r1", "zone_dns_edit", "cloudflare"))

def test_unrecognized_connector_fails_closed():
    # _connector_host() must fail closed (PermissionError, caught and re-raised as a denial) when
    # the client's redirect_uri doesn't map to any recognized category -- not silently allow.
    tools = MessagesTools(FakeConnFactory(), settings(), require_scope=lambda s: None,
        actor=lambda: "agent-a", connector_host=lambda: None)
    with pytest.raises(ValueError):
        asyncio.run(tools.messages_request("r1", "zone_dns_edit", "cloudflare"))

# Direct regression guard for connector_host being a genuinely SEPARATE identity axis from
# client_id (_actor()) -- found for ARB Messages' generalization: client_id churns per
# re-authorization (bad for a low-maintenance allowlist), but MUST stay the identity used for
# delivery/key-registration scoping (collapsing to a shared connector_host would let concurrent
# sessions of the same connector, which register distinct keys, interfere with each other).
def test_connector_host_and_actor_are_independent_identities():
    tools = MessagesTools(FakeConnFactory(), settings(),
        require_scope=lambda s: None, actor=lambda: "session-a-client-id",
        connector_host=lambda: "claude.ai")
    assert tools._actor() == "session-a-client-id"
    assert tools._connector_host() == "claude.ai"
    tools2 = MessagesTools(FakeConnFactory(), settings(),
        require_scope=lambda s: None, actor=lambda: "session-b-client-id",
        connector_host=lambda: "claude.ai")
    # two different sessions of the same connector: same allowlisted category, different identity
    assert tools2._actor() != tools._actor()
    assert tools2._connector_host() == tools._connector_host()

# Direct regression guard for the structural provider allowlist added after the generalization
# review (agy-print, cold-Opus, pi-GLM independently flagged the absence of any structural check
# beyond agent identity as a defense-in-depth gap for a system that could carry high-stakes
# requests). Denied before the queue is ever touched, same layer as the connector-allowlist check.
def test_provider_not_in_allowlist_denied():
    audits = []
    tools = MessagesTools(FakeConnFactory(), settings(allowed_providers=frozenset({"cloudflare"})),
        require_scope=lambda s: None, actor=lambda: "agent-a", connector_host=lambda: "claude.ai",
        audit_sink=audits.append)
    with pytest.raises(ValueError):
        asyncio.run(tools.messages_request("r1", "do something", "not-an-allowed-provider"))
    assert any(a.get("event") == "denied" and a.get("reason") == "provider not allowlisted" for a in audits)

# Direct regression guard: the sealed body is raw ciphertext (not valid UTF-8), and the MCP
# JSON-RPC layer serializes tool results to JSON. Returning bytes straight through crashed
# serialization AFTER read_and_mark_delivered() had already committed delivered_at -- the payload
# became permanently unrecoverable through the API on the first real credential delivered in prod
# (2026-07-02). The body must be base64-encoded before it leaves the door.
def test_messages_poll_body_is_json_safe_base64(monkeypatch):
    raw_sealed = b"\xff\xfe\x00not-utf8-sealed-bytes"
    monkeypatch.setattr(store, "read_and_mark_delivered",
        lambda conn, agent_id, request_id: {"status": "delivered", "body": raw_sealed})
    tools = MessagesTools(FakeConnFactory(), settings(), require_scope=lambda s: None,
        actor=lambda: "agent-a")
    result = asyncio.run(tools.messages_poll("r1"))
    json.dumps(result)  # must not raise
    assert "body" not in result
    assert isinstance(result["body_b64"], str)
    assert base64.b64decode(result["body_b64"]) == raw_sealed

def test_messages_poll_already_delivered_body_is_json_safe_base64(monkeypatch):
    raw_sealed = b"\xff\xfe\x00not-utf8-sealed-bytes"
    monkeypatch.setattr(store, "read_and_mark_delivered",
        lambda conn, agent_id, request_id: {"status": "already_delivered", "body": raw_sealed})
    tools = MessagesTools(FakeConnFactory(), settings(), require_scope=lambda s: None,
        actor=lambda: "agent-a")
    result = asyncio.run(tools.messages_poll("r1"))
    json.dumps(result)  # must not raise
    assert result["status"] == "already_delivered"
    assert "body" not in result
    assert isinstance(result["body_b64"], str)
    assert base64.b64decode(result["body_b64"]) == raw_sealed

def test_messages_request_delivered_body_is_json_safe_base64(monkeypatch):
    raw_sealed = b"\xff\xfe\x00not-utf8-sealed-bytes"
    monkeypatch.setattr(store, "read_and_mark_delivered",
        lambda conn, agent_id, request_id: {"status": "delivered", "body": raw_sealed})
    monkeypatch.setattr(store, "enqueue", lambda *a, **k: 1)
    import arb_messages.keys as keys_module
    monkeypatch.setattr(keys_module, "live_key", lambda conn, agent_id: b"x" * 32)
    tools = MessagesTools(FakeConnFactory(), settings(), require_scope=lambda s: None,
        actor=lambda: "agent-a", connector_host=lambda: "claude.ai")
    result = asyncio.run(tools.messages_request("r1", "zone_dns_edit", "cloudflare"))
    json.dumps(result)  # must not raise
    assert "body" not in result
    assert isinstance(result["body_b64"], str)
    assert base64.b64decode(result["body_b64"]) == raw_sealed

def test_messages_request_returns_immediately_without_sleeping(monkeypatch):
    calls = {"read": 0, "sleep": 0}

    async def recording_sleep(_seconds):
        calls["sleep"] += 1

    def read_and_mark_delivered(conn, agent_id, request_id):
        calls["read"] += 1
        return {"status": "pending"}

    monkeypatch.setattr(door_tools, "anyio", SimpleNamespace(sleep=recording_sleep), raising=False)
    monkeypatch.setattr("arb_messages.mcp.door_tools.store.read_and_mark_delivered",
        read_and_mark_delivered)
    monkeypatch.setattr("arb_messages.mcp.door_tools.store.enqueue", lambda *a, **k: 1)
    monkeypatch.setattr("arb_messages.mcp.door_tools.keys.live_key",
        lambda conn, agent_id: b"x" * 32)
    tools = MessagesTools(FakeConnFactory(), settings(), require_scope=lambda s: None,
        actor=lambda: "agent-a", connector_host=lambda: "claude.ai")
    result = asyncio.run(tools.messages_request("r1", "zone_dns_edit", "cloudflare"))
    assert result == {"status": "pending", "request_id": "r1"}
    assert calls["read"] == 1
    assert calls["sleep"] == 0

def test_messages_request_passes_through_claimed(monkeypatch):
    calls = {"read": 0, "sleep": 0}

    async def recording_sleep(_seconds):
        calls["sleep"] += 1

    def read_and_mark_delivered(conn, agent_id, request_id):
        calls["read"] += 1
        return {"status": "claimed"}

    monkeypatch.setattr(door_tools, "anyio", SimpleNamespace(sleep=recording_sleep), raising=False)
    monkeypatch.setattr("arb_messages.mcp.door_tools.store.read_and_mark_delivered",
        read_and_mark_delivered)
    monkeypatch.setattr("arb_messages.mcp.door_tools.store.enqueue", lambda *a, **k: 1)
    monkeypatch.setattr("arb_messages.mcp.door_tools.keys.live_key",
        lambda conn, agent_id: b"x" * 32)
    tools = MessagesTools(FakeConnFactory(), settings(), require_scope=lambda s: None,
        actor=lambda: "agent-a", connector_host=lambda: "claude.ai")
    result = asyncio.run(tools.messages_request("r1", "zone_dns_edit", "cloudflare"))
    assert result == {"status": "claimed", "request_id": "r1"}
    assert calls["read"] == 1
    assert calls["sleep"] == 0

def test_messages_request_retry_returns_already_fulfilled_result(monkeypatch):
    raw_sealed = b"\xff\xfe\x00not-utf8-sealed-bytes"
    calls = {"read": 0, "sleep": 0}

    async def recording_sleep(_seconds):
        calls["sleep"] += 1

    def read_and_mark_delivered(conn, agent_id, request_id):
        calls["read"] += 1
        return {"status": "already_delivered", "body": raw_sealed}

    monkeypatch.setattr(door_tools, "anyio", SimpleNamespace(sleep=recording_sleep), raising=False)
    monkeypatch.setattr("arb_messages.mcp.door_tools.store.read_and_mark_delivered",
        read_and_mark_delivered)
    monkeypatch.setattr("arb_messages.mcp.door_tools.store.enqueue", lambda *a, **k: 1)
    monkeypatch.setattr("arb_messages.mcp.door_tools.keys.live_key",
        lambda conn, agent_id: b"x" * 32)
    tools = MessagesTools(FakeConnFactory(), settings(), require_scope=lambda s: None,
        actor=lambda: "agent-a", connector_host=lambda: "claude.ai")
    result = asyncio.run(tools.messages_request("r1", "zone_dns_edit", "cloudflare"))
    json.dumps(result)
    assert result["status"] == "already_delivered"
    assert "body" not in result
    assert base64.b64decode(result["body_b64"]) == raw_sealed
    assert calls["read"] == 1
    assert calls["sleep"] == 0

def test_encode_delivery_accepts_memoryview(monkeypatch):
    raw_sealed = b"\xff\xfe\x00not-utf8-sealed-bytes"
    monkeypatch.setattr(store, "read_and_mark_delivered",
        lambda conn, agent_id, request_id: {"status": "delivered", "body": memoryview(raw_sealed)})
    tools = MessagesTools(FakeConnFactory(), settings(), require_scope=lambda s: None,
        actor=lambda: "agent-a")
    result = asyncio.run(tools.messages_poll("r1"))
    json.dumps(result)  # must not raise
    assert "body" not in result
    assert base64.b64decode(result["body_b64"]) == raw_sealed

def test_encode_delivery_rejects_non_bytes_body_loudly(monkeypatch):
    monkeypatch.setattr(store, "read_and_mark_delivered",
        lambda conn, agent_id, request_id: {"status": "delivered", "body": "not-bytes"})
    tools = MessagesTools(FakeConnFactory(), settings(), require_scope=lambda s: None,
        actor=lambda: "agent-a")
    with pytest.raises(TypeError, match="sealed body must be bytes-like, got str"):
        asyncio.run(tools.messages_poll("r1"))

def test_request_id_is_a_caller_supplied_parameter():
    # Plan-review r2 (agy-print P1): request_id must be a real, caller-supplied parameter, not
    # server-generated -- the direct regression guard for the missing-parameter bug. (The actual
    # retry-lands-on-the-same-row dedup behavior is covered by Task 2's
    # test_enqueue_dedupes_same_agent_request_id, exercised against the real store; this test
    # guards the door-tool signature specifically.)
    import inspect
    sig = inspect.signature(MessagesTools.messages_request)
    assert "request_id" in sig.parameters
