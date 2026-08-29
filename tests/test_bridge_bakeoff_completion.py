from __future__ import annotations

import os
import stat
import json
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

from agent_redis_bridge.bridge import Bridge, build_engine
from agent_redis_bridge.engines.base import EngineError
from agent_redis_bridge.engines.openinterpreter import CellToolPlaneBroker
from agent_redis_bridge.engines.pi_sdk import PiSdkEngine
from agent_redis_bridge.engines.pi_broker_mcp import PiSdkBrokerAdapter
from implbench.harness.git_service import AttemptGitServiceServer
from implbench.harness.git_service import RemoteGitService


def _attempt_metadata(*, tool_gid: int) -> dict[str, object]:
    return {
        "cell_id": "cell-" + "a" * 64,
        "attempt_id": "attempt-" + "b" * 64,
        "fixture_root_oid": "a" * 40,
        "allowed_paths": ["src/**"],
        "tool_gid": tool_gid,
        "tool_endpoint": "/tmp/implbench-controller-attempt.sock",
    }


def _unit_broker(endpoint: str, *, socket_gid: int, identity: dict[str, str]) -> CellToolPlaneBroker:
    assert endpoint == "/tmp/implbench-controller-attempt.sock"
    return CellToolPlaneBroker(
        lambda request: {"request": request, "actor": "tool"},
        lambda: {
            "mode": "receipt-only", "ref_namespace": "cell-attempt", "receipt_oids": [],
            "dirty": False, "seal_complete": True, "receipts_authenticated": True,
            "infrastructure_failure": None,
        },
        SimpleNamespace(identity=dict(identity)), socket_gid=socket_gid,
    )


def test_red_scored_run_uses_receipt_only_completion_mode() -> None:
    scored = SimpleNamespace(run_id="oi-pi-bakeoff-run-20260714T000000Z", payload={})
    ordinary = SimpleNamespace(run_id="implbench-seat-20260714T000000Z", payload={})

    assert Bridge.is_scored_request(scored)
    assert not Bridge.is_scored_request(ordinary)
    assert Bridge.completion_mode(scored) == "receipt-only"
    assert Bridge.completion_mode(ordinary) == "host-gated"


def test_scored_oi_binds_authenticated_cell_broker_before_engine_construction(tmp_path: Path) -> None:
    events: list[str] = []

    class CanonicalGitService:
        receipt_chain = SimpleNamespace(
            identity={"cell_id": "cell-" + "a" * 64, "attempt_id": "attempt-" + "b" * 64}
        )
        tool_gid = os.getgid()

        def handle(self, request, *, actor):
            return {"request": request, "actor": actor}

        def completion_projection(self):
            return {
                "mode": "receipt-only",
                "ref_namespace": "cell-attempt",
                "receipt_oids": [],
                "dirty": False,
                "seal_complete": True,
                "receipts_authenticated": True,
                "infrastructure_failure": None,
            }

    service = CanonicalGitService()
    gid = os.getgid()
    metadata = _attempt_metadata(tool_gid=gid)
    bridge = Bridge.__new__(Bridge)
    bridge.args = SimpleNamespace(
        cell_runtime=SimpleNamespace(tool_gid=gid),
        scored_tool_broker_factory=_unit_broker,
    )
    envelope = SimpleNamespace(run_id="oi-pi-bakeoff-test", payload=metadata)

    with pytest.raises(EngineError, match="bound scored cell"):
        build_engine(
            SimpleNamespace(
                engine="openinterpreter", provider="p", model="m", harness="zcode",
                tool_broker=None, interpreter_bin=None, interpreter_sha256=None,
            ),
            cwd=str(tmp_path),
        )

    bridge._bind_scored_tool_plane(envelope, tmp_path)
    args = SimpleNamespace(
        engine="openinterpreter", provider="p", model="m", harness="zcode",
        tool_broker=bridge.args.tool_broker, interpreter_bin=None, interpreter_sha256=None,
        _scored_tool_plane_bound=True,
    )
    engine = build_engine(args, cwd=str(tmp_path))

    assert events == []
    assert isinstance(engine.tool_broker, CellToolPlaneBroker)
    assert engine.tool_broker.completion_projection()["receipts_authenticated"] is True


