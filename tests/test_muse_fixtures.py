"""Replay REAL captured Muse output through the mapper.

The fixtures under tests/fixtures/muse/ were captured on 2026-08-06 from
`Muse Code 0.1.0 (0.1.0-R708.1)` -- one free `--provider echo` run and one real
model turn at `--reasoning-effort high` that executed a bash tool.

These tests exist because the hand-written envelopes in test_muse_events.py were
built from the probe artefact's PROSE, and the prose was wrong in two ways that
no amount of offline testing could surface:

1. `task.lifecycle.*` payloads nest their fields under `payload["event"]`,
   while `run.*` and `tool.*` are flat. The first mapper read lifecycle fields
   flat, got None for every one, and emitted NO tool calls at all -- silently,
   because a missing key is not an error.
2. Not every `task.lifecycle.scheduled` is a tool. The real turn scheduled two
   `model.meta.response` tasks alongside one `tool.bash`, so keying off
   "scheduled has an idempotency_key" invents tool calls that never complete.

A fixture is the only kind of test that can fail for reason (1) or (2), which
is why the offline suite passed while the mapper was wrong.
"""
import json
import unittest
from pathlib import Path

from arb_warm_orch.muse_events import (
    MuseEventMapper,
    normalise_call_id,
    parse_line,
)
from arb_warm_orch.turn_events import (
    TextDelta,
    ToolCallCompleted,
    ToolCallStarted,
)

FIXTURES = Path(__file__).parent / "fixtures" / "muse"


def replay(name):
    mapper = MuseEventMapper()
    events = []
    for line in (FIXTURES / name).read_text().splitlines():
        envelope = parse_line(line)
        if envelope is not None:
            events.extend(mapper.feed(envelope))
    return mapper, events


class TestEchoProviderCapture(unittest.TestCase):
    """The free echo run -- the trap-1 fixture."""

    def test_terminal_is_completed_despite_two_failed_events(self):
        mapper, _ = replay("echo-provider.jsonl")
        self.assertEqual(mapper.terminal, "completed")
        self.assertEqual(mapper.terminal_text, "echo: hello")

    def test_the_two_failures_are_captured_with_their_reasons(self):
        # Proves the nested-payload unwrap: read flat, this list is empty.
        mapper, _ = replay("echo-provider.jsonl")
        self.assertEqual(len(mapper.failures), 2)
        for reason in mapper.failures:
            self.assertIn("base instructions", reason)

    def test_echo_run_emits_no_tool_calls(self):
        _, events = replay("echo-provider.jsonl")
        self.assertEqual(
            [e for e in events if isinstance(e, ToolCallStarted)], [])


class TestRealTurnCapture(unittest.TestCase):
    """A real model turn that ran `echo hi-from-muse` via the bash tool."""

    NAME = "real-turn-bash-high-effort.jsonl"

    def test_turn_completes_with_the_tool_output(self):
        mapper, _ = replay(self.NAME)
        self.assertEqual(mapper.terminal, "completed")
        self.assertEqual(mapper.terminal_text, "hi-from-muse")

    def test_exactly_one_tool_call_started(self):
        """The regression guard. THREE tasks were scheduled in this turn; two
        were `model.meta.response`. Keying off idempotency_key alone yields 3."""
        _, events = replay(self.NAME)
        started = [e for e in events if isinstance(e, ToolCallStarted)]
        self.assertEqual(len(started), 1, f"expected 1 tool call, got {started}")
        self.assertEqual(started[0].kind, "execute")
        self.assertEqual(started[0].title, "tool.bash")

    def test_every_started_call_has_a_matching_completion(self):
        """G6, asserted against real ids rather than an assumed prefix rule."""
        _, events = replay(self.NAME)
        started = {e.tool_call_id for e in events
                   if isinstance(e, ToolCallStarted)}
        done = {e.tool_call_id for e in events
                if isinstance(e, ToolCallCompleted)}
        self.assertTrue(started, "fixture has no tool call to pair")
        self.assertEqual(started, done)

    def test_g6_prefix_rule_holds_on_the_raw_bytes(self):
        raw = (FIXTURES / self.NAME).read_text().splitlines()
        sched = res = None
        for line in raw:
            d = json.loads(line)
            payload = d.get("payload") or {}
            event = payload.get("event") or {}
            key = event.get("idempotency_key", "")
            if d["payload_type"] == "task.lifecycle.scheduled" and str(
                key
            ).startswith("tool:"):
                sched = str(key)
            if d["payload_type"] == "tool.result":
                res = payload["call_id"]
        self.assertIsNotNone(sched)
        self.assertIsNotNone(res)
        self.assertEqual(normalise_call_id(sched), res)

    def test_reply_text_excludes_tool_traffic(self):
        _, events = replay(self.NAME)
        reply = "".join(e.text for e in events if isinstance(e, TextDelta))
        self.assertEqual(reply.strip(), "hi-from-muse")

    def test_no_reasoning_payload_at_high_effort(self):
        """G3, recorded as a measurement rather than left open.

        This turn ran at --reasoning-effort high and emitted NO reasoning-shaped
        payload_type. ReasoningDelta stays unmapped on evidence, not on
        omission. If a future Muse build adds one, this test fails and tells
        the next reader to map it.
        """
        types = {json.loads(l)["payload_type"]
                 for l in (FIXTURES / self.NAME).read_text().splitlines()}
        reasoning = [t for t in types if "reason" in t or "think" in t]
        self.assertEqual(reasoning, [], f"Muse now emits {reasoning} -- map it")



class TestResumeAndKillCaptures(unittest.TestCase):
    """Gate G1 + the F3 baseline, captured live 2026-08-07.

    Until this run, the design's CENTRAL premise -- that two independent
    processes sharing one --session-id carry context -- was inherited from the
    probe artefact, re-run by nobody, and covered by no fixture. Panel finding
    F3 caught that the brief had graded it `demonstrated` anyway. These are the
    captures that make it measured.
    """

    def test_baseline_two_clean_processes_share_context(self):
        """F3 premise. Turn 1 stores a token; turn 2, a SEPARATE process on the
        same --session-id, returns it."""
        m1, _ = replay("resume-turn1-set-token.jsonl")
        m2, _ = replay("resume-turn2-recall-token.jsonl")
        self.assertEqual(m1.terminal, "completed")
        self.assertEqual(m2.terminal, "completed")
        self.assertIn("PLUM-4271", m2.terminal_text)

    def test_a_killed_turn_has_no_terminal_event(self):
        """G1, first half: SIGTERM mid-turn truncates the stream. This is the
        capture behind muse_runner's `terminal is None -> MuseTurnFailed`."""
        mapper, _ = replay("killed-turn-sigterm.jsonl")
        self.assertIsNone(
            mapper.terminal,
            "a SIGTERMed turn must not present a terminal event")

    def test_session_survives_a_killed_turn(self):
        """G1 VERDICT: PASS. After a turn was killed (rc=143), the next process
        on the same --session-id still recalled the token set BEFORE the kill.

        This is what licenses `interrupt()` retaining the session id rather
        than rotating it (design §4) -- previously an explicit hypothesis.
        """
        mapper, _ = replay("killed-turn-then-recall.jsonl")
        self.assertEqual(mapper.terminal, "completed")
        self.assertIn("ZEPHYR-3390", mapper.terminal_text)

if __name__ == "__main__":
    unittest.main()
