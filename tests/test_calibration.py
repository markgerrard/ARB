from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_redis_bridge import calibration as cal


def tmp_log() -> Path:
    return Path(tempfile.mkdtemp(prefix="calib-")) / "log.jsonl"


def iso(days_ago: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


class RecordTest(unittest.TestCase):
    def test_record_and_load_roundtrip(self):
        log = tmp_log()
        d = cal.record_decision(
            log, scope="phase3-i5a", decision="consensus-accept",
            reviewers=[("codex", "APPROVE"), ("agy", "APPROVE WITH NOTES")],
            findings=[{"id": "f1", "reviewer": "codex", "title": "x"}],
            decision_id="dec1", now=iso(),
        )
        self.assertEqual(d["id"], "dec1")
        entries = cal.load(log)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["type"], "decision")
        self.assertEqual(entries[0]["reviewers"][0], {"name": "codex", "verdict": "APPROVE"})

    def test_record_roundtrip_with_seat_models(self):
        log = tmp_log()
        d = cal.record_decision(
            log, scope="phase3-i5a", decision="consensus-accept",
            reviewers=[("codex", "APPROVE"), ("agy", "APPROVE")],
            findings=[{"id": "f1", "reviewer": "codex", "title": "x"}],
            seat_models={"codex": "gpt-5.6", "agy": "claude-sonnet-5"},
            decision_id="dec1", now=iso(),
        )
        self.assertEqual(d["seat_models"], {"codex": "gpt-5.6", "agy": "claude-sonnet-5"})
        self.assertEqual(cal.load(log)[0]["seat_models"]["codex"], "gpt-5.6")

    def test_record_roundtrip_without_seat_models_stays_legacy_shape(self):
        log = tmp_log()
        cal.record_decision(
            log, scope="phase3-i5a", decision="consensus-accept",
            reviewers=[("codex", "APPROVE")], findings=[],
            decision_id="dec1", now=iso(),
        )
        self.assertNotIn("seat_models", cal.load(log)[0])

    def test_append_only(self):
        log = tmp_log()
        cal.record_decision(log, scope="s", decision="d", reviewers=[], findings=[], decision_id="a", now=iso())
        cal.record_decision(log, scope="s", decision="d", reviewers=[], findings=[], decision_id="b", now=iso())
        self.assertEqual([e["id"] for e in cal.load(log)], ["a", "b"])


class OutcomeValidationTest(unittest.TestCase):
    def setUp(self):
        self.log = tmp_log()

    def test_finding_status_requires_finding_id(self):
        with self.assertRaises(ValueError):
            cal.record_outcome(self.log, decision_id="d", status="confirmed", evidence="e")

    def test_decision_status_rejects_finding_id(self):
        with self.assertRaises(ValueError):
            cal.record_outcome(self.log, decision_id="d", status="held", evidence="e", finding_id="f1")

    def test_bad_status_rejected(self):
        with self.assertRaises(ValueError):
            cal.record_outcome(self.log, decision_id="d", status="meh", evidence="e")

    def test_evidence_required(self):
        with self.assertRaises(ValueError):
            cal.record_outcome(self.log, decision_id="d", status="held", evidence="")


