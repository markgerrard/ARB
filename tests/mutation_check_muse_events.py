"""Mutation sweep: prove the muse_events tests CAN fail.

Not collected by pytest (filename is not `test_*.py`). Run it directly:

    PYTHONPATH=$PWD/src /Volumes/<workspace>/repos/ARB/.venv/bin/python \
        tests/mutation_check_muse_events.py

WHY THIS EXISTS. The TestToolCalls tests were written after the implementation,
so they never ran red against missing code. A test that has never failed is not
evidence -- it is a claim. This sweep converts the claim into a measurement by
breaking the behaviour under test and asserting the suite notices.

It also guards the two recorded traps directly. Both are defined by what the
code must NOT do, and a test for a negative is exactly the kind that silently
stops testing anything.

SAFETY. The real `src/` is never modified. Each mutation is applied to a
throwaway COPY of the tree and pytest is pointed at the copy, so there is no
restore step that can fail and leave the repo mutated.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mutation_lib import sweep  # noqa: E402

TARGET = "arb_warm_orch/muse_events.py"
TEST_FILE = "tests/test_muse_events.py tests/test_muse_fixtures.py"

# (label, find, replace, the test that must catch it)
MUTATIONS = [
    (
        "lifecycle.failed sets terminal (trap 1 inverted)",
        '        if payload_type == "task.lifecycle.failed":\n'
        "            # Recorded as detail; deliberately NOT terminal.",
        '        if payload_type == "task.lifecycle.failed":\n'
        '            self.terminal = "failed"\n'
        "            # Recorded as detail; deliberately NOT terminal.",
        "test_lifecycle_failed_does_not_set_terminal",
    ),
    (
        "lifecycle.status becomes a text event (trap 2 inverted)",
        '        if payload_type == "task.lifecycle.proposed":',
        '        if payload_type == "task.lifecycle.status":\n'
        '            return [TextDelta(text=str(payload.get("message", "")))]\n\n'
        '        if payload_type == "task.lifecycle.proposed":',
        "test_lifecycle_status_is_never_reasoning",
    ),
    (
        "normalise_call_id stops stripping the prefix (G6 join broken)",
        "    if raw.startswith(_CALL_ID_PREFIX):\n"
        "        return raw[len(_CALL_ID_PREFIX):]\n"
        "    return raw",
        "    return raw",
        "test_started_and_completed_ids_pair_up",
    ),
    (
        "missing tool outcome reads as success (fail-open)",
        '            status = "completed" if outcome == "success" else "failed"',
        '            status = "failed" if outcome == "error" else "completed"',
        "test_missing_outcome_is_failed_not_completed",
    ),
    # NOTE. The first version of this mutation deleted the `startswith("{")`
    # guard, and was NOT CAUGHT -- correctly, because it is an EQUIVALENT
    # MUTANT: with the guard gone the preamble falls through to json.loads,
    # raises ValueError, and the existing `except` returns None regardless. No
    # test can catch a mutation that changes no observable behaviour, and
    # chasing one would have meant weakening a test to make a tool happy.
    #
    # The claim the test actually makes is "skipped, NOT an error" -- so the
    # mutation that matters is one that raises.
    # `parse_line` has TWO independent skip mechanisms -- the `startswith("{")`
    # guard and the try/except round json.loads -- and they MUTUALLY MASK each
    # other. Removing either alone is survivable, so each needs its own
    # mutation with the honest expectation of which test dies:
    (
        "parse_line raises on malformed JSON instead of skipping",
        "    try:\n        parsed = json.loads(text)\n"
        "    except (ValueError, TypeError):\n        return None",
        "    parsed = json.loads(text)",
        "test_malformed_json_is_skipped_not_raised",
    ),
    (
        "BOTH skip mechanisms removed -- preamble now raises",
        '    if not text.startswith("{"):\n        return None\n'
        "    try:\n        parsed = json.loads(text)\n"
        "    except (ValueError, TypeError):\n        return None",
        "    parsed = json.loads(text)",
        "test_preamble_line_is_skipped_not_an_error",
    ),
    # The two bugs the LIVE CAPTURE found on 2026-08-06. Both were invisible
    # to the offline suite because the hand-written envelopes encoded the
    # probe artefact's prose, and the prose was wrong.
    (
        "lifecycle fields read FLAT again (the nesting bug)",
        '    event = payload.get("event")\n'
        "    return event if isinstance(event, dict) else {}",
        "    return payload",
        "test_the_two_failures_are_captured_with_their_reasons",
    ),
    (
        "scheduled drops the tool: discriminator (invents model-task calls)",
        "            if not key.startswith(_CALL_ID_PREFIX):\n"
        "                return []",
        "            pass",
        "test_exactly_one_tool_call_started",
    ),
    (
        "muse_kind always says execute",
        '    return _MUSE_KINDS.get(task_kind, "other")',
        '    return "execute"',
        "test_unknown_task_kind_falls_back_to_other",
    ),
]


if __name__ == "__main__":
    raise SystemExit(sweep(TARGET, TEST_FILE, MUTATIONS))
