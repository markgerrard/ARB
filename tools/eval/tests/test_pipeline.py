import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from arb_eval import pipeline, schema
from arb_eval.viability import PASS, UNKNOWN


def controls_for(cls, count=2):
    return [{
        "id": f"C{i}", "class": cls,
        "location": {"file": "clean.py", "line": 100 + i, "symbol": f"clean_{i}"},
        "description": "plausible but clean",
    } for i in range(count)]


def scenario_from(seeds, panel=None, power=None, control_loci=None):
    classes = []
    for seed in seeds:
        if seed["class"] not in classes:
            classes.append(seed["class"])
    return schema.from_dict({
        "id": "pipe-test",
        "description": "pipeline test",
        "subject": {"repo": ".", "base": "base-sha", "head": "head-sha"},
        "seeded_defects": seeds,
        "panel": panel or [{"seat": "codex", "model": "gpt", "harness": "mock"}],
        "power": power or {"V": 0.8, "nu": 0.01, "alpha": 0.10, "I_min": 5, "R_min": 1},
        "control_loci": control_loci if control_loci is not None else [
            locus for cls in classes for locus in controls_for(cls)
        ],
    })


def five_seeds(cls="secrets-in-logs"):
    return [{
        "id": f"D{i}", "class": cls, "legible": True,
        "location": {"file": "a.py", "line": 10 + i, "symbol": f"seed_{i}"},
        "description": "seeded defect",
    } for i in range(5)]


class TestSegment(unittest.TestCase):
    def test_segment_bundle_and_noop(self):
        bundled = "- secrets in logs at src/auth.py:10\n- input trust at src/api.py:20"
        out = pipeline.segment_reply(bundled)
        self.assertEqual(len(out), 2)
        self.assertEqual(pipeline.segment_reply("No issues found."), [])

    def test_no_issues_does_not_eat_real_finding(self):
        reply = "No issues in auth.\nFINDING: token logged at src/auth.py:10"
        self.assertEqual(pipeline.segment_reply(reply), ["FINDING: token logged at src/auth.py:10"])

    def test_mixed_format_bundle_keeps_all_candidates(self):
        reply = "FINDING: token logged at src/auth.py:10\ninput-trust at src/api.py:20"
        self.assertEqual(
            pipeline.segment_reply(reply),
            ["FINDING: token logged at src/auth.py:10", "input-trust at src/api.py:20"],
        )

    def test_malformed_json_is_not_a_finding(self):
        self.assertEqual(pipeline.segment_reply("{bad json"), [])

    def test_format_conformance(self):
        self.assertEqual(pipeline.format_conformance([]), (1.0, 0, 0))
        self.assertEqual(
            pipeline.format_conformance([
                "logic | app.py:10 | bad branch",
                "not the format",
                "not-a-class | app.py:11 | desc",
            ]),
            (1 / 3, 1, 3),
        )