def test_default_scored_git_service_refuses_missing_receipt_chain_before_launch(tmp_path: Path) -> None:
    bridge = Bridge.__new__(Bridge)
    bridge.args = SimpleNamespace()
    envelope = SimpleNamespace(
        run_id="oi-pi-bakeoff-test",
        payload={
            "cell_id": "cell-" + "a" * 64,
            "attempt_id": "attempt-" + "b" * 64,
            "fixture_root_oid": "a" * 40,
            "allowed_paths": ["src/**"],
        },
    )

    with pytest.raises(EngineError, match="tool-plane GID"):
        bridge._bind_scored_tool_plane(envelope, tmp_path)


def test_default_scored_git_service_constructs_with_complete_controller_metadata(tmp_path: Path) -> None:
    gid = os.getgid()
    bridge = Bridge.__new__(Bridge)
    bridge.args = SimpleNamespace(
        scored_tool_broker_factory=_unit_broker,
    )
    envelope = SimpleNamespace(
        run_id="oi-pi-bakeoff-test",
        payload=_attempt_metadata(tool_gid=gid),
    )

    bridge._bind_scored_tool_plane(envelope, tmp_path)

    assert bridge.args.tool_broker.is_authenticated
    assert bridge.args.tool_broker.socket_gid == gid
    assert bridge.args.tool_broker.completion_projection()["receipts_authenticated"] is True


def test_scored_bind_requires_controller_gid_on_envelope_even_with_local_identity(tmp_path: Path) -> None:
    bridge = Bridge.__new__(Bridge)
    bridge.args = SimpleNamespace(
        cell_runtime=SimpleNamespace(tool_gid=os.getgid()),
        scored_tool_broker_factory=_unit_broker,
    )
    envelope = SimpleNamespace(
        run_id="oi-pi-bakeoff-test",
        payload={
            "cell_id": "cell-" + "a" * 64,
            "attempt_id": "attempt-" + "b" * 64,
            "fixture_root_oid": "a" * 40,
            "allowed_paths": ["src/**"],
        },
    )

    with pytest.raises(EngineError, match="envelope|GID"):
        bridge._bind_scored_tool_plane(envelope, tmp_path)


def test_scored_bind_rejects_local_identity_conflicting_with_controller_gid(tmp_path: Path) -> None:
    bridge = Bridge.__new__(Bridge)
    bridge.args = SimpleNamespace(
        cell_runtime=SimpleNamespace(tool_gid=os.getgid()),
        scored_tool_broker_factory=_unit_broker,
    )
    envelope = SimpleNamespace(
        run_id="oi-pi-bakeoff-test",
        payload={
            "cell_id": "cell-" + "a" * 64,
            "attempt_id": "attempt-" + "b" * 64,
            "fixture_root_oid": "a" * 40,
            "allowed_paths": ["src/**"],
            "tool_gid": os.getgid() + 1,
            "tool_endpoint": "/tmp/implbench-controller-attempt.sock",
        },
    )

    with pytest.raises(EngineError, match="conflict|match"):
        bridge._bind_scored_tool_plane(envelope, tmp_path)


def test_scored_bind_plumbs_the_cell_runtime_tool_gid_into_the_broker(tmp_path: Path) -> None:
    gid = os.getgid()

    class CanonicalGitService:
        receipt_chain = object()
        tool_gid = gid

        def handle(self, request, *, actor):
            return {"request": request, "actor": actor}

        def completion_projection(self):
            return {"receipts_authenticated": True}

    bridge = Bridge.__new__(Bridge)
    bridge.args = SimpleNamespace(
        cell_runtime=SimpleNamespace(tool_gid=gid),
        scored_tool_broker_factory=_unit_broker,
    )
    bridge._bind_scored_tool_plane(
        SimpleNamespace(
            run_id="oi-pi-bakeoff-test",
            payload=_attempt_metadata(tool_gid=gid),
        ),
        tmp_path,
    )

    assert bridge.args.scored_tool_gid == gid
    assert bridge.args.tool_broker.socket_gid == gid


