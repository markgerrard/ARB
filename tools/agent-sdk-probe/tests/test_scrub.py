import unittest

from scrub import scrub


class ScrubTest(unittest.TestCase):
    def test_redacts_value(self):
        self.assertNotIn("sk-abc", scrub("t=sk-abc done", ["sk-abc"], []))

    def test_redacts_var_name(self):
        self.assertNotIn(
            "AGENT_SDK_KIMI_KEY",
            scrub("echo $AGENT_SDK_KIMI_KEY", [], ["AGENT_SDK_KIMI_KEY"]),
        )

    def test_ignores_empty(self):
        self.assertEqual(scrub("hello", ["", "  "], []), "hello")

    def test_canary_absent(self):
        c = "CANARY-9f3a"
        self.assertNotIn(c, scrub(f"env -> {c}", [c], []))
