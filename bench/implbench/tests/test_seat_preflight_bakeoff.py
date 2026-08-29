from __future__ import annotations

import importlib.machinery
import importlib.util

import pytest


_path = __import__("pathlib").Path(__file__).resolve().parents[3] / "scripts" / "seat-preflight"
_spec = importlib.util.spec_from_loader("seat_preflight_bakeoff", importlib.machinery.SourceFileLoader("seat_preflight_bakeoff", str(_path)))
seat = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seat)


def _manifest() -> dict:
    controls = {name: {"requested": "UNSUPPORTED", "effective": "UNSUPPORTED", "verified_via": "provider-runtime-ack"} for name in seat.BAKEOFF_CONTROL_NAMES}
    controls["reasoning"] = {"requested": "medium", "effective": "medium", "verified_via": "provider-runtime-ack"}
    return {
        "schema_version": "manifest-v2",
        "arms": [
            {"pair": "GLM", "arm": "glm-pi", "engine": "pi-sdk", "provider": "zai", "model": "glm-5.2", "harness": "Pi", "agent_prefix": "pi-sdk-agentredisbridge-bake-glm52", "home": "PI_CODING_AGENT_DIR", "retire_env": "BRIDGE_PI_RETIRE_AFTER_TURN", "retire_value": "1", "controls": controls},
            {"pair": "GLM", "arm": "glm-zcode", "engine": "openinterpreter", "provider": "zai-coding-plan", "model": "glm-5.2", "harness": "zcode", "agent_prefix": "interpreter-agentredisbridge-bake-glm52-zcode", "home": "INTERPRETER_HOME", "retire_env": "BRIDGE_INTERPRETER_RETIRE_AFTER_TURN", "retire_value": "1", "controls": controls},
            {"pair": "Kimi", "arm": "kimi-pi", "engine": "pi-sdk", "provider": "kimi-coding", "model": "k2p7", "harness": "Pi", "agent_prefix": "pi-sdk-agentredisbridge-bake-k2p7", "home": "PI_CODING_AGENT_DIR", "retire_env": "BRIDGE_PI_RETIRE_AFTER_TURN", "retire_value": "1", "controls": controls},
            {"pair": "Kimi", "arm": "kimi-cli", "engine": "openinterpreter", "provider": "kimi-for-coding", "model": "k2p7", "harness": "kimi-cli", "agent_prefix": "interpreter-agentredisbridge-bake-k2p7-kimicli", "home": "INTERPRETER_HOME", "retire_env": "BRIDGE_INTERPRETER_RETIRE_AFTER_TURN", "retire_value": "1", "controls": controls},
        ],
        "extensions": {"role_profiles": [], "project_instruction_files": [], "optional_skill_packs": [], "memory_mcps": [], "unrelated_extensions": []},
    }


def test_bakeoff_manifest_mode_requires_all_frozen_arms_and_value_free_controls() -> None:
    ok, message = seat.check_bakeoff_manifest(_manifest())
    assert ok, message

    bad = _manifest()
    bad["arms"][0]["controls"]["reasoning"]["verified_via"] = "request-echo"
    ok, message = seat.check_bakeoff_manifest(bad)
    assert not ok
    assert "reasoning" in message


def test_bakeoff_manifest_mode_rejects_missing_arm_and_nonempty_extensions() -> None:
    bad = _manifest()
    bad["arms"] = bad["arms"][:-1]
    ok, message = seat.check_bakeoff_manifest(bad)
    assert not ok
    assert "arms" in message

    bad = _manifest()
    bad["extensions"]["memory_mcps"] = ["not-allowed"]
    ok, message = seat.check_bakeoff_manifest(bad)
    assert not ok
    assert "extensions" in message


def test_manifest_mode_runs_scored_preflight_after_static_validation(monkeypatch, tmp_path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("{}")
    path.chmod(0o600)
    monkeypatch.setattr(seat, "check_bakeoff_manifest", lambda value: (True, "static ok"))
    monkeypatch.setattr(seat, "run_scored_path_preflight", lambda value: 1)

    assert seat.run_main(["--strict", "--manifest", str(path)]) == 1
