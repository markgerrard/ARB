"""Contained single-test execution for diagnose.

Spike-validated mechanism: macOS `sandbox-exec` with an allow-default profile
that denies network and confines writes to the realpath-resolved work directory
plus `/private/var/folders`. Timeout and child reaping are handled in Python
with `communicate(timeout=...)`, `start_new_session=True`, and `killpg`.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess


def _sandbox_wrapper(argv: list[str], cwd: str) -> tuple[list[str], bool]:
    sandbox_exec = shutil.which("sandbox-exec")
    if not sandbox_exec:
        return argv, False
    work = os.path.realpath(cwd)
    profile = (
        "(version 1)"
        "(allow default)"
        "(deny network*)"
        "(deny file-write*)"
        f'(allow file-write* (subpath "{work}"))'
        '(allow file-write* (subpath "/private/var/folders"))'
        '(allow file-write* (subpath "/dev"))'
    )
    return [sandbox_exec, "-p", profile, "--", *argv], True


def run_contained(argv: list[str], cwd: str, timeout_s: float) -> dict:
    wrapped, contained = _sandbox_wrapper(argv, cwd)
    if not contained:
        return {
            "reproduced": False,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
            "contained": False,
        }
    proc = subprocess.Popen(
        wrapped,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
        return {
            "reproduced": proc.returncode != 0,
            "returncode": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": False,
            "contained": True,
        }
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.communicate()
        return {
            "reproduced": False,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "timed_out": True,
            "contained": True,
        }
