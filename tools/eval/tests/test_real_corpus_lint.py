import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arb_eval import schema

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
sys.path.insert(0, str(FIXTURES))
import real_corpus_lint  # noqa: E402


class TestRealCorpusLint(unittest.TestCase):
    def _repo(self, td: str) -> Path:
        repo = Path(td) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "x@y.z"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "x"], check=True)
        (repo / "app.py").write_text("def f():\n    seed = 1\n    clean = 2\n")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
        return repo

    def test_seed_id_in_patch_or_commit_message_fails_build(self):
        for surface in ("patch", "commit", "tag", "branch"):
            with self.subTest(surface=surface), tempfile.TemporaryDirectory() as td:
                repo = self._repo(td)
                patch = Path(td) / "seed.patch"
                patch.write_text("clean patch\n")
                if surface == "patch":
                    patch.write_text("# D0 is named here\n")
                elif surface == "commit":
                    subprocess.run(["git", "-C", str(repo), "commit", "--allow-empty", "-q", "-m", "mentions D0"],
                                   check=True)
                elif surface == "tag":
                    subprocess.run(["git", "-C", str(repo), "tag", "tag-D0"], check=True)
                elif surface == "branch":
                    subprocess.run(["git", "-C", str(repo), "branch", "branch-D0"], check=True)
                with self.assertRaises(real_corpus_lint.LintError):
                    real_corpus_lint.assert_marker_free(repo, [patch], ["D0"])

    def test_seed_and_control_within_window_in_one_function_raises_scenario_error(self):
        data = {
            "schema_rev": 2,
            "id": "rev2",
            "subject": {"repo": ".", "base": "b", "head": "h"},
            "seeded_defects": [{
                "id": "D0", "class": "logic", "legible": True,
                "location": {"file": "app.py", "line": 2}, "description": "x",
            }],
            "control_loci": [{
                "id": "C0", "class": "logic",
                "location": {"file": "app.py", "line": 3}, "description": "clean",
            }],
            "panel": [{"seat": "codex", "model": "gpt", "harness": "mock"}],
        }
        with self.assertRaises(schema.ScenarioError):
            schema.from_dict(data)
        legacy = dict(data)
        legacy.pop("schema_rev")
        schema.from_dict(legacy)
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(td)
            self.assertTrue(real_corpus_lint.same_enclosing_function(repo, data["seeded_defects"][0],
                                                                     data["control_loci"][0]))

    def test_anti_oversplit_lint_caps_effective_clusters_at_rationale_count(self):
        controls = [
            {"id": "C1", "cluster": "a", "why_clean": "same reason"},
            {"id": "C2", "cluster": "b", "why_clean": "same reason"},
            {"id": "C3", "cluster": "c", "why_clean": "different"},
        ]
        result = real_corpus_lint.effective_clusters(controls)
        self.assertEqual(result["nominal"], 3)
        self.assertEqual(result["effective"], 2)

    def test_builder_refuses_without_pinned_sha(self):
        env = dict(os.environ)
        env.pop("ARB_EVAL_REAL_BASE_SHA", None)
        proc = subprocess.run(
            [str(FIXTURES / "build_floor_real_secrets.sh")],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("ARB_EVAL_REAL_BASE_SHA", proc.stderr)
