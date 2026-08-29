# `TranscriptFlusher` stale `tool_name`/`kind` on late-arriving tool-call detail (design)

> Status: design, **round 2 complete** 2026-07-02 (4-seat panel: codex contributor + cold-Opus +
> agy-print + pi-GLM judgment seat, all independent, both rounds). **Round 1 result — REVISE,
> unanimous 4/4:** all four traced the same P0 (a proposed `pending.kind` update was based on a
> false premise about `bridge.py`'s `_capture()` fallback and would have flipped every
> coalescing engine's tool-call row from a tool-name header to an output body — confirmed
> against an existing passing test); cold-Opus additionally found cursor-acp's raw ACP `kind`
> values were never in the Go renderer's recognized set to begin with — the actual mechanism
> behind the originally-observed blank bullets, not fixed by the tool-name-only correction
> alone. Both incorporated as Fix 1 (tool_name-only) and Fix 2 (kind-collision removal).
> **Round 2 result — REVISE→addressed, converging on APPROVE-class verdicts (2 APPROVE-WITH-
> NOTES, 1 APPROVE, 1 REVISE — all four substantively agreeing on the same finding, differing
> only in severity calibration):** codex and cold-Opus independently found Fix 2's "cursor-acp
> only" scoping didn't hold — `gemini_acp.py`/`grok_acp.py` have byte-identical raw-kind
> passthrough code, proven by an existing shared test
> (`tests/test_engine_progress_schema.py:198-248`) that already asserts identical behavior
> across all three engines. Given gemini-acp is a live deployed seat and the fix is a
> near-zero-cost one-line-per-engine removal, Fix 2 is now widened to all three ACP engines.
> Codex also caught a real ambiguity (omit-the-key vs. set-to-`None` are NOT equivalent at the
> pre-`_capture()` observation point) and an existing test that Fix 2 would silently break if
> not explicitly updated. Both resolved. See inline `<!-- r1: ... -->` / `<!-- r2: ... -->`
> markers below for exactly what changed, when, and why. No reviewer required a third round.
>
> **Blast radius note (why this gets full panel treatment, not a quick patch):**
> `src/agent_redis_bridge/transcript_flusher.py`'s `TranscriptFlusher` is shared, engine-agnostic
> code sitting downstream of every bridge engine's progress events (`codex`, `agy-print`,
> `agy-tmux`, `gemini-acp`, `grok-acp`, `kimi-code-acp`, `mini-agent-acp`, `pi-rpc`, `pi-sdk`,
> `agent-sdk`, `cursor-acp` — the full `ENGINE_TO_TOOL` list). A fix here changes what every
> engine's tool-call transcript row (`arbmem:trace`, consumed by `tools/arb-watch-go`'s
> `renderTranscriptLine` and any other transcript viewer) looks like, not just cursor-acp's. This
> is exactly the class of change `AGENTS.md`'s "protected instruction files" caution and general
> shared-code discipline calls for extra scrutiny on, even though `transcript_flusher.py` isn't
> literally on that protected list — the *reason* for the caution (broad, hard-to-fully-audit
> blast radius) applies the same way.

## Problem

While live-verifying a `cursor-acp` fix that surfaces a file-edit's path/diff on tool-call
*completion* (merged separately, documented in a follow-up review brief not included in this
copy), inspecting the resulting `arbmem:trace` row
for a real dispatch showed the transcript still rendering the tool call's *original, generic*
label — not the enriched one. Confirmed via a precise Python client read (not a `redis-cli`
text-parsing misread, which initially looked like a different, blanker symptom):

```python
{'item_id': 'tool_6f7b8092-...', 'kind': 'edit', 'tool_name': 'Edit File', 'content': '', ...}
```

— `tool_name` is `"Edit File"`, the pre-completion generic title, even though the live
`events:live` event for the *same tool call's completion* correctly carries the enriched
`"Edit File: /path/to/farewell.py"` and a structured `path` field (verified directly against
that stream first, ruling out the cursor-acp engine fix itself as the culprit).

