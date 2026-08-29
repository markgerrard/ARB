from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_redis_bridge.engines.base import EngineError
from agent_redis_bridge.bridge import build_engine
from agent_redis_bridge.engines.openinterpreter import (
    CellToolPlaneBroker,
    MAX_FRAME_BYTES,
    OpenInterpreterEngine,
    decode_frame,
    verify_interpreter_binary,
)


def _versioned_executable(tmp_path: Path, version: str = "0.0.21") -> Path:
    path = tmp_path / "interpreter"
    path.write_text(f"#!/bin/sh\nprintf 'interpreter {version}\\n'\n")
    path.chmod(0o755)
    return path


def test_decode_frame_success_error_malformed_and_oversized() -> None:
    assert decode_frame(b'{"jsonrpc":"2.0","id":1}\n') == {"jsonrpc": "2.0", "id": 1}
    with pytest.raises(EngineError, match="error"):
        decode_frame(b'{"jsonrpc":"2.0","error":{"code":-1}}\n')
    with pytest.raises(EngineError, match="malformed"):
        decode_frame(b"not-json\n")
    with pytest.raises(EngineError, match="oversized"):
        decode_frame(b"{" + b"x" * MAX_FRAME_BYTES + b"}\n")


def test_binary_pin_requires_absolute_real_non_symlink_exact_version_and_sha(tmp_path: Path) -> None:
    binary = _versioned_executable(tmp_path)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    assert verify_interpreter_binary(binary, expected_sha256=digest) == {
        "path": str(binary),
        "realpath": str(binary),
        "version": "0.0.21",
        "sha256": digest,
    }
    with pytest.raises(EngineError, match="sha256"):
        verify_interpreter_binary(binary, expected_sha256="0" * 64)
    with pytest.raises(EngineError, match="0.0.21"):
        verify_interpreter_binary(
            _versioned_executable(tmp_path, "0.0.20"), expected_sha256="0" * 64
        )


