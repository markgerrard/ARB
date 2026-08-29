# Cold-Opus subagent run-id labeling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a cold-Opus reviewer subagent's `run_id` (the label arb-watch's Run column renders)
come from an `[ARB_RUN:...]` marker embedded in its prompt, instead of always showing the raw
`agent_id` GUID — without touching `seat_id` or `orchestrator`, which are correct today and must
stay exactly as they are.

**Architecture:** One method, `TranscriptTailer._resolve_cold_identity()` in
`src/agent_redis_bridge/claude_tail/tailer.py`, currently returns immediately when
`self._identity_locked` is `True` — before ever consulting `self.first_user_marker` (which is
already parsed, unconditionally, by `_capture_first_user_marker()` one line earlier in the call
chain, and simply discarded). The fix adds one branch inside that early-return: if a marker was
found and it has a `run_id`, patch `self.identity.run_id` from it via `dataclasses.replace`,
leaving every other field untouched. No hook, sidecar schema, or `service.py` change.

**Tech Stack:** Python 3, `pytest`, the existing `agent_redis_bridge.claude_tail` package
conventions (`FakeRedis`, `_write_jsonl`, `Identity` dataclass — all already in
`tests/claude_tail/test_tailer.py`, reused as-is, not reinvented).

## Global Constraints

- Read `docs/superpowers/specs/2026-07-01-cold-opus-run-id-label-design.md` in full before
  starting — it is panel-reviewed (codex + agy-print + cold-Opus, round 1, APPROVE WITH NOTES)
  and this plan implements it exactly, including both panel-found fixes (the `replace` import,
  and the existing test that needs its expectation updated, not just new tests added).
- Do not touch `src/agent_redis_bridge/claude_tail/service.py`, `scripts/claude_tail_hooks/*.py`,
  or the `.arb-tail.json` sidecar schema — this fix is scoped to `tailer.py` only, by design (see
  spec § Architecture for why a hook-side or service.py-side fix was rejected in favor of this
  smaller surface).
- Only `identity.run_id` may change when `identity_locked` is `True`. `seat_id` and `orchestrator`
  must be provably unchanged in every test that exercises the locked path — this is not
  incidental, it's the whole point of the design (see spec § "Only run_id is patched").
- Every new/modified test must assert on the *emitted* identity (the fields actually written to
  the fake Redis client via `poll()`), not on internal tailer state inspected directly — the
  2026-06-30 feature's round-4 postmortem found a tautological test that asserted internal state
  matching a buggy formula instead of checking real output. Don't repeat that mistake.

---

### Task 1: Patch `_resolve_cold_identity` to consume the already-parsed marker's `run_id`

**Files:**
- Modify: `src/agent_redis_bridge/claude_tail/tailer.py:1-13` (imports), `tailer.py:175-181`
  (`_resolve_cold_identity`)
- Modify: `tests/claude_tail/test_tailer.py:200-229`
  (rename + update `test_locked_cold_identity_is_not_overridden_by_a_later_marker`)
- Modify: `tests/claude_tail/test_tailer.py` (add two new test functions, placed directly after
  the renamed test from this task)

