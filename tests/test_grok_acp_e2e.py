"""
End-to-end test for the grok-acp bridge integration.

This test exercises the full path:
    human (via agent-dispatch or ctl) 
    → Redis bus 
    → running agent-redis-bridge with --engine grok-acp 
    → grok agent stdio (ACP) 
    → actual work in the target workdir 
    → normalized result back through the bridge 
    → reply to caller

It is intended to be run against a live grok-acp bridge instance
(e.g. one started with agent-redis-bridge@grok-acp-*.service).

These tests are skipped by default; set RUN_GROK_ACP_E2E=1 to run them live.

Example usage:
    RUN_GROK_ACP_E2E=1 \
    AGENT_ENV_FILE=.env.grok-dev \
    FROM_AGENT_ID=human-codexctl \
    pytest tests/test_grok_acp_e2e.py -q -s
"""

import os
import subprocess
import sys
from pathlib import Path

try:
    import pytest
except ModuleNotFoundError:  # pragma: no cover
    # This is a pytest-only live e2e (it uses pytest fixtures like tmp_path and a
    # running grok-acp bridge). pytest is a dev-only dependency, absent from the
    # runtime venv. Guarding the import lets `python -m unittest discover` import
    # this module cleanly — it defines no unittest.TestCase, so it contributes 0
    # tests rather than erroring the whole run. Under pytest it works normally.
    pytest = None  # type: ignore[assignment]
else:
    if os.environ.get("RUN_GROK_ACP_E2E") != "1":
        pytestmark = pytest.mark.skip(reason="set RUN_GROK_ACP_E2E=1 to run live grok-acp e2e tests")


# --- Configuration -----------------------------------------------------------

# These can be overridden via environment variables for CI / different hosts.
BRIDGE_ROOT = Path(__file__).parent.parent.resolve()
DISPATCH_SCRIPT = BRIDGE_ROOT / "scripts" / "agent-dispatch"

DEFAULT_ENV_FILE = os.environ.get(
    "AGENT_ENV_FILE", str(BRIDGE_ROOT / ".env.grok-dev")
)
DEFAULT_FROM_AGENT = os.environ.get("FROM_AGENT_ID", "human-codexctl")

# Target a grok-acp instance. Adjust as needed for your environment.
DEFAULT_TARGET_ID = os.environ.get("GROK_ACP_TARGET_ID", "grok-project-b-dev")


def _run_dispatch(
    task: str,
    *,
    target_id: str = DEFAULT_TARGET_ID,
    timeout: int = 120,
    env_file: str = DEFAULT_ENV_FILE,
    from_agent: str = DEFAULT_FROM_AGENT,
) -> dict:
    """
    Run the agent-dispatch script against a grok-acp target and return the parsed result.
    """
    env = os.environ.copy()
    env["AGENT_ENV_FILE"] = env_file
    env["FROM_AGENT_ID"] = from_agent

    cmd = [
        str(DISPATCH_SCRIPT),
        "--engine",
        "grok-acp",
        "--target-id",
        target_id,
        "--timeout",
        str(timeout),
        "--adhoc",
        task,
    ]

    proc = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout + 10,
    )

    if proc.returncode != 0:
        pytest.fail(
            f"agent-dispatch failed (rc={proc.returncode})\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )

    # The dispatch script prints a JSON result object on success.
    # We look for the last JSON-looking blob in stdout.
    output = proc.stdout.strip()
    # Very small parser: find the last { ... } block
    try:
        import json

        # The script prints "task-id: xxx" on stderr and the JSON result on stdout.
        # For simplicity we assume the final non-empty line that looks like JSON is the result.
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        result_line = next((line for line in reversed(lines) if line.startswith("{")), None)
        if result_line is None:
            pytest.fail(f"Could not find JSON result in dispatch output:\n{output}")

        return json.loads(result_line)
    except Exception as e:
        pytest.fail(f"Failed to parse dispatch result: {e}\nRaw output:\n{output}")


# --- Tests -------------------------------------------------------------------


def test_grok_acp_bridge_can_execute_commands_and_return_output():
    """
    Basic smoke: ask the remote Grok (via grok-acp) to run a few shell commands
    and return the exact output. This proves the full request → ACP → execution
    → reply loop is working.
    """
    task = (
        "Run the following shell commands and return their exact combined output "
        "(including any '---' separators):\n"
        "pwd && echo '--- GROK_ACP_E2E_TEST ---' && date && echo 'Bridge write test successful'"
    )

    result = _run_dispatch(task, timeout=60)

    assert result.get("ok") is True, f"Dispatch failed: {result.get('error')}"
    payload = result.get("result", "")

    assert "GROK_ACP_E2E_TEST" in payload
    assert "Bridge write test successful" in payload
    assert "pwd" not in payload or "/" in payload  # crude check that pwd ran


def test_grok_acp_bridge_can_be_asked_to_write_files(tmp_path: Path):
    """
    Stronger test of write capability: ask the remote Grok (via the bridge) to
    write a small Python test file to a temporary location and then read it back.

    This is the kind of "write a test file" operation the bridge is ultimately
    intended to support when doing real work.
    """
    target_file = tmp_path / "grok_acp_generated_test.py"

    task = f"""
Write the following content to the file at the absolute path {target_file}:

# Auto-generated by grok-acp E2E test
def test_generated_by_grok_acp():
    print("This test was written by a Grok instance running behind the agent-redis-bridge (grok-acp engine).")
    assert True

print("File successfully written by grok-acp bridge.")

Then read the file back and print its entire contents so the caller can verify it.
"""

    result = _run_dispatch(task, timeout=90)

    assert result.get("ok") is True, f"Write dispatch failed: {result.get('error')}"

    # The remote Grok should have printed the file contents in the result.
    payload = result.get("result", "")
    assert "This test was written by a Grok instance" in payload
    assert "File successfully written by grok-acp bridge" in payload

    # As a belt-and-suspenders check, also read the file locally if possible.
    # (This will only work if the bridge workdir + the tmp_path happen to be on the same host.)
    if target_file.exists():
        content = target_file.read_text()
        assert "grok-acp E2E test" in content


if __name__ == "__main__":
    # Allow running directly for quick manual testing
    pytest.main([__file__, "-q", "-s"])
