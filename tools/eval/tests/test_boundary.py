import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arb_eval import boundary, pipeline
from test_pipeline import controls_for, scenario_from

HEURISTIC_SKIP_REASON = "heuristic fallback requires tree-sitter and ctags unavailable"


class TestBoundaryOracle(unittest.TestCase):
    def _write_fixture(self, td: str) -> Path:
        p = Path(td) / "app.py"
        p.write_text(
            "def outer():\n"
            "    def inner():\n"
            "        return 1\n"
            "    return inner()\n\n"
            "class Service:\n"
            "    def method(self):\n"
            "        return 2\n\n"
            "@route('/x')\n"
            "async def route_handler():\n"
            "    return 3\n"
        )
        return p

    def test_heuristic_tier_methods_nested_decorated(self):
        if boundary.tree_sitter_available() or boundary.ctags_available():
            self.skipTest(HEURISTIC_SKIP_REASON)
        with tempfile.TemporaryDirectory() as td:
            self._write_fixture(td)
            cases = [
                (3, "inner"),
                (8, "method"),
                (12, "route_handler"),
            ]
            for line, expected in cases:
                with self.subTest(line=line):
                    got = boundary.enclosing_symbol(Path(td), "app.py", line)
                    self.assertEqual(got.symbol, expected)
                    self.assertEqual(got.tier, "heuristic")

    def test_heuristic_tier_skip_reason_names_availability_guard(self):
        self.assertIn("tree-sitter", HEURISTIC_SKIP_REASON)
        self.assertIn("ctags", HEURISTIC_SKIP_REASON)

    def test_forced_absence_falls_through_and_records_tier(self):
        with tempfile.TemporaryDirectory() as td:
            self._write_fixture(td)
            with mock.patch.object(boundary, "_tree_sitter_symbol", return_value=None), \
                 mock.patch.object(shutil, "which", return_value=None):
                got = boundary.enclosing_symbol(Path(td), "app.py", 8)
            self.assertEqual(got.symbol, "method")
            self.assertEqual(got.tier, "heuristic")

    @unittest.skipUnless(boundary.ctags_available(), "ctags unavailable; boundary oracle ctags test skipped")
    def test_ctags_tier_when_available(self):
        with tempfile.TemporaryDirectory() as td:
            self._write_fixture(td)
            with mock.patch.object(boundary, "_tree_sitter_symbol", return_value=None):
                got = boundary.enclosing_symbol(Path(td), "app.py", 8)
            self.assertEqual(got.symbol, "method")
            self.assertEqual(got.tier, "ctags")

    @unittest.skipUnless(boundary.tree_sitter_available(), "tree-sitter unavailable; boundary oracle tree-sitter test skipped")
    def test_treesitter_tier_when_available(self):
        with tempfile.TemporaryDirectory() as td:
            self._write_fixture(td)
            got = boundary.enclosing_symbol(Path(td), "app.py", 8)
            self.assertEqual(got.symbol, "method")
            self.assertEqual(got.tier, "tree-sitter")


class TestBoundaryPipelineWiring(unittest.TestCase):
    def test_matcher_decision_records_boundary_oracle_field(self):
        seeds = [{
            "id": "D0", "class": "logic", "legible": True,
            "location": {"file": "a.py", "line": 2},
            "description": "seeded",
        }]
        sc = scenario_from(seeds, control_loci=controls_for("logic", 5))
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            (repo / "a.py").write_text("def f():\n    bad = 1\n    return bad\n")
            sc.subject["repo"] = str(repo)
            raw = "FINDING bad"
            result = pipeline.run_floor(
                sc,
                dispatcher=pipeline.MockDispatcher({"review:codex:0": raw}),
                normalizer=pipeline.MockNormalizer({
                    raw: [{"class": "logic", "location": {"file": "a.py", "line": 3}}],
                }),
                output_root=Path(td),
                repeats=1,
            )
            rows = [json.loads(line) for line in Path(result.events_path).read_text().splitlines()]
            function_rows = [
                row for row in rows
                if row.get("event") == "matcher_decision" and row.get("basis") == "function"
            ]
            self.assertTrue(function_rows)
            self.assertTrue(all("boundary_oracle" in row for row in function_rows))
