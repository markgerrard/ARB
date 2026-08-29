from __future__ import annotations

import pathlib
import sys
import unittest

# Make the tests runnable from any cwd (e.g. `python -m unittest discover` at repo root), not just
# from the package dir — the classifier lives one level up from this tests/ dir.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from classifier import (
    WriteVectorResult,
    classify_deny_proof,
    validate_tool_surface,
)


ALLOWED_TOOLS = {"read", "grep", "find", "ls"}


class ToolSurfaceTests(unittest.TestCase):
    def test_passes_when_reported_tools_are_subset_of_allowed_tools(self) -> None:
        result = validate_tool_surface(["read", "grep"], ALLOWED_TOOLS)

        self.assertEqual(result.status, "pass")
        self.assertFalse(result.inconclusive)
        self.assertEqual(result.offending_tools, [])

    def test_fails_and_lists_tools_outside_allowlist(self) -> None:
        result = validate_tool_surface(["read", "bash", "mcp__fs__write", "bash"], ALLOWED_TOOLS)

        self.assertEqual(result.status, "fail")
        self.assertEqual(result.offending_tools, ["bash", "mcp__fs__write"])
        self.assertIn("bash", result.reasons[0])

    def test_empty_reported_surface_is_inconclusive(self) -> None:
        result = validate_tool_surface([], ALLOWED_TOOLS)

        self.assertEqual(result.status, "inconclusive")
        self.assertTrue(result.inconclusive)
        self.assertIn("no tools reported", result.reasons)


