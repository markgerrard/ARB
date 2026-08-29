import unittest

from authorbench import judge, report, score


class TestScoreReport(unittest.TestCase):
    def test_reporter_refuses_rank_fields(self):
        for key in ["winner", "composite", "score_sum", "cross_seat_sort_key", "Rank", "rank_order", "cross-seat-sort-key", "crossSeatSortKey"]:
            with self.subTest(key=key):
                with self.assertRaises(report.WallBreach):
                    report.guard({"seat": "codex", key: "forbidden"})

    def test_headline_counts_not_rates(self):
        text = report.render_seat_report(
            "codex",
            {"AB-D1": {"R1": score.MET}},
            {"mechanical_counts": {"verified": 7, "checked": 9, "fabricated": 2}},
        )
        self.assertIn("7 verified of 9, 2 fabricated", text)
        self.assertNotIn("7/9", text)
        self.assertNotIn("%", text)

    def test_r1_mechanical_caps_cell(self):
        anchors = [judge.Anchor(2, "good"), judge.Anchor(2, "also good")]
        self.assertEqual(score.cell("R1", anchors, {"r1_cap": score.PARTIAL}), score.PARTIAL)
        self.assertEqual(score.cell("R6", anchors, {"r1_cap": score.NOT_MET}), score.MET)

    def test_all_judges_errored_cell_is_unknown(self):
        verdict = score.cell("R2", [judge.JUDGE_ERROR, judge.JUDGE_ERROR], {})
        self.assertEqual(verdict, score.UNKNOWN_JUDGE_ERROR)
        self.assertTrue(verdict.rerunnable)

    def test_parse_anchor_missing_dim_is_judge_error_not_dropped(self):
        parsed = judge.parse_anchors("R1: 2 | cites src/x.py:4\nR3: 1 | thin alternatives\n")
        self.assertEqual(parsed["R1"].score, 2)
        self.assertIs(parsed["R2"], judge.JUDGE_ERROR)
        self.assertEqual(parsed["R3"].quote, "thin alternatives")


if __name__ == "__main__":
    unittest.main()
