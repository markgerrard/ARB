import json
import tempfile
import unittest
from pathlib import Path

from authorbench import ledger


class TestLedger(unittest.TestCase):
    def test_burned_pair_refuses_without_override(self):
        hard = ledger.check_burn("warm-orchestrator", "AB-D1", "none-known", override=None)
        self.assertFalse(hard.proceed)
        self.assertEqual(hard.reason, "hard-burned-lineage")

        mirror = ledger.check_burn("codex-lineage", "AB-D1", "possible(public-mirror)", override=None)
        self.assertFalse(mirror.proceed)
        self.assertEqual(mirror.reason, "prior-exposure-requires-override")

        fork = ledger.check_burn("codex-lineage", "AB-D1", "possible(fork-known)", override=None)
        self.assertFalse(fork.proceed)
        self.assertEqual(fork.reason, "prior-exposure-requires-override")

        allowed = ledger.check_burn("codex-lineage", "AB-D1", "possible(public-mirror)", override="accepted by human")
        self.assertTrue(allowed.proceed)
        self.assertTrue(allowed.override_recorded)

    def test_possible_other_flags_and_proceeds(self):
        decision = ledger.check_burn("codex-lineage", "AB-D1", "possible(seen-adjacent-doc)", override=None)
        self.assertTrue(decision.proceed)
        self.assertTrue(decision.flagged)
        self.assertEqual(decision.reason, "possible-other")

    def test_ledger_records_every_authoring_event(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)
            ledger.record(run, "lineage-a", "AB-D1", "none-known")
            ledger.record(run, "lineage-b", "AB-S1", "possible(seen-adjacent-doc)", override="recorded")
            rows = [
                json.loads(line)
                for line in (run / "exposure.ndjson").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([(row["seat_lineage"], row["brief_id"]) for row in rows], [("lineage-a", "AB-D1"), ("lineage-b", "AB-S1")])
            self.assertEqual(rows[1]["override"], "recorded")

    def test_invalid_prior_exposure_refuses(self):
        with self.assertRaises(ValueError):
            ledger.check_burn("lineage", "AB-D1", "maybe", override=None)


if __name__ == "__main__":
    unittest.main()
