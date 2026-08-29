# Cold-Opus subagent run-id labeling (design)

> Status: design, **panel-reviewed once** 2026-07-01 (3-seat panel: codex + agy-print +
> cold-Opus, all independent). Filed after a live cold-Opus dogfood test (spawned during the
> dispatch run-id-discipline work) empirically confirmed the gap: a
> `[ARB_RUN:... ARB_SEAT:... ARB_ORCH:...]` marker embedded as the literal first line of a real
> subagent's prompt had **zero effect** on the seat's `run_id` in `events:live` — every event
> still carried the raw `agent_id` GUID. Filed as `docs/BACKLOG.md` → "Cold-Opus subagents can
> never get a run-id label in arb-watch"; memory `cold-opus-run-id-unreachable`. Builds directly
> on `docs/superpowers/specs/2026-06-30-cold-opus-subagent-visibility-design.md` (the feature
> this gap lives inside) — read that first; this spec assumes its `sidecar`/`identity_locked`
> mechanism as given and does not re-derive it.
>
> **Round 1 panel result — APPROVE WITH NOTES (unanimous on substance):** codex approved with
> notes; agy-print requested changes; cold-Opus approved with notes. All three independently
> verified the core design against real source (not spec prose) and found it correct — patching
> only `run_id`, preserving `seat_id`/`orchestrator`, reusing `first_user_marker` race-free, and
> leaving the unlocked path untouched. All three also independently found the same real gap: the
> snippet below used `replace()` without noting `tailer.py` doesn't yet import it from
> `dataclasses` (would `NameError` on first real use) — fixed inline below. Agy-print additionally
> found, and the orchestrator independently verified against `tests/claude_tail/test_tailer.py:
> 200-220`, that an **existing** test (`test_locked_cold_identity_is_not_overridden_by_a_later_
> marker`) explicitly asserts the pre-fix behavior (`run_id` stays locked even with a marker
> present) — this fix intentionally changes that outcome, so the test's `run_id` expectation must
> be updated from `"sess-x"` to `"run-1"` (its `seat_id`/`orchestrator` assertions are unaffected
> and stay as-is). Folded into § Testing below. No design-level objection from any seat.

## Problem

