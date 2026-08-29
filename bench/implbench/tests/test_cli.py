from __future__ import annotations

from pathlib import Path

from implbench.harness import cli, validate


def test_cli_validate_invokes_scored_deny_proofs(monkeypatch) -> None:
    monkeypatch.setattr(validate, "run_validate", lambda gates_subset=None: validate.ValidateReport(False, {}, ["miss"], True))
    assert cli.main(["validate"]) == 1


def test_cli_prune_routes_through_protected_ref_surface(monkeypatch, capsys) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(cli, "prune_protected_refs", lambda repo, before, evidence_root: seen.update(repo=repo, before=before, evidence_root=evidence_root) or ["refs/implbench/runs/old"])
    assert cli.main(["prune", "--before", "2026-07-09", "--evidence-root", "/tmp/evidence"]) == 0
    assert seen["before"] == "2026-07-09"
    assert "refs/implbench/runs/old" in capsys.readouterr().out


def test_cli_run_requires_one_seat() -> None:
    assert cli.main(["run", "--manifest", "/tmp/m", "--concurrency", "2"]) == 2


def test_cli_harness_has_no_host_completion_or_legacy_gate_import() -> None:
    root = Path(__file__).resolve().parents[1] / "harness"
    source = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
    assert "completion_gate" not in source
    assert "import evaluate_gate" not in source