### Root cause

`TranscriptFlusher._process()` (`transcript_flusher.py:127-180`) coalesces every progress event
sharing the same `item_id` (ACP's `tool_call_id`, or the engine-derived equivalent) into one
pending row, buffering `content`/`delta` text as it streams in. But `tool_name` (and `kind`) are
only ever set **once**, inside the `if pending is None:` branch that only runs on the very first
event seen for a given `item_id`:

```python
pending = self._pending.get(key)
if pending is None:
    pending = _PendingItem(
        ...,
        kind=kind,
        tool_name=str(data.get("tool_name") or data.get("command") or ""),
        chunks=[],
    )
    self._pending[key] = pending
pending.chunks.append(text)
```

Every subsequent event for that same `item_id` only appends to `pending.chunks` — `tool_name`
and `kind` are never revisited, no matter what a later event carries. For cursor-acp
specifically: the *first* event for an edit tool call is the `pending`-status `tool_call`
message, which genuinely only has the generic `title: "Edit File"` (Cursor doesn't know the
target path yet at that point — confirmed live). The richer detail (path, via the `content`
diff array) only arrives on the *completion* `tool_call_update`, sharing the same `item_id` —
and is silently discarded by this coalescing logic before it ever reaches `arbmem:trace`.

### Why this isn't narrowly a cursor-acp bug

This is a structural property of `TranscriptFlusher`, not anything cursor-acp-specific — any
engine whose tool-call label becomes more specific or changes between "started" and "finished"
(not just cursor-acp's edit-diff case) would exhibit the identical staleness. No existing test
in `tests/test_transcript_flusher.py` exercises a same-`item_id` sequence where `tool_name`
*changes* between events — every existing test either keeps `tool_name` unset/constant across a
coalesced item, or uses a *different* `item_id` to trigger the boundary-flush path (which
sidesteps the bug entirely, since a new `item_id` always creates a fresh `_PendingItem`). This
is a genuine, previously-untested gap in shared code, not a documented and deliberately-pinned
behavior this design would be overturning.

## Architecture

### Fix 1 — last-known-non-empty value wins for `tool_name` ONLY (`kind` untouched)

<!-- r1 (P0, unanimous 4/4 — codex, agy-print, pi-GLM, cold-Opus independently traced the same
mechanism): the original text here proposed updating `pending.kind` alongside `pending.tool_name`,
reasoning that cursor-acp's completion event "carries no kind field," so a non-empty guard would
only ever preserve, never regress, the value. That premise is false. Trace the actual path
(verified by all four reviewers independently):

1. `cursor_acp.py`'s completion `tool_call_update` sets `data["kind"] = update.get("kind")` =
   `None` (`cursor_acp.py:593`) — correct, Cursor genuinely omits `kind` on completion.
2. `_with_progress_schema` (`cursor_acp.py:78-79`) only fills `enriched["kind"]` when the key is
   *absent*; here it's present with value `None`, so no override happens — `data["kind"]` stays
   `None`.
3. **`bridge.py`'s `_capture()` (`bridge.py:1774-1776`) is what actually resolves this:**
   `kind = data.get("kind"); if not isinstance(kind, str) or not kind: kind = event`. Since
   `data["kind"]` is `None` (not a truthy string), `kind` falls back to `event` —
   `"command_finished"`. This resolved value is written into `item["kind"]` *before* the flusher
   ever sees it.
4. The flusher's own `kind` resolution (`transcript_flusher.py:139`,
   `item.get("kind") or data.get("kind") or item.get("event")`) picks up that already-resolved
   `item["kind"] == "command_finished"` — **non-empty**.

So the proposed `if kind: pending.kind = kind` guard is **always true** on a completion event,
not conditionally true only when something already blanked it. It unconditionally overwrites
`pending.kind` from the *start* event's value (`"command_started"` for codex/gemini-acp/grok-acp,
or `"edit"`/`"execute"`/`"search"` — Cursor's own raw ACP sub-kind — for cursor-acp's *start*
event specifically) to `"command_finished"` on every single coalesced tool call, for every
engine that emits a start+finish pair sharing one `item_id` (confirmed for codex, cursor-acp,
gemini-acp, grok-acp — all four).

