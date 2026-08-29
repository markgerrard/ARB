import json
import tempfile
import unittest
from pathlib import Path

from authorbench import store


class TestReproducibility(unittest.TestCase):
    def _run(self, root: Path) -> Path:
        run = root / "run-1"
        (run / "bundle").mkdir(parents=True)
        (run / "export" / "src").mkdir(parents=True)
        (run / "drafts" / "codex").mkdir(parents=True)
        (run / "staging" / "codex").mkdir(parents=True)
        (run / "bundle" / "fact_key.yaml").write_text(
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
                    "traps: []",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (run / "export" / "src" / "thing.py").write_text("needle = True\n", encoding="utf-8")
        (run / "drafts" / "codex" / "raw.md").write_text(
            "Claim cites src/thing.py:1.\nObligation: delete guard => red.\n",
            encoding="utf-8",
        )
        return run

    def test_mechanical_rederives_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as td:
            run = self._run(Path(td))
            recorder = store.Recorder(run)
            original_events = [
                {
                    "event": "blinding_scan",
                    "seat": "codex",
                    "hits": [],
                    "result": "clean",
                },
                {
                    "event": "mechanical_r1",
                    "seat": "codex",
                    "brief_id": "AB-X",
                    "cited": 1,
                    "true": 1,
                    "fabricated": 0,
                    "citations": [
                        {"claim": "needle is present", "file": "src/thing.py", "line": 1, "true": True},
                    ],
                },
                {
                    "event": "mechanical_r4",
                    "seat": "codex",
                    "brief_id": "AB-X",
                    "obligations": 1,
                    "with_red_condition": 1,
                },
            ]
            for event in original_events:
                recorder.write(event)
            stored_payloads = [
                {k: v for k, v in json.loads(line).items() if k not in {"ts", "run_id"}}
                for line in (run / "events.ndjson").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(stored_payloads, original_events)
            self.assertEqual(store.rederive_mechanical(run)["codex"], original_events)

            (run / "drafts" / "codex" / "raw.md").write_text("Claim cites src/thing.py:2.\n", encoding="utf-8")
            self.assertNotEqual(store.rederive_mechanical(run)["codex"], stored_payloads)

    def test_judged_rerun_new_id_original_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            run = self._run(Path(td))
            original = run / "events.ndjson"
            original.write_text('{"event":"original"}\n', encoding="utf-8")
            rerun = store.new_judging_run(run, "judge-rerun-2")
            store.Recorder(rerun).write({"event": "judge_phase"})
            self.assertEqual(original.read_text(encoding="utf-8"), '{"event":"original"}\n')
            self.assertNotEqual(rerun, run)
            self.assertTrue((rerun / "events.ndjson").exists())

    def test_write_guards_every_event(self):
        with tempfile.TemporaryDirectory() as td:
            recorder = store.Recorder(Path(td) / "run")
            with self.assertRaises(Exception):
                recorder.write({"event": "bad", "winner": "codex"})
            self.assertFalse((Path(td) / "run" / "events.ndjson").exists())

    def test_memory_key(self):
        self.assertEqual(
            store.memory_key("codex", "gpt-5", "v1.0", "run-1"),
            "author-bench/codex/gpt-5/v1.0/run-1",
        )


if __name__ == "__main__":
    unittest.main()