class TestMatch(unittest.TestCase):
    def test_match_symbol_window_and_function(self):
        seed = schema.Seed(
            id="D1", cls="secrets-in-logs",
            location={"file": "a.py", "line": 10, "symbol": "log_request"},
            description="",
        )
        self.assertEqual(
            pipeline.match_finding(
                {"class": "secrets-in-logs", "location": {"file": "a.py", "symbol": "log_request"}},
                seed,
            ).basis,
            "symbol",
        )
        self.assertEqual(
            pipeline.match_finding(
                {"class": "secrets-in-logs", "location": {"file": "a.py", "line": 17}},
                seed,
                window=10,
            ).basis,
            "window",
        )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.py"
            p.write_text("def f():\n    x=1\n    y=2\n    z=3\n")
            seed2 = schema.Seed(
                id="D2", cls="logic", location={"file": "a.py", "line": 2}, description=""
            )
            m = pipeline.match_finding(
                {"class": "logic", "location": {"file": "a.py", "line": 4}},
                seed2,
                repo=Path(td),
                window=0,
            )
            self.assertEqual(m.basis, "function")

    def test_function_match_respects_block_scope(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.py"
            p.write_text("def f():\n    x = 1\n\noutside = True\n")
            seed = schema.Seed("D1", "logic", {"file": "a.py", "line": 2}, "")
            outside = pipeline.match_finding(
                {"class": "logic", "location": {"file": "a.py", "line": 4}},
                seed,
                repo=Path(td),
                window=0,
            )
            inside = pipeline.match_finding(
                {"class": "logic", "location": {"file": "a.py", "line": 2}},
                seed,
                repo=Path(td),
                window=0,
            )
            self.assertNotEqual(outside.outcome, "detected")
            self.assertEqual(inside.basis, "function")

    def test_window_does_not_cross_function_boundary(self):
        # P-3 gate (cold-opus P0): a finding in a DIFFERENT function than the seed but within the line
        # window must NOT match via the window fallback — that conflates a clean-control false positive
        # with a seed detection (inflating caught, hiding noise). Both funcs resolve + differ -> miss.
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.py"
            p.write_text("def f():\n    leak = 1\n    return leak\n\n"
                         "def g():\n    clean = 2\n    return clean\n")
            seed = schema.Seed("D1", "logic", {"file": "a.py", "line": 2}, "")
            m = pipeline.match_finding(
                {"class": "logic", "location": {"file": "a.py", "line": 6}},  # in g(), within window of line 2
                seed, repo=Path(td), window=10,
            )
            self.assertNotEqual(m.outcome, "detected")

    def test_match_ambiguous_unknown_class(self):
        seed = schema.Seed("D1", "logic", {"file": "a.py", "line": 1}, "")
        self.assertEqual(pipeline.match_finding({"class": "unknown", "location": {}}, seed).outcome,
                         "matcher-ambiguous")


class TestRunFloor(unittest.TestCase):
    def test_off_quorum_sentinel_refuses_real_run(self):
        sc = scenario_from([
            {"id": "D1", "class": "secrets-in-logs", "legible": True,
             "location": {"file": "a.py", "line": 1}, "description": "x"}
        ])
        with self.assertRaisesRegex(pipeline.Parked, "off-quorum normalizer seat unresolved"):
            pipeline.run_floor(sc, dispatcher=pipeline.MockDispatcher({}))

    def test_panel_seat_normalizer_parks(self):
        sc = scenario_from([
            {"id": "D1", "class": "secrets-in-logs", "legible": True,
             "location": {"file": "a.py", "line": 1}, "description": "x"}
        ])
        with self.assertRaisesRegex(pipeline.Parked, "panel seat"):
            pipeline.run_floor(
                sc,
                dispatcher=pipeline.MockDispatcher({}),
                normalizer_seat="codex",
            )

    def test_instance_level_relabeling_no_class_pass_fail(self):
        sc = scenario_from([
            {"id": "D1", "class": "secrets-in-logs", "legible": True,
             "location": {"file": "a.py", "line": 10, "symbol": "log_request"},
             "description": "token logged"}
        ])
        dispatcher = pipeline.MockDispatcher({
            "review:codex:0": "FINDING: token logged at a.py:10",
        })
        normalizer = pipeline.MockNormalizer({
            "FINDING: token logged at a.py:10": [{
                "class": "secrets-in-logs",
                "location": {"file": "a.py", "line": 10, "symbol": "log_request"},
                "severity": "P1",
                "statement": "token logged",
                "confidence": 1.0,
            }],
        })
        with tempfile.TemporaryDirectory() as td:
            result = pipeline.run_floor(sc, dispatcher=dispatcher, normalizer=normalizer,
                                        output_root=Path(td), repeats=1)
            self.assertEqual(result.claim_levels["secrets-in-logs"], "INSTANCE-LEVEL")
            self.assertEqual(result.verdicts["codex"], {})
            self.assertIn("INSTANCE-LEVEL", result.report_text)
            self.assertNotIn("secrets-in-logs  PASS", result.report_text)

    def test_run_writes_ndjson_and_gold_flag_and_wall_compliant_grid(self):
        seeds = five_seeds()
        # >= 5 effective control clusters so a clean seat's Wilson CI separates (this test exercises the
        # PASS path; pooling/no-gate behavior is covered by test_repeats_are_pooled_no_hard_gate...).
        sc = scenario_from(seeds, control_loci=controls_for("secrets-in-logs", 5))
        raw_lines = []
        norms = {}
        for i in range(5):
            raw = f"FINDING: token logged D{i}"
            raw_lines.append(raw)
            norms[raw] = [{
                "class": "secrets-in-logs",
                "location": {"file": "a.py", "line": 10 + i, "symbol": f"seed_{i}"},
                "severity": "P1",
                "statement": "token logged",
                "confidence": 1.0,
            }]
        dispatcher = pipeline.MockDispatcher({"review:codex:0": "\n".join(raw_lines)})
        with tempfile.TemporaryDirectory() as td:
            result = pipeline.run_floor(
                sc,
                dispatcher=dispatcher,
                normalizer=pipeline.MockNormalizer(norms),
                output_root=Path(td),
                repeats=1,
            )
            self.assertEqual(result.verdicts["codex"]["secrets-in-logs"], PASS)
            self.assertEqual([t["task_id"] for t in dispatcher.tasks], ["review:codex:0"])
            self.assertEqual(dispatcher.tasks[0]["repo"], ".")
            self.assertEqual(dispatcher.tasks[0]["base"], "base-sha")
            self.assertEqual(dispatcher.tasks[0]["head"], "head-sha")
            self.assertTrue(result.gold_unadjudicated)
            self.assertIn("GOLD_UNADJUDICATED", result.report_text)
            events = Path(result.events_path).read_text().splitlines()
            event_types = [json.loads(line)["event"] for line in events]
            for expected in ("scenario_loaded", "dispatch_start", "dispatch_end",
                             "finding_emitted", "segmented", "normalized",
                             "matcher_decision", "oracle_result", "run_end"):
                self.assertIn(expected, event_types)
            self.assertFalse(any(json.loads(line).get("task", {}).get("kind") == "control"
                                 for line in events))

    def test_detail_fields_for_matcher_and_format_split(self):
        seeds = five_seeds("logic")
        sc = scenario_from(seeds, control_loci=controls_for("logic", 5))
        raw_lines = ["not the format"] + [f"logic | b.py:{10 + i} | seeded" for i in range(4)]
        norms = {
            "not the format": [{"class": "unknown", "location": {}}],
            **{
                f"logic | b.py:{10 + i} | seeded": [{
                    "class": "logic",
                    "location": {"file": "b.py", "line": 10 + i, "symbol": f"other_{i}"},
                }]
                for i in range(4)
            },
        }
        with tempfile.TemporaryDirectory() as td:
            result = pipeline.run_floor(
                sc,
                dispatcher=pipeline.MockDispatcher({"review:codex:0": "\n".join(raw_lines)}),
                normalizer=pipeline.MockNormalizer(norms),
                output_root=Path(td),
                repeats=1,
            )
            detail = json.loads(Path(result.detail_path).read_text())
            row = detail["oracle"]["codex"]["logic"]
            self.assertEqual(row["matcher_ambiguous_n"], 5)
            self.assertIsNone(row["matcher_band"])
            self.assertAlmostEqual(row["format_conformance_mean"], 4 / 5)
            for verdict in result.verdicts["codex"].values():
                self.assertIn(verdict, {"PASS", "FAIL", "UNKNOWN"})

    def test_verdict_is_derived_from_ndjson_events(self):
        seeds = five_seeds()
        sc = scenario_from(seeds)
        raw_lines = []
        norms = {}
        for i in range(5):
            raw = f"FINDING token logged D{i}"
            raw_lines.append(raw)
            norms[raw] = [{
                "class": "secrets-in-logs",
                "location": {"file": "a.py", "line": 10 + i, "symbol": f"seed_{i}"},
                "severity": "P1",
                "statement": "token logged",
                "confidence": 1.0,
            }]
        responses = {"review:codex:0": "\n".join(raw_lines)}

        original = pipeline._Recorder.write

        def drop_matcher_decisions(self, event, **payload):
            if event == "matcher_decision":
                return None
            return original(self, event, **payload)

        pipeline._Recorder.write = drop_matcher_decisions
        try:
            with tempfile.TemporaryDirectory() as td:
                result = pipeline.run_floor(
                    sc,
                    dispatcher=pipeline.MockDispatcher(responses),
                    normalizer=pipeline.MockNormalizer(norms),
                    output_root=Path(td),
                    repeats=1,
                )
                self.assertEqual(result.verdicts["codex"]["secrets-in-logs"], UNKNOWN)
        finally:
            pipeline._Recorder.write = original

    def test_forbidden_normalized_fields_do_not_reach_outputs(self):
        seeds = five_seeds()
        sc = scenario_from(seeds)
        raw_lines = []
        norms = {}
        for i in range(5):
            raw = f"FINDING token logged D{i}"
            raw_lines.append(raw)
            norms[raw] = [{
                "class": "secrets-in-logs",
                "location": {"file": "a.py", "line": 10 + i, "symbol": f"seed_{i}"},
                "rank": 1,
            }]
        responses = {"review:codex:0": "\n".join(raw_lines)}
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(Exception, "forbidden seat-value"):
                pipeline.run_floor(
                    sc,
                    dispatcher=pipeline.MockDispatcher(responses),
                    normalizer=pipeline.MockNormalizer(norms),
                    output_root=Path(td),
                    repeats=1,
                )

    def test_control_locus_clean_is_not_noise(self):
        seeds = five_seeds("logic")
        sc = scenario_from(seeds, control_loci=controls_for("logic", 5))  # >= T=5 so guard doesn't fire
        raw_lines = []
        norms = {}
        for i in range(5):
            raw = f"FINDING seed {i}"
            raw_lines.append(raw)
            norms[raw] = [{
                "class": "logic",
                "location": {"file": "a.py", "line": 10 + i, "symbol": f"seed_{i}"},
            }]
        with tempfile.TemporaryDirectory() as td:
            result = pipeline.run_floor(sc, dispatcher=pipeline.MockDispatcher({
                                            "review:codex:0": "\n".join(raw_lines)}),
                                        normalizer=pipeline.MockNormalizer(norms),
                                        output_root=Path(td), repeats=1)
            self.assertEqual(result.verdicts["codex"]["logic"], PASS)
            events = [json.loads(line) for line in Path(result.events_path).read_text().splitlines()]
            controls = [e for e in events if e.get("event") == "matcher_decision"
                        and e.get("locus") == "control"]
            self.assertEqual([e["outcome"] for e in controls], ["clean"] * 5)

    def test_missing_control_loci_leaves_noise_unmeasured(self):
        seeds = five_seeds("logic")
        sc = scenario_from(seeds, control_loci=[])
        raw_lines = []
        norms = {}
        for i in range(5):
            raw = f"FINDING seed {i}"
            raw_lines.append(raw)
            norms[raw] = [{
                "class": "logic",
                "location": {"file": "a.py", "line": 10 + i, "symbol": f"seed_{i}"},
            }]
        with tempfile.TemporaryDirectory() as td:
            result = pipeline.run_floor(sc, dispatcher=pipeline.MockDispatcher({
                                            "review:codex:0": "\n".join(raw_lines)}),
                                        normalizer=pipeline.MockNormalizer(norms),
                                        output_root=Path(td), repeats=1)
            self.assertEqual(result.verdicts["codex"]["logic"], UNKNOWN)
            details = json.loads(Path(result.detail_path).read_text())
            self.assertEqual(details["oracle"]["codex"]["logic"]["noise_n"], 0)

    def test_control_locus_flagged_counts_as_noise(self):
        seeds = five_seeds("logic")
        controls = controls_for("logic", 1)
        sc = scenario_from(seeds, control_loci=controls)
        raw_lines = []
        norms = {}
        for i in range(5):
            raw = f"FINDING seed {i}"
            raw_lines.append(raw)
            norms[raw] = [{
                "class": "logic",
                "location": {"file": "a.py", "line": 10 + i, "symbol": f"seed_{i}"},
            }]
        raw = "FINDING clean control"
        raw_lines.append(raw)
        norms[raw] = [{
            "class": "logic",
            "location": {"file": "clean.py", "line": 100, "symbol": "clean_0"},
        }]
        with tempfile.TemporaryDirectory() as td:
            result = pipeline.run_floor(sc, dispatcher=pipeline.MockDispatcher({
                                            "review:codex:0": "\n".join(raw_lines)}),
                                        normalizer=pipeline.MockNormalizer(norms),
                                        output_root=Path(td), repeats=1)
            self.assertEqual(result.verdicts["codex"]["logic"], UNKNOWN)
            events = [json.loads(line) for line in Path(result.events_path).read_text().splitlines()]
            self.assertEqual(
                [e["outcome"] for e in events if e.get("event") == "matcher_decision"
                 and e.get("locus") == "control"],
                ["flagged"],
            )

    def test_dispatch_error_records_incomplete_unknown_no_matcher_rows(self):
        class FailingDispatcher:
            tasks = []
            def dispatch(self, task):
                self.tasks.append(task)
                raise pipeline.DispatchError("boom")

        seeds = five_seeds("logic")
        sc = scenario_from(seeds, control_loci=controls_for("logic", 2))
        dispatcher = FailingDispatcher()
        with tempfile.TemporaryDirectory() as td:
            result = pipeline.run_floor(sc, dispatcher=dispatcher,
                                        normalizer=pipeline.MockNormalizer({}),
                                        output_root=Path(td), repeats=1)
            self.assertEqual(result.verdicts["codex"]["logic"], UNKNOWN)
            events = [json.loads(line) for line in Path(result.events_path).read_text().splitlines()]
            self.assertIn("dispatch_error", [e["event"] for e in events])
            self.assertIn("incomplete_repeat", [e["event"] for e in events])
            self.assertEqual(
                [e for e in events if e.get("event") == "matcher_decision"
                 and e.get("task", {}).get("repeat") == 0],
                [],
            )
            self.assertIn("infra_incomplete(codex, kind=review, repeat=0)", result.report_text)


class TestIncompleteRepeat(unittest.TestCase):
    def _confined_dir(self, td: str, body: str) -> Path:
        d = Path(td) / "conf"
        d.mkdir()
        script = d / "confined-review.sh"
        script.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body)
        script.chmod(0o755)
        return d

    def _scenario(self, cls="logic"):
        return scenario_from(five_seeds(cls), control_loci=controls_for(cls, 5))

    def test_confined_timeout_renders_unknown_not_fail(self):
        with tempfile.TemporaryDirectory() as td:
            conf = self._confined_dir(td, "sleep 2\n")
            dispatcher = pipeline.ContainerDispatcher("scenario.json", str(conf), seats=("codex",))
            old_timeout = os.environ.get("ARB_EVAL_DISPATCH_TIMEOUT")
            os.environ["ARB_EVAL_DISPATCH_TIMEOUT"] = "1"
            try:
                with self.assertRaises(pipeline.DispatchError) as cm:
                    dispatcher.dispatch({
                        "seat": "codex", "repo": ".", "base": "base", "head": "head",
                        "task_id": "direct-timeout",
                    })
                self.assertEqual(cm.exception.kind, "timeout")

                sc = self._scenario()
                sc.subject["repo"] = td
                result = pipeline.run_floor(
                    sc,
                    dispatcher=pipeline.ContainerDispatcher("scenario.json", str(conf), seats=("codex",)),
                    normalizer=pipeline.MockNormalizer({}),
                    output_root=Path(td),
                    repeats=1,
                )
            finally:
                if old_timeout is None:
                    os.environ.pop("ARB_EVAL_DISPATCH_TIMEOUT", None)
                else:
                    os.environ["ARB_EVAL_DISPATCH_TIMEOUT"] = old_timeout

            self.assertEqual(result.verdicts["codex"]["logic"], UNKNOWN)
            self.assertIn("infra_incomplete(codex, kind=timeout, repeat=0)", result.report_text)
            events = [json.loads(line) for line in Path(result.events_path).read_text().splitlines()]
            self.assertIn("dispatch_error", [e["event"] for e in events])
            self.assertIn("incomplete_repeat", [e["event"] for e in events])
            self.assertEqual([e for e in events if e.get("event") == "matcher_decision"], [])
            inc = next(e for e in events if e["event"] == "incomplete_repeat")
            self.assertEqual(inc["kind"], "timeout")
            self.assertEqual(inc["classes"], ["logic"])

    def test_canary_failure_parks(self):
        with tempfile.TemporaryDirectory() as td:
            conf = self._confined_dir(td, "exit 43\n")
            dispatcher = pipeline.ContainerDispatcher("scenario.json", str(conf), seats=("codex",))
            with self.assertRaises(pipeline.DispatchError) as cm:
                dispatcher.dispatch({
                    "seat": "codex", "repo": ".", "base": "base", "head": "head",
                    "task_id": "direct-canary",
                })
            self.assertEqual(cm.exception.kind, "canary")
            sc = self._scenario()
            sc.subject["repo"] = td
            with self.assertRaises(pipeline.Parked):
                pipeline.run_floor(
                    sc,
                    dispatcher=dispatcher,
                    normalizer=pipeline.MockNormalizer({}),
                    output_root=Path(td),
                    repeats=1,
                )

    def test_confined_review_nonce_fence_uses_engine_version_not_placeholder(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixture = root / "fixture"
            fixture.mkdir()
            scenario = root / "scenario.json"
            scenario.write_text(json.dumps({
                "seeded_defects": [{"id": "D0"}],
                "control_loci": [],
            }))
            bin_dir = root / "bin"
            bin_dir.mkdir()
            fake_docker = bin_dir / "docker"
            fake_docker.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                "count_file=\"$TMPDIR/fake-docker-count\"\n"
                "count=0\n"
                "[ -f \"$count_file\" ] && count=$(cat \"$count_file\")\n"
                "count=$((count + 1))\n"
                "printf '%s' \"$count\" > \"$count_file\"\n"
                "if [ \"$count\" -eq 1 ]; then exit 0; fi\n"
                "printf 'logic | a.py:1 | fake review\\n'\n"
                "printf 'ARB_PROV_%s{\"codex\":\"codex-cli 1.2.3\"}</ARB_PROV_%s>\\n' \"$ARB_PROV_NONCE\" \"$ARB_PROV_NONCE\"\n"
            )
            fake_docker.chmod(0o755)
            env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", TMPDIR=td,
                       ARB_PROV_NONCE="nonce123",
                       OPENAI_API_KEY="test-key")  # script refuses to run without an operator-supplied key
            script = Path(__file__).resolve().parents[1] / "confinement" / "confined-review.sh"
            proc = subprocess.run(
                [str(script), "codex", str(fixture), "base", "head", str(scenario)],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, env=env,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('"codex":"codex-cli 1.2.3"', proc.stdout)
        self.assertNotIn("cli-version-only", proc.stdout)

    def test_container_dispatcher_keeps_engine_version_out_of_model_reported(self):
        with tempfile.TemporaryDirectory() as td:
            conf = self._confined_dir(
                td,
                "printf 'logic | a.py:1 | fake review\\n'\n"
                "printf 'ARB_PROV_%s{\"codex\":\"codex-cli 1.2.3\"}</ARB_PROV_%s>\\n' \"$ARB_PROV_NONCE\" \"$ARB_PROV_NONCE\"\n",
            )
            dispatcher = pipeline.ContainerDispatcher("scenario.json", str(conf), seats=("codex",))
            reply = dispatcher.dispatch({
                "seat": "codex", "repo": ".", "base": "base", "head": "head",
                "task_id": "direct-provenance", "provenance_nonce": "nonce123",
            })
        self.assertIn("fake review", reply)
        self.assertEqual(dispatcher.last_engine_versions, {"codex": "codex-cli 1.2.3"})
        self.assertIsNone(dispatcher.last_model_reported)

    def test_run_floor_confined_command_records_model_wired_to_dispatch(self):
        with tempfile.TemporaryDirectory() as td:
            conf = self._confined_dir(
                td,
                "test \"${ARB_EVAL_MODEL:-}\" = \"model-a\"\n"
                "printf 'no issues\\n'\n"
                "printf 'ARB_PROV_%s{\"codex\":\"codex-cli 1.2.3\"}</ARB_PROV_%s>\\n' \"$ARB_PROV_NONCE\" \"$ARB_PROV_NONCE\"\n",
            )
            sc = scenario_from(
                five_seeds("logic"),
                panel=[{"seat": "codex", "model": "model-a", "harness": "confined"}],
                control_loci=controls_for("logic", 5),
            )
            sc.subject["repo"] = td
            result = pipeline.run_floor(
                sc,
                dispatcher=pipeline.ContainerDispatcher("scenario.json", str(conf), seats=("codex",)),
                normalizer=pipeline.MockNormalizer({}),
                output_root=Path(td),
                repeats=1,
            )
            detail = json.loads(Path(result.detail_path).read_text())
        self.assertNotIn("infra_incomplete", result.report_text)
        self.assertIn("model=model-a", detail["provenance"]["model"]["codex"]["confined_command"])

    def test_delete_incomplete_exclusion_would_fake_fail(self):
        class MixedDispatcher:
            tasks = []
            def dispatch(self, task):
                self.tasks.append(task)
                if task["repeat"] == 1:
                    raise pipeline.DispatchError("slow", kind="timeout")
                return "\n".join(f"FINDING clean {i}" for i in range(5))

        sc = self._scenario()
        normalizer = pipeline.MockNormalizer({
            f"FINDING clean {i}": [{
                "class": "logic",
                "location": {"file": "clean.py", "line": 100 + i, "symbol": f"clean_{i}"},
            }]
            for i in range(5)
        })
        with tempfile.TemporaryDirectory() as td:
            result = pipeline.run_floor(
                sc,
                dispatcher=MixedDispatcher(),
                normalizer=normalizer,
                output_root=Path(td),
                repeats=2,
            )
            self.assertEqual(result.verdicts["codex"]["logic"], UNKNOWN)

        original = pipeline._incomplete_seats
        pipeline._incomplete_seats = lambda path: {}
        try:
            with tempfile.TemporaryDirectory() as td:
                result = pipeline.run_floor(
                    sc,
                    dispatcher=MixedDispatcher(),
                    normalizer=normalizer,
                    output_root=Path(td),
                    repeats=2,
                )
                self.assertNotEqual(result.verdicts["codex"]["logic"], UNKNOWN)
        finally:
            pipeline._incomplete_seats = original


class TestScenarioPortability(unittest.TestCase):
    def test_relative_repo_resolves_against_scenario_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "repo").mkdir()
            scenario_path = root / "scenario.json"
            scenario_path.write_text(json.dumps({
                "id": "portable",
                "subject": {"repo": "repo", "base": "base", "head": "head"},
                "seeded_defects": [{
                    "id": "D0", "class": "logic", "legible": True,
                    "location": {"file": "a.py", "line": 1},
                    "description": "x",
                }],
                "panel": [{"seat": "codex", "model": "gpt", "harness": "mock"}],
            }))
            sc = schema.load(scenario_path)
            self.assertEqual(sc.subject["repo"], str((root / "repo").resolve()))
            self.assertEqual(sc.subject["repo_declared"], "repo")

    def test_missing_repo_or_sha_raises_scenario_error(self):
        sc = scenario_from(five_seeds("logic"))
        sc.subject["repo"] = "/missing/repo"
        with self.assertRaisesRegex(schema.ScenarioError, "subject repo/SHA unresolved"):
            schema.validate_subject(sc)

        import subprocess
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "x@y.z"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "x"], check=True)
            (repo / "a.py").write_text("x = 1\n")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
            sc = scenario_from(five_seeds("logic"))
            sc.subject.update({"repo": str(repo), "base": "nope", "head": "HEAD"})
            with self.assertRaisesRegex(schema.ScenarioError, "subject repo/SHA unresolved"):
                schema.validate_subject(sc)

    def test_duplicate_seed_locations_are_instance_level(self):
        seeds = [{
            "id": f"D{i}", "class": "logic", "legible": True,
            "location": {"file": "a.py", "line": 10, "symbol": "same"},
            "description": "duplicate",
        } for i in range(5)]
        sc = scenario_from(seeds)
        self.assertEqual(sc.instances_per_class()["logic"], 1)
        self.assertFalse(sc.class_level_ok(5)["logic"])

    def test_duplicate_control_loci_are_rejected(self):
        seeds = five_seeds("logic")
        controls = [
            {"id": "C1", "class": "logic",
             "location": {"file": "clean.py", "line": 10, "symbol": "same"},
             "description": "clean"},
            {"id": "C2", "class": "logic",
             "location": {"file": "clean.py", "line": 99, "symbol": "same"},
             "description": "same symbol is same locus"},
        ]
        with self.assertRaisesRegex(schema.ScenarioError, "duplicates"):
            scenario_from(seeds, control_loci=controls)

    def test_finding_at_seed_and_control_overlap_counts_once_as_detection(self):
        # D3 gate (codex/cold-opus): one finding co-located with BOTH a seed and a nearby control
        # (same class+file, within matcher_window) must count as a detection (seed precedence) and
        # NEVER also as noise. Otherwise a genuine catch is double-counted as a false positive.
        seeds = [{"id": "D0", "class": "logic", "legible": True,
                  "location": {"file": "a.py", "line": 10, "symbol": "seed_0"},
                  "description": "seeded"}]
        controls = [{"id": "C0", "class": "logic",
                     "location": {"file": "a.py", "line": 15},  # 5 lines from the seed (< window)
                     "description": "plausible but clean"}]
        sc = scenario_from(seeds, control_loci=controls)
        raw = "FINDING near seed and control"
        norms = {raw: [{"class": "logic", "location": {"file": "a.py", "line": 12}}]}
        with tempfile.TemporaryDirectory() as td:
            result = pipeline.run_floor(sc, dispatcher=pipeline.MockDispatcher(
                                            {"review:codex:0": raw}),
                                        normalizer=pipeline.MockNormalizer(norms),
                                        output_root=Path(td), repeats=1)
            events = [json.loads(line) for line in Path(result.events_path).read_text().splitlines()]
            seed_dec = [e for e in events if e.get("event") == "matcher_decision"
                        and e.get("locus") == "seed"]
            ctrl_dec = [e for e in events if e.get("event") == "matcher_decision"
                        and e.get("locus") == "control"]
            self.assertEqual([e["outcome"] for e in seed_dec], ["detected"])
            self.assertEqual([e["outcome"] for e in ctrl_dec], ["clean"])  # consumed by the seed

    def test_one_finding_flags_at_most_one_control(self):
        # D3 gate (cold-opus P2-1): a single finding within window of TWO control loci must produce
        # exactly ONE flag, not two — clustered controls must not over-count noise.
        seeds = five_seeds("logic")
        controls = [
            {"id": "C0", "class": "logic", "location": {"file": "clean.py", "line": 100},
             "description": "clean"},
            {"id": "C1", "class": "logic", "location": {"file": "clean.py", "line": 105},
             "description": "clean"},
        ]
        sc = scenario_from(seeds, control_loci=controls)
        raw_lines, norms = [], {}
        for i in range(5):
            r = f"FINDING seed {i}"
            raw_lines.append(r)
            norms[r] = [{"class": "logic",
                         "location": {"file": "a.py", "line": 10 + i, "symbol": f"seed_{i}"}}]
        r = "FINDING near both controls"
        raw_lines.append(r)
        norms[r] = [{"class": "logic", "location": {"file": "clean.py", "line": 102}}]
        with tempfile.TemporaryDirectory() as td:
            result = pipeline.run_floor(sc, dispatcher=pipeline.MockDispatcher(
                                            {"review:codex:0": "\n".join(raw_lines)}),
                                        normalizer=pipeline.MockNormalizer(norms),
                                        output_root=Path(td), repeats=1)
            events = [json.loads(line) for line in Path(result.events_path).read_text().splitlines()]
            outcomes = sorted(e["outcome"] for e in events
                              if e.get("event") == "matcher_decision" and e.get("locus") == "control")
            self.assertEqual(outcomes, ["clean", "flagged"])  # one flag, not two

    def test_correlated_controls_collapse_to_one_cluster_for_noise(self):
        # P-3 effective-N (measurement-principles P1 #5): 3 controls sharing a why-clean cluster are
        # ONE independent sample. Even if the seat flags all 3, noise must be 1 flagged / 1 cluster
        # (per repeat), not 3/3 — correlated controls buy no independent confidence.
        seeds = five_seeds("logic")
        controls = [
            {"id": f"C{i}", "class": "logic", "cluster": "logs-a-scalar",
             "location": {"file": "clean.py", "line": 100 + i, "symbol": f"clean_{i}"},
             "description": "clean, same idiom"} for i in range(3)
        ]
        sc = scenario_from(seeds, control_loci=controls)
        self.assertEqual(sc.control_cluster_count_per_class()["logic"], 1)
        raw_lines, norms = [], {}
        for i in range(5):
            r = f"FINDING seed {i}"
            raw_lines.append(r)
            norms[r] = [{"class": "logic", "location": {"file": "a.py", "line": 10 + i, "symbol": f"seed_{i}"}}]
        for i in range(3):
            r = f"FINDING control {i}"
            raw_lines.append(r)
            norms[r] = [{"class": "logic", "location": {"file": "clean.py", "line": 100 + i, "symbol": f"clean_{i}"}}]
        with tempfile.TemporaryDirectory() as td:
            result = pipeline.run_floor(sc, dispatcher=pipeline.MockDispatcher(
                                            {"review:codex:0": "\n".join(raw_lines)}),
                                        normalizer=pipeline.MockNormalizer(norms),
                                        output_root=Path(td), repeats=1)
            d = json.loads(Path(result.detail_path).read_text())["oracle"]["codex"]["logic"]
            self.assertEqual(d["noise_n"], 1)  # one cluster, not three controls
            self.assertEqual(d["noise_k"], 1)  # cluster flagged (>=1 of its controls)

    def test_explicit_cluster_does_not_collide_with_singleton_id(self):
        # P-3 effective-N gate (codex/cold-opus P2): an explicit cluster tag equal to another control's
        # id must NOT merge them (that silently under-counts noise — anti-conservative). Singleton keys
        # are namespaced, so C1's cluster="C0" stays distinct from untagged C0's singleton.
        seeds = five_seeds("logic")
        controls = [
            {"id": "C0", "class": "logic",
             "location": {"file": "clean.py", "line": 100, "symbol": "k0"}, "description": "clean"},
            {"id": "C1", "class": "logic", "cluster": "C0",  # collides with C0's id if not namespaced
             "location": {"file": "clean.py", "line": 110, "symbol": "k1"}, "description": "clean"},
        ]
        sc = scenario_from(seeds, control_loci=controls)
        self.assertEqual(sc.control_cluster_count_per_class()["logic"], 2)  # NOT merged to 1

    def test_repeats_are_pooled_no_hard_gate_clean_seat_passes_at_small_n(self):
        # P-3 decision panel B (unanimous): NO hard under-T gate. Repeats are POOLED (n = #clusters,
        # not clusters x repeats), so a clean seat with real Wilson separation PASSes even at small n,
        # and repeats can't re-inflate the count. Here: 5 seed clusters + 8 control clusters (< full
        # power), clean, repeats=2 -> caught 5/5 (lo~0.57) vs noise 0/8 (hi~0.37) -> honest PASS.
        seeds = five_seeds("logic")
        sc = scenario_from(seeds, control_loci=controls_for("logic", 8))
        raw_lines, norms = [], {}
        for i in range(5):
            r = f"FINDING seed {i}"
            raw_lines.append(r)
            norms[r] = [{"class": "logic", "location": {"file": "a.py", "line": 10 + i, "symbol": f"seed_{i}"}}]
        with tempfile.TemporaryDirectory() as td:
            result = pipeline.run_floor(sc, dispatcher=pipeline.MockDispatcher(
                                            {"review:codex:0": "\n".join(raw_lines),
                                             "review:codex:1": "\n".join(raw_lines)}),
                                        normalizer=pipeline.MockNormalizer(norms),
                                        output_root=Path(td), repeats=2)
            self.assertEqual(result.verdicts["codex"]["logic"], PASS)
            o = json.loads(Path(result.detail_path).read_text())["oracle"]["codex"]["logic"]
            self.assertEqual(o["caught_n"], 5)   # pooled: 5 clusters, NOT 5 x 2 repeats
            self.assertEqual(o["noise_n"], 8)    # pooled: 8 clusters, NOT 8 x 2 repeats
            self.assertNotIn("under_t_guard", o)  # the hard gate is gone

    def test_correlated_seeds_collapse_for_caught_all_rule(self):
        # P-3 Choice-1 (seed-symmetry): 2 seeds of one mechanism are ONE caught sample, and a cluster
        # counts as detected only if ALL its member seeds are detected (anti-over-claim). Catching 1 of
        # 2 same-mechanism seeds -> that cluster is a miss.
        seeds = [
            {"id": "D0", "class": "logic", "cluster": "mech-A", "legible": True,
             "location": {"file": "a.py", "line": 10, "symbol": "s0"}, "description": "x"},
            {"id": "D1", "class": "logic", "cluster": "mech-A", "legible": True,
             "location": {"file": "a.py", "line": 100, "symbol": "s1"}, "description": "x"},  # far from D0 (> window)
            {"id": "D2", "class": "logic", "legible": True,
             "location": {"file": "b.py", "line": 10, "symbol": "s2"}, "description": "x"},
            {"id": "D3", "class": "logic", "legible": True,
             "location": {"file": "b.py", "line": 20, "symbol": "s3"}, "description": "x"},
            {"id": "D4", "class": "logic", "legible": True,
             "location": {"file": "b.py", "line": 30, "symbol": "s4"}, "description": "x"},
        ]
        sc = scenario_from(seeds, control_loci=controls_for("logic", 5))
        self.assertEqual(sc.seed_cluster_count_per_class()["logic"], 4)  # mech-A + D2 + D3 + D4
        # seat detects D0 (NOT D1), D2, D3, D4 -> mech-A cluster missed (1 of 2), other 3 caught
        norms, lines = {}, []
        for sid, f, ln, sym in [("D0", "a.py", 10, "s0"), ("D2", "b.py", 10, "s2"),
                                ("D3", "b.py", 20, "s3"), ("D4", "b.py", 30, "s4")]:
            r = f"FIND {sid}"
            lines.append(r)
            norms[r] = [{"class": "logic", "location": {"file": f, "line": ln, "symbol": sym}}]
        with tempfile.TemporaryDirectory() as td:
            result = pipeline.run_floor(sc, dispatcher=pipeline.MockDispatcher(
                                            {"review:codex:0": "\n".join(lines)}),
                                        normalizer=pipeline.MockNormalizer(norms),
                                        output_root=Path(td), repeats=1)
            o = json.loads(Path(result.detail_path).read_text())["oracle"]["codex"]["logic"]
            self.assertEqual(o["caught_n"], 4)   # 4 seed clusters
            self.assertEqual(o["caught_k"], 3)   # mech-A missed (D1 not caught); D2/D3/D4 caught

    def test_one_mechanism_many_locations_cannot_class_pass(self):
        # P-3 over-claim regression (codex constructed it). 5 seed LOCATIONS that are ONE mechanism
        # reach CLASS-LEVEL (I_min on locations) but are 1 independent seed cluster. Under B (repeats
        # POOLED, no gate) this stays UNKNOWN via the wide 1-cluster Wilson CI — NOT by a hard rail,
        # and NOT inflated to PASS by repeats. This is the canonical over-claim, closed structurally.
        seeds = [{"id": f"D{i}", "class": "logic", "cluster": "mech-A", "legible": True,
                  "location": {"file": f"f{i}.py", "line": 10, "symbol": "s"}, "description": "x"}
                 for i in range(5)]
        sc = scenario_from(seeds, control_loci=controls_for("logic", 8))
        self.assertEqual(sc.seed_cluster_count_per_class()["logic"], 1)       # one mechanism
        self.assertFalse(sc.class_level_ok(5)["logic"])  # 1 mechanism < I_min=5 -> not class-level
        norms, lines = {}, []
        for i in range(5):
            r = f"FIND D{i}"
            lines.append(r)
            norms[r] = [{"class": "logic", "location": {"file": f"f{i}.py", "line": 10, "symbol": "s"}}]
        with tempfile.TemporaryDirectory() as td:
            result = pipeline.run_floor(sc, dispatcher=pipeline.MockDispatcher(
                                            {"review:codex:0": "\n".join(lines),
                                             "review:codex:1": "\n".join(lines)}),
                                        normalizer=pipeline.MockNormalizer(norms),
                                        output_root=Path(td), repeats=2)
            # one mechanism is INSTANCE-LEVEL: no class-level PASS/FAIL emitted at all (the eligibility
            # gate closes it before the CI even runs — can't certify a CLASS from one mechanism)
            self.assertEqual(result.claim_levels["logic"], "INSTANCE-LEVEL")
            self.assertNotIn("logic", result.verdicts["codex"])
            o = json.loads(Path(result.detail_path).read_text())["oracle"]["codex"]["logic"]
            self.assertEqual(o["caught_n"], 1)   # 1 seed cluster, pooled (NOT 1 x 2 repeats = 2)

    def test_bridge_path_is_never_confined_and_env_var_cannot_spoof_it(self):
        # The bridge path CANNOT read-confine, so it always refuses — and crucially, setting
        # ARB_SEAT_CONFINED_ROOT does NOT grant confinement (a settable string is spoofable; the real
        # proof is the ContainerDispatcher canary). Only the explicit unguaranteed flag escapes.
        import os
        d = pipeline.BridgeDispatcher()
        repo = "/tmp/arb-fixture-x"
        for k in ("ARB_SEAT_CONFINED_ROOT", "ARB_ALLOW_UNCONFINED_FLOOR"):
            os.environ.pop(k, None)
        try:
            with self.assertRaises(pipeline.Parked):            # no env -> refuse
                d.assert_read_confined(repo)
            os.environ["ARB_SEAT_CONFINED_ROOT"] = repo
            with self.assertRaises(pipeline.Parked):            # env var alone must NOT spoof confinement
                d.assert_read_confined(repo)
            os.environ.pop("ARB_SEAT_CONFINED_ROOT")
            os.environ["ARB_ALLOW_UNCONFINED_FLOOR"] = "1"
            d.assert_read_confined(repo)                        # explicit unguaranteed override -> ok
        finally:
            for k in ("ARB_SEAT_CONFINED_ROOT", "ARB_ALLOW_UNCONFINED_FLOOR"):
                os.environ.pop(k, None)

    def test_answer_key_leak_in_fixture_is_parked(self):
        # In-band-leak pre-flight: a fixture that NAMES its own seed/control IDs (in a tracked file or a
        # commit message) is the answer-key inside what the seat reads -> Park. Clean / non-git -> ok.
        import subprocess
        sc = scenario_from(five_seeds("logic"), control_loci=controls_for("logic", 3))  # seed ids D0..D4

        def make_repo(td, file_text, commit_msg):
            subprocess.run(["git", "init", "-q", td], check=True)
            subprocess.run(["git", "-C", td, "config", "user.email", "x@y.z"], check=True)
            subprocess.run(["git", "-C", td, "config", "user.name", "x"], check=True)
            (Path(td) / "a.py").write_text(file_text)
            subprocess.run(["git", "-C", td, "add", "."], check=True)
            subprocess.run(["git", "-C", td, "commit", "-q", "-m", commit_msg], check=True)

        with tempfile.TemporaryDirectory() as td:           # clean fixture -> ok
            make_repo(td, "value = 1\n", "feat: a thing")
            pipeline._assert_no_answer_key_in_fixture(sc, td)
        with tempfile.TemporaryDirectory() as td:           # seed id in a tracked file -> Park
            make_repo(td, "# D0 is seeded here\nvalue = 1\n", "feat: a thing")
            with self.assertRaises(pipeline.Parked):
                pipeline._assert_no_answer_key_in_fixture(sc, td)
        with tempfile.TemporaryDirectory() as td:           # seed id in a commit message -> Park
            make_repo(td, "value = 1\n", "seed D0: plant the leak")
            with self.assertRaises(pipeline.Parked):
                pipeline._assert_no_answer_key_in_fixture(sc, td)
        with tempfile.TemporaryDirectory() as td:           # seed id in an UNTRACKED file -> Park
            make_repo(td, "value = 1\n", "feat: a thing")    # (bind-mounted + seat-visible; agy P1)
            (Path(td) / "LEAK.txt").write_text("D0 is the seed\n")  # never git-added
            with self.assertRaises(pipeline.Parked):
                pipeline._assert_no_answer_key_in_fixture(sc, td)
        with tempfile.TemporaryDirectory() as td:           # seed id only in git HISTORY -> Park
            make_repo(td, "# D0 here\n", "feat: a thing")    # commit with the id...
            (Path(td) / "a.py").write_text("value = 1\n")    # ...then scrub it from the worktree
            subprocess.run(["git", "-C", td, "commit", "-aqm", "scrub"], check=True)
            with self.assertRaises(pipeline.Parked):         # log -p still surfaces it (cold-opus P2)
                pipeline._assert_no_answer_key_in_fixture(sc, td)
        with tempfile.TemporaryDirectory() as td:           # non-git (mock '.') -> skip, ok
            pipeline._assert_no_answer_key_in_fixture(sc, td)

    def test_control_locus_overlapping_seed_is_rejected(self):
        # D3 gate (codex #2): a control must be a CLEAN instance — one at a seed's exact
        # (class, file, symbol|line) is incoherent (a real defect masquerading as a clean locus).
        seeds = five_seeds("logic")  # seed_0 @ a.py:10 symbol seed_0
        controls = [{"id": "C0", "class": "logic",
                     "location": {"file": "a.py", "line": 10, "symbol": "seed_0"},
                     "description": "accidentally on a seed"}]
        with self.assertRaisesRegex(schema.ScenarioError, "seed"):
            scenario_from(seeds, control_loci=controls)

    def test_orphan_control_class_warns(self):
        # D3 gate (cold-opus P2-2): a control locus in a class with no seed is silently dropped from
        # scoring — the run must surface it, not swallow it.
        seeds = five_seeds("logic")
        controls = controls_for("logic", 2) + [
            {"id": "ORPH", "class": "cors", "location": {"file": "clean.py", "line": 200},
             "description": "control with no seed of this class"}]
        sc = scenario_from(seeds, control_loci=controls)
        raw_lines, norms = [], {}
        for i in range(5):
            r = f"FINDING seed {i}"
            raw_lines.append(r)
            norms[r] = [{"class": "logic",
                         "location": {"file": "a.py", "line": 10 + i, "symbol": f"seed_{i}"}}]
        with tempfile.TemporaryDirectory() as td:
            result = pipeline.run_floor(sc, dispatcher=pipeline.MockDispatcher(
                                            {"review:codex:0": "\n".join(raw_lines)}),
                                        normalizer=pipeline.MockNormalizer(norms),
                                        output_root=Path(td), repeats=1)
            self.assertIn("cors", result.report_text)
            self.assertIn("orphan", result.report_text.lower())