**Why this is a real, not theoretical, regression:** `tools/arb-watch-go/reduce.go`'s
`renderTranscriptLine` (`:174-187`) explicitly switches rendering mode on `kind`:
`command_started`/`tool_call` → a tool-name header line (`⏺ Bash(ls)`, using `tool_name`);
`command_finished`/`command_output`/`tool_output` → an output body line (`⎿ <content>`, using
`content`, ignoring `tool_name` entirely). Flipping `kind` from `command_started` to
`command_finished` mid-coalesce doesn't just change a label — it changes which *field* the
renderer reads, so `tool_name` (however well-enriched) stops being displayed at all for the
row's whole lifetime. `tests/e2e_transcript_roundtrip.py:323-328` already asserts
`kind == "command_started"` on exactly this kind of coalesced row — the original fix would have
broken that test outright, not just regressed rendering silently.

**Resolution: drop the `kind` update from this fix entirely.** `kind` is a rendering-mode
discriminator, not a freshness/richness field like `tool_name` — "non-empty wins" is the wrong
model for it. The bug this spec fixes is exclusively about `tool_name` going stale; `kind` was
never actually broken by the coalescing logic in a way this fix should touch. -->

In `_process()`, when `pending is not None` (i.e., this `item_id` has already been seen), update
`pending.tool_name` from the current event if it carries a non-empty value, rather than leaving
it frozen at whatever the first event provided. **Leave `pending.kind` exactly as today —
set once, at first occurrence, never revisited:**

```python
pending = self._pending.get(key)
if pending is None:
    pending = _PendingItem(
        run_id=str(item.get("run_id") or ""),
        task_id=task_id,
        turn_epoch=key[1],
        seat_id=str(item.get("seat_id") or ""),
        orchestrator=str(item.get("orchestrator") or ""),
        item_id=item_id,
        seq=seq,
        kind=kind,
        tool_name=str(data.get("tool_name") or data.get("command") or ""),
        chunks=[],
    )
    self._pending[key] = pending
else:
    latest_tool_name = str(data.get("tool_name") or data.get("command") or "")
    if latest_tool_name:
        pending.tool_name = latest_tool_name
    # kind is intentionally NOT updated here — see r1 marker above. It's a rendering-mode
    # discriminator (tools/arb-watch-go keys its start-header vs. output-body branch on it),
    # not a freshness field; the flusher's own kind-fallback chain guarantees a completion
    # event's kind is never actually empty in production, so "non-empty wins" would in
    # practice mean "always overwrite," which is unsafe here.
pending.chunks.append(text)
```

**"Non-empty wins" (not "always overwrite") for `tool_name` specifically:** most mid-turn
`tool_call_update`s (a bare `status: "in_progress"` transition, say) won't repeat the label at
all, and must not clobber what a real title already established. This half of the original
reasoning was correct — it's only the extension to `kind` that didn't hold.

