from __future__ import annotations

import subprocess
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DocIndexTests(unittest.TestCase):
    def run_script(self, script: str) -> None:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / script)],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"{script} failed\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}",
        )

    def test_doc_index_is_complete_and_generated_index_is_fresh(self) -> None:
        self.run_script("check-doc-index")

    def test_shared_doc_fragments_do_not_drift(self) -> None:
        self.run_script("check-doc-drift")

    def test_doc_recipes_are_labelled(self) -> None:
        self.run_script("check-doc-recipes")

    def copy_repo_for_probe(self, target: Path) -> None:
        ignored = shutil.ignore_patterns(".git", "__pycache__", "*.pyc")
        for child in ROOT.iterdir():
            if child.name == ".git":
                continue
            dest = target / child.name
            if child.is_dir():
                shutil.copytree(child, dest, ignore=ignored)
            else:
                shutil.copy2(child, dest)

    def test_doc_index_rejects_multiline_and_overlong_purposes(self) -> None:
        sys.path.insert(0, str(ROOT / "scripts"))
        from doc_index_lib import validate_entries

        entries = [
            {
                "path": "README.md",
                "purpose": "line one\nline two",
                "status": "current",
                "audience": "any",
            },
            {
                "path": "SPEC.md",
                "purpose": "x" * 121,
                "status": "current",
                "audience": "any",
            },
        ]

        errors = validate_entries(entries)
        self.assertIn("README.md: purpose must be a single line", errors)
        self.assertIn("SPEC.md: purpose exceeds 120 chars", errors)

    def test_doc_drift_reports_missing_fragment_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp)
            self.copy_repo_for_probe(probe)
            (probe / "docs/fragments/dispatch-recipe.md").unlink()

            proc = subprocess.run(
                [sys.executable, str(probe / "scripts/check-doc-drift")],
                cwd=probe,
                text=True,
                capture_output=True,
            )

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("doc-drift:", proc.stderr)
        self.assertIn("missing fragment dispatch-recipe", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_doc_drift_reports_missing_marker_without_traceback(self) -> None:
        # The dispatch-recipe fragment lives in the bridge package README since the
        # root README was split into per-area front doors; check-doc-drift's target
        # list moved with it, so the probe must strip the marker where it now is.
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp)
            self.copy_repo_for_probe(probe)
            readme = probe / "src/agent_redis_bridge/README.md"
            readme.write_text(
                readme.read_text().replace("<!-- fragment:dispatch-recipe begin -->", "", 1)
            )

            proc = subprocess.run(
                [sys.executable, str(probe / "scripts/check-doc-drift")],
                cwd=probe,
                text=True,
                capture_output=True,
            )

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("doc-drift:", proc.stderr)
        self.assertIn(
            "src/agent_redis_bridge/README.md: missing fragment markers for dispatch-recipe",
            proc.stderr,
        )
        self.assertNotIn("Traceback", proc.stderr)

    def test_doc_drift_reports_src_package_missing_from_overview(self) -> None:
        # ARB-B12 assertion: a shipped package the orientation doc never names
        # must fail check-doc-drift, not wait for a reviewer to notice.
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp)
            self.copy_repo_for_probe(probe)
            pkg = probe / "src" / "zz_probe_unnamed_pkg"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("")

            proc = subprocess.run(
                [sys.executable, str(probe / "scripts/check-doc-drift")],
                cwd=probe,
                text=True,
                capture_output=True,
            )

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("doc-drift:", proc.stderr)
        self.assertIn("does not name src package `zz_probe_unnamed_pkg`", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_doc_drift_allows_skill_only_fragments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp)
            self.copy_repo_for_probe(probe)
            skill = probe / "skills/using-agent-bridge/SKILL.md"
            skill.write_text(
                skill.read_text().replace("abstain | approve | block | needs-changes | timed-out", "approve", 1)
            )

            proc = subprocess.run(
                [sys.executable, str(probe / "scripts/check-doc-drift")],
                cwd=probe,
                text=True,
                capture_output=True,
            )

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("doc-drift:", proc.stderr)
        self.assertIn("SKILL.md: fragment vote-fence differs", proc.stderr)
        self.assertNotIn("README.md: missing fragment markers for vote-fence", proc.stderr)

    def run_doc_recipes_probe(self, body: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp)
            self.copy_repo_for_probe(probe)
            fixture = probe / "docs/fixture-recipes-probe.md"
            fixture.write_text(body)

            return subprocess.run(
                [sys.executable, str(probe / "scripts/check-doc-recipes")],
                cwd=probe,
                text=True,
                capture_output=True,
            )

    def test_doc_recipes_detects_dispatch_after_shell_operator(self) -> None:
        proc = self.run_doc_recipes_probe(
            """# fixture

```bash
cd /x && agent-dispatch --engine codex "t"
```
"""
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(
            "docs/fixture-recipes-probe.md:4: dispatch recipe lacks",
            proc.stdout,
        )

        proc = self.run_doc_recipes_probe(
            """# fixture

```bash
cd /x && agent-dispatch --engine codex --adhoc "t"
# see the agent-dispatch docs
```
"""
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_doc_recipes_scans_tilde_and_indented_fences(self) -> None:
        proc = self.run_doc_recipes_probe(
            """# fixture

~~~bash
agent-dispatch --engine codex "t"
```
~~~
"""
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(
            "docs/fixture-recipes-probe.md:4: dispatch recipe lacks",
            proc.stdout,
        )

        proc = self.run_doc_recipes_probe(
            """# fixture

  ```bash
  agent-dispatch --engine codex "t"
  ```
"""
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(
            "docs/fixture-recipes-probe.md:4: dispatch recipe lacks",
            proc.stdout,
        )

        proc = self.run_doc_recipes_probe(
            """# fixture

<!-- doc-recipes: allow-bare -->
  ~~~bash
  agent-dispatch --engine codex "historical"
  ~~~
"""
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_doc_recipes_checks_each_invocation_independently(self) -> None:
        proc = self.run_doc_recipes_probe(
            """# fixture

```bash
dispatch-dev --engine codex "t"
agent-dispatch --engine codex "t"
```
"""
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(
            "docs/fixture-recipes-probe.md:5: dispatch recipe lacks",
            proc.stdout,
        )

        proc = self.run_doc_recipes_probe(
            """# fixture

```bash
agent-dispatch \\
  --run-id "$RID" \\
  "t"
```
"""
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        proc = self.run_doc_recipes_probe(
            """# fixture

```bash
agent-dispatch --run-id x "a"
agent-dispatch "b"
```
"""
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(
            "docs/fixture-recipes-probe.md:5: dispatch recipe lacks",
            proc.stdout,
        )
        self.assertNotIn("docs/fixture-recipes-probe.md:4: dispatch recipe lacks", proc.stdout)


def test_tracked_markdown_excludes_symlinks(tmp_path, monkeypatch):
    # A symlinked markdown file (e.g. GEMINI.md -> AGENTS.md) is not an independent
    # document and must NOT be required in docs/index.json — exclude symlinks from
    # the tracked-markdown discovery so the alias doesn't demand its own entry.
    sys.path.insert(0, str(ROOT / "scripts"))
    import doc_index_lib

    (tmp_path / "real.md").write_text("# real")
    (tmp_path / "alias.md").symlink_to(tmp_path / "real.md")
    monkeypatch.setattr(doc_index_lib, "ROOT", tmp_path)

    class _Proc:
        stdout = "real.md\nalias.md\n"

    monkeypatch.setattr(doc_index_lib.subprocess, "run", lambda *a, **k: _Proc())
    paths = doc_index_lib.tracked_markdown_paths()
    assert "real.md" in paths
    assert "alias.md" not in paths, "symlinked markdown must be excluded from the index requirement"


if __name__ == "__main__":
    unittest.main()
