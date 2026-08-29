"""Tests for the deterministic core: stats, viability oracle, the structural wall, power.

Run: cd tools/eval && python -m pytest -q   (or: python -m unittest discover tests)
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arb_eval import power, report, schema, stats          # noqa: E402
from arb_eval.viability import classify, PASS, FAIL, UNKNOWN  # noqa: E402


class TestWilson(unittest.TestCase):
    def test_known_interval(self):
        # 8/10 at 95% -> Wilson ~ [0.490, 0.943] (standard reference values).
        iv = stats.wilson(8, 10, 0.05)
        self.assertAlmostEqual(iv.lo, 0.490, places=2)
        self.assertAlmostEqual(iv.hi, 0.943, places=2)

    def test_zero_trials_is_maximal_uncertainty(self):
        iv = stats.wilson(0, 0)
        self.assertEqual((iv.lo, iv.hi), (0.0, 1.0))


class TestViability(unittest.TestCase):
    def test_above_noise_passes(self):
        # catches 28/30, noise 2/30 -> clearly above own noise -> PASS
        self.assertEqual(classify(28, 30, 2, 30).verdict, PASS)

    def test_at_noise_fails(self):
        # flag-everything: caught ~= noise (both high) -> not above noise -> FAIL/UNKNOWN, never PASS
        v = classify(27, 30, 26, 30).verdict
        self.assertIn(v, (FAIL, UNKNOWN))
        self.assertNotEqual(v, PASS)

    def test_below_noise_fails(self):
        self.assertEqual(classify(1, 30, 20, 30).verdict, FAIL)

    def test_underpowered_is_unknown(self):
        # tiny N, modest separation -> intervals overlap -> UNKNOWN (not a seat judgement)
        self.assertEqual(classify(2, 3, 0, 3).verdict, UNKNOWN)


class TestWall(unittest.TestCase):
    def test_guard_rejects_denylisted_fields(self):
        for bad in ("rank", "seat_value", "marginal_contribution", "drop", "score"):
            with self.assertRaises(report.WallBreach):
                report.guard({"seat": "codex", bad: 1})

    def test_guard_allows_clean_record(self):
        report.guard({"seat": "codex", "secrets-in-logs": "PASS"})  # no raise

    def test_grid_has_no_raw_rates_and_carries_disclaimer(self):
        grid = report.render_grid(
            {"codex": {"secrets-in-logs": "PASS"}, "agy": {"secrets-in-logs": "FAIL"}},
            ["secrets-in-logs"],
        )
        self.assertIn("PASS", grid)
        self.assertIn("accepted", grid.lower())          # residual named, not walled
        self.assertNotIn("0.", grid)                      # no raw rates leaked into headline
        # rows alphabetical: agy before codex
        self.assertLess(grid.index("agy"), grid.index("codex"))


class TestPower(unittest.TestCase):
    def test_budget_coheres(self):
        b = power.compute()
        self.assertGreaterEqual(b.instances * b.repeats, b.trials)
        self.assertGreaterEqual(b.instances, b.target.I_min)
        self.assertGreaterEqual(b.repeats, b.target.R_min)
        self.assertGreater(b.gold_per_seat, 30)           # must beat the v0.1 self-defeating 30

    def test_gold_gate_is_actually_clearable(self):
        # the derived gold size must let a true-0.95 matcher clear the 0.85 gate
        b = power.compute()
        k = round(b.target.matcher_true_recall * b.gold_per_seat)
        self.assertGreaterEqual(stats.wilson(k, b.gold_per_seat).lo, b.target.matcher_gate)


class TestSchema(unittest.TestCase):
    def test_rejects_unknown_class(self):
        with self.assertRaises(schema.ScenarioError):
            schema.from_dict({"id": "x", "subject": {}, "panel": [],
                              "seeded_defects": [{"id": "D1", "class": "not-a-class"}]})

    def test_class_level_vs_instance_level(self):
        seeds = [{"id": f"D{i}", "class": "secrets-in-logs", "legible": True} for i in range(6)]
        seeds += [{"id": "E1", "class": "cors", "legible": True}]
        sc = schema.from_dict({"id": "x", "subject": {},
                               "panel": [{"seat": "codex"}], "seeded_defects": seeds})
        ok = sc.class_level_ok(i_min=5)
        self.assertTrue(ok["secrets-in-logs"])   # 6 >= 5
        self.assertFalse(ok["cors"])              # 1 < 5 -> instance-level only


class TestAllowlistWall(unittest.TestCase):
    """The wall is an allowlist now — these are the escapes the panel demonstrated, blocked."""

    def test_seat_name_smuggle_blocked(self):
        with self.assertRaises(report.WallBreach):
            report.render_grid({"codex (rank 1)": {"secrets-in-logs": "PASS"}}, ["secrets-in-logs"])

    def test_nontaxonomy_column_blocked(self):
        with self.assertRaises(report.WallBreach):
            report.render_grid({"codex": {"ranking": "PASS"}}, ["ranking"])

    def test_nonverdict_value_blocked(self):
        with self.assertRaises(report.WallBreach):
            report.render_grid({"codex": {"secrets-in-logs": "1st"}}, ["secrets-in-logs"])

    def test_guard_recurses_and_normalizes(self):
        # synonyms / nested / whitespace that escaped the old name-denylist
        for bad in ({"detail": {"rank": 1}}, {" rank ": 1}, {"seat_quality": 1},
                    {"r a n k": 1}, {"rank_order": 1}, {"ordering": 1}, {"goodness": 1}):
            with self.assertRaises(report.WallBreach):
                report.guard(bad)


class TestDegenerate(unittest.TestCase):
    """Behavior under degenerate input — the property the original (coherence-only) tests missed."""

    def test_unresolvable_target_raises_not_cap(self):
        # the cap-as-converged bug: an unresolvable V must RAISE, never return cap as a budget
        with self.assertRaises(power.Unachievable):
            power.compute(power.PowerTarget(V=0.02, nu=0.10))

    def test_out_of_range_params_raise(self):
        for t in (power.PowerTarget(alpha=0.0), power.PowerTarget(alpha=1.2),
                  power.PowerTarget(V=0.0), power.PowerTarget(V=-0.1), power.PowerTarget(nu=1.0)):
            with self.assertRaises(power.Unachievable):
                power.compute(t)

    def test_wilson_and_zfor_domain(self):
        for k, n in ((-1, 10), (11, 10)):
            with self.assertRaises(ValueError):
                stats.wilson(k, n)
        with self.assertRaises(ValueError):
            stats.z_for(0.0)

    def test_budget_exposes_required_control_loci(self):
        b = power.compute()
        self.assertEqual(b.control_loci_required, b.trials)


class TestSchemaValidation(unittest.TestCase):
    def test_bad_power_param_is_clean_scenario_error(self):
        with self.assertRaises(schema.ScenarioError):
            schema.from_dict({"id": "x", "subject": {}, "panel": [{"seat": "codex"}],
                              "seeded_defects": [{"id": "D1", "class": "cors", "legible": True}],
                              "power": {"alpha": 0.0}})

    def test_bad_control_loci_shape_is_clean_scenario_error(self):
        with self.assertRaises(schema.ScenarioError):
            schema.from_dict({"id": "x", "subject": {}, "panel": [{"seat": "codex"}],
                              "seeded_defects": [{"id": "D1", "class": "cors", "legible": True}],
                              "control_loci": -3})


if __name__ == "__main__":
    unittest.main()
