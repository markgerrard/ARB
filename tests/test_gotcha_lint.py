from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from agent_redis_bridge import gotcha_lint as gl


def write_registry(root: Path, gotchas: list[dict]) -> Path:
    p = root / ".gotchas.json"
    p.write_text(json.dumps({"gotchas": gotchas}), encoding="utf-8")
    return p


def git_repo_with(files: dict[str, str]) -> str:
    repo = tempfile.mkdtemp(prefix="gotcha-")
    run = lambda *a: subprocess.run(["git", "-C", repo, *a], check=True, capture_output=True)
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    for rel, text in files.items():
        fp = Path(repo) / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(text, encoding="utf-8")
        run("add", rel)
    run("commit", "-q", "-m", "seed")
    return repo


ENFORCED = {
    "id": "pest-throwable", "description": "toThrow(Throwable) is loose",
    "pattern": r"toThrow\(\s*\\?Throwable", "include": ["*.php"], "occurrences": 3, "enforce": True,
}
BRIEFING = {
    "id": "cascade", "description": "cascadeOnDelete on history FK",
    "pattern": r"cascadeOnDelete\(", "include": ["*.php"], "occurrences": 2, "enforce": False,
}


class LoadRegistryTest(unittest.TestCase):
    def test_valid(self):
        with tempfile.TemporaryDirectory() as d:
            p = write_registry(Path(d), [ENFORCED])
            g = gl.load_registry(p)
            self.assertEqual(g[0].id, "pest-throwable")
            self.assertTrue(g[0].enforce)

    def test_missing_field_raises(self):
        with tempfile.TemporaryDirectory() as d:
            p = write_registry(Path(d), [{"id": "x", "description": "y"}])  # no pattern
            with self.assertRaises(gl.RegistryError):
                gl.load_registry(p)

    def test_bad_pattern_raises(self):
        with tempfile.TemporaryDirectory() as d:
            p = write_registry(Path(d), [{"id": "x", "description": "y", "pattern": "([unclosed"}])
            with self.assertRaises(gl.RegistryError):
                gl.load_registry(p)

    def test_missing_file_raises(self):
        with self.assertRaises(gl.RegistryError):
            gl.load_registry(Path("/nonexistent/.gotchas.json"))


class AppliesToTest(unittest.TestCase):
    def test_include_exclude(self):
        # NB: use *vendor/* not */vendor/* — fnmatch's */vendor/* requires a char
        # before the slash, so it would NOT exclude a root-level vendor/ path.
        g = gl.Gotcha(id="x", description="d", pattern="p", include=("*.php",), exclude=("*vendor/*",))
        self.assertTrue(g.applies_to("app/Foo.php"))
        self.assertFalse(g.applies_to("app/Foo.py"))
        self.assertFalse(g.applies_to("vendor/x/Foo.php"))
        self.assertFalse(g.applies_to("app/vendor/Foo.php"))

    def test_empty_include_matches_all(self):
        g = gl.Gotcha(id="x", description="d", pattern="p")
        self.assertTrue(g.applies_to("anything.txt"))


class DiffParseTest(unittest.TestCase):
    def test_added_lines_with_paths_and_linenos(self):
        diff = (
            "diff --git a/app/X.php b/app/X.php\n"
            "--- a/app/X.php\n"
            "+++ b/app/X.php\n"
            "@@ -10,2 +10,3 @@\n"
            " context\n"
            "+added one\n"
            "+added two\n"
            "-removed\n"
        )
        items = list(gl.iter_diff_added_lines(diff))
        self.assertEqual([(p, t) for p, _, t in items], [("app/X.php", "added one"), ("app/X.php", "added two")])
        # new-file line numbers: context=10, added one=11, added two=12
        self.assertEqual([ln for _, ln, _ in items], [11, 12])

    def test_dev_null_target_ignored(self):
        diff = "--- a/x\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n"
        self.assertEqual(list(gl.iter_diff_added_lines(diff)), [])


class ScanTest(unittest.TestCase):
    def test_enforced_and_briefing_and_include_filter(self):
        gA = gl.Gotcha(**{**ENFORCED, "include": tuple(ENFORCED["include"])})
        gB = gl.Gotcha(**{**BRIEFING, "include": tuple(BRIEFING["include"])})
        items = [
            ("a.php", 1, "expect()->toThrow(\\Throwable::class)"),  # hits enforced
            ("b.php", 2, "$t->cascadeOnDelete();"),                  # hits briefing
            ("c.py", 3, "toThrow(Throwable)"),                       # excluded by include (*.php)
        ]
        hits = gl.scan_lines([gA, gB], items)
        ids = sorted((h.gotcha.id, h.path) for h in hits)
        self.assertEqual(ids, [("cascade", "b.php"), ("pest-throwable", "a.php")])


class MainExitCodeTest(unittest.TestCase):
    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = gl.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_enforced_hit_exits_1(self):
        repo = git_repo_with({"tests/X.php": "expect(fn() => f())->toThrow(\\Throwable::class);\n"})
        write_registry(Path(repo), [ENFORCED])
        rc, _, err = self._run(["--root", repo])
        self.assertEqual(rc, 1)
        self.assertIn("pest-throwable", err)

    def test_clean_tree_exits_0(self):
        repo = git_repo_with({"tests/X.php": "expect(fn() => f())->toThrow(MyError::class);\n"})
        write_registry(Path(repo), [ENFORCED])
        rc, _, _ = self._run(["--root", repo])
        self.assertEqual(rc, 0)

    def test_briefing_only_hit_exits_0(self):
        repo = git_repo_with({"db/migrations/m.php": "$t->cascadeOnDelete();\n"})
        write_registry(Path(repo), [{**BRIEFING, "include": ["*.php"]}])
        rc, out, _ = self._run(["--root", repo])
        self.assertEqual(rc, 0)
        self.assertIn("cascade", out)  # warned on stdout, did not fail

    def test_check_graduation_flags_due_briefing(self):
        repo = git_repo_with({"x.php": "ok\n"})
        # a briefing gotcha that has recurred enough to deserve promotion
        write_registry(Path(repo), [{**BRIEFING, "occurrences": gl.GRADUATE_AT}])
        rc, _, err = self._run(["--root", repo, "--check-graduation"])
        self.assertEqual(rc, 1)
        self.assertIn("graduate", err)

    def test_diff_mode_flags_only_added_line(self):
        repo = git_repo_with({"tests/X.php": "clean();\n"})
        write_registry(Path(repo), [ENFORCED])
        # add an offending line and commit so a diff range exists
        run = lambda *a: subprocess.run(["git", "-C", repo, *a], check=True, capture_output=True)
        (Path(repo) / "tests/X.php").write_text("clean();\n->toThrow(\\Throwable::class);\n", encoding="utf-8")
        run("commit", "-aqm", "add bad line")
        rc, _, err = self._run(["--root", repo, "--diff", "HEAD~1..HEAD"])
        self.assertEqual(rc, 1)
        self.assertIn("pest-throwable", err)


if __name__ == "__main__":
    unittest.main()