**Interfaces:**
- Consumes: `Identity` (frozen dataclass, `run_id`/`task_id`/`seat_id`/`orchestrator` — already
  defined in `src/agent_redis_bridge/claude_tail/identity.py`, unchanged by this task).
  `TranscriptTailer.first_user_marker: dict[str, str] | None` (already set by
  `_capture_first_user_marker()`, an existing method this task does not modify) — keys `run_id`,
  `seat_id`, `orchestrator`, always all three present together when the dict is not `None`
  (`identity.py`'s `parse_marker` returns them as a set or not at all).
- Produces: `TranscriptTailer._resolve_cold_identity()`'s locked branch now mutates
  `self.identity` (via `dataclasses.replace`, keeping `task_id`/`seat_id`/`orchestrator`) when a
  marker with a `run_id` was captured on the transcript's first user line. No new public method or
  attribute — later tasks (there are none planned beyond this) would consume `tailer.identity` the
  same way every existing caller already does.

- [ ] **Step 1: Rename and update the existing test to assert the new (intentional) behavior**

Replace the existing `test_locked_cold_identity_is_not_overridden_by_a_later_marker` function
(currently at `tests/claude_tail/test_tailer.py:200-229`) with this renamed version. Only the
function name and the final three assertions change — the setup is identical:

```python
def test_locked_cold_identity_run_id_is_overridden_by_marker_but_seat_and_orchestrator_are_not(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "user", "message": {"content": "[ARB_RUN:run-1 ARB_SEAT:cold-seat-1 ARB_ORCH:warm-orch] review this"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "first"}]}},
    )
    redis = FakeRedis()
    locked_identity = Identity(run_id="sess-x", task_id="agent-1", seat_id="cold-opus-agent-1", orchestrator="claude-bridge-dev")
    tailer = TranscriptTailer(
        str(transcript),
        locked_identity,
        OffsetStore(redis, "p:"),
        live_redis=redis,
        trace_redis=redis,
        prefix="agent_scratch:",
        redactor=_redactor,
        cold_agent_id="agent-1",
        cold_session_id="sess-x",
        identity_locked=True,
    )

    tailer.poll()

    live = [fields for key, fields, _ in redis.xadds if key.endswith("events:live")]
    # The marker's run_id (run-1) DOES override the locked run_id (sess-x) -- that's this fix's
    # whole purpose. seat_id and orchestrator stay locked regardless (the marker's seat-1/warm-orch
    # values are deliberately ignored -- see spec "Only run_id is patched").
    assert {fields["run_id"] for fields in live} == {"run-1"}
    assert {fields["seat_id"] for fields in live} == {"cold-opus-agent-1"}
    assert {fields["orchestrator"] for fields in live} == {"claude-bridge-dev"}
```

- [ ] **Step 2: Add a regression guard for the (overwhelmingly common) no-marker locked case**

Add this new test directly after the one from Step 1:

```python
def test_locked_cold_identity_without_marker_is_fully_unchanged(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {"type": "user", "message": {"content": "please review this diff, no marker here"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
    )
    redis = FakeRedis()
    locked_identity = Identity(run_id="sess-x", task_id="agent-1", seat_id="cold-opus-agent-1", orchestrator="claude-bridge-dev")
    tailer = TranscriptTailer(
        str(transcript),
        locked_identity,
        OffsetStore(redis, "p:"),
        live_redis=redis,
        trace_redis=redis,
        prefix="agent_scratch:",
        redactor=_redactor,
        cold_agent_id="agent-1",
        cold_session_id="sess-x",
        identity_locked=True,
    )

    tailer.poll()

    live = [fields for key, fields, _ in redis.xadds if key.endswith("events:live")]
    assert {fields["run_id"] for fields in live} == {"sess-x"}
    assert {fields["seat_id"] for fields in live} == {"cold-opus-agent-1"}
    assert {fields["orchestrator"] for fields in live} == {"claude-bridge-dev"}
```

- [ ] **Step 3: Add a test for a marker placed mid-paragraph, not at the line's literal start**

Add this new test directly after the one from Step 2 (panel round 1 flagged that the spec's own
live repro happened to put the marker first, which doesn't distinguish `re.search` from
`re.match` — this test closes that gap):

```python
def test_locked_cold_identity_marker_mid_paragraph_still_overrides_run_id(tmp_path):
    transcript = tmp_path / "s.jsonl"
    _write_jsonl(
        transcript,
        {
            "type": "user",
            "message": {
                "content": (
                    "You are reviewing a diff. Context first, then the tag: "
                    "[ARB_RUN:mid-label ARB_SEAT:cold-seat-1 ARB_ORCH:warm-orch] "
                    "now go read the files."
                )
            },
        },
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "first"}]}},
    )
    redis = FakeRedis()
    locked_identity = Identity(run_id="sess-x", task_id="agent-1", seat_id="cold-opus-agent-1", orchestrator="claude-bridge-dev")
    tailer = TranscriptTailer(
        str(transcript),
        locked_identity,
        OffsetStore(redis, "p:"),
        live_redis=redis,
        trace_redis=redis,
        prefix="agent_scratch:",
        redactor=_redactor,
        cold_agent_id="agent-1",
        cold_session_id="sess-x",
        identity_locked=True,
    )

    tailer.poll()

    live = [fields for key, fields, _ in redis.xadds if key.endswith("events:live")]
    assert {fields["run_id"] for fields in live} == {"mid-label"}
    assert {fields["seat_id"] for fields in live} == {"cold-opus-agent-1"}
    assert {fields["orchestrator"] for fields in live} == {"claude-bridge-dev"}
```

- [ ] **Step 4: Run the tests to verify the two behavior tests fail and the regression guard passes**

Run: `.venv/bin/python3 -m pytest tests/claude_tail/test_tailer.py -v -k "test_locked_cold_identity"`

Expected:
```
test_locked_cold_identity_run_id_is_overridden_by_marker_but_seat_and_orchestrator_are_not FAILED
  (AssertionError: assert {'sess-x'} == {'run-1'} -- current code keeps the locked run_id)
test_locked_cold_identity_without_marker_is_fully_unchanged PASSED
  (nothing to fix for the no-marker case -- this is a sanity check, not a red test)
test_locked_cold_identity_marker_mid_paragraph_still_overrides_run_id FAILED
  (same reason as the first failure)
```
If `test_locked_cold_identity_without_marker_is_fully_unchanged` does NOT pass at this point,
stop — that means the no-marker locked path is already broken independent of this fix, and that
must be investigated before proceeding (do not proceed with a red baseline you don't understand).

- [ ] **Step 5: Add the missing import**

In `src/agent_redis_bridge/claude_tail/tailer.py`, the current imports (lines 1-13) are:

```python
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import time
from typing import Any, Callable

from .identity import Identity, cold_identity, has_done_marker, parse_marker
from .lifecycle import Lifecycle
from .mapper import DriftError, Event, map_line
from .offset import OffsetStore, offset_key
from ..visibility_tee import live_tee, trace_tee
```

Change to (adding one line after `from __future__ import annotations`):

```python
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import os
import time
from typing import Any, Callable

from .identity import Identity, cold_identity, has_done_marker, parse_marker
from .lifecycle import Lifecycle
from .mapper import DriftError, Event, map_line
from .offset import OffsetStore, offset_key
from ..visibility_tee import live_tee, trace_tee
```

- [ ] **Step 6: Patch `_resolve_cold_identity`**

In the same file, `_resolve_cold_identity` currently reads (around line 175):

```python
    def _resolve_cold_identity(self, marker_text: str) -> None:
        if self._identity_locked:
            return
        if self._cold_agent_id is None or self._cold_session_id is None:
            return
        self.identity = cold_identity(self._cold_agent_id, self._cold_session_id, marker_text)
        self._identity_resolved = True
```

Change the first branch to:

```python
    def _resolve_cold_identity(self, marker_text: str) -> None:
        if self._identity_locked:
            if self.first_user_marker and self.first_user_marker.get("run_id"):
                self.identity = replace(self.identity, run_id=self.first_user_marker["run_id"])
            return
        if self._cold_agent_id is None or self._cold_session_id is None:
            return
        self.identity = cold_identity(self._cold_agent_id, self._cold_session_id, marker_text)
        self._identity_resolved = True
```

- [ ] **Step 7: Run the same three tests again to verify they all pass**

Run: `.venv/bin/python3 -m pytest tests/claude_tail/test_tailer.py -v -k "test_locked_cold_identity"`

Expected: all three `PASSED`.

- [ ] **Step 8: Run the full claude_tail suite to confirm no regressions elsewhere**

Run: `.venv/bin/python3 -m pytest tests/claude_tail/ -v`

Expected: `107 passed` (105 tests currently pass on this branch before Task 1; this task adds two
new tests — the mid-paragraph marker test and the no-marker regression guard — and renames one
existing test without adding or removing a test, so the net count is 105 + 2 = 107). If the count
is anything other than 107, stop and investigate before committing.

- [ ] **Step 9: Commit**

```bash
git add src/agent_redis_bridge/claude_tail/tailer.py tests/claude_tail/test_tailer.py
git commit -m "$(cat <<'EOF'
fix(claude-tail): cold-Opus subagents can carry a run-id label

_resolve_cold_identity() returned immediately once identity_locked, before ever
consulting self.first_user_marker -- already parsed unconditionally one call
earlier and simply discarded. A cold-Opus subagent's [ARB_RUN:...] marker
therefore had zero effect on its run_id in arb-watch, always showing the raw
agent_id GUID instead.

Patches only identity.run_id from the marker when locked; seat_id and
orchestrator are deliberately left untouched (seat_id stays cold-opus-<agent_id>
for the Go frontend's dedup/opus-filter invariants; orchestrator already comes
from the sidecar's parent-registry lookup, more reliable than a caller-typed
marker string). No hook, sidecar, or service.py change.

Panel-reviewed (codex + agy-print + cold-Opus, round 1, APPROVE WITH NOTES):
unanimous on the design; unanimous finding that this needs `from dataclasses
import replace` (tailer.py had no such import); agy-print additionally caught
that the existing test asserting the old (never-overridden) behavior needed its
run_id expectation updated, not just new tests added alongside it.

See docs/superpowers/specs/2026-07-01-cold-opus-run-id-label-design.md.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01BTwpGsSa89Bp8vzzP2h6Hh
EOF
)"
```

---

## Post-merge live verification (orchestrator step, not part of Task 1 — cannot be executed by a dispatched subagent)

A dispatched implementor (codex, or any `agent-dispatch`/`go-client` seat) cannot spawn a native
Claude Code `Agent`/Task-tool subagent — only the orchestrating Claude Code session driving this
plan can. Once Task 1 is merged to the branch the orchestrator is running from, the orchestrator
must:

1. Spawn a real `code-reviewer-report-writer` subagent (via the `Agent` tool) whose prompt's
   **first line** is `[ARB_RUN:<a-chosen-label> ARB_SEAT:x ARB_ORCH:y]` followed by a real
   (small) review task that uses at least one tool call — reproduces the exact repro that found
   this gap (see memory `cold-opus-run-id-unreachable`).
2. Query `events:live` on the managed Valkey (`ARB_LIVE_REDIS_URL` from
   `envs/agent-redis-bridge-dev.env`, key `agent_scratch:events:live`) for that seat's events and
   confirm: `run_id == "<a-chosen-label>"` (not the raw agent-id GUID), `orchestrator` correctly
   reflects the real parent session's seat id (not `y` — proving the marker's `ARB_ORCH` group is
   correctly ignored), and `seat_id` still starts with `cold-opus-`.
3. Confirm the daemon (`com.example.claude-tail.bridge-dev`) is running code that includes this fix —
   if it was started before this merge landed, `launchctl kickstart -k
   gui/$(id -u)/com.example.claude-tail.bridge-dev` (code-only change, kickstart not bootout/bootstrap
   — see memory `bridge-dev-fleet-launchd`).

## Self-review notes

- **Spec coverage:** § Architecture's exact snippet (including the `replace` import panel found)
  → Task 1 Steps 5-6. § Testing's four bullet points (marker override + regression guard +
  unlocked-untouched + mid-paragraph) → Task 1 Steps 1-3 (the unlocked-path bullet needs no new
  test — the spec itself says existing unlocked tests already cover it and must keep passing
  unmodified, which Step 8's full-suite run verifies). § Testing's "existing test requiring an
  update" → Task 1 Step 1. § "Live verification" → the Post-merge section above.
- **Placeholder scan:** no TBD/TODO; every step has literal file paths, literal code, and literal
  commands with expected output.
- **Type consistency:** `Identity` fields (`run_id`, `task_id`, `seat_id`, `orchestrator`) and
  `first_user_marker`'s dict keys (`run_id`, `seat_id`, `orchestrator`) match `identity.py`'s
  actual `parse_marker`/`Identity` definitions throughout — verified against source, not assumed.