def test_scored_factory_kwarg_reaches_pi_socket_as_controller_minted_gid(tmp_path: Path) -> None:
    groups = [group for group in os.getgroups() if group != os.getgid()]
    if not groups:
        pytest.skip("host has no non-ambient supplementary group")
    minted_gid = groups[0]
    captured: list[int] = []
    captured_payloads: list[dict] = []

    class CanonicalGitService:
        receipt_chain = SimpleNamespace(
            identity={"cell_id": "cell-" + "a" * 64, "attempt_id": "attempt-" + "b" * 64}
        )

        def __init__(self, tool_gid: int):
            self.tool_gid = tool_gid

        def handle(self, request, *, actor):
            return {"request": request, "actor": actor}

        def completion_projection(self):
            return {"receipts_authenticated": True}

    bridge = Bridge.__new__(Bridge)

    def factory(envelope, worktree, *, tool_gid):
        captured.append(tool_gid)
        captured_payloads.append(dict(envelope.payload))
        return CanonicalGitService(tool_gid)

    bridge.args = SimpleNamespace(
        cell_runtime=SimpleNamespace(tool_gid=minted_gid),
        scored_tool_broker_factory=_unit_broker,
    )
    bridge._bind_scored_tool_plane(
        SimpleNamespace(
            run_id="oi-pi-bakeoff-test",
            payload=_attempt_metadata(tool_gid=minted_gid),
        ),
        tmp_path,
    )

    from agent_redis_bridge.engines.pi_broker_mcp import PiSdkBrokerAdapter

    broker = PiSdkBrokerAdapter(bridge.args.tool_broker, cwd=tmp_path, socket_gid=minted_gid)
    broker.start()
    try:
        mode = stat.S_IMODE(broker.socket_path.stat().st_mode)
        assert captured == []
        assert captured_payloads == []
        assert broker.socket_path.stat().st_gid == minted_gid
        assert mode == 0o660
    finally:
        broker.stop()


def test_scored_pi_launches_through_authenticated_cell_broker_after_binding(tmp_path: Path) -> None:
    class CanonicalGitService:
        receipt_chain = SimpleNamespace(identity={"cell_id": "cell-1", "attempt_id": "attempt-1"})
        tool_gid = os.getgid()

        def handle(self, request, *, actor):
            return {"request": request, "actor": actor}

        def completion_projection(self):
            return {
                "mode": "receipt-only", "ref_namespace": "cell-attempt", "receipt_oids": [],
                "dirty": False, "seal_complete": True, "receipts_authenticated": True,
                "infrastructure_failure": None,
            }

    bridge = Bridge.__new__(Bridge)
    bridge.args = SimpleNamespace(
        cell_runtime=SimpleNamespace(tool_gid=os.getgid()),
        scored_tool_broker_factory=_unit_broker,
    )
    bridge._bind_scored_tool_plane(
        SimpleNamespace(
            run_id="oi-pi-bakeoff-test",
                payload=_attempt_metadata(tool_gid=os.getgid()),
        ),
        tmp_path,
    )

    engine = build_engine(
        SimpleNamespace(
            engine="pi-sdk", model="m", pi_tools=None, tool_broker=bridge.args.tool_broker,
            _scored_tool_plane_bound=True,
        ),
        cwd=str(tmp_path),
    )

    assert isinstance(engine, PiSdkEngine)
    assert engine.scored_broker_identity == {
        "cell_id": "cell-" + "a" * 64,
        "attempt_id": "attempt-" + "b" * 64,
    }
    params = engine.thread_start_params()
    assert params["tools"] == []
    assert params["mcpServers"][0]["name"] == "cell-broker"
    assert params["mcpServers"][0]["env"]["PI_SDK_BROKER_IDENTITY"] == (
        "cell-" + "a" * 64 + "/attempt-" + "b" * 64
    )
    assert engine.tool_broker.completion_projection()["receipts_authenticated"] is True