**`seq` is intentionally left alone here** — `pending.seq` is set once at creation and used for
ordering (`build_trace_fields`'s `seq` field), not identity; changing it mid-coalescing isn't
part of this fix and isn't implicated by the bug.

### Fix 2 — ACP engines' raw `kind` collides with the bridge's rendering-discriminator `kind`

<!-- r1 (new, from cold-Opus's round-1 finding — the deeper issue the other three reviewers'
narrower "is the kind-update safe" framing didn't surface): fixing #1 alone (tool_name-only,
kind untouched) does NOT fix what was actually observed on screen for cursor-acp. It only
prevents the fix from making things *worse* for codex/gemini-acp/grok-acp. Traced independently
by cold-Opus and confirmed against the real repro data captured earlier in this investigation
(`{'kind': 'edit', 'tool_name': 'Edit File', 'content': '', ...}`).

r2 (P1, all four reviewers converged on the same underlying fact this round, with varying
severity — codex and cold-Opus treated it as blocking, pi-GLM and agy-print as a strong note):
the round-1 spec claimed this collision was cursor-acp-specific, with gemini-acp/grok-acp
"already using bridge-semantic kind." That claim doesn't hold. `grok_acp.py:428` and
`gemini_acp.py:356` have **byte-identical** `"kind": update.get("kind")` code to cursor-acp's
(confirmed by direct grep, independently by pi-GLM and cold-Opus). More decisively:
`tests/test_engine_progress_schema.py`'s `test_acp_call_sites_inject_turn_item_kind_and_seq`
(`:198-248`) already loops over `GeminiAcpEngine`/`CursorAcpEngine`/`GrokAcpEngine` with one
shared fixture and asserts `kind == "execute"` for **all three** — i.e. all three engines'
*code* propagates a raw sub-kind identically, verified by an existing test, not just a
structural code-shape argument. Whether gemini's/grok's *real* ACP servers actually send a
non-null `kind` on `tool_call` the way Cursor's does is a live-wire fact this pass didn't probe
(ACP is a shared, cross-vendor protocol, so it's plausible they do) — but given gemini-acp is a
**currently deployed, live seat**, and the fix is a one-line removal per engine with zero
downside if it turns out unnecessary, this spec widens Fix 2 to all three ACP engines rather
than defend a narrower, unverified claim. Codex remains genuinely unaffected — confirmed at
`codex.py:210-215`, it hardcodes bridge-semantic kind strings directly, never passing through a
raw server-provided sub-kind. -->

**Root cause.** `cursor_acp.py`, `grok_acp.py`, and `gemini_acp.py`'s `normalize_session_update()`
functions each set `data["kind"] = update.get("kind")` (`cursor_acp.py:593`, `grok_acp.py:428`,
`gemini_acp.py:356`) for every `tool_call`/`tool_call_update` event. On the *start* event
specifically, an ACP server may supply a `kind` value that's the server's own tool-taxonomy
sub-kind (`"edit"`, `"execute"`, `"search"` — confirmed live for Cursor; structurally identical
code paths exist for Gemini/Grok) — **not** one of the bridge-semantic lifecycle names
(`"command_started"`/`"command_finished"`) the rest of the pipeline expects in a field literally
named `kind`. Because `data["kind"]` is present whenever the server sends one (even though its
value means something different from what the field name implies), `_capture()`'s
`data.get("kind")`-first priority (`bridge.py:1774`) picks up the raw sub-kind for `item["kind"]`
on the start event, and the flusher's `_process()` locks `pending.kind` to it for that tool
call's entire coalesced lifetime (Fix 1 above deliberately preserves this locking behavior —
correctly, for the reasons above).

Raw sub-kinds like `"edit"`/`"execute"`/`"search"` are not in `renderTranscriptLine`'s recognized
set (`model_text`, `model_thinking`, `command_started`/`tool_call`, `command_output`/
`command_finished`/`tool_output`) — so any tool-call row whose engine hit this collision falls
through to the generic `return "⏺ " + content` branch (`reduce.go:189`), and `content` (not
`tool_name`) is what that branch uses — which is always empty for a tool-call row (`content`/
`chunks` only ever accumulate streaming `delta`/`content` text, never `tool_name`). **This is
the actual mechanism behind the blank `⏺` bullets originally observed** for cursor-acp — present
before this investigation even started, unrelated to (and not fixed by) Fix 1 alone — and, per
the r2 widening above, a live risk for gemini-acp's already-deployed seat too.

**Fix:** each of the three engines should stop putting the raw ACP sub-kind into the `data["kind"]`
field, since that field name is a shared contract with `_capture()`/the flusher/the Go renderer
meaning "bridge-semantic lifecycle kind," not "engine-specific tool taxonomy." Two ways to
resolve this — pick based on whether the raw sub-kind (`edit`/`execute`/`search`) has any real
downstream value:

- **(a) Simplest — drop it, in all three engines.** <!-- r2 (P1, codex): the original text here
  treated "explicitly set it to `None`" and "omit the key" as interchangeable. They are not, at
  the pre-`_capture()` observation point: `_with_progress_schema` (`cursor_acp.py:78-79`, and the
  equivalent in `grok_acp.py`/`gemini_acp.py`) checks **key presence**
  (`if "kind" not in enriched:`), not truthiness — so `data["kind"] = None` (key present, value
  `None`) would NOT trigger that fallback, leaving `data["kind"]` as `None` at the raw
  engine-emitted-progress-event level (before `_capture()` ever runs its own, separate,
  truthiness-based fallback). `_capture()`'s own fallback does correctly resolve either shape to
  the same final `item["kind"]` — but any code or test that inspects the engine's raw emitted
  `data` dict directly (see `tests/test_engine_progress_schema.py` below) would see a different
  value depending on which shape is chosen. **Resolution: omit the key entirely — do not set it
  to `None`.** This is the unambiguous, single correct shape; don't leave the choice open. -->
  Don't set `data["kind"]` at all for `tool_call`/`tool_call_update` events in any of the three
  engines (remove the `"kind": update.get("kind")` entry entirely — do not replace it with
  `"kind": None`). `_with_progress_schema` already fills a sensible default (`event_name`, i.e.
  `"command_started"`/`"command_finished"`) whenever the key is *absent* — so simply not setting
  it lets that existing fallback do the right thing, and `_capture()`'s own fallback chain then
  correctly resolves to the bridge-semantic kind these engines never should have been
  overriding.
- **(b) Preserve the raw sub-kind under a non-colliding name**, e.g.
  `data["acp_kind"] = update.get("kind")`, if there's a reason to want it available later (a
  future `renderTranscriptLine` enhancement, mirroring the existing `apply_patch`+`meta["file"]`
  special-case pattern at `reduce.go:167-173`, could render `"edit"` tool calls with a nicer
  `⏺ Edit(path)` line instead of the generic fallback — genuinely useful, but a separate,
  larger follow-up, not part of this fix).

