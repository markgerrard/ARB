"""
Opt-in end-to-end tests for the pi-rpc bridge integration.

The live env file `.env.pi-dev` is gitignored; create it from the tracked template:
    cp .env.pi-dev.example .env.pi-dev   # then edit as needed

Example usage:
    RUN_PI_RPC_E2E=1 \
    AGENT_ENV_FILE=.env.pi-dev \
    FROM_AGENT_ID=claude-bridge-dev \
    PI_RPC_TARGET_ID=pi-bridge-dev \
    pytest tests/test_pi_rpc_e2e.py -q -s
"""

import json
import os
import subprocess
from pathlib import Path

try:
    import pytest
except ModuleNotFoundError:  # pragma: no cover
    pytest = None  # type: ignore[assignment]
else:
    if os.environ.get("RUN_PI_RPC_E2E") != "1":
        pytestmark = pytest.mark.skip(reason="set RUN_PI_RPC_E2E=1 to run live pi-rpc e2e tests")


BRIDGE_ROOT = Path(__file__).parent.parent.resolve()
DISPATCH_SCRIPT = BRIDGE_ROOT / "scripts" / "agent-dispatch"
DEFAULT_ENV_FILE = os.environ.get("AGENT_ENV_FILE", str(BRIDGE_ROOT / ".env.pi-dev"))
DEFAULT_FROM_AGENT = os.environ.get("FROM_AGENT_ID", "claude-bridge-dev")
DEFAULT_TARGET_ID = os.environ.get("PI_RPC_TARGET_ID", "pi-bridge-dev")


def _run_dispatch(task: str, *, timeout: int = 120) -> dict:
    env = os.environ.copy()
    env["AGENT_ENV_FILE"] = DEFAULT_ENV_FILE
    env["FROM_AGENT_ID"] = DEFAULT_FROM_AGENT
    cmd = [
        str(DISPATCH_SCRIPT),
        "--engine",
        "pi-rpc",
        "--target-id",
        DEFAULT_TARGET_ID,
        "--timeout",
        str(timeout),
        "--adhoc",
        task,
    ]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout + 10)
    if proc.returncode != 0:
        pytest.fail(f"agent-dispatch failed (rc={proc.returncode})\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")

    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    result_line = next((line for line in reversed(lines) if line.startswith("{")), None)
    if result_line is None:
        pytest.fail(f"Could not find JSON result in dispatch output:\n{proc.stdout}")
    return json.loads(result_line)


def test_pi_rpc_bridge_can_execute_commands_and_return_output() -> None:
    marker = "PI_RPC_E2E_COMMAND_MARKER"
    result = _run_dispatch(
        "Run `pwd && echo PI_RPC_E2E_COMMAND_MARKER && echo pi-rpc-ok`, then return the exact output.",
        timeout=90,
    )

    assert result.get("ok") is True, result.get("error")
    payload = result.get("result", "")
    assert marker in payload
    assert "pi-rpc-ok" in payload


def test_pi_rpc_full_tools_bridge_can_write_and_verify_file(tmp_path: Path) -> None:
    if os.environ.get("PI_RPC_FULL_TOOLS_E2E") != "1":
        pytest.skip("set PI_RPC_FULL_TOOLS_E2E=1 when targeting a full-tools pi-rpc worker")
    target_file = tmp_path / "pi_rpc_e2e_generated.txt"
    result = _run_dispatch(
        f"Write exactly `pi-rpc-write-ok` to {target_file}, then read the file back and return its contents.",
        timeout=120,
    )

    assert result.get("ok") is True, result.get("error")
    assert "pi-rpc-write-ok" in result.get("result", "")
    if target_file.exists():
        assert target_file.read_text().strip() == "pi-rpc-write-ok"