class ReportTest(unittest.TestCase):
    def _seed(self, log):
        cal.record_decision(
            log, scope="i1", decision="consensus-accept",
            reviewers=[("codex", "APPROVE")],
            findings=[{"id": "f1", "reviewer": "codex", "title": "a"},
                      {"id": "f2", "reviewer": "codex", "title": "b"},
                      {"id": "f3", "reviewer": "codex", "title": "c"}],
            decision_id="d1", now=iso(),
        )

    def test_empty_log_is_honest(self):
        rep = cal.build_report([], now=iso())
        self.assertEqual(rep["decisions"], 0)
        self.assertIn("Insufficient data", cal.format_report(rep))

    def test_reviewer_confirmation_rate_and_pending(self):
        log = tmp_log()
        self._seed(log)
        cal.record_outcome(log, decision_id="d1", finding_id="f1", status="confirmed", evidence="repro", now=iso())
        cal.record_outcome(log, decision_id="d1", finding_id="f2", status="false_positive", evidence="not real", now=iso())
        # f3 left unresolved -> pending
        rep = cal.build_report(cal.load(log), now=iso())
        r = rep["reviewers"]["codex"]
        self.assertEqual((r["raised"], r["confirmed"], r["false_positive"], r["pending"]), (3, 1, 1, 1))
        self.assertAlmostEqual(r["confirmation_rate"], 0.5)

    def test_noisy_seat_flagged_as_question(self):
        log = tmp_log()
        # 4 findings, 1 confirmed 3 false-positive -> 25% -> anomaly
        cal.record_decision(log, scope="i", decision="consensus-accept",
                            reviewers=[("coldopus", "APPROVE")],
                            findings=[{"id": f"f{i}", "reviewer": "coldopus", "title": ""} for i in range(4)],
                            decision_id="d1", now=iso())
        cal.record_outcome(log, decision_id="d1", finding_id="f0", status="confirmed", evidence="e", now=iso())
        for i in (1, 2, 3):
            cal.record_outcome(log, decision_id="d1", finding_id=f"f{i}", status="false_positive", evidence="e", now=iso())
        rep = cal.build_report(cal.load(log), now=iso())
        self.assertTrue(any("coldopus" in a and "Investigate" in a for a in rep["anomalies"]))
        # report-only: never says "down-weight" as an instruction
        self.assertTrue(any("do NOT down-weight" in a for a in rep["anomalies"]))

    def test_regressed_decision_flagged(self):
        log = tmp_log()
        cal.record_decision(log, scope="i", decision="override", reviewers=[], findings=[], decision_id="d1", now=iso())
        cal.record_outcome(log, decision_id="d1", status="regressed", evidence="prod incident", now=iso())
        rep = cal.build_report(cal.load(log), now=iso())
        self.assertEqual(rep["decision_types"]["override"], {"held": 0, "regressed": 1})
        self.assertTrue(any("override" in a for a in rep["anomalies"]))

    def test_stale_held_flagged_for_reconfirmation(self):
        log = tmp_log()
        cal.record_decision(log, scope="i", decision="consensus-accept", reviewers=[], findings=[], decision_id="d1", now=iso(200))
        cal.record_outcome(log, decision_id="d1", status="held", evidence="ok then", now=iso(120))
        rep = cal.build_report(cal.load(log), now=iso(), stale_days=90)
        self.assertEqual(len(rep["stale_held"]), 1)
        self.assertGreater(rep["stale_held"][0]["age_days"], 90)
        self.assertTrue(any("Re-confirm" in a or "re-confirm" in a for a in rep["anomalies"]))

    def test_same_finding_id_across_decisions_does_not_collide(self):
        # Regression (codex review): finding ids are local labels ("f1") and recur
        # across decisions. Keying outcomes by finding_id alone made one review's
        # outcome overwrite another's. Must be scoped by (decision_id, finding_id).
        log = tmp_log()
        cal.record_decision(log, scope="a", decision="consensus-accept",
                            reviewers=[("codex", "APPROVE")],
                            findings=[{"id": "f1", "reviewer": "codex", "title": ""}], decision_id="d1", now=iso())
        cal.record_decision(log, scope="b", decision="consensus-accept",
                            reviewers=[("agy", "APPROVE")],
                            findings=[{"id": "f1", "reviewer": "agy", "title": ""}], decision_id="d2", now=iso())
        cal.record_outcome(log, decision_id="d1", finding_id="f1", status="confirmed", evidence="e", now=iso())
        cal.record_outcome(log, decision_id="d2", finding_id="f1", status="false_positive", evidence="e", now=iso())
        rep = cal.build_report(cal.load(log), now=iso())
        # both reviewers tracked independently despite the shared finding id
        self.assertEqual(rep["reviewers"]["codex"]["confirmed"], 1)
        self.assertEqual(rep["reviewers"]["codex"]["false_positive"], 0)
        self.assertEqual(rep["reviewers"]["agy"]["false_positive"], 1)
        self.assertEqual(rep["reviewers"]["agy"]["confirmed"], 0)

    def test_latest_outcome_wins(self):
        log = tmp_log()
        cal.record_decision(log, scope="i", decision="consensus-accept",
                            reviewers=[("codex", "APPROVE")],
                            findings=[{"id": "f1", "reviewer": "codex", "title": ""}], decision_id="d1", now=iso())
        cal.record_outcome(log, decision_id="d1", finding_id="f1", status="false_positive", evidence="e", now=iso(5))
        cal.record_outcome(log, decision_id="d1", finding_id="f1", status="confirmed", evidence="reproduced later", now=iso())
        rep = cal.build_report(cal.load(log), now=iso())
        self.assertEqual(rep["reviewers"]["codex"]["confirmed"], 1)
        self.assertEqual(rep["reviewers"]["codex"]["false_positive"], 0)

    def test_by_model_grouping_inherits_decision_model_not_outcome_model(self):
        entries = [
            {"type": "decision", "id": "d1", "ts": iso(), "scope": "i", "decision": "consensus-accept",
             "reviewers": [{"name": "codex", "verdict": "APPROVE"}],
             "findings": [{"id": "f1", "reviewer": "codex", "title": ""}],
             "seat_models": {"codex": "gpt-5.6"}},
            {"type": "outcome", "id": "o1", "assessed_at": iso(), "decision_id": "d1",
             "finding_id": "f1", "status": "confirmed", "evidence": "e",
             "seat_models": {"codex": "wrong-outcome-model"}},
        ]
        rep = cal.build_report(entries, now=iso())
        by_model = rep["reviewers"]["codex"]["by_model"]
        self.assertIn("gpt-5.6", by_model)
        self.assertNotIn("wrong-outcome-model", by_model)
        self.assertEqual(by_model["gpt-5.6"]["confirmed"], 1)

    def test_by_model_formatting_only_when_reviewer_has_two_models(self):
        log = tmp_log()
        cal.record_decision(log, scope="a", decision="consensus-accept",
                            reviewers=[("codex", "APPROVE")],
                            findings=[{"id": "f1", "reviewer": "codex", "title": ""}],
                            seat_models={"codex": "gpt-5.5"}, decision_id="d1", now=iso())
        cal.record_outcome(log, decision_id="d1", finding_id="f1", status="confirmed", evidence="e", now=iso())
        one_model = cal.format_report(cal.build_report(cal.load(log), now=iso()))
        self.assertNotIn("by model:", one_model)

        cal.record_decision(log, scope="b", decision="consensus-accept",
                            reviewers=[("codex", "APPROVE")],
                            findings=[{"id": "f1", "reviewer": "codex", "title": ""}],
                            seat_models={"codex": "gpt-5.6"}, decision_id="d2", now=iso())
        cal.record_outcome(log, decision_id="d2", finding_id="f1", status="false_positive", evidence="e", now=iso())
        two_models = cal.format_report(cal.build_report(cal.load(log), now=iso()))
        self.assertIn("  by model:", two_models)
        self.assertIn("  - gpt-5.5: raised 1 | confirmed 1 | false-positive 0 | pending 0 | confirm-rate 100%", two_models)
        self.assertIn("  - gpt-5.6: raised 1 | confirmed 0 | false-positive 1 | pending 0 | confirm-rate 0%", two_models)

    def test_legacy_entries_without_seat_models_match_golden_report_bytes(self):
        entries = [
            {"type": "decision", "id": "legacy1", "ts": "2026-07-01T00:00:00+00:00",
             "scope": "i", "decision": "consensus-accept", "diff_range": "",
             "reviewers": [{"name": "codex", "verdict": "APPROVE"}],
             "findings": [{"id": "f1", "reviewer": "codex", "title": "bug"}], "note": ""},
            {"type": "outcome", "id": "out1", "assessed_at": "2026-07-02T00:00:00+00:00",
             "decision_id": "legacy1", "finding_id": "f1", "status": "confirmed",
             "evidence": "repro", "source": "live", "note": ""},
        ]
        got = cal.format_report(cal.build_report(entries, now="2026-07-07T00:00:00+00:00"))
        want = (Path(__file__).parent / "golden" / "calibration_legacy_report.txt").read_text(encoding="utf-8")
        self.assertEqual(got + "\n", want)

    def _record_model_findings(self, log, *, decision_prefix, reviewer, model, total, confirmed):
        for i in range(total):
            did = f"{decision_prefix}-{i}"
            cal.record_decision(log, scope="i", decision="consensus-accept",
                                reviewers=[(reviewer, "APPROVE")],
                                findings=[{"id": "f1", "reviewer": reviewer, "title": ""}],
                                seat_models={reviewer: model}, decision_id=did, now=iso())
            status = "confirmed" if i < confirmed else "false_positive"
            cal.record_outcome(log, decision_id=did, finding_id="f1", status=status, evidence="e", now=iso())

    def test_model_drift_flags_at_30_points_not_29(self):
        log = tmp_log()
        self._record_model_findings(log, decision_prefix="a", reviewer="codex", model="gpt-5.5", total=100, confirmed=100)
        self._record_model_findings(log, decision_prefix="b", reviewer="codex", model="gpt-5.6", total=100, confirmed=71)
        rep = cal.build_report(cal.load(log), now=iso())
        self.assertFalse(any("model-version drift" in a for a in rep["anomalies"]))

        log = tmp_log()
        self._record_model_findings(log, decision_prefix="a", reviewer="codex", model="gpt-5.5", total=10, confirmed=10)
        self._record_model_findings(log, decision_prefix="b", reviewer="codex", model="gpt-5.6", total=10, confirmed=7)
        rep = cal.build_report(cal.load(log), now=iso())
        drift = [a for a in rep["anomalies"] if "model-version drift" in a]
        self.assertEqual(len(drift), 1)
        self.assertIn("codex", drift[0])
        self.assertIn("gpt-5.5=100% (10 records)", drift[0])
        self.assertIn("gpt-5.6=70% (10 records)", drift[0])
        self.assertIn("Investigate", drift[0])
        self.assertNotIn("weight", drift[0].lower())

    def test_model_drift_flags_exact_30_point_float_boundary(self):
        log = tmp_log()
        self._record_model_findings(log, decision_prefix="a", reviewer="codex", model="gpt-5.5", total=10, confirmed=7)
        self._record_model_findings(log, decision_prefix="b", reviewer="codex", model="gpt-5.6", total=10, confirmed=4)

        rep = cal.build_report(cal.load(log), now=iso())

        drift = [a for a in rep["anomalies"] if "model-version drift" in a]
        self.assertEqual(len(drift), 1)
        self.assertIn("gpt-5.5=70% (10 records)", drift[0])
        self.assertIn("gpt-5.6=40% (10 records)", drift[0])

    def test_model_drift_flags_three_model_nonconsecutive_spread(self):
        log = tmp_log()
        self._record_model_findings(log, decision_prefix="a", reviewer="codex", model="gpt-5.5", total=10, confirmed=8)
        self._record_model_findings(log, decision_prefix="b", reviewer="codex", model="gpt-5.6", total=20, confirmed=13)
        self._record_model_findings(log, decision_prefix="c", reviewer="codex", model="gpt-5.7", total=10, confirmed=5)

        rep = cal.build_report(cal.load(log), now=iso())

        drift = [a for a in rep["anomalies"] if "model-version drift" in a]
        self.assertEqual(len(drift), 1)
        self.assertIn("gpt-5.5=80% (10 records)", drift[0])
        self.assertIn("gpt-5.6=65% (20 records)", drift[0])
        self.assertIn("gpt-5.7=50% (10 records)", drift[0])

    def test_model_drift_requires_five_resolved_records_per_model(self):
        log = tmp_log()
        self._record_model_findings(log, decision_prefix="a", reviewer="codex", model="gpt-5.5", total=5, confirmed=5)
        self._record_model_findings(log, decision_prefix="b", reviewer="codex", model="gpt-5.6", total=4, confirmed=0)
        rep = cal.build_report(cal.load(log), now=iso())
        self.assertFalse(any("model-version drift" in a for a in rep["anomalies"]))

        cal.record_decision(log, scope="i", decision="consensus-accept",
                            reviewers=[("codex", "APPROVE")],
                            findings=[{"id": "f1", "reviewer": "codex", "title": ""}],
                            seat_models={"codex": "gpt-5.6"}, decision_id="b-4", now=iso())
        cal.record_outcome(log, decision_id="b-4", finding_id="f1", status="false_positive", evidence="e", now=iso())
        rep = cal.build_report(cal.load(log), now=iso())
        self.assertTrue(any("model-version drift" in a for a in rep["anomalies"]))

    def test_explicit_null_seat_models_reports_as_unknown(self):
        entries = [
            {"type": "decision", "id": "d1", "ts": iso(), "scope": "i", "decision": "consensus-accept",
             "reviewers": [{"name": "codex", "verdict": "APPROVE"}],
             "findings": [{"id": "f1", "reviewer": "codex", "title": ""}],
             "seat_models": None},
            {"type": "outcome", "id": "o1", "assessed_at": iso(), "decision_id": "d1",
             "finding_id": "f1", "status": "confirmed", "evidence": "e"},
        ]

        rep = cal.build_report(entries, now=iso())

        by_model = rep["reviewers"]["codex"]["by_model"]
        self.assertEqual(by_model["unknown"]["confirmed"], 1)

    def test_naive_legacy_assessed_at_does_not_crash_report(self):
        entries = [
            {"type": "decision", "id": "d1", "ts": "2026-07-01T00:00:00", "scope": "i",
             "decision": "consensus-accept", "reviewers": [], "findings": []},
            {"type": "outcome", "id": "o1", "assessed_at": "2026-07-01T00:00:00",
             "decision_id": "d1", "status": "held", "evidence": "legacy"},
        ]

        rep = cal.build_report(entries, now="2026-07-02T00:00:00+00:00", stale_days=90)

        self.assertEqual(rep["decision_types"]["consensus-accept"], {"held": 1, "regressed": 0})
        self.assertEqual(rep["stale_held"], [])


