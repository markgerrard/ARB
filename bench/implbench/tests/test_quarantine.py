from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from implbench.harness.quarantine import (
    CensusViolation,
    QuarantineError,
    census_repository,
    clone_quarantine,
    export_quarantine,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q", "--ref-format=files")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "base.txt")
    env = {"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example", "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example"}
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], env={**__import__("os").environ, **env}, check=True)
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "fixture.txt").write_text("fixture\n")
    _git(repo, "add", "fixture.txt")
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], env={**__import__("os").environ, **env}, check=True)
    return repo, base, _git(repo, "rev-parse", "HEAD")


def test_export_has_exact_refs_and_full_census(tmp_path: Path) -> None:
    source, base, fixture = _repo(tmp_path)
    export = tmp_path / "export.git"
    result = export_quarantine(source, export, base_oid=base, fixture_oid=fixture)
    assert result.refs == {
        "refs/arb-export/base": base,
        "refs/arb-export/fixture": fixture,
    }
    assert result.census.violation is None
    assert _git(export, "rev-parse", "--show-ref-format") == "files"


def test_export_rejects_prior_result_or_dangling_object_before_clone(tmp_path: Path) -> None:
    source, base, fixture = _repo(tmp_path)
    _git(source, "update-ref", "refs/implbench/results/oi-pi-bakeoff-test/cell-x/attempt-y", fixture)
    export = tmp_path / "export.git"
    with pytest.raises(QuarantineError, match="EXTRA_REF"):
        export_quarantine(source, export, base_oid=base, fixture_oid=fixture)
    assert not export.exists()


def test_clone_uses_no_local_no_tags_and_exact_refspec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, base, fixture = _repo(tmp_path)
    export = tmp_path / "export.git"
    export_quarantine(source, export, base_oid=base, fixture_oid=fixture)
    seen: list[list[str]] = []
    original = subprocess.run

    def wrapped(argv, *args, **kwargs):
        if argv and argv[0] == "git" and ("fetch" in argv or "clone" in argv):
            seen.append(list(argv))
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", wrapped)
    cell = tmp_path / "cell"
    clone_quarantine(export, cell)
    assert seen and any("--no-local" in call and "--no-tags" in call for call in seen)
    assert any(any("refs/arb-export/base" in arg for arg in call) for call in seen)
    assert any(any("refs/arb-export/fixture" in arg for arg in call) for call in seen)


def test_census_reports_invalid_ref_shape(tmp_path: Path) -> None:
    source, base, fixture = _repo(tmp_path)
    _git(source, "update-ref", "refs/tags/not-allowed", base)
    result = census_repository(source, expected_refs={"refs/arb-export/base": base, "refs/arb-export/fixture": fixture})
    assert result.violation is CensusViolation.EXTRA_REF
