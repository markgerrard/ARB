"""Offline tests for the Muse JSONL envelope mapper.

Zero `muse` invocations by construction: every input here is a literal envelope
dict or line. The whole point of splitting `muse_events` out of `muse_runner`
(design §2.1) is that the Muse-specific knowledge is testable without a process,
which is what keeps the live-capture budget small.
"""
import unittest

from arb_warm_orch.muse_events import (
    MuseEventMapper,
    muse_kind,
    normalise_call_id,
    parse_line,
)
from arb_warm_orch.turn_events import (
    TextDelta,
    ToolCallCompleted,
    ToolCallStarted,
)


def env(payload_type, payload, **kw):
    d = {"schema_version": 1, "payload_type": payload_type, "payload": payload}
    d.update(kw)
    return d


def lc(kind, task_id=None, **event_fields):
    """Build a `task.lifecycle.<kind>` envelope with the REAL nested shape.

    MEASURED 2026-08-06 (tests/fixtures/muse/*.jsonl): lifecycle fields live
    under `payload["event"]`, NOT flat on the payload. `run.*` and `tool.*`
    ARE flat. An earlier version of this file built them flat, from the probe
    artefact's prose, and the mapper was written to match -- so the suite was
    green against a shape Muse never emits.
    """
    ev = {"kind": kind}
    if task_id is not None:
        ev["task_id"] = task_id
    ev.update(event_fields)
    payload = {"kind": "task_lifecycle", "event": ev}
    if task_id is not None:
        payload["task_id"] = task_id
    return {"schema_version": 1,
            "payload_type": f"task.lifecycle.{kind}",
            "payload": payload}


class TestTextAndPreamble(unittest.TestCase):
    def test_output_delta_becomes_text_delta(self):
        m = MuseEventMapper()
        out = m.feed(env("run.output.delta", {"text": "hi"}))
        self.assertEqual(out, [TextDelta(text="hi")])

    def test_preamble_line_is_skipped_not_an_error(self):
        # `muse exec` prints this before the JSONL. Treating it as malformed
        # would make every successful turn log an error (design §2.3).
        self.assertIsNone(parse_line("muse: workspace root: /tmp/x"))

    def test_malformed_json_is_skipped_not_raised(self):
        self.assertIsNone(parse_line('{"unterminated'))

    def test_json_line_parses(self):
        self.assertEqual(parse_line('{"a":1}'), {"a": 1})


class TestTraps(unittest.TestCase):
    def test_lifecycle_failed_does_not_set_terminal(self):
        # Trap 1 (design §5.2): the echo provider emits TWO of these on a
        # SUCCESSFUL run. A naive adapter reports a false failure.
        m = MuseEventMapper()
        m.feed(lc("failed",
                  reason="provider does not support base instructions"))
        m.feed(lc("failed", reason="same again"))
        self.assertIsNone(m.terminal)
        self.assertEqual(
            m.failures,
            ["provider does not support base instructions", "same again"],
        )

    def test_terminal_comes_only_from_run_terminal(self):
        m = MuseEventMapper()
        m.feed(lc("failed", reason="noise"))
        m.feed(env("run.terminal.completed",
                   {"terminal": "completed", "text": "OK"}))
        self.assertEqual(m.terminal, "completed")
        self.assertEqual(m.terminal_text, "OK")

    def test_lifecycle_status_is_never_reasoning(self):
        # Trap 2 (design §5.3): this is provider-retry telemetry, not thought.
        m = MuseEventMapper()
        out = m.feed(lc("status",
                        message="opening meta model stream attempt 1/10",
                        details={"facets": [{"kind": "external_attempt"}]}))
        self.assertEqual(out, [])

    def test_unknown_payload_type_is_ignored(self):
        m = MuseEventMapper()
        self.assertEqual(m.feed(env("some.future.event", {"x": 1})), [])



class TestToolCalls(unittest.TestCase):
    """Tool-call correlation across proposed -> scheduled -> result.

    NOTE ON PROVENANCE: these tests were written AFTER the implementation, so
    they never ran red against missing code. Their ability to fail is proven
    separately by the mutation sweep in tests/test_muse_events_mutation.py --
    a test that has never failed is not yet evidence of anything.
    """

    def test_call_id_prefix_is_stripped(self):
        self.assertEqual(normalise_call_id("tool:call_019f"), "call_019f")
        self.assertEqual(normalise_call_id("call_019f"), "call_019f")

    def test_only_one_prefix_is_stripped(self):
        self.assertEqual(normalise_call_id("tool:tool:x"), "tool:x")

    def test_scheduled_emits_started_with_kind_from_proposed(self):
        m = MuseEventMapper()
        self.assertEqual(
            m.feed(lc("proposed", task_id="t1", task_kind="tool.bash")),
            [],
        )
        out = m.feed(lc("scheduled", task_id="t1",
                        idempotency_key="tool:call_019f"))
        self.assertEqual(
            out,
            [ToolCallStarted(tool_call_id="call_019f",
                             title="tool.bash", kind="execute")],
        )

    def test_tool_result_outcome_maps_to_status(self):
        m = MuseEventMapper()
        ok = m.feed(env("tool.result",
                        {"call_id": "call_a",
                         "correlation_facts": {"outcome": "success"}}))
        bad = m.feed(env("tool.result",
                         {"call_id": "call_b",
                          "correlation_facts": {"outcome": "error"}}))
        self.assertEqual(
            ok, [ToolCallCompleted(tool_call_id="call_a", status="completed")])
        self.assertEqual(
            bad, [ToolCallCompleted(tool_call_id="call_b", status="failed")])

    def test_missing_outcome_is_failed_not_completed(self):
        # Fail closed: an absent verdict must not read as success, or
        # turn_events.py:105-106's "a failing tool must stay visible" is defeated.
        m = MuseEventMapper()
        out = m.feed(env("tool.result", {"call_id": "call_c"}))
        self.assertEqual(out[0].status, "failed")

    def test_started_and_completed_ids_pair_up(self):
        # The whole point of the join (gate G6). If these two ids differ,
        # buzz's idle deadline never resets and nothing raises.
        m = MuseEventMapper()
        m.feed(lc("proposed", task_id="t2", task_kind="tool.bash"))
        started = m.feed(lc("scheduled", task_id="t2",
                           idempotency_key="tool:call_pair"))[0]
        done = m.feed(env("tool.result",
                          {"call_id": "call_pair",
                           "correlation_facts": {"outcome": "success"}}))[0]
        self.assertEqual(started.tool_call_id, done.tool_call_id)

    def test_scheduled_without_proposed_still_emits(self):
        m = MuseEventMapper()
        out = m.feed(lc("scheduled", task_id="t9",
                        idempotency_key="tool:call_z"))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].tool_call_id, "call_z")
        self.assertEqual(out[0].kind, "other")

    def test_non_tool_scheduled_tasks_emit_nothing(self):
        """MEASURED: a real turn scheduled two `model.meta.response` tasks
        alongside one `tool.bash`. Their idempotency_key is `model:<run>:<task>`,
        and emitting ToolCallStarted for them invents calls that never complete."""
        m = MuseEventMapper()
        m.feed(lc("proposed", task_id="m1", task_kind="model.meta.response"))
        out = m.feed(lc("scheduled", task_id="m1",
                        idempotency_key="model:run-abc:m1"))
        self.assertEqual(out, [])

    def test_unknown_task_kind_falls_back_to_other(self):
        self.assertEqual(muse_kind("tool.telepathy"), "other")
        self.assertEqual(muse_kind(None), "other")

if __name__ == "__main__":
    unittest.main()