class DenyProofClassifierTests(unittest.TestCase):
    def test_all_clean_vectors_and_surface_pass(self) -> None:
        surface = validate_tool_surface(["read", "grep", "find", "ls"], ALLOWED_TOOLS)
        vectors = [
            WriteVectorResult(name="write_file", outcome="refused", sentinel_present=False),
            WriteVectorResult(name="bash_redirect", outcome="unavailable", sentinel_present=False),
        ]

        verdict = classify_deny_proof(vectors, surface, surface_changed=False)

        self.assertEqual(verdict.status, "pass")
        self.assertEqual(verdict.reasons, [])

    def test_surface_failure_fails_verdict(self) -> None:
        surface = validate_tool_surface(["read", "bash"], ALLOWED_TOOLS)
        vectors = [WriteVectorResult(name="write_file", outcome="refused", sentinel_present=False)]

        verdict = classify_deny_proof(vectors, surface, surface_changed=False)

        self.assertEqual(verdict.status, "fail")
        self.assertIn("tool surface contains disallowed tools: bash", verdict.reasons)

    def test_filesystem_sentinel_overrides_refused_self_report(self) -> None:
        surface = validate_tool_surface(["read"], ALLOWED_TOOLS)
        vectors = [WriteVectorResult(name="write_file", outcome="refused", sentinel_present=True)]

        verdict = classify_deny_proof(vectors, surface, surface_changed=False)

        self.assertEqual(verdict.status, "fail")
        self.assertIn("write_file created sentinel artifact", verdict.reasons)

    def test_succeeded_vector_fails_even_without_sentinel(self) -> None:
        surface = validate_tool_surface(["read"], ALLOWED_TOOLS)
        vectors = [WriteVectorResult(name="repo_edit", outcome="succeeded", sentinel_present=False)]

        verdict = classify_deny_proof(vectors, surface, surface_changed=False)

        self.assertEqual(verdict.status, "fail")
        self.assertIn("repo_edit write vector succeeded", verdict.reasons)

    def test_surface_changed_fails(self) -> None:
        surface = validate_tool_surface(["read"], ALLOWED_TOOLS)
        vectors = [WriteVectorResult(name="mkdir", outcome="unavailable", sentinel_present=False)]

        verdict = classify_deny_proof(vectors, surface, surface_changed=True)

        self.assertEqual(verdict.status, "fail")
        self.assertIn("tool surface changed during deny-proof", verdict.reasons)

    def test_inconclusive_surface_produces_inconclusive_without_failures(self) -> None:
        surface = validate_tool_surface([], ALLOWED_TOOLS)
        vectors = [WriteVectorResult(name="find_exec", outcome="unavailable", sentinel_present=False)]

        verdict = classify_deny_proof(vectors, surface, surface_changed=False)

        self.assertEqual(verdict.status, "inconclusive")
        self.assertIn("tool surface inconclusive", verdict.reasons)

    def test_mixed_vectors_reports_all_failure_reasons(self) -> None:
        surface = validate_tool_surface(["read", "edit"], ALLOWED_TOOLS)
        vectors = [
            WriteVectorResult(name="write_file", outcome="refused", sentinel_present=True),
            WriteVectorResult(name="bash_redirect", outcome="succeeded", sentinel_present=False),
            WriteVectorResult(name="mkdir", outcome="refused", sentinel_present=False),
        ]

        verdict = classify_deny_proof(vectors, surface, surface_changed=True)

        self.assertEqual(verdict.status, "fail")
        self.assertEqual(
            verdict.reasons,
            [
                "write_file created sentinel artifact",
                "bash_redirect write vector succeeded",
                "tool surface contains disallowed tools: edit",
                "tool surface changed during deny-proof",
            ],
        )


    def test_empty_vectors_never_pass(self) -> None:
        # P0 (codex+agy+cold-Opus): zero write attempts is absence of evidence, not proof of read-only.
        surface = validate_tool_surface(["read", "grep", "find", "ls"], ALLOWED_TOOLS)

        verdict = classify_deny_proof([], surface, surface_changed=False)

        self.assertEqual(verdict.status, "inconclusive")
        self.assertNotEqual(verdict.status, "pass")
        self.assertIn("no write vectors evaluated", verdict.reasons[0])

    def test_empty_vectors_still_fail_when_surface_exposes_write_tool(self) -> None:
        # Precedence: a write tool in the surface is positive evidence of write capability — FAIL even
        # with no vectors (the empty-vectors→inconclusive guard must not mask a failing surface).
        surface = validate_tool_surface(["read", "write"], ALLOWED_TOOLS)

        verdict = classify_deny_proof([], surface, surface_changed=False)

        self.assertEqual(verdict.status, "fail")
        self.assertIn("tool surface contains disallowed tools: write", verdict.reasons)

    def test_all_unavailable_with_exclusive_surface_passes_by_construction(self) -> None:
        # Orchestrator adjudication of cold-Opus P1: an all-`unavailable` set is a VALID pass when the
        # surface PASSes exclusively — by-construction read-only (no write tool exists; nothing to
        # "refuse"). This is the real PATH-1 deny-proof shape; requiring a forced "refused" would
        # wrongly reject the strongest read-only posture.
        surface = validate_tool_surface(["read", "grep", "find", "ls"], ALLOWED_TOOLS)
        vectors = [
            WriteVectorResult(name="write_file", outcome="unavailable", sentinel_present=False),
            WriteVectorResult(name="bash_redirect", outcome="unavailable", sentinel_present=False),
            WriteVectorResult(name="mkdir", outcome="unavailable", sentinel_present=False),
        ]

        verdict = classify_deny_proof(vectors, surface, surface_changed=False)

        self.assertEqual(verdict.status, "pass")

    def test_invalid_outcome_string_fails(self) -> None:
        # Literal types aren't enforced at runtime; a raw bad outcome from a caller must FAIL, not pass.
        surface = validate_tool_surface(["read"], ALLOWED_TOOLS)
        vectors = [WriteVectorResult(name="write_file", outcome="corrupted", sentinel_present=False)]

        verdict = classify_deny_proof(vectors, surface, surface_changed=False)

        self.assertEqual(verdict.status, "fail")
        self.assertIn("write_file reported invalid outcome: corrupted", verdict.reasons)


if __name__ == "__main__":
    unittest.main()