class TestAnthropicNormalizer(unittest.TestCase):
    """Deterministic checks for the off-quorum normalizer (P-1). The LIVE M3 call is verified
    manually (needs network+key); these lock the failure-handling + guard behavior."""

    def _norm_no_network(self):
        # build without hitting the network: stub the anthropic client
        n = pipeline.AnthropicNormalizer.__new__(pipeline.AnthropicNormalizer)
        n.model = "MiniMax-M3"; n.temperature = 0.0; n.max_tokens = 512
        n.taxonomy = list(schema.TAXONOMY); n._sys = "x"
        return n

    def test_collision_guard_refuses_quorum_model(self):
        for m in ("opus", "codex", "gemini", "agy-print"):
            with self.assertRaises(pipeline.Parked):
                pipeline.AnthropicNormalizer(model=m)

    def test_fail_loud_on_unparseable_never_drops(self):
        n = self._norm_no_network()
        class B: type = "text"; text = "Sure, here's prose with no JSON at all."
        class R: content = [B()]
        n.client = type("C", (), {"messages": type("M", (), {"create": staticmethod(lambda **k: R())})()})()
        out = n.normalize("a finding")
        self.assertEqual(len(out), 1)                       # never []
        self.assertEqual(out[0]["class"], "unknown")        # fail loud
        self.assertIn("normalize_error", out[0])            # error carried, visible in NDJSON

    def test_extract_json_tolerates_fences(self):
        self.assertEqual(pipeline._extract_json('```json\n{"class":"cors"}\n```')["class"], "cors")
        self.assertEqual(pipeline._extract_json('{"class":"tls-transport"}')["class"], "tls-transport")

    def test_unknown_routes_to_matcher_ambiguous(self):
        seed = schema.Seed(id="D1", cls="secrets-in-logs", location={"file": "a.py", "line": 1},
                           description="x")
        self.assertEqual(pipeline.match_finding({"class": "unknown", "location": {}}, seed).outcome,
                         "matcher-ambiguous")


if __name__ == "__main__":
    unittest.main()
