"""Arm-time checks for the checkout-local FABA SubagentStop wiring."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
FABA = HERE.parent
for path in (str(FABA), str(FABA / "subagent")):
    if path not in sys.path:
        sys.path.insert(0, path)

import faba_launch
import run_author_round
import run_probe_round


WIRED = {
    "hooks": {
        "SubagentStop": [
            {"hooks": [{"command": "python tools/faba/subagent/subagent_stop_gate.py"}]}
        ]
    }
}


def _write(repo: Path, rel: str, value) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")


@pytest.mark.parametrize(
    "wired_paths",
    [
        (".claude/settings.local.json",),
        (".claude/settings.json",),
        (".claude/settings.json", ".claude/settings.local.json"),
    ],
)
def test_gate_hook_wired_accepts_either_checkout_settings_file(tmp_path, wired_paths):
    for rel in wired_paths:
        _write(tmp_path, rel, WIRED)

    assert faba_launch.gate_hook_wired(tmp_path) is None


def test_gate_hook_wired_accepts_wired_file_and_warns_for_malformed_sibling(
    tmp_path, capsys
):
    _write(tmp_path, ".claude/settings.json", WIRED)
    _write(tmp_path, ".claude/settings.local.json", "{")

    assert faba_launch.gate_hook_wired(tmp_path) is None

    warning = capsys.readouterr().err
    assert warning.count("\n") == 1
    assert "warning" in warning.lower()
    assert str(tmp_path / ".claude/settings.local.json") in warning
    assert "invalid JSON" in warning


def test_gate_hook_wired_refuses_when_both_files_are_malformed(tmp_path):
    _write(tmp_path, ".claude/settings.json", "{")
    _write(tmp_path, ".claude/settings.local.json", "{")

    problem = faba_launch.gate_hook_wired(tmp_path)

    assert problem is not None
    assert str(tmp_path / ".claude/settings.json") in problem
    assert str(tmp_path / ".claude/settings.local.json") in problem
    assert problem.count("invalid JSON") == 2


def test_gate_hook_wired_refuses_for_malformed_and_unwired_valid_files(tmp_path):
    _write(tmp_path, ".claude/settings.json", {"hooks": {}})
    _write(tmp_path, ".claude/settings.local.json", "{")

    problem = faba_launch.gate_hook_wired(tmp_path)

    assert problem is not None
    assert str(tmp_path / ".claude/settings.local.json") in problem
    assert "invalid JSON" in problem
    assert "no qualifying SubagentStop hook was found" in problem


@pytest.mark.parametrize(
    ("files", "named_problem"),
    [
        ({}, "both settings files are missing"),
        ({".claude/settings.json": "{"}, ".claude/settings.json"),
        ({".claude/settings.local.json": "{"}, ".claude/settings.local.json"),
        ({".claude/settings.json": {"hooks": {}}}, "no qualifying SubagentStop"),
        (
            {".claude/settings.json": {"hooks": {"SubagentStop": [{"hooks": []}]}}},
            "no qualifying SubagentStop",
        ),
    ],
)
def test_gate_hook_wired_reports_checkout_scope_and_recipe(tmp_path, files, named_problem):
    for rel, value in files.items():
        _write(tmp_path, rel, value)

    problem = faba_launch.gate_hook_wired(tmp_path)

    assert problem is not None
    assert named_problem in problem
    assert str(tmp_path / ".claude/settings.json") in problem
    assert str(tmp_path / ".claude/settings.local.json") in problem
    assert "user-level settings were not checked" in problem
    assert "tools/faba/subagent/README.md#Wiring" in problem


@pytest.mark.parametrize(
    ("driver", "argv", "label"),
    [
        (
            run_author_round,
            ["--stage", "design", "--subject-summary", "doctor", "--task", "test"],
            "author",
        ),
        (
            run_probe_round,
            [
                "--artefact-id", "art-test", "--subject-summary", "doctor",
                "--round", "1", "--task", "test",
            ],
            "probe",
        ),
    ],
)
def test_each_real_driver_refuses_before_workspace_or_pointer(
    tmp_path, monkeypatch, capsys, driver, argv, label
):
    pointer = tmp_path / "pointer.json"
    created = []
    monkeypatch.setattr(driver, "REPO", tmp_path)
    monkeypatch.setattr(driver, "POINTER", pointer)
    monkeypatch.setattr(driver.tempfile, "mkdtemp", lambda **kwargs: created.append(kwargs))

    assert driver.main(argv) == 2

    error = capsys.readouterr().err
    assert f"[{label}]" in error
    assert str(tmp_path / ".claude/settings.json") in error
    assert str(tmp_path / ".claude/settings.local.json") in error
    assert created == []
    assert not pointer.exists()


@pytest.mark.parametrize("driver", [run_author_round, run_probe_round])
@pytest.mark.parametrize(
    "wired_paths",
    [
        (".claude/settings.local.json",),
        (".claude/settings.json",),
        (".claude/settings.json", ".claude/settings.local.json"),
    ],
)
def test_each_real_driver_passes_doctor_for_all_wired_layouts(
    tmp_path, monkeypatch, capsys, driver, wired_paths
):
    for rel in wired_paths:
        _write(tmp_path, rel, WIRED)
    monkeypatch.setattr(driver, "REPO", tmp_path)
    monkeypatch.delenv("ARB_MEMORY_REDIS_URL", raising=False)
    argv = (
        ["--stage", "design", "--subject-summary", "doctor", "--task", "test"]
        if driver is run_author_round
        else [
            "--artefact-id", "art-test", "--subject-summary", "doctor",
            "--round", "1", "--task", "test",
        ]
    )

    assert driver.main(argv) == 2

    error = capsys.readouterr().err
    assert "gate wiring check failed" not in error
    assert "ARB_MEMORY_REDIS_URL" in error
