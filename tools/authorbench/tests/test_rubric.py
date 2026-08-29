import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from authorbench import factkey, rubric


class TestRubric(unittest.TestCase):
    def _export_and_key(self, root: Path):
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        (repo / "src").mkdir()
        (repo / "src" / "thing.py").write_text("def target_symbol():\n    return 'true-pattern'\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "fixture"], cwd=repo, check=True, stdout=subprocess.PIPE)
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        export = root / "export"
        factkey.export_at(sha, export, repo=repo)
        key_path = root / "fact_key.yaml"
        key_path.write_text(
            yaml.safe_dump(
                {
                    "version": 1,
                    "brief_id": "AB-X",
                    "base_sha": sha,
                    "facts": [
                        {"id": "F1", "claim": "target truth", "file": "src/thing.py", "line": 2, "pattern": "true-pattern"}
                    ],
                    "traps": [
                        {
                            "id": "T1",
                            "name": "trap",
                            "precondition": "target_symbol can be called",
                            "file": "src/thing.py",
                            "symbol": "target_symbol",
                            "historical_catch": "docs/panel.md:3",
                        }
                    ],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return export, factkey.load_factkey(key_path)

    def test_fabricated_citation_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            export, fk = self._export_and_key(Path(td))
            good = rubric.check_r1("Claim cites src/thing.py:2.", fk, export)
            bad = rubric.check_r1("Claim cites src/thing.py:1.", fk, export)
            self.assertEqual(good.fabricated, 0)
            self.assertEqual(good.true, 1)
            self.assertEqual(bad.fabricated, 1)

    def test_count_r4_counts_only_concrete_red_conditions(self):
        draft = """
Obligation: keep the author subset blind; delete the exclusion => red.
Obligation: test the report carefully.
Obligation: remove the wall -> red.
"""
        result = rubric.count_r4(draft)
        self.assertEqual(result.obligations, 3)
        self.assertEqual(result.with_red_condition, 2)

    def test_r6_context_is_input_only_no_verdict(self):
        with tempfile.TemporaryDirectory() as td:
            _, fk = self._export_and_key(Path(td))
            context = rubric.r6_context(fk)
            self.assertEqual(context.traps[0]["id"], "T1")
            self.assertIn("precondition", context.traps[0])
            self.assertNotIn("verdict", context.traps[0])
            self.assertFalse(hasattr(rubric, "check_r6"))


if __name__ == "__main__":
    unittest.main()
