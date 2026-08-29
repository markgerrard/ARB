from __future__ import annotations

import subprocess
from pathlib import Path

from .gates import BatteryResult
from .scorer_sandbox import BatteryBoundaryError, ScorerRole, ScorerSandbox
from .tasks import Task

METHOD = "openssl-aes-256-cbc-pbkdf2"
HEADER = f"implbench:method={METHOD}\n".encode()


def decrypt(enc_path: Path, key: str) -> bytes:
    data = enc_path.read_bytes()
    if not data.startswith(HEADER):
        raise ValueError("battery-plaintext-corrupt")
    res = subprocess.run(
        ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-pass", "stdin"],
        input=key.encode() + b"\n" + data[len(HEADER) :],
        capture_output=True,
        check=False,
    )
    if res.returncode != 0:
        raise ValueError("decrypt-failure")
    return res.stdout


def run_battery(task: Task, worktree: Path, key: str | None = None, key_env: str = "IMPLBENCH_BATTERY_KEY") -> BatteryResult:
    del task, worktree, key, key_env
    raise BatteryBoundaryError("host battery decryption and execution are disabled; use G1 keyed-runner")


def run_keyed_battery(sandbox: ScorerSandbox, argv: list[str], *, timeout_s: float = 30.0) -> BatteryResult:
    """Run only the keyed-runner command inside a prepared G1 topology."""

    result = sandbox.run(ScorerRole.KEYED_RUNNER, argv, timeout_s=timeout_s)
    return BatteryResult(result.exit_code)


def encrypt_for_tests(plaintext: bytes, enc_path: Path, key: str) -> None:
    enc_path.parent.mkdir(parents=True, exist_ok=True)
    res = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-pass", "stdin"],
        input=key.encode() + b"\n" + plaintext,
        capture_output=True,
        check=True,
    )
    enc_path.write_bytes(HEADER + res.stdout)
