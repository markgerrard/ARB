import unittest
import tempfile
import json
from pathlib import Path
from unittest import mock

import yaml

from authorbench import cli, run


class TestCLI(unittest.TestCase):
    def test_freeze_accepts_artefact_commit_and_base_sha(self):
        with mock.patch("authorbench.cli.bundle.freeze") as freeze:
            self.assertEqual(cli.main(["freeze", "src", "--stage", "design", "--corpus-version", "v1.0", "--base-sha", "abc123"]), 0)
            freeze.assert_called_with("src", corpus_version="v1.0", base_sha="abc123", artefact_commit=None)

        with mock.patch("authorbench.cli.bundle.freeze") as freeze:
            self.assertEqual(cli.main(["freeze", "src", "--stage", "spec", "--corpus-version", "v1.0", "--artefact-commit", "def456"]), 0)
            freeze.assert_called_with("src", corpus_version="v1.0", base_sha=None, artefact_commit="def456")

    def test_default_judge_panel_excludes_agy_while_spike_locked(self):
        with mock.patch("authorbench.cli.run.judge_phase") as judge_phase:
            cli.main(["judge", "--run", "run-1"])
            self.assertEqual(judge_phase.call_args.kwargs["panel"], ["codex", "pi-glm", "m3"])

        with self.assertRaises(SystemExit):
            cli.main(["judge", "--run", "run-1", "--panel", "codex,agy,pi-glm"])

    def test_author_refuses_burned_lineage_without_override(self):
        with self.assertRaises(SystemExit):
            cli.main(
                [
                    "author",
                    "--candidate",
                    "codex",
                    "--brief",
                    "AB-D1",
                    "--seat-lineage",
                    "codex-lineage",
                    "--prior-exposure",
                    "possible(public-mirror)",
                ]
            )

    def test_run_pipeline_calls_each_stage_in_order(self):
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bdir = self._bundle(root)
            with (
                mock.patch("authorbench.run.freeze_check", side_effect=lambda *a, **k: calls.append("freeze-check")),
                mock.patch("authorbench.run.author_turn", side_effect=lambda *a, **k: calls.append("author")),
                mock.patch("authorbench.run.judge_phase", side_effect=lambda *a, **k: calls.append("judge")),
                mock.patch("authorbench.run.score_run", side_effect=lambda *a, **k: calls.append("score")),
                mock.patch("authorbench.run.report_run", side_effect=lambda *a, **k: calls.append("report")),
            ):
                run.run_bench("codex", "v1.0", baseline="baseline", panel=["codex", "pi-glm", "m3"], run_dir=root / "run", bundles=[bdir])
        self.assertEqual(calls, ["freeze-check", "author", "judge", "score", "report"])

    def test_default_live_runners_thread_role_env_to_confinement_script(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bdir = self._bundle(root)
            run_dir = root / "run-1"
            run_dir.mkdir()
            profile = run.jail.author_profile(run.bundle.load(bdir), "fixture")

            with mock.patch("authorbench.run.subprocess.run") as subprocess_run:
                subprocess_run.return_value = mock.Mock(stdout="draft\n")
                self.assertEqual(
                    run._default_author_runner(role="author", prompt="prompt", profile=profile, run_dir=run_dir),
                    "draft\n",
                )
            call = subprocess_run.call_args
            self.assertEqual(call.args[0], ["tools/authorbench/confinement/confined-authorbench.sh", "author", "run", "prompt"])
            author_env = call.kwargs["env"]
            self.assertEqual(author_env["AUTHORBENCH_WORKSPACE"], str(run_dir / "export"))
            self.assertEqual(author_env["AUTHORBENCH_AUTHOR_VISIBLE"], str(run_dir / "author-visible" / "AB-X"))

            draft = run_dir / "staging" / "codex" / "AB-X" / "draft.md"
            draft.parent.mkdir(parents=True)
            draft.write_text("Claim cites src/thing.py:1.\n", encoding="utf-8")
            (run_dir / "export").mkdir(exist_ok=True)
            fact_key = run_dir / "bundles" / "AB-X" / "fact_key.yaml"
            fact_key.parent.mkdir(parents=True)
            fact_key.write_text((bdir / "fact_key.yaml").read_text(encoding="utf-8"), encoding="utf-8")
            rubric_path = run_dir / "rubrics" / "AB-X" / "rubric.yaml"
            profile = run.jail.judge_profile(run_dir / "export", draft, fact_key, rubric_path)

            with mock.patch("authorbench.run.subprocess.run") as subprocess_run:
                subprocess_run.return_value = mock.Mock(stdout="R1: 2 | ok\n")
                result = run._default_judge_runner(["codex"], {"codex": "R1: 2 | ok\n"}, profile=profile, run_dir=run_dir)
            self.assertEqual(result, {"codex": "R1: 2 | ok\n"})
            call = subprocess_run.call_args
            self.assertEqual(call.args[0], ["tools/authorbench/confinement/confined-authorbench.sh", "judge", "run", "R1: 2 | ok\n"])
            judge_env = call.kwargs["env"]
            self.assertEqual(judge_env["AUTHORBENCH_EXPORT"], str(run_dir / "export"))
            self.assertEqual(judge_env["AUTHORBENCH_DRAFT"], str(draft))
            self.assertEqual(judge_env["AUTHORBENCH_FACT_KEY"], str(fact_key))
            self.assertEqual(judge_env["AUTHORBENCH_RUBRIC"], str(rubric_path))
            self.assertTrue(rubric_path.exists())

    def _bundle(self, root: Path) -> Path:
        bdir = root / "corpus" / "v1.0" / "AB-X"
        (bdir / "steer").mkdir(parents=True)
        (bdir / "brief.md").write_text("Write a short draft with citations.\n", encoding="utf-8")
        (bdir / "steer" / "grounded-claims.md").write_text("cite file:line\n", encoding="utf-8")
        (bdir / "fact_key.yaml").write_text(
            "\n".join(
                [
                    "version: 1",
                    "brief_id: AB-X",
                    "base_sha: fixture",
                    "facts:",
                    "  - id: F1",
                    "    claim: needle is present",
                    "    file: src/thing.py",
                    "    line: 1",
                    "    pattern: needle",
                    "traps:",
                    "  - id: T1",
                    "    name: trap",
                    "    precondition: avoid stale claims",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (bdir / "bundle.yaml").write_text(
            yaml.safe_dump(
                {
                    "id": "AB-X",
                    "stage": "design",
                    "corpus_version": "v1.0",
                    "base_sha": "fixture",
                    "length_budget": 300,
                    "normalizer_version": 1,
                    "factkey_version": 1,
                    "outcome_globs": [],
                    "hashes": {},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return bdir

    def test_run_pipeline_writes_author_judge_score_and_report_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bdir = self._bundle(root)
            run_dir = root / "run-1"
            (run_dir / "export" / "src").mkdir(parents=True)
            (run_dir / "export" / "src" / "thing.py").write_text("needle = True\n", encoding="utf-8")

            def author_runner(**kwargs):
                self.assertEqual(kwargs["role"], "author")
                return "Claim cites src/thing.py:1.\nObligation: delete guard => red.\n"

            def judge_runner(panel, packet, **kwargs):
                self.assertIn("Claim cites src/thing.py:1", packet["draft"])
                return {seat: "R1: 2 | cites src/thing.py:1\nR2: 1 | partial\nR3: 1 | alternative\nR4: 2 | delete guard\nR5: 2 | caveat\nR6: 2 | avoided\n" for seat in panel}

            result = run.run_bench(
                "codex",
                "v1.0",
                panel=["codex", "pi-glm"],
                run_dir=run_dir,
                bundles=[bdir],
                author_runner=author_runner,
                judge_runner=judge_runner,
                canary_runner=lambda **kwargs: None,
                export_dir=run_dir / "export",
            )

            self.assertEqual(result.stages, ["freeze-check", "author", "judge", "score", "report"])
            self.assertTrue((run_dir / "drafts" / "codex" / "raw.md").exists())
            self.assertTrue((run_dir / "staging" / "codex" / "draft.md").exists())
            self.assertTrue((run_dir / "scores.json").exists())
            self.assertTrue((run_dir / "reports" / "report-codex.md").exists())
            events = [json.loads(line)["event"] for line in (run_dir / "events.ndjson").read_text(encoding="utf-8").splitlines()]
            for expected in ["author_dispatch", "author_draft", "judge_dispatch", "judge_anchor", "mechanical_r1", "mechanical_r4", "report_rendered"]:
                self.assertIn(expected, events)

    def test_run_bench_real_v1_corpus_keeps_per_brief_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir = root / "run-real-corpus"
            export = run_dir / "export"
            export.mkdir(parents=True)
            brief_ids = ["AB-D1", "AB-P1", "AB-S1"]

            def author_runner(**kwargs):
                brief_id = kwargs["bundle"].manifest["id"]
                return f"{brief_id} draft cites no source.\nObligation: preserve {brief_id} => red.\n"

            def judge_runner(panel, packet, **kwargs):
                draft = packet["draft"]
                return {
                    seat: (
                        f"R1: 1 | {draft.splitlines()[0]}\n"
                        "R2: 1 | partial\n"
                        "R3: 1 | alternative\n"
                        "R4: 2 | red\n"
                        "R5: 2 | caveat\n"
                        "R6: 2 | avoided\n"
                    )
                    for seat in panel
                }

            run.run_bench(
                "codex",
                "v1.0",
                panel=["codex"],
                run_dir=run_dir,
                author_runner=author_runner,
                judge_runner=judge_runner,
                canary_runner=lambda **kwargs: None,
                export_dir=export,
            )

            drafts = {
                brief_id: (run_dir / "drafts" / "codex" / brief_id / "raw.md").read_text(encoding="utf-8")
                for brief_id in brief_ids
            }
            self.assertEqual(set(drafts), set(brief_ids))
            self.assertEqual(len(set(drafts.values())), 3)
            self.assertEqual(
                sorted(path.name for path in (run_dir / "bundles").iterdir() if path.is_dir()),
                brief_ids,
            )
            scores = json.loads((run_dir / "scores.json").read_text(encoding="utf-8"))
            self.assertEqual(sorted(scores["codex"]["grid"]), brief_ids)

    def test_cli_score_and_report_write_outputs_from_existing_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bdir = self._bundle(root)
            run_dir = root / "run-1"
            (run_dir / "bundle").mkdir(parents=True)
            for name in ["bundle.yaml", "fact_key.yaml"]:
                (run_dir / "bundle" / name).write_text((bdir / name).read_text(encoding="utf-8"), encoding="utf-8")
            (run_dir / "export" / "src").mkdir(parents=True)
            (run_dir / "export" / "src" / "thing.py").write_text("needle = True\n", encoding="utf-8")
            (run_dir / "drafts" / "codex").mkdir(parents=True)
            (run_dir / "drafts" / "codex" / "raw.md").write_text("Claim cites src/thing.py:1.\n", encoding="utf-8")
            (run_dir / "judge").mkdir()
            (run_dir / "judge" / "codex.json").write_text(
                json.dumps(
                    {
                        "codex": {
                            "R1": {"score": 2, "quote": "cites"},
                            "R2": {"score": 1, "quote": "partial"},
                            "R3": {"score": 1, "quote": "alts"},
                            "R4": {"score": 2, "quote": "red"},
                            "R5": {"score": 2, "quote": "honest"},
                            "R6": {"score": 2, "quote": "avoided"},
                        }
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(cli.main(["score", "--run", str(run_dir)]), 0)
            self.assertEqual(cli.main(["report", "--run", str(run_dir)]), 0)
            self.assertTrue((run_dir / "scores.json").exists())
            self.assertIn("Headline:", (run_dir / "reports" / "report-codex.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
