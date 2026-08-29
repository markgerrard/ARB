import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

import yaml

from authorbench import factkey


@contextmanager
def chdir(path: Path):
    import os

    old = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


class TestFactKey(unittest.TestCase):
    def _repo_and_export(self, root: Path) -> tuple[Path, Path, str]:
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        (repo / "src").mkdir()
        (repo / "src" / "thing.py").write_text(
            "def target_symbol():\n    return 'needle-pattern'\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "fixture"], cwd=repo, check=True, stdout=subprocess.PIPE)
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        export = root / "export"
        factkey.export_at(sha, export, repo=repo)
        return repo, export, sha

    def _repo_and_outcome_exports(self, root: Path) -> tuple[Path, Path, str]:
        repo, export, sha = self._repo_and_export(root)
        (repo / "docs").mkdir()
        (repo / "docs" / "outcome-record.md").write_text("canonical outcome\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "outcome"], cwd=repo, check=True, stdout=subprocess.PIPE)
        return repo, export, sha

    def _key(self, path: Path, sha: str, *, line: int = 2, pattern: str = "needle-pattern", symbol: str = "target_symbol") -> Path:
        data = {
            "version": 1,
            "brief_id": "AB-X",
            "base_sha": sha,
            "facts": [
                {
                    "id": "F1",
                    "claim": "target returns the needle",
                    "file": "src/thing.py",
                    "line": line,
                    "pattern": pattern,
                }
            ],
            "traps": [
                {
                    "id": "T1",
                    "name": "symbol trap",
                    "precondition": "target symbol exists",
                    "file": "src/thing.py",
                    "symbol": symbol,
                    "historical_catch": "docs/panel.md:1",
                }
            ],
        }
        key = path / "fact_key.yaml"
        key.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return key

    def _key_with_bundle(self, root: Path, sha: str, outcome_globs: list[str]) -> Path:
        bundle_dir = root / "bundle"
        bundle_dir.mkdir()
        key = self._key(bundle_dir, sha)
        (bundle_dir / "bundle.yaml").write_text(
            yaml.safe_dump({"id": "AB-X", "outcome_globs": outcome_globs}, sort_keys=False),
            encoding="utf-8",
        )
        return key

    def test_factkey_validates_against_base_sha(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, export, sha = self._repo_and_export(root)
            fk = factkey.load_factkey(self._key(root, sha))
            self.assertEqual(factkey.validate(fk, export), [])

    def test_corrupt_line_blocks_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, export, sha = self._repo_and_export(root)
            fk = factkey.load_factkey(self._key(root, sha, line=1, pattern="needle-pattern"))
            errors = factkey.validate(fk, export)
            self.assertTrue(errors)
            self.assertIn("F1", errors[0].message)

    def test_trap_symbol_absent_at_base_sha_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, export, sha = self._repo_and_export(root)
            fk = factkey.load_factkey(self._key(root, sha, symbol="missing_symbol"))
            errors = factkey.validate(fk, export)
            self.assertTrue(errors)
            self.assertIn("T1", errors[0].message)

    def test_outcome_globs_match_head_and_not_base_export(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, export, sha = self._repo_and_outcome_exports(root)
            fk = factkey.load_factkey(self._key_with_bundle(root, sha, ["docs/outcome-*.md"]))
            with chdir(repo):
                self.assertEqual(factkey.validate(fk, export), [])

    def test_outcome_glob_matching_base_export_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, export, sha = self._repo_and_outcome_exports(root)
            (export / "docs").mkdir()
            (export / "docs" / "outcome-record.md").write_text("leaked outcome\n", encoding="utf-8")
            fk = factkey.load_factkey(self._key_with_bundle(root, sha, ["docs/outcome-*.md"]))
            with chdir(repo):
                errors = factkey.validate(fk, export)
            self.assertTrue(errors)
            self.assertIn("outcome_globs", errors[-1].message)

    def test_outcome_glob_missing_at_head_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, export, sha = self._repo_and_outcome_exports(root)
            fk = factkey.load_factkey(self._key_with_bundle(root, sha, ["docs/missing-outcome-*.md"]))
            with chdir(repo):
                errors = factkey.validate(fk, export)
            self.assertTrue(errors)
            self.assertIn("outcome_globs", errors[-1].message)


if __name__ == "__main__":
    unittest.main()
