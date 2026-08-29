"""Regression guard: bridge.py must import WITHOUT claude_agent_sdk installed.

The agent-sdk engine pulls in `claude_agent_sdk` (an optional, separately-declared dep).
Importing it eagerly at bridge.py module scope broke codex/agy/pi seats on hosts that
don't have the package — even though those seats never construct an agent-sdk engine.
AgentSdkEngine is therefore imported lazily inside build_engine()'s `agent-sdk` branch.

Run in a SUBPROCESS so the import-blocking never pollutes the rest of the suite's
module state (cf. the test_bridge_notify_inbox reload-isolation flake).
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

_BLOCK_AND_IMPORT = textwrap.dedent(
    """
    import builtins
    _real_import = builtins.__import__

    def _block(name, *args, **kwargs):
        if name == "claude_agent_sdk" or name.startswith("claude_agent_sdk."):
            raise ModuleNotFoundError("No module named 'claude_agent_sdk' (simulated)")
        return _real_import(name, *args, **kwargs)

    builtins.__import__ = _block

    # The exact thing that broke on the other server: importing bridge at all.
    import agent_redis_bridge.bridge as bridge
    assert hasattr(bridge, "build_engine"), "build_engine missing"

    # A core (non-agent-sdk) engine must be reachable without the package.
    from agent_redis_bridge.engines.codex import CodexEngine  # noqa: F401

    print("OK")
    """
)


def test_bridge_imports_without_claude_agent_sdk():
    result = subprocess.run(
        [sys.executable, "-c", _BLOCK_AND_IMPORT],
        cwd=str(REPO),
        env={"PYTHONPATH": str(REPO / "src"), "PATH": "/usr/bin:/bin"},
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, (
        "bridge.py failed to import without claude_agent_sdk "
        f"(eager import regressed?):\nSTDOUT:{result.stdout}\nSTDERR:{result.stderr}"
    )
    assert "OK" in result.stdout