def test_scored_pi_broker_forwards_authenticated_status_to_controller_attempt_endpoint(tmp_path: Path) -> None:
    """The real Pi MCP broker path preserves the scored identity and RPC actor."""
    observed: list[tuple[dict[str, object], str]] = []

    class ControllerEndpoint:
        def handle(self, request, *, actor):
            observed.append((dict(request), actor))
            return {"head": "a" * 40, "actor": actor}

    service = ControllerEndpoint()
    server = AttemptGitServiceServer(
        service, root=tmp_path, attempt_id="attempt-" + "b" * 64,
        tool_gid=os.getgid(), peer_uids=(os.getuid(),),
    )
    binding = server.start()
    assert Path(binding["endpoint"]).exists()
    assert RemoteGitService(endpoint=binding["endpoint"], capability=binding["capability"], tool_gid=os.getgid()).handle({"op": "status"}) == {"head": "a" * 40, "actor": "tool"}
    assert observed == [({"op": "status"}, "tool")]
    observed.clear()
    tool_endpoint = Path(tempfile.gettempdir()) / f"implbench-test-tool-{os.getpid()}.sock"
    tool_endpoint.unlink(missing_ok=True)
    authority_read, authority_write = os.pipe()
    os.write(authority_write, json.dumps({
        "git_endpoint": binding["endpoint"], "git_capability": binding["capability"],
        "socket_gid": os.getgid(), "cell_id": "cell-" + "a" * 64,
        "attempt_id": "attempt-" + "b" * 64,
        "workdir": str(tmp_path.resolve()),
    }).encode("utf-8"))
    os.close(authority_write)
    entry = Path(__file__).parents[1] / "bench" / "implbench" / "scored_plane_entry.py"
    tool_process = subprocess.Popen(
        [sys.executable, "-u", str(entry), "tool", "--authority-fd", str(authority_read),
         "--endpoint", str(tool_endpoint)],
        pass_fds=(authority_read,), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    os.close(authority_read)
    try:
        deadline = time.monotonic() + 5
        while not tool_endpoint.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert tool_endpoint.exists()
        bridge = Bridge.__new__(Bridge)
        bridge.args = SimpleNamespace(cell_runtime=SimpleNamespace(tool_gid=os.getgid()))
        payload = _attempt_metadata(tool_gid=os.getgid())
        payload["tool_endpoint"] = str(tool_endpoint)
        bridge._bind_scored_tool_plane(SimpleNamespace(run_id="oi-pi-bakeoff-test", payload=payload), tmp_path)
        assert tool_process.pid != os.getpid()
        assert bridge.args.tool_broker.handle_tool_request({"op": "status"}) == {"head": "a" * 40, "actor": "tool"}
        assert observed == [({"op": "status"}, "tool")]
        observed.clear()
        assert bridge.args.tool_broker.handle_tool_request({"op": "write", "path": "result.txt", "content": "one\n"})["bytes"] == 4
        assert bridge.args.tool_broker.handle_tool_request({"op": "read", "path": "result.txt"})["content"] == "one\n"
        assert bridge.args.tool_broker.handle_tool_request({"op": "edit", "path": "result.txt", "old_text": "one", "new_text": "two"})["replacements"] == 1
        assert bridge.args.tool_broker.handle_tool_request({"op": "bash", "command": "printf tool-ok"}) == {
            "exit_code": 0, "stdout": "tool-ok", "stderr": "",
        }
        with pytest.raises(EngineError, match="Git execution"):
            bridge.args.tool_broker.handle_tool_request({"op": "bash", "command": "git status"})
        adapter = PiSdkBrokerAdapter(bridge.args.tool_broker, cwd=tmp_path, socket_gid=os.getgid())
        adapter.start()
        try:
            assert Path(binding["endpoint"]).exists()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.connect(str(adapter.socket_path))
                stream = connection.makefile("rwb")
                stream.write((json.dumps({"token": adapter.token, "identity": adapter.identity}) + "\n").encode())
                stream.flush()
                assert json.loads(stream.readline()) == {"ok": True, "identity": adapter.identity}
                stream.write(b'{"kind":"tool","params":{"op":"status"}}\n')
                stream.flush()
                reply = json.loads(stream.readline())
            assert reply["ok"] is True, reply
            assert reply["result"] == {"head": "a" * 40, "actor": "tool"}
            assert observed == [({"op": "status"}, "tool")]
            assert adapter.identity == "cell-" + "a" * 64 + "/attempt-" + "b" * 64
            assert adapter.socket_path.stat().st_gid == os.getgid()
            assert stat.S_IMODE(adapter.socket_path.stat().st_mode) == 0o660
        finally:
            adapter.stop()
    finally:
        tool_process.terminate()
        try:
            tool_process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            tool_process.kill(); tool_process.wait(timeout=3)
        tool_endpoint.unlink(missing_ok=True)
        server.close()


@pytest.mark.parametrize("engine_name", ["pi-sdk", "pi-rpc"])
def test_scored_pi_refuses_missing_or_untrusted_broker(tmp_path: Path, engine_name: str) -> None:
    with pytest.raises(EngineError, match="broker"):
        build_engine(
            SimpleNamespace(
                engine=engine_name, model="m", pi_tools=None, tool_broker=None,
                _scored_tool_plane_bound=True,
            ),
            cwd=str(tmp_path),
        )


def test_scored_pi_refuses_broker_without_canonical_identity(tmp_path: Path) -> None:
    broker = CellToolPlaneBroker(
        lambda params: params, lambda: {"receipts_authenticated": True}, object(), socket_gid=os.getgid()
    )
    with pytest.raises(EngineError, match="canonical receipt identity"):
        build_engine(
            SimpleNamespace(
                engine="pi-sdk", model="m", pi_tools=None, tool_broker=broker,
                _scored_tool_plane_bound=True,
            ),
            cwd=str(tmp_path),
        )