**This spec picks (a)** — simplest, smallest diff, and sufficient to fix the observed bug (once
`kind` correctly resolves to `"command_started"`/`"command_finished"`, `renderTranscriptLine`'s
existing `command_started`/`tool_call` branch renders `tool_name` — which, combined with Fix 1,
now correctly carries the path-enriched label on completion). (b) is noted as a real, deferred
enhancement idea, not built here — don't implement it as part of this pass.

### Scope discipline

<!-- r1: widened from the original "flusher-only" claim once Fix 2 was added — cursor_acp.py IS
now touched, with the justification above. r2 (P1, unanimous-in-substance across all four
reviewers): widened again from "cursor-acp only" to all three ACP engines — see Fix 2's r2
marker for the full evidence chain (identical code + an existing shared test proving identical
propagation behavior across all three). Still narrowly scoped: one field removed per engine,
plus the flusher's tool_name-only update. -->

This fix touches four files, each narrowly:
- `src/agent_redis_bridge/transcript_flusher.py` — `_process()`'s `tool_name`-only update
  (Fix 1). `kind` handling is explicitly UNCHANGED from today.
- `src/agent_redis_bridge/engines/cursor_acp.py`,
  `src/agent_redis_bridge/engines/gemini_acp.py`,
  `src/agent_redis_bridge/engines/grok_acp.py` — remove the raw-ACP-`kind`-into-`data["kind"]`
  assignment for `tool_call`/`tool_call_update` events, in each of the three (Fix 2).

It does **not** touch:
- `codex.py` — confirmed genuinely unaffected; its `kind` values are hardcoded bridge-semantic
  strings, never a passthrough of server-provided data (`codex.py:210-215`).
- `bridge.py`'s `_capture()` — its existing fallback chain is correct and is exactly what makes
  Fix 2 work once these engines stop overriding it.