Bridge-dispatched seats (codex, agy-print, etc.) can carry a caller-chosen `--run-id` so
arb-watch's Run column shows a readable label instead of a raw task-id GUID (enforced as of
2026-07-01, see `dispatch-run-id-discipline` memory). Cold-Opus reviewer subagents (native
Claude Code Agent/Task tool) have **no equivalent** — there is no CLI flag for a subagent spawn
the way there is for `agent-dispatch`/`go-client`, and the one mechanism that predates all of
this (`identity.py`'s `[ARB_RUN:...]` marker convention) turns out to be unreachable for real
subagents. Every cold-Opus seat shows its raw `agent_id` in the Run column, with no way to label
it, even when the caller controls the subagent's prompt and could embed a marker in it.

## Root cause (verified empirically 2026-07-01, not just read from source)

`service.py:_discover_specs()`'s cold branch (added in the 2026-06-30 visibility feature) calls
`cold_identity(agent_id, session_id, "")` — an **empty** marker string — at *discovery* time,
then sets `identity_locked=True` on the `_TailerSpec` as soon as the `SubagentStart`-written
sidecar is found. Because `subagent_start.py` writes the sidecar *before* creating the `.output`
symlink (a deliberate ordering from the original design, to avoid a different race), the sidecar
is essentially always present by the time the daemon's poll loop first discovers the new
`.output` file — so `identity_locked=True` fires on the very first observation, before the
tailer has read a single byte of the subagent's actual transcript.

`tailer.py`'s `_resolve_cold_identity()` (the method that would otherwise parse a
`[ARB_RUN:...]` marker from the transcript's first user-turn text) has this as its first line:

```python
def _resolve_cold_identity(self, marker_text: str) -> None:
    if self._identity_locked:
        return
    ...
```

So once locked — which happens before any content is read — the marker is never consulted again,
for the rest of that seat's lifetime. This was intentional at the time: `identity_locked` exists
specifically so a real subagent's markerless prompt (the overwhelming common case) doesn't cause
`_resolve_cold_identity`'s no-marker fallback to stomp the sidecar-derived `orchestrator` back to
`""`. The lock is correct for `orchestrator`; it is accidentally also blocking `run_id`, which
the sidecar never had an opinion on in the first place.

**One layer up, the parsing already happens and is already thrown away.**
`_capture_first_user_marker()` (`tailer.py:139-154`) fires unconditionally on the transcript's
first `user`-type line — locked or not — and unconditionally sets
`self.first_user_marker = parse_marker(text)` *before* calling `self._resolve_cold_identity(text)`
on the same line. So by the time `_resolve_cold_identity` returns early because of the lock,
`self.first_user_marker` already holds the fully-parsed `{"run_id":..., "seat_id":...,
"orchestrator":...}` dict (or `None`, if no marker matched) — it's simply never read again after
that point. Confirmed on a real subagent transcript from this session
(`~/.claude/projects/-Users-mark-<workspace>/<session>/subagents/agent-a8bce9ddb028b02c2.jsonl`):
its first line is `type: user`, `message.content` starts with the literal marker text I embedded,
and `parse_marker()` would successfully extract `run_id=panel-cold-opus-livetest-20260701` from
it — the daemon simply never asks.

## Architecture

**Single change, entirely inside `tailer.py`'s `_resolve_cold_identity()`.** No hook change, no
sidecar schema change, no `service.py` change, no new file-read at `SubagentStart` time (and
therefore none of the file-existence-race risk that a hook-side read would introduce — the
transcript is read exactly where it already is, by the poller that already owns reading it
incrementally).

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

**`tailer.py` must add `from dataclasses import replace` to its imports** (found independently by
all three panelists — `tailer.py` currently has no such import; `service.py` already imports it
for its own unrelated `replace()` call, but that doesn't help `tailer.py`). Without it this snippet
raises `NameError` the first time a locked cold seat's first user line carries a marker.

`self.first_user_marker` is already guaranteed set (to a dict or `None`) by the time this runs,
because `_capture_first_user_marker()` assigns it on the line immediately before calling this
method (`tailer.py:152-154`) — no new capture logic needed, just consuming what already exists.

**Only `run_id` is patched — deliberately, not `seat_id` or `orchestrator`:**

- `orchestrator` already comes from the sidecar's parent-registry lookup (`service.py`), which is
  more reliable than a caller-typed string in a prompt and requires no marker at all. Overriding
  it from the marker would let a caller's (possibly wrong) `ARB_ORCH` value silently override
  ground truth the daemon already resolved correctly. Leave it alone.
- `seat_id` overriding was **explicitly rejected** in the 2026-06-30 design (§ "Why not a custom
  seat_id") for two concrete reasons that still apply unchanged: `tools/arb-watch-go/model.go`'s
  `dedupSeatRuns()` collapses rows sharing `(seat_id, run_id)`, so letting a caller pick an
  arbitrary `seat_id` risks collisions between concurrent cold-Opus reviewers; and
  `reduce.go`'s `agentOf()` special-cases the literal `cold-opus-` prefix for the "opus" filter.
  `seat_id` stays pinned to `cold-opus-<agent_id>` unconditionally, exactly as today.
- `run_id` has no such constraint — it's the one field arb-watch's Run column actually renders as
  the label (`runDisplay()`, `tools/arb-watch-go/model.go:1154-1168`), and multiple cold-Opus
  seats sharing one caller-chosen label is the whole point (grouping a panel of cold-Opus
  reviewers under one run, the same way bridge seats already group under a shared `--run-id`).

**Caller-facing contract, unchanged from the original (never-working) design:** embed
`[ARB_RUN:<label> ARB_SEAT:<anything> ARB_ORCH:<anything>]` as the literal first line of the
subagent's prompt when spawning via the `Agent` tool. Only the `ARB_RUN:` group is consumed by
this fix; `ARB_SEAT:`/`ARB_ORCH:` in the marker are parsed (by `parse_marker`, unchanged) but not
applied — harmless to include for symmetry with the bridge-seat convention, but callers should
not expect them to do anything. Markerless prompts (the default, everything before this fix)
behave exactly as today: raw-GUID `run_id`, `cold-opus-<agent_id>` seat_id, sidecar orchestrator.

## Testing

- `tests/claude_tail/test_tailer.py` (extend):
  - A **locked** cold identity whose transcript's first user line carries
    `[ARB_RUN:my-label ...]` → after that line is processed, `tailer.identity.run_id ==
    "my-label"`, `tailer.identity.seat_id` and `tailer.identity.orchestrator` **unchanged** from
    their locked (sidecar-derived) values. Assert on the *emitted* identity after a real
    `poll()`/`_process_line()` call, not just internal state — the round-4 postmortem on the
    original feature flagged exactly this class of tautological-test risk.
  - A **locked** cold identity whose first user line carries **no** marker → `run_id`/`seat_id`/
    `orchestrator` all unchanged from today's behavior (raw GUID) — explicit regression guard,
    since this is the overwhelmingly common real-world case and must not regress.
  - An **unlocked** cold identity (no sidecar — the pre-hook, pre-2026-06-30 marker convention)
    is untouched by this change at all — existing tests for that path must still pass unmodified.
  - A marker placed **mid-paragraph** in the first user line (not just at its literal start) still
    yields the same `run_id` patch — `parse_marker` uses `re.search`, not `re.match` (panel round 1,
    found independently: this spec's own live repro happened to put the marker first, which
    doesn't exercise the `search`-vs-`match` distinction).
- **Existing test requiring an update (panel round 1, found by agy-print, independently verified
  by the orchestrator against source):** `tests/claude_tail/test_tailer.py:200-220`,
  `test_locked_cold_identity_is_not_overridden_by_a_later_marker`, currently asserts `run_id ==
  "sess-x"` (the locked value) even though its transcript's first user line carries
  `[ARB_RUN:run-1 ...]` — i.e. it explicitly pins the *pre-fix* behavior this spec intentionally
  changes. Update its `run_id` assertion to expect `"run-1"`; its `seat_id`/`orchestrator`
  assertions (`cold-opus-agent-1` / `claude-bridge-dev`) are unaffected and must still pass
  unchanged — this test becomes the direct regression guard that `seat_id`/`orchestrator` survive
  the marker override while `run_id` doesn't.
- **Live verification**: spawn a real `code-reviewer-report-writer` subagent with
  `[ARB_RUN:<label> ARB_SEAT:x ARB_ORCH:y]` as the first line of its prompt (the exact
  reproduction used to find this gap) and confirm `events:live` shows `run_id=<label>` for that
  seat throughout its lifecycle, while `orchestrator` still correctly reflects the real parent
  session (not `y`) and `seat_id` still starts with `cold-opus-`.

## Risks / open questions

1. **Marker must be the literal first line of the prompt.** `_capture_first_user_marker()` only
   ever inspects the transcript's first `user`-type line (`_first_user_seen` guard) — a marker
   appended later, or buried mid-prompt behind other text in that same first line, will not be
   found by `parse_marker`'s regex unless it's a substring `re.search` match anywhere in that
   line's text (it is — `_MARKER_RE.search`, not `.match` — so the marker doesn't need to be at
   the very start of the line, only within the first user-turn's text). Confirm this during
   implementation with a marker placed mid-paragraph, not just at the literal start (this spec's
   own live-test happened to put it first, which doesn't exercise the `search`-vs-`match`
   distinction).
2. **No enforcement that a real dispatch pipeline actually embeds the marker.** Unlike
   `agent-dispatch`/`go-client` (which now hard-refuse a missing `--run-id`/`--adhoc`), there is
   no equivalent gate for a cold-Opus `Agent` tool call — the caller (a Claude Code orchestrator
   session) can simply forget to embed the marker, same as the original dispatch-discipline gap
   this session found for bridge seats. Out of scope for this fix (which only makes the marker
   *work*, it doesn't enforce its use) — worth a follow-up memory/skill note once this ships, the
   same way `dispatch-run-id-discipline` was written up for the bridge-seat case.
3. **`first_user_marker` is per-tailer, in-memory state** — if a tailer is ever reconstructed
   mid-lifecycle for a cold seat already past its first line (shouldn't currently happen — cold
   tailers are created once at discovery and live for the seat's lifetime — but worth confirming
   no code path re-creates a `TranscriptTailer` for an already-partially-tailed cold `.output`
   file without replaying its first line first).
