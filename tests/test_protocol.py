import unittest

from agent_redis_bridge.protocol import build_task_prompt, parse_structured_reply


class ProtocolTest(unittest.TestCase):
    def test_build_task_prompt_adds_structured_directive(self) -> None:
        prompt = build_task_prompt("Run tests.", expect_structured=True)

        self.assertIn("Run tests.", prompt)
        self.assertIn("<structured_reply_instructions>", prompt)
        self.assertIn("DONE_WITH_CONCERNS", prompt)

    def test_build_task_prompt_adds_system_prompt_before_task_and_structured(self) -> None:
        prompt = build_task_prompt("Run tests.", system_prompt="Be strict.", expect_structured=True)

        self.assertTrue(prompt.startswith("<system_guidance>\nBe strict.\n</system_guidance>\n\nRun tests."))
        self.assertLess(prompt.index("<system_guidance>"), prompt.index("Run tests."))
        self.assertLess(prompt.index("Run tests."), prompt.index("<structured_reply_instructions>"))

    def test_build_task_prompt_omits_empty_system_prompt(self) -> None:
        self.assertEqual(build_task_prompt("Run tests.", system_prompt=""), "Run tests.")
        self.assertEqual(build_task_prompt("Run tests.", system_prompt="  \n"), "Run tests.")
        self.assertEqual(build_task_prompt("Run tests.", system_prompt=None), "Run tests.")

    def test_build_task_prompt_leaves_plain_task_unchanged(self) -> None:
        self.assertEqual(build_task_prompt("Run tests."), "Run tests.")

    def test_parse_fenced_json_structured_reply(self) -> None:
        parsed = parse_structured_reply(
            'Done.\n```json\n{"status":"DONE","summary":"ok","concerns":[]}\n```'
        )

        self.assertEqual(parsed.structured, {"status": "DONE", "summary": "ok", "concerns": []})
        self.assertIsNone(parsed.error)

    def test_parse_inline_json_structured_reply(self) -> None:
        parsed = parse_structured_reply('Done. {"status":"NEEDS_CONTEXT","questions":["Which branch?"]}')

        self.assertEqual(parsed.structured, {"status": "NEEDS_CONTEXT", "questions": ["Which branch?"]})

    def test_parse_malformed_json_returns_missing_block(self) -> None:
        parsed = parse_structured_reply("Done. ```json\n{\"status\":\"DONE\"\n```")

        self.assertIsNone(parsed.structured)
        self.assertEqual(parsed.error, "missing-structured-block")

    def test_parse_missing_structured_block_returns_error(self) -> None:
        parsed = parse_structured_reply("Done with no status.")

        self.assertIsNone(parsed.structured)
        self.assertEqual(parsed.error, "missing-structured-block")

    def test_parse_uses_last_valid_fenced_status_block(self) -> None:
        parsed = parse_structured_reply(
            'First: ```json\n{"foo":"bar"}\n```\nSecond: ```json\n{"status":"DONE"}\n```'
        )

        self.assertEqual(parsed.structured, {"status": "DONE"})
        self.assertIsNone(parsed.error)

    def test_parse_inline_status_after_braced_text(self) -> None:
        parsed = parse_structured_reply('Check files {a, b}. Final status: {"status": "DONE"}')

        self.assertEqual(parsed.structured, {"status": "DONE"})
        self.assertIsNone(parsed.error)

    def test_parse_early_non_status_json_without_status_errors(self) -> None:
        parsed = parse_structured_reply('Details: ```json\n{"foo":"bar"}\n```\nNo status block.')

        self.assertIsNone(parsed.structured)
        self.assertEqual(parsed.error, "invalid-structured-status")

    def test_optional_fields_with_wrong_types_are_dropped(self) -> None:
        parsed = parse_structured_reply(
            'Done. ```json\n{"status":"DONE","summary":1,"concerns":"oops","next_steps":["ship"]}\n```'
        )

        self.assertEqual(parsed.structured, {"status": "DONE", "next_steps": ["ship"]})
        self.assertIsNone(parsed.error)


if __name__ == "__main__":
    unittest.main()
