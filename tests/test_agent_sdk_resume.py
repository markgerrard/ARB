from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from claude_agent_sdk import project_key_for_directory

from agent_redis_bridge.engines.agent_sdk import AgentSdkEngine
from agent_redis_bridge.engines.agent_sdk_continuation import (
    ContinuationWorkspaceError,
    ContinuationWorkspaceStore,
)
from agent_redis_bridge.engines.agent_sdk_session import (
    _key_path,
)
from agent_redis_bridge.engines.base import EngineError


class FakeClient:
    def __init__(self) -> None:
        self.connected = False
        self.disconnected = False

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True


def _engine(
    tmp_path: Path,
    *,
    model: str,
    client_factory=None,
) -> AgentSdkEngine:
    cwd = tmp_path / "worktree"
    cwd.mkdir(exist_ok=True)
    return AgentSdkEngine(
        cwd=str(cwd),
        model=model,
        tool_ceiling="Read",
        key="test-key",
        session_root=tmp_path / "sessions",
        agent_id="agent/sdk-resume",
        startup_probe=False,
        client_factory=client_factory or (lambda **kwargs: FakeClient()),
    )


def _seed_session(engine: AgentSdkEngine, session_id: str) -> None:
    path = _session_path(engine, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"type":"user","message":"stored"}\n', encoding="utf-8")


def _session_path(engine: AgentSdkEngine, session_id: str) -> Path:
    key = {
        "project_key": project_key_for_directory(engine.cwd),
        "session_id": session_id,
    }
    return _key_path(engine.session_root, engine.agent_id, key)


def test_subscription_options_resume_only_when_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-key")
    engine = _engine(tmp_path, model="sonnet-5")
    session_id = str(uuid.uuid4())
    engine._last_session_id = session_id

    assert engine._build_subscription_options().resume is None
    assert engine._build_subscription_options(explicit_resume=True).resume == session_id


def test_subscription_build_options_forwards_explicit_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-key")
    engine = _engine(tmp_path, model="sonnet-5")
    session_id = str(uuid.uuid4())
    engine._last_session_id = session_id

    assert engine._build_options(explicit_resume=True).resume == session_id


def test_subscription_resume_thread_reconnects_from_session_store(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-key")
    captured_options = []
    clients = []

    def factory(**kwargs):
        captured_options.append(kwargs["options"])
        client = FakeClient()
        clients.append(client)
        return client

    engine = _engine(tmp_path, model="sonnet-5", client_factory=factory)
    session_id = str(uuid.uuid4())
    _seed_session(engine, session_id)
    engine.start()
    try:
        assert engine.resume_thread(session_id) == session_id
        assert [options.resume for options in captured_options] == [None, session_id]
        assert clients[0].disconnected
        assert clients[1].connected
    finally:
        engine.stop()


def test_subscription_resume_thread_store_miss_fails_before_reconnect(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-key")
    clients = []

    def factory(**kwargs):
        client = FakeClient()
        clients.append(client)
        return client

    engine = _engine(tmp_path, model="sonnet-5", client_factory=factory)
    engine.start()
    try:
        with pytest.raises(EngineError, match="thread-resume-unavailable"):
            engine.resume_thread(str(uuid.uuid4()))
        assert len(clients) == 1
        assert not clients[0].disconnected
    finally:
        engine.stop()


def test_subscription_resume_thread_empty_session_fails_before_reconnect(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-key")
    clients = []

    def factory(**kwargs):
        client = FakeClient()
        clients.append(client)
        return client

    engine = _engine(tmp_path, model="sonnet-5", client_factory=factory)
    session_id = str(uuid.uuid4())
    session_path = _session_path(engine, session_id)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text("", encoding="utf-8")
    engine.start()
    try:
        with pytest.raises(EngineError, match="thread-resume-unavailable"):
            engine.resume_thread(session_id)
        assert len(clients) == 1
        assert not clients[0].disconnected
    finally:
        engine.stop()


def test_subscription_resume_thread_rejects_non_uuid_id(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-key")
    clients = []

    def factory(**kwargs):
        client = FakeClient()
        clients.append(client)
        return client

    engine = _engine(tmp_path, model="sonnet-5", client_factory=factory)
    session_id = "not-a-uuid"
    _seed_session(engine, session_id)
    engine.start()
    try:
        with pytest.raises(EngineError, match=f"thread-resume-unavailable.*{session_id}.*UUID"):
            engine.resume_thread(session_id)
        assert len(clients) == 1
        assert not clients[0].disconnected
    finally:
        engine.stop()


def test_subscription_resume_thread_reconnect_failure_marks_engine_unhealthy(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-key")
    clients = []

    class ReconnectFailingClient(FakeClient):
        def __init__(self, fail_connect: bool) -> None:
            super().__init__()
            self.fail_connect = fail_connect

        async def connect(self) -> None:
            if self.fail_connect:
                raise RuntimeError("reconnect failed")
            await super().connect()

    def factory(**kwargs):
        client = ReconnectFailingClient(fail_connect=bool(clients))
        clients.append(client)
        return client

    engine = _engine(tmp_path, model="sonnet-5", client_factory=factory)
    session_id = str(uuid.uuid4())
    _seed_session(engine, session_id)
    engine.start()
    try:
        with pytest.raises(RuntimeError, match="reconnect failed"):
            engine.resume_thread(session_id)
        assert engine.healthy is False
    finally:
        engine.stop()


def test_api_key_resume_thread_reconnects_from_session_store(tmp_path):
    captured_options = []

    def factory(**kwargs):
        captured_options.append(kwargs["options"])
        return FakeClient()

    engine = _engine(tmp_path, model="minimax-m3", client_factory=factory)
    session_id = str(uuid.uuid4())
    _seed_session(engine, session_id)
    engine.start()
    try:
        assert engine.resume_thread(session_id) == session_id
        assert [options.resume for options in captured_options] == [None, session_id]
    finally:
        engine.stop()


def test_api_key_resume_thread_store_miss_fails_before_reconnect(tmp_path):
    clients = []

    def factory(**kwargs):
        client = FakeClient()
        clients.append(client)
        return client

    engine = _engine(tmp_path, model="minimax-m3", client_factory=factory)
    engine.start()
    try:
        with pytest.raises(EngineError, match="thread-resume-unavailable"):
            engine.resume_thread(str(uuid.uuid4()))
        assert len(clients) == 1
        assert not clients[0].disconnected
    finally:
        engine.stop()


def test_continuation_workspace_store_binds_session_to_sender_and_worktree(tmp_path):
    store = ContinuationWorkspaceStore(tmp_path / "sessions", "asdk-test")

    store.record(thread_id="session-1", sender="claude-owner", worktree_name="session-1")

    assert store.load("session-1").sender == "claude-owner"
    assert store.load("session-1").worktree_name == "session-1"


def test_continuation_workspace_store_refuses_conflicting_rebind(tmp_path):
    store = ContinuationWorkspaceStore(tmp_path / "sessions", "asdk-test")
    store.record(thread_id="session-1", sender="claude-owner", worktree_name="session-1")

    with pytest.raises(ContinuationWorkspaceError, match="conflict"):
        store.record(thread_id="session-1", sender="claude-attacker", worktree_name="other")


def test_continuation_workspace_store_serializes_one_session(tmp_path):
    store = ContinuationWorkspaceStore(tmp_path / "sessions", "asdk-test")
    lease = store.acquire("session-1")
    try:
        with pytest.raises(ContinuationWorkspaceError, match="busy"):
            store.acquire("session-1")
    finally:
        lease.release()