def test_verify_interpreter_binary_version_spawn_uses_scrubbed_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preflight --version is a real child; it must not inherit lane-writer/bus creds."""
    import subprocess
    from unittest import mock

    binary = _versioned_executable(tmp_path)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    captured: dict = {}
    real_run = subprocess.run

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real_run(*args, **kwargs)

    polluted = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "ARB_GATE_LANE_WRITER_DSN": "postgresql://lw-secret@db/arb_memory",
        "ARB_GATE_LANE_WRITER_ROLE": "arb_gate_lw_seat_a",
        "ARB_GATE_LANE_WRITER_CONSUMER_ID": "consumer-a",
        "ARB_GATE_LANE_WRITER_LANE": "gated",
        "ARB_MEMORY_REDIS_URL": "rediss://:publish-secret@bus:6379/9",
        "AGENT_REDIS_PASSWORD": "bus-secret",
    }
    monkeypatch.setattr(
        "agent_redis_bridge.engines.openinterpreter.subprocess.run", spy
    )
    with mock.patch.dict(os.environ, polluted, clear=False):
        verify_interpreter_binary(binary, expected_sha256=digest)

    assert "env" in captured, "verify_interpreter_binary must pass env= (not inherit)"
    env = captured["env"]
    assert env is not None
    for key in (
        "ARB_GATE_LANE_WRITER_DSN",
        "ARB_GATE_LANE_WRITER_ROLE",
        "ARB_GATE_LANE_WRITER_CONSUMER_ID",
        "ARB_GATE_LANE_WRITER_LANE",
        "ARB_MEMORY_REDIS_URL",
        "AGENT_REDIS_PASSWORD",
    ):
        assert key not in env, f"{key} must not reach the --version child"


def test_binary_pin_rejects_relative_and_symlink_paths(tmp_path: Path) -> None:
    binary = _versioned_executable(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(binary)
    with pytest.raises(EngineError, match="absolute"):
        verify_interpreter_binary(Path("interpreter"), expected_sha256=None)
    with pytest.raises(EngineError, match="symlink"):
        verify_interpreter_binary(link, expected_sha256=None)


def test_engine_requires_explicit_provider_model_and_harness(tmp_path: Path) -> None:
    for kwargs in (
        {"model": "m", "harness": "zcode"},
        {"provider": "p", "harness": "zcode"},
        {"provider": "p", "model": "m"},
    ):
        with pytest.raises(EngineError, match="explicit"):
            OpenInterpreterEngine(cwd=str(tmp_path), tool_broker=CellToolPlaneBroker(handler=lambda _: {}), **kwargs)
    with pytest.raises(EngineError, match="harness"):
        OpenInterpreterEngine(
            cwd=str(tmp_path), provider="p", model="m", harness="other", tool_broker=CellToolPlaneBroker(handler=lambda _: {})
        )


def test_engine_rejects_unbound_cell_broker(tmp_path: Path) -> None:
    with pytest.raises(EngineError, match="bound"):
        OpenInterpreterEngine(
            cwd=str(tmp_path), provider="p", model="m", harness="zcode", tool_broker=CellToolPlaneBroker()
        )


def test_git_service_broker_routes_as_tool_actor() -> None:
    seen: list[tuple[dict, str]] = []

    class GitService:
        receipt_chain = object()
        tool_gid = os.getgid()

        def handle(self, request, *, actor):
            seen.append((request, actor))
            return {"ok": True}

        def completion_projection(self):
            return {
                "mode": "receipt-only", "ref_namespace": "cell-attempt", "receipt_oids": [],
                "dirty": False, "seal_complete": True, "receipts_authenticated": True,
                "infrastructure_failure": None,
            }

    broker = CellToolPlaneBroker.from_git_service(GitService())

    assert broker.handle_tool_request({"op": "status"}) == {"ok": True}
    assert seen == [({"op": "status"}, "tool")]


def test_normal_bridge_build_rejects_unbound_cell_broker(tmp_path: Path) -> None:
    with pytest.raises(EngineError, match="bound"):
        build_engine(
            SimpleNamespace(
                engine="openinterpreter",
                provider="p",
                model="m",
                harness="zcode",
                tool_broker=None,
                interpreter_bin=None,
                interpreter_sha256=None,
            ),
            cwd=str(tmp_path),
        )


def test_normal_bridge_build_routes_through_bound_cell_broker(tmp_path: Path) -> None:
    seen: list[dict] = []
    engine = build_engine(
        SimpleNamespace(
            engine="openinterpreter",
            provider="p",
            model="m",
            harness="zcode",
            tool_broker=CellToolPlaneBroker(handler=lambda request: seen.append(request) or {"ok": True}),
            interpreter_bin=None,
            interpreter_sha256=None,
        ),
        cwd=str(tmp_path),
    )

    assert engine.tool_broker.handle_tool_request({"op": "status"}) == {"ok": True}
    assert seen == [{"op": "status"}]


def test_malformed_control_ack_is_engine_error_not_attribute_error(tmp_path: Path) -> None:
    engine = OpenInterpreterEngine(
        cwd=str(tmp_path), provider="p", model="m", harness="zcode",
        tool_broker=CellToolPlaneBroker(handler=lambda _: {}),
    )
    with pytest.raises(EngineError, match="acknowledgement"):
        engine.consume_terminal({"method": "control/ack", "params": []})


def test_command_is_exact_and_never_shell(tmp_path: Path) -> None:
    binary = _versioned_executable(tmp_path)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    engine = OpenInterpreterEngine(
        cwd=str(tmp_path),
        binary=binary,
        expected_sha256=digest,
        provider="zai-coding-plan",
        model="glm-5.2",
        harness="zcode",
        tool_broker=CellToolPlaneBroker(handler=lambda _: {}),
    )
    assert engine.command_args() == [str(binary), "app-server", "--listen", "stdio://"]
    assert engine.popen_kwargs()["shell"] is False


def test_scored_provider_environment_is_explicitly_bound_to_engine_child(tmp_path: Path) -> None:
    provider_env = {"PATH": "/usr/bin:/bin", "DUMMY_API_KEY": "secret"}
    engine = OpenInterpreterEngine(
        cwd=str(tmp_path), provider="p", model="m", harness="zcode",
        tool_broker=CellToolPlaneBroker(handler=lambda _: {}), process_env=provider_env,
    )
    assert engine.popen_kwargs()["env"] == provider_env


class _FakeStdin:
    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, value: str) -> None:
        self.writes.append(value)

    def flush(self) -> None:
        pass


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = _FakeStdin()
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def test_tool_requests_only_use_injected_broker_and_direct_execution_is_denied(tmp_path: Path) -> None:
    seen: list[dict] = []
    engine = OpenInterpreterEngine(
        cwd=str(tmp_path),
        provider="p",
        model="m",
        harness="zcode",
        tool_broker=CellToolPlaneBroker(handler=lambda request: seen.append(request) or {"ok": True}),
    )
    process = _FakeProcess()
    engine.process = process
    engine.handle_server_request(
        {"jsonrpc": "2.0", "id": 7, "method": "tool/request", "params": {"tool": "shell", "command": "id"}}
    )
    assert seen == [{"method": "tool/request", "params": {"tool": "shell", "command": "id"}}]
    reply = json.loads(process.stdin.writes[-1])
    assert reply["id"] == 7 and reply["result"] == {"ok": True}
    assert not hasattr(engine, "execute_tool")


def test_terminal_error_is_surfaced_and_provenance_is_structured(tmp_path: Path) -> None:
    engine = OpenInterpreterEngine(
        cwd=str(tmp_path), provider="p", model="m", harness="kimi-cli", tool_broker=CellToolPlaneBroker(handler=lambda _: {})
    )
    result = engine.consume_terminal(
        {"method": "control/ack", "params": {"provider": "p", "model": "m", "harness": "kimi-cli", "reasoning": {"requested": "medium", "effective": "medium", "verified_via": "provider-runtime-ack"}, "source": "runtime"}}
    )
    assert result is None
    assert engine.provenance["verified_via"] == "runtime"
    with pytest.raises(EngineError, match="provider exploded"):
        engine.consume_terminal(
            {"method": "turn/completed", "params": {"turn": {"status": "errored", "error": "provider exploded"}}}
        )


def test_engine_retires_after_turn_and_stop_reaps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDGE_INTERPRETER_RETIRE_AFTER_TURN", "1")
    engine = OpenInterpreterEngine(
        cwd=str(tmp_path), provider="p", model="m", harness="zcode", tool_broker=CellToolPlaneBroker(handler=lambda _: {})
    )
    process = _FakeProcess()
    engine.process = process
    assert engine.supports_continuation is False
    assert engine.retire_after_turn is True
    engine.stop()
    assert process.terminated is True
    assert process.returncode == 0


def test_control_ack_binds_exact_identity_reasoning_and_trusted_source(tmp_path: Path) -> None:
    engine = OpenInterpreterEngine(
        cwd=str(tmp_path),
        provider="p",
        model="m",
        harness="zcode",
        tool_broker=CellToolPlaneBroker(handler=lambda _: {}),
    )

    engine.consume_terminal(
        {
            "method": "control/ack",
            "params": {
                "provider": "p",
                "model": "m",
                "harness": "zcode",
                "reasoning": {"requested": "medium", "effective": "medium", "verified_via": "provider-runtime-ack"},
                "source": "runtime",
            },
        }
    )
    assert engine.provenance["controls"]["reasoning"]["effective"] == "medium"

    for field, value in (("provider", "other"), ("model", "other"), ("harness", "kimi-cli")):
        bad = {
            "provider": "p",
            "model": "m",
            "harness": "zcode",
            "reasoning": {"requested": "medium", "effective": "medium", "verified_via": "provider-runtime-ack"},
            "source": "runtime",
        }
        bad[field] = value
        with pytest.raises(EngineError, match="control acknowledgement"):
            engine.consume_terminal({"method": "control/ack", "params": bad})

    with pytest.raises(EngineError, match="acknowledgement"):
        engine.consume_terminal(
            {
                "method": "control/ack",
                "params": {
                    "provider": "p",
                    "model": "m",
                    "harness": "zcode",
                    "reasoning": {"requested": "low", "effective": "low", "verified_via": "request-echo"},
                    "source": "request",
                },
            }
        )
