# TranscriptFlusher stale tool_name/kind Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** A cursor-acp (and gemini-acp/grok-acp) tool-call's transcript row in `arbmem:trace`
should show the enriched, up-to-date tool label (e.g. the edited file's path once known) instead
of freezing at the tool call's initial, generic label — and cursor-acp's raw ACP tool sub-kind
(`"edit"`/`"execute"`/`"search"`) must stop colliding with the bridge's rendering-discriminator
`kind` field, which is the actual mechanism behind the blank `⏺` bullets originally observed in
a live dispatch.

**Architecture:** Two independent fixes, both already fully specified and two-round panel
reviewed (read `docs/superpowers/specs/2026-07-02-transcript-flusher-stale-tool-name-design.md`
in full before starting — this plan implements it exactly, including every `<!-- r1: ... -->` /
`<!-- r2: ... -->` marker's resolution, not just the final-state prose):

1. **Fix 1** (`transcript_flusher.py`): `TranscriptFlusher._process()`'s coalescing logic
   updates `pending.tool_name` from later same-`item_id` events when they carry a non-empty
   value. `pending.kind` is explicitly and deliberately left untouched — an earlier version of
   this fix updated `kind` too and a 4-seat panel found that unsafe (it would have flipped every
   coalescing engine's tool-call row from a tool-name header to an output body in
   `tools/arb-watch-go`'s renderer, breaking an existing test). Do not reintroduce that.
2. **Fix 2** (`cursor_acp.py`, `gemini_acp.py`, `grok_acp.py`): each engine's
   `normalize_session_update()` currently sets `data["kind"] = update.get("kind")` for
   `tool_call`/`tool_call_update` events, propagating the ACP server's raw tool sub-kind into a
   field the bridge/flusher/Go-renderer contractually treat as "bridge-semantic lifecycle kind."
   Remove that assignment entirely (omit the key — do not set it to `None`, those are NOT
   equivalent at the pre-`_capture()` observation point, see spec Fix 2's r2 marker) in all
   three engines. `codex.py` needs no change — its kind values are already hardcoded
   bridge-semantic strings, never a server passthrough.

**Tech Stack:** Python 3, `pytest`, existing test conventions in `tests/test_transcript_flusher.py`
(`FakeTraceRedis`, the `_item()` helper), `tests/test_cursor_acp.py`/`test_gemini_acp.py`/
`test_grok_acp.py` (each engine's existing `normalize_session_update` test classes), and
`tests/test_engine_progress_schema.py` (the shared cross-engine test both fixes intersect with).

## Global Constraints

- Read `docs/superpowers/specs/2026-07-02-transcript-flusher-stale-tool-name-design.md` in full
  before starting — it is panel-reviewed twice (codex + agy-print + cold-Opus + pi-GLM, both
  rounds) and this plan implements it exactly.
- **Do not update `pending.kind` in `TranscriptFlusher._process()`.** This was tried, found
  unsafe by a unanimous 4-seat panel (round 1), and explicitly reverted in the spec. If you find
  yourself writing `pending.kind = kind` inside the `else` branch, stop — re-read spec Fix 1's
  r1 marker.
- **Omit `data["kind"]` entirely in the three engines' fix — do not set it to `None`.** These
  produce the same *final* `item["kind"]` (via `_capture()`'s own separate fallback), but differ
  at the pre-`_capture()` engine-emitted-progress-event level, which an existing test
  (`tests/test_engine_progress_schema.py`) directly inspects. Only omission is correct.
- **`codex.py` is out of scope** — confirmed genuinely unaffected by both panel rounds. Do not
  touch it.
- **`tests/test_engine_progress_schema.py`'s `test_acp_call_sites_inject_turn_item_kind_and_seq`
  (`:198-248`) WILL need its expected value changed** (from `kind="execute"` to
  `kind="command_started"`, for all three looped engine cases) as a direct, required consequence
  of Fix 2 — this is not an optional cleanup, the fix is incomplete without it (the test would
  fail against a correct implementation otherwise, silently signaling implementation-in-progress
  rather than done).
- Every new/modified test must assert on the actual data structures produced (returned
  `TurnResult`/event tuples, `arbmem:trace` fields via `FakeTraceRedis.xadds`), not on internal
  state inspected directly — matching this codebase's established discipline (see the
  `2026-07-01-cold-opus-run-id-label` plan's identical constraint, same reasoning).

---

### Task 1: Fix 1 — `tool_name`-only coalescing update in `TranscriptFlusher._process()`

**Files:**
- Modify: `src/agent_redis_bridge/transcript_flusher.py` (`_process()`, the `else` branch of the
  `if pending is None:` check — see spec Fix 1 for the exact code shape)
- Modify: `tests/test_transcript_flusher.py` (add tests; likely extend the `_item()` helper to
  accept a `tool_name`/`command` override, or construct raw dicts inline — match the file's
  existing convention)

**Interfaces:**
- Consumes: `_PendingItem` (dataclass, unchanged — `tool_name`/`kind`/`chunks`/etc. fields
  already exist), `TranscriptFlusher._process(item: dict)` (existing method, this task edits its
  body only).
- Produces: no new public interface — this is a pure behavior fix inside an existing private
  method.

**Steps:**

1. Write the failing regression-guard test first: two `_process()` calls sharing one `item_id`
   — first with a generic `tool_name`/`command`, second with a richer one and no `kind` — flush
   and assert the written row's `tool_name` is the richer value. Confirm it fails against
   current code (frozen at first value).
2. Write the guard-against-regression test: same shape, but the second event's `tool_name`/
   `command` is empty — assert the flushed row keeps the first event's value (not blanked).
3. Write the `kind`-preservation regression guard (the actual P0 test from spec round 1): shape
   the test item the way `_capture()` really produces it — first event `kind:
   "command_started"` (or `"edit"`), second event (same `item_id`) with a **non-empty**
   `"command_finished"` top-level `kind` (matching production — NOT an empty/synthetic one) —
   assert the flushed row's `kind` stays exactly the *first* event's value.
4. Implement the fix in `_process()`: in the `else` branch (pending already exists), compute
   `latest_tool_name = str(data.get("tool_name") or data.get("command") or "")`; if truthy,
   `pending.tool_name = latest_tool_name`. Do not touch `pending.kind` anywhere in this branch.
5. Run the three new tests — confirm all pass now.
6. Run the full existing `tests/test_transcript_flusher.py` suite — confirm every existing test
   still passes unmodified, **especially** anything touching `test_flusher_interleaved_item_id_
   splits_into_ordered_rows` (the interleaving test) — the fix must be orthogonal to it.
7. Run `tests/e2e_transcript_roundtrip.py` — confirm the existing `kind == "command_started"`
   assertion on a coalesced row (around `:323-328`) still passes. This is the direct regression
   guard against reintroducing the round-1 P0. If it fails, you've reintroduced the kind-update
   bug — stop and re-check step 4.
8. Run `tests/test_transcript_hotpath.py` — confirm unaffected (grep it first for any
   `tool_name`/`kind` assumption before assuming so).

**Acceptance:** all four test files above pass; the new tests fail against a version of the code
with `pending.kind = kind` added back into the `else` branch (a quick manual sanity check, not a
permanent test — confirms the guard is real).

---

### Task 2: Fix 2 — remove raw ACP `kind` passthrough in `cursor_acp.py`, `gemini_acp.py`,
`grok_acp.py`

**Files:**
- Modify: `src/agent_redis_bridge/engines/cursor_acp.py` (`normalize_session_update()`, the
  `tool_call`/`tool_call_update` branch — remove the `"kind": update.get("kind")` entry, around
  `:593`)
- Modify: `src/agent_redis_bridge/engines/gemini_acp.py` (same shape, around `:356`)
- Modify: `src/agent_redis_bridge/engines/grok_acp.py` (same shape, around `:428`)
- Modify: `tests/test_cursor_acp.py`, `tests/test_gemini_acp.py`, `tests/test_grok_acp.py` (each
  engine's existing `normalize_session_update`/`tool_call` tests — check whether any currently
  assert on the raw `data["kind"]` value and update if so; add a new test per engine confirming
  the key is absent). <!-- plan-r1 (P2, codex + agy-print independently): --> **Known required
  update, not just "check":** `tests/test_gemini_acp.py:141-151`
  (`test_tool_updates_normalize_to_command_events`) currently has exact expected tuples for
  both the start and completion events, each including a literal `"kind": None` — these must be
  updated by removing that key-value pair entirely (not changed to omit-equivalent — the dict
  literal itself must drop the key) once Fix 2 lands, or this test will fail.
- <!-- plan-r1 (P2, codex): --> Modify: `tests/test_engine_progress_schema.py` (added explicitly
  to this task's file list — see step 5 below; already required by the spec, just previously
  missing from this list, which an implementor following file lists mechanically could miss).

**Interfaces:**
- Consumes: each engine's existing `normalize_session_update(update, tool_titles)` function
  signature — unchanged.
- Produces: the returned `(event_name, data)` tuple's `data` dict no longer contains a `"kind"`
  key for `tool_call`/`tool_call_update` events, in all three engines.

**Steps:**

1. For **cursor_acp.py**: write a failing test asserting `data` (the second element of
   `normalize_session_update()`'s return tuple) has no `"kind"` key, for **both** a `tool_call`
   start event with a raw ACP `kind: "edit"` in the input **and** a `tool_call_update`
   completion event (matching the spec's testing section, which asks for both, not just start
   — cheap to cover both since the code change is one line covering both paths). Confirm both
   fail against current code.
2. Remove the `"kind": update.get("kind")` line from `cursor_acp.py`'s `tool_call`/
   `tool_call_update` data-construction block. Confirm the tests from step 1 pass.
3. Repeat steps 1-2 for **gemini_acp.py** and **grok_acp.py** — same test shape, same fix shape,
   each in its own file.
4. For each of the three engines, write (or extend) an end-to-end-shaped test: construct the
   event the way `handle_progress`/`bridge.py`'s `_capture()` would actually consume it (i.e.
   pass the engine's real returned `data` dict through `_capture()`'s resolution logic, or a
   fixture that mirrors it), and assert `item["kind"]` resolves to `"command_started"`/
   `"command_finished"` — not empty, not the raw sub-kind. Do this for at least cursor-acp and
   one of gemini-acp/grok-acp (per the spec's testing section — full coverage for all three is
   good but not mandatory if time-constrained; note in your final report which you covered).
5. Update `tests/test_engine_progress_schema.py`'s `test_acp_call_sites_inject_turn_item_kind_
   and_seq` (`:198-248`): change the expected `kind` from `"execute"` to `"command_started"` for
   all three looped engine cases (Gemini/Cursor/Grok). This is required, not optional — see
   Global Constraints. Confirm the test passes after Fix 2 lands for all three engines (it
   should fail if you've only fixed one or two).
6. Write the full-chain test (Fix 1 + Fix 2 combined, cursor-acp specifically — this is the
   spec's stated real acceptance criterion): a cursor-acp start event (no `kind` collision,
   post-Fix-2) followed by a completion event carrying the enriched `tool_name`/`path` (post
   the earlier, already-merged edit-path fix) — assert the final coalesced row (via
   `TranscriptFlusher`) has both `kind == "command_started"` and `tool_name` containing the
   path.

**Acceptance:** all three engines' `normalize_session_update()` no longer emit a `"kind"` key
for tool-call events; `tests/test_engine_progress_schema.py` passes with updated expectations;
the full-chain test proves the combined fix produces a renderable, informative transcript line.

---

### Task 3: Full verification and live check

**Files:** none (verification only)

**Steps:**

1. Run the complete test suite relevant to this change:
   `PYTHONPATH=src <venv-python> -m pytest tests/test_transcript_flusher.py
   tests/test_transcript_hotpath.py tests/e2e_transcript_roundtrip.py tests/test_cursor_acp.py
   tests/test_gemini_acp.py tests/test_grok_acp.py tests/test_engine_progress_schema.py -v` —
   confirm 100% pass, paste the actual output in your final report (not just a claim).
2. Run the broader suite (excluding the known pre-existing, unrelated `tests/arb_files`/
   `tests/arb_email` module-collision — see prior work in this repo's history for that context)
   to catch any unexpected cross-cutting regression:
   `PYTHONPATH=src <venv-python> -m pytest tests/ -q --ignore=tests/arb_files
   --ignore=tests/arb_email`.
3. Report both outputs verbatim in your final reply.

**Note for the orchestrator (not the implementor):** live verification (re-dispatching a real
cursor-acp task and confirming the `arbmem:trace` row + the Go renderer's actual output) happens
after this plan's work is merged, using the same precise-Python-client-read discipline
established earlier in this investigation — not part of this implementation task.
