from __future__ import annotations

from pathlib import Path

from implbench.harness import cli


def test_cli_requires_exactly_one_concurrency(monkeypatch) -> None:
    assert cli.main(["run", "--manifest", "/tmp/m", "--concurrency", "2"]) == 2


def test_cli_delegates_manifest_commands_to_injected_handlers(monkeypatch, tmp_path: Path) -> None:
    seen: list[tuple[str, str | None]] = []
    monkeypatch.setattr(cli, "load_manifest_guard", lambda path, **_: {"run_id": "r", "evidence": {"root": str(tmp_path)}})
    for name in ("preflight", "calibrate", "pilot", "run"):
        monkeypatch.setitem(cli.HANDLERS, name, lambda manifest, seat=None, name=name: seen.append((name, seat)))
    assert cli.main(["preflight", "--manifest", "/tmp/m"]) == 0
    assert cli.main(["calibrate", "--manifest", "/tmp/m", "--seat", "seat-a"]) == 0
    assert cli.main(["pilot", "--manifest", "/tmp/m"]) == 0
    assert cli.main(["run", "--manifest", "/tmp/m"]) == 0
    assert seen == [("preflight", None), ("calibrate", "seat-a"), ("pilot", None), ("run", None)]


def test_cli_prune_requires_evidence_root(monkeypatch) -> None:
    assert cli.main(["prune", "--before", "2026-07-09"]) == 2


def test_cli_preflight_invokes_production_readiness_controller(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "load_manifest_guard", lambda path, **_: {"run_id": "r", "evidence": {"root": str(tmp_path)}})
    seen: list[object] = []
    runtime = object()
    monkeypatch.setattr(cli, "production_runtime", lambda manifest: runtime)
    monkeypatch.setattr(cli, "run_production_preflight", lambda manifest, **kwargs: seen.append((manifest, kwargs["runtime"])) or type("Result", (), {"status": "UNKNOWN"})())

    assert cli.main(["preflight", "--manifest", "/tmp/m"]) == 1
    assert seen == [({"run_id": "r", "evidence": {"root": str(tmp_path)}}, runtime)]
