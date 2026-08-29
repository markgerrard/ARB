import tempfile
import unittest
from pathlib import Path
import subprocess

import yaml

from authorbench import factkey


CORPUS = Path("tools/authorbench/corpus/v1.0")


def _require_history(tc: unittest.TestCase, *shas: str) -> None:
    """The frozen corpus pins commits in the private origin's history. A fresh-history checkout
    (the public mirror) cannot resolve them; skip with the reason rather than fail on git."""
    for sha in shas:
        if subprocess.run(["git", "rev-parse", "--verify", "-q", f"{sha}^{{commit}}"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            tc.skipTest(f"fresh-history checkout: pinned commit {sha} is not in this repo")


class TestFrozenCorpusValidates(unittest.TestCase):
    def test_every_committed_bundle_passes_v9(self):
        bundles = sorted(path for path in CORPUS.iterdir() if (path / "bundle.yaml").exists())
        self.assertEqual({path.name for path in bundles}, {"AB-D1", "AB-S1", "AB-P1"})
        for bundle_dir in bundles:
            with self.subTest(bundle=bundle_dir.name):
                fk = factkey.load_factkey(bundle_dir / "fact_key.yaml")
                _require_history(self, fk.base_sha)
                with tempfile.TemporaryDirectory() as td:
                    export = factkey.export_at(fk.base_sha, td)
                    self.assertEqual(factkey.validate(fk, export), [])

    def test_verbatim_brief_base_sha_resolves_real_tree(self):
        bundle_yaml = yaml.safe_load((CORPUS / "AB-S1" / "bundle.yaml").read_text(encoding="utf-8"))
        _require_history(self, "364c0b0")
        expected = subprocess.check_output(["git", "rev-parse", "364c0b0^"], text=True).strip()
        self.assertEqual(bundle_yaml["base_sha"], expected)


if __name__ == "__main__":
    unittest.main()
