from __future__ import annotations

import subprocess as sp
import sys
import textwrap
import time
import shutil
import pytest
from pathlib import Path

from skills.diagnose.containment import run_contained


def _script(tmp_path: Path, body: str) -> list[str]:
    path = tmp_path / "t.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, str(path)]


def test_network_egress_blocked(tmp_path):
    result = run_contained(
        _script(
            tmp_path,
            """
            import socket
            socket.create_connection(("1.1.1.1", 53), timeout=3)
            """,
        ),
        cwd=str(tmp_path),
        timeout_s=10,
    )
    assert (not result["contained"]) or (result["returncode"] != 0)


def test_write_outside_confine_fails(tmp_path):
    probe = Path.home() / ".diag_escape_probe"
    if probe.exists():
        probe.unlink()
    result = run_contained(
        _script(
            tmp_path,
            f"""
            open({str(probe)!r}, "w").write("x")
            """,
        ),
        cwd=str(tmp_path),
        timeout_s=10,
    )
    assert (not result["contained"]) or (result["returncode"] != 0)
    assert not probe.exists()


def test_write_inside_confine_succeeds(tmp_path):
    result = run_contained(
        _script(
            tmp_path,
            """
            from pathlib import Path
            Path("inside.txt").write_text("ok", encoding="utf-8")
            """,
        ),
        cwd=str(tmp_path),
        timeout_s=10,
    )
    if result["contained"]:
        assert result["returncode"] == 0
        assert (tmp_path / "inside.txt").read_text(encoding="utf-8") == "ok"


requires_sandbox_exec = pytest.mark.skipif(
    shutil.which("sandbox-exec") is None,
    reason="diagnose containment uses macOS sandbox-exec; unavailable hosts fail-closed with test-containment-unavailable",
)


@requires_sandbox_exec
def test_hanging_test_and_children_reaped(tmp_path):
    marker = "diag-contained-sleeper-120"
    result = run_contained(
        _script(
            tmp_path,
            f"""
            import subprocess, sys, time
            subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", {marker!r}])
            time.sleep(120)
            """,
        ),
        cwd=str(tmp_path),
        timeout_s=3,
    )
    assert result["timed_out"] is True
    time.sleep(0.2)
    out = sp.run(["pgrep", "-f", marker], capture_output=True, text=True, check=False)
    assert out.stdout.strip() == ""
