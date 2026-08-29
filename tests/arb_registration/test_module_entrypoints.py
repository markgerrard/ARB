from __future__ import annotations

import importlib.util
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "module,prog",
    [
        ("arb_registration.register_cli", "seat-register"),
        pytest.param(
            "arb_registration.registrar_cli",
            "seat-registrar",
            marks=pytest.mark.skipif(
                sys.version_info >= (3, 14) and importlib.util.find_spec("coincurve") is None,
                reason="registrar_cli imports coincurve, which has no wheel for Python >= 3.14",
            ),
        ),
        ("arb_registration.token_cli", "seat-token"),
        ("arb_registration.bus_register_cli", "bus-register"),
        ("arb_registration.bus_registrar_cli", "bus-registrar"),
        ("arb_registration.bus_approval_cli", "bus-registrar-approve"),
    ],
)
def test_python_module_entrypoint_invokes_cli(module, prog):
    proc = subprocess.run(
        [sys.executable, "-m", module, "--help"], text=True, capture_output=True
    )
    assert proc.returncode == 0
    assert f"usage: {prog}" in proc.stdout
