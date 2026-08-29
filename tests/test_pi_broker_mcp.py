from __future__ import annotations

import os
import socket
import stat
from types import SimpleNamespace

import pytest

from agent_redis_bridge.engines.base import EngineError
from agent_redis_bridge.engines.openinterpreter import CellToolPlaneBroker
from agent_redis_bridge.engines.pi_broker_mcp import PiSdkBrokerAdapter
from agent_redis_bridge.engines.pi_sdk import PiSdkEngine


def _broker(*, socket_gid: int | None = None) -> CellToolPlaneBroker:
    receipt_chain = SimpleNamespace(identity={"cell_id": "cell-" + "a" * 64, "attempt_id": "attempt-" + "b" * 64})
    return CellToolPlaneBroker(
        lambda params: {"params": params},
        lambda: {"receipts_authenticated": True},
        receipt_chain,
        socket_gid=socket_gid,
    )


def test_scored_broker_requires_a_provisioned_tool_group() -> None:
    with pytest.raises(EngineError, match="provisioned tool-plane GID"):
        PiSdkBrokerAdapter(_broker(), cwd="/tmp")


def test_broker_private_group_mode_allows_group_boundary_without_world_access() -> None:
    broker = PiSdkBrokerAdapter(_broker(socket_gid=os.getgid()), cwd="/tmp")
    broker.start()
    try:
        mode = stat.S_IMODE(broker.socket_path.stat().st_mode)
        assert mode == 0o660
        assert broker.socket_path.stat().st_gid == os.getgid()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(str(broker.socket_path))
            connection.sendall((f'{{"token":"{broker.token}","identity":"{broker.identity}"}}\n').encode())
            assert b'"ok": true' in connection.recv(4096)
    finally:
        broker.stop()
    assert not broker.socket_path.exists()


def test_scored_sdk_stop_removes_broker_after_graceful_process_wait() -> None:
    broker = _broker(socket_gid=os.getgid())
    engine = PiSdkEngine(cwd="/tmp", model=None, scored=True, tool_broker=broker, host_script_path="/dev/null")
    engine.scored_broker.start()
    engine.process = SimpleNamespace(
        stdin=SimpleNamespace(write=lambda value: None, flush=lambda: None),
        wait=lambda timeout=None: 0,
        poll=lambda: 0,
    )
    engine.stop()
    assert not engine.scored_broker.socket_path.exists()
    assert engine.scored_broker._thread is None


def test_scored_sdk_start_failure_removes_broker() -> None:
    broker = _broker(socket_gid=os.getgid())
    engine = PiSdkEngine(cwd="/tmp", model=None, scored=True, tool_broker=broker, host_script_path="/does/not/exist")
    try:
        engine.start()
    except Exception as exc:
        assert "host script not found" in str(exc)
    else:
        raise AssertionError("missing host script must fail")
    assert not engine.scored_broker.socket_path.exists()
    assert engine.scored_broker._thread is None
