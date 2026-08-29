import ast
import unittest
from pathlib import Path


TEST_DIR = Path(__file__).resolve().parent
HERMETIC_TEST_FILES = [
    "test_bundle.py",
    "test_factkey.py",
    "test_normalize.py",
    "test_rubric.py",
    "test_ledger.py",
    "test_score_report.py",
    "test_reproducibility.py",
    "test_cli.py",
    "test_frozen_corpus_validates.py",
]
ALLOWED_SUBPROCESSES = {"git", "tar"}


class TestHermeticSuite(unittest.TestCase):
    def test_no_docker_no_engine_imports_in_hermetic_paths(self):
        for name in HERMETIC_TEST_FILES:
            with self.subTest(file=name):
                text = (TEST_DIR / name).read_text(encoding="utf-8")
                self.assertNotIn("shutil.which(\"docker\")", text)
                self.assertNotIn('"docker"', text)
                tree = ast.parse(text)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "run":
                        if node.args and isinstance(node.args[0], ast.List) and node.args[0].elts:
                            first = node.args[0].elts[0]
                            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                                self.assertIn(first.value, ALLOWED_SUBPROCESSES)

    def test_live_proofs_skip_with_asserted_reason(self):
        text = (TEST_DIR / "test_jail.py").read_text(encoding="utf-8")
        self.assertIn("LIVE_SKIP_REASON", text)
        self.assertIn("requires docker and arb-eval-seat:latest", text)
        self.assertIn("docker\", \"image\", \"inspect\", \"arb-eval-seat:latest\"", text)
        self.assertGreaterEqual(text.count("@unittest.skipUnless(_has_authorbench_image(), LIVE_SKIP_REASON)"), 2)


if __name__ == "__main__":
    unittest.main()
