import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arb_eval import gold, pipeline, stats
from test_pipeline import controls_for, five_seeds, scenario_from


class TestGold(unittest.TestCase):
    def _pair_root(self, td: str) -> Path:
        root = Path(td) / "gold"
        seat_dir = root / "codex"
        seat_dir.mkdir(parents=True)
        return root

    def test_export_packet_has_no_outcome_fields_and_inlines_snippet(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            (repo / "a.py").write_text("one\nmatch_line\nthree\n")
            root = self._pair_root(td)
            (root / "codex" / "pairs.ndjson").write_text(json.dumps({
                "pair_id": "p1", "seat": "codex",
                "raw_finding": "logic | a.py:2 | x",
                "context": {"repo": str(repo), "base": "b", "head": "h"},
                "pipeline": {
                    "normalized": {"location": {"file": "a.py", "line": 2}},
                    "match_outcome": "detected",
                    "basis": "window",
                },
                "adjudications": [], "final_verdict": None,
            }) + "\n")
            out = Path(td) / "packets.ndjson"
            with mock.patch.object(gold, "GOLD_ROOT", root):
                gold.export("codex", out)
            packet = json.loads(out.read_text())
            self.assertIn("match_line", packet["snippet"])
            for forbidden in ("pipeline", "match_outcome", "basis", "final_verdict"):
                self.assertNotIn(forbidden, packet)

    def test_rate_stub_parses_retries_once_then_records_rater_error(self):
        with tempfile.TemporaryDirectory() as td:
            packets = Path(td) / "packets.ndjson"
            packets.write_text(json.dumps({"pair_id": "p1"}) + "\n")
            out = Path(td) / "rater.ndjson"
            calls = []
            def bad(_packet):
                calls.append(1)
                return "not parseable"
            gold.rate("model-alias-1", packets, out, call=bad)
            row = json.loads(out.read_text())
            self.assertEqual(len(calls), 2)
            self.assertEqual(row["verdict"], "rater_error")

    def test_ingest_and_score_recall_precision_and_pairwise_kappa(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._pair_root(td)
            pairs = [
                {"pair_id": "p1", "seat": "codex", "raw_finding": "logic | a.py:1 | p1", "context": {},
                 "pipeline": {"match_outcome": "detected"}, "adjudications": [],
                 "final_verdict": None},
                {"pair_id": "p2", "seat": "codex", "raw_finding": "logic | a.py:2 | p2", "context": {},
                 "pipeline": {"match_outcome": "matcher-ambiguous"}, "adjudications": [],
                 "final_verdict": None},
            ]
            (root / "codex" / "pairs.ndjson").write_text("\n".join(json.dumps(p) for p in pairs) + "\n")
            rater = Path(td) / "rater.ndjson"
            rater.write_text("\n".join([
                json.dumps({"pair_id": "p1", "rater": "model-alias-1", "verdict": "match"}),
                json.dumps({"pair_id": "p1", "rater": "mark", "verdict": "match"}),
                json.dumps({"pair_id": "p1", "rater": "grok", "verdict": "match"}),
                json.dumps({"pair_id": "p2", "rater": "model-alias-1", "verdict": "match"}),
                json.dumps({"pair_id": "p2", "rater": "mark", "verdict": "nonmatch"}),
                json.dumps({"pair_id": "p2", "rater": "grok", "verdict": "ambiguous"}),
            ]) + "\n")
            with mock.patch.object(gold, "GOLD_ROOT", root):
                gold.ingest(rater)
                pairs_after = [json.loads(line) for line in (root / "codex" / "pairs.ndjson").read_text().splitlines()]
                summary = gold.score("codex")
            self.assertEqual(summary["recall"]["k"], 1)
            self.assertEqual(summary["recall"]["n"], 1)
            self.assertEqual(summary["precision"]["k"], 1)
            self.assertEqual(summary["precision"]["n"], 1)
            self.assertAlmostEqual(summary["raw_agreement"], 0.5)
            self.assertEqual(pairs_after[1]["final_verdict"], "ambiguous")
            self.assertEqual(stats.cohen_kappa(["match"], ["match"]), 1.0)
            self.assertLessEqual(summary["kappa"], 0.0)

    def test_gate_suppresses_below_gate_seat_only(self):
        seeds = five_seeds("logic")
        sc = scenario_from(
            seeds,
            panel=[
                {"seat": "codex", "model": "gpt", "harness": "mock"},
                {"seat": "agy", "model": "gemini", "harness": "mock"},
            ],
            control_loci=controls_for("logic", 5),
        )
        lines = "\n".join(f"FINDING seed {i}" for i in range(5))
        norms = {
            f"FINDING seed {i}": [{
                "class": "logic",
                "location": {"file": "a.py", "line": 10 + i, "symbol": f"seed_{i}"},
            }]
            for i in range(5)
        }
        responses = {"review:codex:0": lines, "review:agy:0": lines}
        below = {"recall": {"lo": 0.1, "hi": 0.2}, "matcher_gate": 0.85, "gold_version": "abc"}
        above = {"recall": {"lo": 0.9, "hi": 1.0}, "matcher_gate": 0.85, "gold_version": "def"}
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(gold, "load_summary", side_effect=lambda seat: below if seat == "codex" else above):
            result = pipeline.run_floor(
                sc, dispatcher=pipeline.MockDispatcher(responses),
                normalizer=pipeline.MockNormalizer(norms), output_root=Path(td), repeats=1,
            )
        self.assertNotIn("codex", result.verdicts)
        self.assertIn("agy", result.verdicts)
        self.assertIn("SUPPRESSED: codex matcher recall lo=0.100 < gate=0.85", result.report_text)

    def test_delete_gate_wiring_would_unsuppress(self):
        seeds = five_seeds("logic")
        sc = scenario_from(
            seeds,
            panel=[
                {"seat": "codex", "model": "gpt", "harness": "mock"},
                {"seat": "agy", "model": "gemini", "harness": "mock"},
            ],
            control_loci=controls_for("logic", 5),
        )
        lines = "\n".join(f"FINDING seed {i}" for i in range(5))
        norms = {
            f"FINDING seed {i}": [{
                "class": "logic",
                "location": {"file": "a.py", "line": 10 + i, "symbol": f"seed_{i}"},
            }]
            for i in range(5)
        }
        responses = {"review:codex:0": lines, "review:agy:0": lines}
        below = {"recall": {"lo": 0.1, "hi": 0.2}, "matcher_gate": 0.85, "gold_version": "abc"}
        above = {"recall": {"lo": 0.9, "hi": 1.0}, "matcher_gate": 0.85, "gold_version": "def"}
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(gold, "load_summary", side_effect=lambda seat: below if seat == "codex" else above):
            result = pipeline.run_floor(
                sc, dispatcher=pipeline.MockDispatcher(responses),
                normalizer=pipeline.MockNormalizer(norms), output_root=Path(td), repeats=1,
            )
        self.assertNotIn("codex", result.verdicts)

        with mock.patch.object(pipeline, "_gold_suppressed", return_value=False):
            with tempfile.TemporaryDirectory() as td, \
                 mock.patch.object(gold, "load_summary", side_effect=lambda seat: below if seat == "codex" else above):
                result = pipeline.run_floor(
                    sc, dispatcher=pipeline.MockDispatcher(responses),
                    normalizer=pipeline.MockNormalizer(norms), output_root=Path(td), repeats=1,
                )
        self.assertIn("codex", result.verdicts)
