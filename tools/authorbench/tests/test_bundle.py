import tempfile
import unittest
from pathlib import Path

import yaml

from authorbench import bundle


class TestBundle(unittest.TestCase):
    def _src(self, root: Path) -> Path:
        src = root / "src"
        (src / "steer").mkdir(parents=True)
        (src / "brief.md").write_text("# Brief\nDo the thing.\n", encoding="utf-8")
        (src / "steer" / "grounded-claims.md").write_text("Cite claims.\n", encoding="utf-8")
        (src / "steer" / "scope-restraint.md").write_text("Stay scoped.\n", encoding="utf-8")
        (src / "fact_key.yaml").write_text("version: 1\nbrief_id: AB-X\nbase_sha: abc123\nfacts: []\ntraps: []\n", encoding="utf-8")
        (src / "bundle.yaml").write_text(
            yaml.safe_dump(
                {
                    "id": "AB-X",
                    "stage": "design",
                    "source": "fixture",
                    "provenance": "reconstructed(v1)",
                    "length_budget": 300,
                    "normalizer_version": 1,
                    "factkey_version": 1,
                    "outcome_globs": ["docs/superpowers/**/fixture-outcome-*"],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return src

    def test_freeze_hashes_every_file_and_writes_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = self._src(root)
            manifest = bundle.freeze(src, corpus_version="v1.0", base_sha="abc123")
            frozen = root / "corpus" / "v1.0" / "AB-X"
            data = yaml.safe_load((frozen / "bundle.yaml").read_text(encoding="utf-8"))
            self.assertEqual(manifest.id, "AB-X")
            for key in [
                "id",
                "stage",
                "corpus_version",
                "source",
                "provenance",
                "base_sha",
                "length_budget",
                "normalizer_version",
                "factkey_version",
                "outcome_globs",
                "steer_blocks",
                "hashes",
            ]:
                self.assertIn(key, data)
            self.assertEqual(data["outcome_globs"], ["docs/superpowers/**/fixture-outcome-*"])
            self.assertEqual(set(data["hashes"]), {"brief_md", "fact_key_yaml", "steer"})
            self.assertEqual(set(data["hashes"]["steer"]), {"grounded-claims", "scope-restraint"})
            for digest in [data["hashes"]["brief_md"], data["hashes"]["fact_key_yaml"], *data["hashes"]["steer"].values()]:
                self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_author_visible_subset_excludes_factkey(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = bundle.freeze(self._src(root), corpus_version="v1.0", base_sha="abc123")
            loaded = bundle.load(manifest.path)
            subset = bundle.author_visible_subset(loaded)
            self.assertEqual(set(subset), {"brief.md", "length_budget", "steer/grounded-claims.md", "steer/scope-restraint.md"})
            self.assertNotIn("fact_key.yaml", subset)
            self.assertFalse(any("fact_key" in key or "factkey" in key for key in subset))

    def test_judge_inputs_include_factkey_author_subset_does_not(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = bundle.freeze(self._src(root), corpus_version="v1.0", base_sha="abc123")
            loaded = bundle.load(manifest.path)
            self.assertIn("fact_key.yaml", loaded.judge_inputs())
            self.assertNotIn("fact_key.yaml", loaded.author_visible_subset())

    def test_freeze_refuses_overwrite_existing_bundle(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = self._src(root)
            bundle.freeze(src, corpus_version="v1.0", base_sha="abc123")
            with self.assertRaises(FileExistsError):
                bundle.freeze(src, corpus_version="v1.0", base_sha="abc123")


if __name__ == "__main__":
    unittest.main()
