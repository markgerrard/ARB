import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arb_eval import gold, pipeline, provenance, publish, report
from test_pipeline import controls_for, five_seeds, scenario_from


class FailingDispatcher:
    def __init__(self, kind="timeout"):
        self.kind = kind

    def dispatch(self, task):
        raise pipeline.DispatchError("forced failure", kind=self.kind)


class TestPublish(unittest.TestCase):
    def _run_dir(self, td: str, seats=("codex",), dirty=False) -> Path:
        seeds = five_seeds("logic")
        panel = [{"seat": seat, "model": "gpt", "harness": "mock"} for seat in seats]
        sc = scenario_from(
            seeds,
            panel=panel,
            power={"V": 0.8, "nu": 0.01, "alpha": 0.01, "I_min": 5, "R_min": 1},
            control_loci=controls_for("logic", 5),
        )
        with mock.patch.object(provenance, "_git_describe", return_value="abc"):
            result = pipeline.run_floor(
                sc,
                dispatcher=FailingDispatcher(),
                normalizer=pipeline.MockNormalizer({}),
                output_root=Path(td),
                repeats=1,
            )
        run = Path(result.detail_path).parent
        if dirty:
            detail = json.loads((run / "detail.json").read_text())
            detail["provenance"]["harness_version"]["describe"] = "abc-dirty"
            (run / "detail.json").write_text(json.dumps(detail))
        return run

    def test_artifact_uses_detail_fields_from_real_run_floor_output(self):
        with tempfile.TemporaryDirectory() as td:
            below = {"recall": {"lo": 0.1, "hi": 0.2}, "matcher_gate": 0.85, "gold_version": "REAL-GOLD"}
            with mock.patch.object(gold, "load_summary", return_value=below):
                run = self._run_dir(td)
            artifact = publish.build_artifact(run)
        self.assertEqual(artifact["gold"]["gold_version"], "REAL-GOLD")
        self.assertTrue(artifact["gold"]["gold_adjudicated"])
        self.assertEqual(artifact["gold"]["suppressed_seats"], ["codex"])
        self.assertTrue(any(line.startswith("SMALL-N controls: logic") for line in artifact["small_n_lines"]))
        self.assertEqual(artifact["infra_incomplete_lines"], ["infra_incomplete(codex, kind=timeout, repeat=0)"])

    def test_artifact_has_four_keys_and_verbatim_disclaimer(self):
        with tempfile.TemporaryDirectory() as td:
            artifact = publish.build_artifact(self._run_dir(td))
        for key in ("seat", "model_version", "harness_version", "corpus_version"):
            self.assertIn(key, artifact)
        self.assertEqual(artifact["disclaimer"], report.DISCLAIMER)

    def test_injected_rank_field_raises_wallbreach(self):
        with tempfile.TemporaryDirectory() as td:
            run = self._run_dir(td)
            detail = json.loads((run / "detail.json").read_text())
            detail["rank"] = 1
            (run / "detail.json").write_text(json.dumps(detail))
            with self.assertRaises(report.WallBreach):
                publish.build_artifact(run)

    def test_injected_matcher_recall_field_raises_wallbreach(self):
        self.assertEqual(report.guard({"matcher_recall": 0.9}), {"matcher_recall": 0.9})
        with self.assertRaises(report.WallBreach):
            report.assert_gold_field_shape({"gold_adjudicated": True, "matcher_recall": 0.9})

    def test_dirty_harness_refuses_publish(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(publish.PublishError, "dirty harness"):
                publish.build_artifact(self._run_dir(td, dirty=True))

    def test_gold_field_is_gate_outcome_only_shape(self):
        with tempfile.TemporaryDirectory() as td:
            artifact = publish.build_artifact(self._run_dir(td))
        self.assertEqual(set(artifact["gold"]), {"gold_adjudicated", "gold_version", "suppressed_seats"})

    def test_assert_verdict_row_scoped_to_grid_not_claim_levels(self):
        with tempfile.TemporaryDirectory() as td:
            run = self._run_dir(td)
            detail = json.loads((run / "detail.json").read_text())
            detail["claim_levels"] = {"logic": "CLASS-LEVEL"}
            (run / "detail.json").write_text(json.dumps(detail))
            artifact = publish.build_artifact(run)
        self.assertEqual(artifact["claim_levels"]["logic"], "CLASS-LEVEL")

    def test_multi_seat_requires_seat(self):
        with tempfile.TemporaryDirectory() as td:
            run = self._run_dir(td, seats=("codex", "agy"))
            with self.assertRaisesRegex(publish.PublishError, "agy.*codex"):
                publish.build_artifact(run)