class CliTest(unittest.TestCase):
    def _run(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cal.main(argv)
        return rc, out.getvalue()

    def test_record_annotate_report_flow(self):
        log = str(tmp_log())
        rc, out = self._run(["--log", log, "record", "--scope", "i5b", "--decision", "consensus-accept",
                             "--reviewer", "codex=APPROVE", "--finding", "f1:codex:bug"])
        self.assertEqual(rc, 0)
        did = out.strip()
        rc, _ = self._run(["--log", log, "annotate", "--decision", did, "--finding", "f1",
                           "--status", "confirmed", "--evidence", "repro test"])
        self.assertEqual(rc, 0)
        rc, out = self._run(["--log", log, "report"])
        self.assertEqual(rc, 0)
        self.assertIn("codex", out)
        self.assertIn("report-only", out)

    def test_report_strict_exits_1_on_anomaly(self):
        log = str(tmp_log())
        self._run(["--log", log, "record", "--scope", "i", "--decision", "override", "--id", "d1"])
        self._run(["--log", log, "annotate", "--decision", "d1", "--status", "regressed", "--evidence", "incident"])
        rc, _ = self._run(["--log", log, "report", "--strict"])
        self.assertEqual(rc, 1)

    def test_record_parses_seat_model_args(self):
        log = str(tmp_log())
        rc, out = self._run(["--log", log, "record", "--scope", "i5b", "--decision", "consensus-accept",
                             "--reviewer", "codex=APPROVE", "--seat-model", "codex=gpt-5.6",
                             "--seat-model", "agy=claude-sonnet-5"])
        self.assertEqual(rc, 0)
        did = out.strip()
        entry = cal.load(Path(log))[0]
        self.assertEqual(entry["id"], did)
        self.assertEqual(entry["seat_models"], {"codex": "gpt-5.6", "agy": "claude-sonnet-5"})


if __name__ == "__main__":
    unittest.main()