- The Go frontend (`tools/arb-watch-go`) — once `kind` correctly resolves to a value it already
  recognizes, the existing renderer needs no changes.

## Testing

### Fix 1 (`transcript_flusher.py`)

`tests/test_transcript_flusher.py` has zero coverage of `tool_name` changing across a coalesced
`item_id`. Add, using the existing `_item()` test helper's shape (extend it to accept a
`tool_name`/`command` override, or construct the raw dict inline — match whichever the file's
existing convention favors):

- **The regression-guard case (fails on current code, passes after the fix):** two `_process()`
  calls sharing one `item_id` — first with `tool_name: "Edit File"` (or `data.command`), second
  with a richer `tool_name`/`command` (e.g. `"Edit File: /path/to/x.py"`) and no `kind` — flush
  (via a boundary event or `turn_end`) and assert the written row's `tool_name` is the **richer**
  value, not the first.
- **The guard-against-regression case:** same two-event sequence, but the *second* event's
  `tool_name`/`command` is absent/empty (a bare status transition) — assert the flushed row
  **keeps** the first event's non-empty `tool_name` (doesn't get blanked by the later empty
  one). This is the direct test for the "non-empty wins" design choice, not "always overwrite."
- <!-- r1 (P1, pi-GLM — sharpened): the original `kind` test here asked for a *synthetic* item
  with an empty `kind`, which cannot occur via the real `_capture()` path (its fallback always
  fills a non-empty value) — such a test would pass without guarding anything real. Replaced
  with the actual regression guard for the P0 this round found: --> **`kind`-preservation
  regression guard (the real P0 guard):** shape the test item the way `_capture()` actually
  produces it — first event `kind: "command_started"` (or `"edit"`, matching cursor-acp's raw
  start-event shape), second event (same `item_id`) with `data["kind"] = None` causing
  `_capture()`'s fallback to resolve `item["kind"]` to `"command_finished"` (i.e. construct the
  test item with a **non-empty** `"command_finished"` top-level `kind`, matching production, not
  an empty one) — assert the flushed row's `kind` stays exactly what the *first* event set,
  proving the fix does NOT propagate the later, different, non-empty kind. This is the test that
  would have caught the original P0 and must fail against the original (pre-r1) proposed code.
- Confirm all **existing** tests in `tests/test_transcript_flusher.py` still pass unmodified —
  none of them exercise a changing `tool_name` mid-coalesce, so none should need updating (this
  is worth stating explicitly and verifying, not assuming) — **especially**
  `tests/e2e_transcript_roundtrip.py:323-328`, which already asserts `kind ==
  "command_started"` on a coalesced row and is the direct regression guard proving Fix 1 doesn't
  reintroduce the P0.
- Also re-run `tests/test_transcript_hotpath.py` — grep it first for any assumption about
  `tool_name` staying fixed across a coalesced item before assuming it's unaffected.

### Fix 2 (`cursor_acp.py`, `gemini_acp.py`, `grok_acp.py`)

- <!-- r2 (P1, codex — precision): "or contains None" removed; the r2 revision above requires
  omission, not None, and this test must assert that exactly. --> A test, **for each of the
  three engines**, asserting `normalize_session_update()`'s returned `data` dict for a
  `tool_call`/`tool_call_update` event no longer contains a `"kind"` key **at all** (not
  present — not present-with-value-`None`), for both the start event (which today incorrectly
  sets it to the raw sub-kind) and the completion event (which already correctly has no
  meaningful kind to contribute).
- An **end-to-end-shaped** test (constructing the event the way `handle_progress`/`_capture`
  would actually consume it, not just unit-testing `normalize_session_update()` in isolation),
  for at least cursor-acp and one of gemini-acp/grok-acp: confirm that once `data["kind"]` is
  absent, `_capture()`'s existing fallback resolves `item["kind"]` to the bridge-semantic
  `"command_started"`/`"command_finished"` — i.e. this test proves Fix 2 actually closes the
  loop into Fix 1's territory, not just that the engine stopped emitting the colliding field in
  isolation.
