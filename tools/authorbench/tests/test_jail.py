import shutil
import subprocess
import tempfile
import unittest
from fnmatch import fnmatch
from pathlib import Path

import yaml

from authorbench import bundle, jail


AB_D1_OUTCOME_GLOBS = ["docs/superpowers/**/2026-07-03-arb-watch-history-*"]
LIVE_SKIP_REASON = "live authorbench confinement proof requires docker and arb-eval-seat:latest"


def _require_history(tc: unittest.TestCase, *shas: str) -> None:
    """The frozen corpus pins commits in the private origin's history. A fresh-history checkout
    (the public mirror) cannot resolve them; skip with the reason rather than fail on git."""
    for sha in shas:
        if subprocess.run(["git", "rev-parse", "--verify", "-q", f"{sha}^{{commit}}"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            tc.skipTest(f"fresh-history checkout: pinned commit {sha} is not in this repo")


def _has_authorbench_image() -> bool:
    return bool(shutil.which("docker")) and subprocess.run(
        ["docker", "image", "inspect", "arb-eval-seat:latest"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def _archive_paths(sha: str, root: Path) -> list[str]:
    export = root / sha
    export.mkdir()
    archive = subprocess.run(
        ["git", "archive", "--format=tar", sha],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout
    tar = export / ".archive.tar"
    tar.write_bytes(archive)
    subprocess.run(["tar", "-xf", str(tar), "-C", str(export)], check=True)
    tar.unlink()
    return [p.relative_to(export).as_posix() for p in export.rglob("*") if p.is_file()]


def _matches(paths: list[str], globs: list[str]) -> list[str]:
    return [path for path in paths for pattern in globs if fnmatch(path, pattern)]


class TestJail(unittest.TestCase):
    def _bundle(self, root: Path) -> bundle.Bundle:
        bdir = root / "bundle"
        (bdir / "steer").mkdir(parents=True)
        (bdir / "brief.md").write_text("brief\n", encoding="utf-8")
        (bdir / "steer" / "grounded-claims.md").write_text("cite\n", encoding="utf-8")
        (bdir / "fact_key.yaml").write_text("version: 1\nbrief_id: AB-X\nfacts: []\ntraps: []\n", encoding="utf-8")
        (bdir / "bundle.yaml").write_text(
            yaml.safe_dump(
                {
                    "id": "AB-X",
                    "length_budget": 300,
                    "outcome_globs": AB_D1_OUTCOME_GLOBS,
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return bundle.load(bdir)

    def test_archive_export_excludes_outcome_globs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _require_history(self, "9de2850", "33a09a6")
            base_paths = _archive_paths("9de2850", root)
            outcome_paths = _archive_paths("33a09a6", root)
            self.assertEqual(_matches(base_paths, AB_D1_OUTCOME_GLOBS), [])
            self.assertTrue(_matches(outcome_paths, AB_D1_OUTCOME_GLOBS))

    def test_author_env_has_no_arb_memory_and_no_network(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            prof = jail.author_profile(self._bundle(root), "9de2850")
            self.assertFalse(prof.arb_memory_enabled)
            self.assertEqual(prof.network, "model-api-only")
            judge = jail.judge_profile(root / "export", root / "draft.md", root / "fact_key.yaml", root / "rubric.yaml")
            self.assertEqual({m.name for m in judge.mounts}, {"export", "draft", "fact_key", "rubric", "home", "config"})

    def test_author_mounts_exclude_factkey(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            b = self._bundle(root)
            prof = jail.author_profile(b, "9de2850")
            jail.assert_manifest(prof, "author")
            planted = root / "author-visible"
            planted.mkdir()
            (planted / "fact_key.yaml").write_text("version: 1\nfacts: []\ntraps: []\n", encoding="utf-8")
            bad = prof.with_mount_path("author_visible", planted)
            with self.assertRaises(jail.ManifestError):
                jail.assert_manifest(bad, "author")

    def test_author_manifest_exactly(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            prof = jail.author_profile(self._bundle(root), "9de2850")
            self.assertEqual({m.name for m in prof.mounts}, {"workspace", "author_visible", "home", "config"})
            self.assertFalse(any("agy-home" in str(m.path) for m in prof.mounts))
            jail.assert_manifest(prof, "author")

    def test_judge_manifest_exactly(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            prof = jail.judge_profile(root / "export", root / "draft.md", root / "fact_key.yaml", root / "rubric.yaml")
            self.assertEqual({m.name for m in prof.mounts}, {"export", "draft", "fact_key", "rubric", "home", "config"})
            self.assertFalse(any("agy-home" in str(m.path) for m in prof.mounts))
            jail.assert_manifest(prof, "judge")

    @unittest.skipUnless(_has_authorbench_image(), LIVE_SKIP_REASON)
    def test_absolute_path_read_blocked_in_jail(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = Path("docs/superpowers/reviews/2026-07-03-arb-watch-history-authoring-bakeoff.md").resolve()
            self.assertTrue(target.exists())
            self.assertEqual(subprocess.run(["bash", "-lc", f"cat {str(target)!r} >/dev/null"]).returncode, 0)
            result = subprocess.run(
                ["tools/authorbench/confinement/confined-authorbench.sh", "author", "path-read", str(target)],
                cwd=Path.cwd(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_path_read_mode_pins_secure_and_leak_exit_polarities(self):
        script = Path("tools/authorbench/confinement/confined-authorbench.sh").read_text(encoding="utf-8")
        self.assertIn("cat \"$1\" >/dev/null", script)
        self.assertIn("exit 0", script)
        self.assertIn("exit 1", script)
        self.assertLess(script.index("cat \"$1\" >/dev/null"), script.index("exit 1"))

    def test_confined_runner_declares_role_surfaces_and_canary_before_turn(self):
        script = Path("tools/authorbench/confinement/confined-authorbench.sh").read_text(encoding="utf-8")
        for token in [
            "AUTHORBENCH_WORKSPACE",
            "AUTHORBENCH_AUTHOR_VISIBLE",
            "AUTHORBENCH_EXPORT",
            "AUTHORBENCH_DRAFT",
            "AUTHORBENCH_FACT_KEY",
            "AUTHORBENCH_RUBRIC",
            "authorbench-canary.sh",
        ]:
            self.assertIn(token, script)
        self.assertLess(script.index("authorbench-canary.sh"), script.index("codex exec"))

    @unittest.skipUnless(_has_authorbench_image(), LIVE_SKIP_REASON)
    def test_canary_reds_on_seeded_token_per_role(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "outcome.txt").write_text("docs/superpowers/reviews/2026-07-03-arb-watch-history-authoring-bakeoff.md\n", encoding="utf-8")
            author = subprocess.run(["tools/authorbench/confinement/authorbench-canary.sh", "author", str(root)], stdout=subprocess.PIPE)
            self.assertNotEqual(author.returncode, 0)
            (root / "outcome.txt").unlink()
            (root / "identity.txt").write_text("AUTHOR_IDENTITY_TOKEN\n", encoding="utf-8")
            judge = subprocess.run(["tools/authorbench/confinement/authorbench-canary.sh", "judge", str(root)], stdout=subprocess.PIPE)
            self.assertNotEqual(judge.returncode, 0)


if __name__ == "__main__":
    unittest.main()
