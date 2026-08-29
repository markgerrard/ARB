from __future__ import annotations

from pathlib import Path

import pytest

from implbench.harness import battery
from implbench.harness.scorer_sandbox import BatteryBoundaryError


def test_host_battery_runner_is_removed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    called = False

    def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("host decryption must not run")

    monkeypatch.setattr(battery, "decrypt", forbidden)
    with pytest.raises(BatteryBoundaryError):
        battery.run_battery(object(), tmp_path)  # type: ignore[arg-type]
    assert not called


def test_keyed_runner_adapter_only_consumes_scorer_result(monkeypatch) -> None:
    class Result:
        exit_code = 0
        stdout = ""
        stderr = ""

    class FakeSandbox:
        def run(self, role, argv, *, timeout_s):
            assert role.value == "keyed-runner"
            assert argv == ["runner"]
            assert timeout_s == 3
            return Result()

    result = battery.run_keyed_battery(FakeSandbox(), ["runner"], timeout_s=3)  # type: ignore[arg-type]
    assert result.exit_code == 0
    assert result.stdout == ""


def test_ciphertext_helper_never_contains_plaintext(tmp_path: Path) -> None:
    secret = b"hidden acceptance result"
    encrypted = tmp_path / "battery.enc"
    battery.encrypt_for_tests(secret, encrypted, "controller-key")
    assert secret not in encrypted.read_bytes()