- Full-chain assertion (the real acceptance criterion): combine Fix 1 + Fix 2 in one test —
  a cursor-acp start event (no `kind` collision) followed by a completion event carrying the
  enriched `tool_name`/`path` — assert the final coalesced row has **both** `kind ==
  "command_started"` (recognized by the Go renderer) **and** `tool_name` containing the path.
  This is the test that proves the fix as a whole actually produces a renderable, informative
  transcript line for a cursor-acp edit — not just that neither half regressed in isolation.
- <!-- r2 (P1, codex + cold-Opus independently — the "fix breaks an existing passing test"
  class round 1 was convened to catch, found again in round 2 for a different test): -->
  **Required update to an existing test, not optional:**
  `tests/test_engine_progress_schema.py`'s `test_acp_call_sites_inject_turn_item_kind_and_seq`
  (`:198-248`) loops over `GeminiAcpEngine`/`CursorAcpEngine`/`GrokAcpEngine` with one shared
  fixture and currently asserts `kind == "execute"` for all three (the exact raw-sub-kind
  passthrough this spec fixes). After Fix 2 lands for all three engines, this test's expected
  `kind` must change to `"command_started"` for all three cases (matching the `event_name` the
  test already separately confirms via `name == "command_started"` — i.e. after the fix, `kind`
  and the event name agree, which is the whole point). Update this test explicitly as part of
  the implementation, don't leave it as a silent breakage discovered by a green-to-red test run.

**Live verification (not unit-testable, but worth doing once merged):** re-dispatch a real
cursor-acp edit task (the exact repro from this investigation) and confirm the `arbmem:trace`
row for the edit tool call now shows `kind: "command_started"` and the enriched `tool_name` with
the path, using the same precise-Python-client read used to find this bug (not a `redis-cli`
flat-text read, which is easy to misparse across field boundaries — noted as a real trap
encountered in this investigation). Bonus: actually run `tools/arb-watch-go`'s
`renderTranscriptLine` (or just eyeball the Go TUI) against the resulting row to confirm the
bullet line itself now shows something informative, closing the loop all the way to what the
user actually sees.

## Risks / open questions

1. <!-- r1: revised — the original framing ("freshness only ever gets better") applied only to
   tool_name and was the exact reasoning the round-1 panel found unsound for kind. r2: further
   revised — round 1's "cursor-acp specific" scoping for Fix 2 didn't hold either (see Fix 2's
   r2 marker); Fix 2 is now three engines, and this risk is correspondingly narrowed. -->
   **Fix 1 (`tool_name`-only, `kind` untouched) is safe for every engine regardless of
   individual wire timing** — it can only ever replace an existing `tool_name` with a later
   non-empty one, never change *which field* the renderer reads (that's `kind`, deliberately
   left alone). **Fix 2 now covers cursor-acp, gemini-acp, and grok-acp** (all three confirmed
   to have identical raw-kind-passthrough code, and an existing shared test proving identical
   propagation behavior) — `codex.py` is the only engine confirmed genuinely unaffected
   (hardcoded bridge-semantic kind strings, no server-provided passthrough). Whether gemini's/
   grok's *real* ACP servers currently exploit this collision in practice (vs. cursor's,
   confirmed live) was not independently wire-probed this round — the fix is applied regardless
   given its near-zero cost and gemini-acp's live-deployment status, per the r2 marker's
   reasoning; if a future new ACP engine is added, check it for the same pattern rather than
   assuming it's exempt.
2. **`_write()` intentionally called only at flush boundaries (new item_id or `turn_end`)** —
   this fix doesn't change *when* a row gets written, only *what value* is captured for
   `tool_name` at that point. No new flush-timing risk introduced.
3. **Fix 2 option (b) (preserve the raw ACP sub-kind under a non-colliding field name, enabling
   a future richer `⏺ Edit(path)`-style render) is explicitly deferred**, not built here — noted
   as a real follow-up idea, not a requirement of this pass.
