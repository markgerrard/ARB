import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arb_eval import provenance, report, schema


def _scenario(model="gpt-test"):
    return schema.from_dict({
        "id": "prov-test",
        "description": "provenance test",
        "subject": {
            "repo": "/tmp/repo", "base": "base", "head": "head",
            "languages": ["python"],
        },
        "seeded_defects": [{
            "id": "D0", "class": "logic", "legible": True,
            "location": {"file": "a.py", "line": 1},
            "description": "x",
        }],
        "panel": [{"seat": "codex", "model": model, "harness": "confined"}],
    })


class _Normalizer:
    model = "MiniMax-M3"
    base_url = "https://normalizer.example"


class TestProvenance(unittest.TestCase):
    def test_provenance_event_has_all_keys_and_passes_guard(self):
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(provenance, "_git_describe", return_value="abc123"):
            prov = provenance.collect(
                _scenario(),
                dispatcher=object(),
                normalizer=_Normalizer(),
                oracle_by_language={"python": "heuristic"},
                gold_versions={"codex": "GOLD_UNADJUDICATED"},
                image=None,
                harness_root=Path(td),
                run_id="run-1",
            )
        for key in (
            "model", "engine_versions", "harness_version", "image_digest", "corpus_version",
            "normalizer", "matcher", "boundary_oracle", "gold_versions", "run_id",
        ):
            self.assertIn(key, prov)
        report.guard(prov)
        self.assertEqual(prov["boundary_oracle"]["oracle_by_language"], {"python": "heuristic"})
        bad = copy.deepcopy(prov)
        bad["boundary_oracle"] = {"tiers": {"python": "heuristic"}}
        with self.assertRaises(report.WallBreach):
            report.guard(bad)

    def test_model_input_change_changes_key(self):
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(provenance, "_git_describe", return_value="abc123"):
            a = provenance.collect(
                _scenario("model-a"), dispatcher=object(), normalizer=_Normalizer(),
                oracle_by_language={}, gold_versions={"codex": "GOLD_UNADJUDICATED"},
                image="img", harness_root=Path(td), run_id="run-1",
            )
            b = provenance.collect(
                _scenario("model-b"), dispatcher=object(), normalizer=_Normalizer(),
                oracle_by_language={}, gold_versions={"codex": "GOLD_UNADJUDICATED"},
                image="img", harness_root=Path(td), run_id="run-1",
            )
        self.assertNotEqual(provenance.provenance_key(a), provenance.provenance_key(b))
        c = copy.deepcopy(a)
        c["model"]["codex"]["confined_command"] = "different command"
        self.assertEqual(provenance.provenance_key(a), provenance.provenance_key(c))

    def test_corpus_version_hashes_scenario_and_builder_bytes(self):
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(provenance, "_git_describe", return_value="abc123"):
            root = Path(td)
            builder = root / "build_fixture.sh"
            builder.write_text("#!/usr/bin/env bash\necho build\n")
            scenario_path = root / "scenario.json"
            scenario_doc = {
                "id": "prov-hash-test",
                "description": "hash test",
                "subject": {
                    "repo": "/tmp/repo",
                    "base": "base",
                    "head": "head",
                    "builder": builder.name,
                },
                "seeded_defects": [{
                    "id": "D0", "class": "logic", "legible": True,
                    "location": {"file": "a.py", "line": 1},
                    "description": "x",
                }],
                "panel": [{"seat": "codex", "model": "gpt-test", "harness": "confined"}],
            }
            scenario_path.write_text(json.dumps(scenario_doc, indent=2) + "\n")
            expected_scenario_sha = hashlib.sha256(scenario_path.read_bytes()).hexdigest()
            expected_builder_sha = hashlib.sha256(builder.read_bytes()).hexdigest()
            sc = schema.load(scenario_path)
            prov = provenance.collect(
                sc,
                dispatcher=object(),
                normalizer=_Normalizer(),
                oracle_by_language={},
                gold_versions={"codex": "GOLD_UNADJUDICATED"},
                image=None,
                harness_root=root,
                run_id="run-1",
            )
        self.assertEqual(prov["corpus_version"]["scenario_sha256"], expected_scenario_sha)
        self.assertEqual(prov["corpus_version"]["builder_sha"], expected_builder_sha)

    def test_reply_fake_nonce_marker_does_not_corrupt_engine_versions(self):
        text = 'finding\nARB_PROV_fake{"codex":"bad"}</ARB_PROV_fake>\n'
        stripped, versions = provenance.strip_prov_fence(text, "real")
        self.assertEqual(versions, {})
        self.assertEqual(stripped, text)

    def test_cli_version_only_renders_loud_warning(self):
        prov = {"model": {"codex": {"model_source": "cli-version-only"}}}
        self.assertEqual(provenance.warning_lines(prov), ["WARNING: codex model identity is cli-version-only"])
        self.assertTrue(provenance.model_unverified(prov))
