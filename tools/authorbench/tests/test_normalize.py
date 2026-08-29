import tempfile
import unittest
from pathlib import Path

from authorbench import normalize


class TestNormalize(unittest.TestCase):
    def test_normalizer_strips_identity_markers(self):
        raw = """Author: codex-bridge-dev
Status: complete
Model: GPT-5 Codex
Session: run-2026-07-09
# draft
- claim
"""
        text = normalize.normalize(raw, version=1)
        self.assertNotIn("codex-bridge-dev", text)
        self.assertNotIn("GPT-5", text)
        self.assertNotIn("Status:", text)
        self.assertEqual(normalize.denylist_scan(text), [])

    def test_seeded_resistant_marker_quarantines_unreachably(self):
        with tempfile.TemporaryDirectory() as td:
            staging = Path(td) / "staging"
            result = normalize.stage_for_judges(
                "This contains AUTHOR_IDENTITY_TOKEN.",
                staging,
                redactor=lambda text, hits: (text, []),
            )
            self.assertFalse(result.staged)
            self.assertTrue(result.aborted)
            self.assertEqual(result.token_class, "author-identity")
            self.assertEqual(list(staging.glob("**/*")), [])

    def test_redaction_pass_then_rescan(self):
        with tempfile.TemporaryDirectory() as td:
            staging = Path(td) / "staging"
            result = normalize.stage_for_judges("This contains AUTHOR_IDENTITY_TOKEN once.", staging)
            self.assertTrue(result.staged)
            self.assertTrue(result.quarantined)
            self.assertEqual(len(result.redaction_log), 1)
            self.assertTrue((staging / "draft.md").exists())
            self.assertNotIn("AUTHOR_IDENTITY_TOKEN", (staging / "draft.md").read_text(encoding="utf-8"))

    def test_still_hot_after_redaction_aborts_loud(self):
        with tempfile.TemporaryDirectory() as td:
            staging = Path(td) / "staging"
            with self.assertRaises(normalize.BlindingAbort) as ctx:
                normalize.stage_for_judges(
                    "AUTHOR_IDENTITY_TOKEN",
                    staging,
                    raise_on_abort=True,
                    redactor=lambda text, hits: (text, []),
                )
            self.assertEqual(ctx.exception.token_class, "author-identity")
            self.assertEqual(list(staging.glob("**/*")), [])

    def test_over_fire_draft_about_seat_ids_passes_clean(self):
        with tempfile.TemporaryDirectory() as td:
            staging = Path(td) / "staging"
            draft = "The design discusses seat ids such as codex-bridge-dev as routing keys."
            result = normalize.stage_for_judges(draft, staging)
            self.assertTrue(result.staged)
            self.assertFalse(result.quarantined)
            self.assertEqual(normalize.denylist_scan((staging / "draft.md").read_text(encoding="utf-8")), [])

    def test_current_author_bare_seat_id_is_redacted_without_global_overfire(self):
        with tempfile.TemporaryDirectory() as td:
            staging = Path(td) / "staging"
            draft = "This draft was written by codex-bridge-dev. Another seat pi-sdk-bridge-dev-glm reviewed later."
            result = normalize.stage_for_judges(
                draft,
                staging,
                author_identity_tokens=["codex-bridge-dev"],
            )
            self.assertTrue(result.staged)
            self.assertTrue(result.quarantined)
            text = (staging / "draft.md").read_text(encoding="utf-8")
            self.assertNotIn("codex-bridge-dev", text)
            self.assertIn("pi-sdk-bridge-dev-glm", text)

    def test_bare_seat_id_denylist_gap_pinned_for_run_author(self):
        hits = normalize.denylist_scan(
            "codex-bridge-dev",
            author_identity_tokens=["codex-bridge-dev"],
        )
        self.assertEqual([hit.token for hit in hits], ["codex-bridge-dev"])

    def test_still_hot_abort_uses_resistant_redaction_not_magic_sentinel(self):
        with tempfile.TemporaryDirectory() as td:
            staging = Path(td) / "staging"
            with self.assertRaises(normalize.BlindingAbort):
                normalize.stage_for_judges(
                    "repeat repeat",
                    staging,
                    author_identity_tokens=["repeat"],
                    redactor=lambda text, hits: (text, []),
                    raise_on_abort=True,
                )
            self.assertEqual(list(staging.glob("**/*")), [])


if __name__ == "__main__":
    unittest.main()
