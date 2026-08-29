from __future__ import annotations

import importlib
import importlib.util
import os
import socket
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.hookimpl(hookwrapper=True)
def pytest_collection_modifyitems(session, config, items):
    yield
    config._h2_mutation_guard_ids = sorted(
        {
            item.callspec.params["guard_id"]
            for item in items
            if item.name.startswith("test_guard_mutation_reds_and_restore_greens")
            and hasattr(item, "callspec")
            and "guard_id" in item.callspec.params
        }
    )


def _load(modname: str, relpath: str):
    spec = importlib.util.spec_from_file_location(modname, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def allow_e2e_subprocess(monkeypatch):
    monkeypatch.setenv("ARB_E2E_ALLOW_SUBPROCESS", "1")


@pytest.fixture(autouse=True)
def _hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state = tmp_path / "state"
    home = tmp_path / "home"
    state.mkdir()
    home.mkdir()
    monkeypatch.setenv("ARB_H2_SHADOW_LOG", str(state / "h2-shadow-log.jsonl"))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("HOME", str(home))

    def _blocked_connect(self, *args, **kwargs):
        raise RuntimeError("e2e hermetic: network blocked")

    monkeypatch.setattr(socket.socket, "connect", _blocked_connect)

    real_run = subprocess.run
    real_popen = subprocess.Popen

    def _subprocess_allowed(cmd) -> bool:
        if os.environ.get("ARB_E2E_ALLOW_SUBPROCESS") == "1":
            return True
        argv = cmd if isinstance(cmd, (list, tuple)) else [cmd]
        return not argv or Path(str(argv[0])).name != "git"

    def _blocked_run(cmd, *args, **kwargs):
        if os.environ.get("ARB_E2E_ALLOW_SUBPROCESS") == "1":
            return real_run(cmd, *args, **kwargs)
        raise RuntimeError("e2e hermetic: subprocess blocked")

    class _BlockedPopen(subprocess.Popen):
        def __init__(self, cmd, *args, **kwargs):
            if not _subprocess_allowed(cmd):
                raise RuntimeError("e2e hermetic: git blocked")
            if os.environ.get("ARB_E2E_ALLOW_SUBPROCESS") != "1":
                raise RuntimeError("e2e hermetic: subprocess blocked")
            super().__init__(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _blocked_run)
    monkeypatch.setattr(subprocess, "Popen", _BlockedPopen)


@pytest.fixture
def real_gate():
    mod = _load("bridge_protocol_gate", "skills/bridge-protocol/gate/gate.py")
    assert mod.H2_MODE == "shadow"
    return mod


@pytest.fixture
def real_coll():
    return _load("h2_collector", "skills/bridge-protocol/gate/h2_collector.py")


@pytest.fixture
def real_grad():
    return importlib.import_module("skills.defect_hunts.h2_graduation")
