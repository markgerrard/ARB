# Cold-Opus subagent visibility — SubagentStart/SubagentStop discovery (design)

> Status: design, **panel-reviewed four times + revised four times** 2026-06-30. Brainstormed
> 2026-06-30 (root cause verified empirically on disk). **Round 1** (3-seat panel: codex +
> agy-print + cold-Opus): all three APPROVE WITH NOTES / REQUEST CHANGES, unanimous on the core
> approach (reuse the existing `.output`-glob discovery, zero daemon-discovery change) but
> convergent on two P1 correctness bugs in the first draft, independently found via three
> different angles (tailer internals, daemon polling, and the Go frontend's dedup logic).
> **Round 2** (same 3-seat panel, re-reviewing the round-1 revision): two of three independently
> found that the round-1 completion-check fix was itself circular/unreachable (codex + agy-print),
> and cold-Opus independently found that the round-1 identity-guard fix would have regressed an
> existing legitimate code path (verified directly against `tailer.py` by the orchestrator, not
> just trusted) — both fixed. **Round 3** (same 3-seat panel reviewing the derived implementation
> plan, `docs/superpowers/plans/2026-06-30-cold-opus-subagent-visibility.md`): codex found a
> write-order race this spec had inherited unnoticed since the original draft — § "Architecture"
> step 4/5 wrote the symlink before the sidecar, which could permanently strand a seat's
> `orchestrator` if a daemon poll landed in the gap — fixed here by reordering to sidecar-first,
> written atomically. **Round 4** (3-seat panel reviewing the codex-implemented diff against the
> round-3 plan): agy-print and codex's own self-review both APPROVEd; cold-Opus alone found a P0
> the other two and all three prior rounds missed — § "Architecture" step 3's path-derivation
> formula dropped the `<session_id>` directory entirely (used the parent transcript's parent
> *directory* instead of stripping only its `.jsonl` *suffix*), producing a silently-dangling
> symlink that would have made the whole feature do nothing in production. Caught by re-deriving
> from raw on-disk evidence rather than trusting this spec's own prose — the unit test "proving"
> the formula was itself tautological (built its expectation with the same buggy formula). Fixed
> here and in the implementation; see the inline note in step 3 below for the full mechanism.
> Supersedes the cold-Opus portion of `2026-06-28-claude-layer-visibility-design.md`
> (§ "cold-Opus subagents have their own transcript files" — true, but at a different,
> version-drifted path than that spec assumed; see § Root cause below).

## Problem

Cold-Opus reviewer subagents (spawned via Claude Code's native Agent/Task tool, e.g.
`subagent_type: code-reviewer-report-writer`) never appear as a seat in arb-watch. They are
invisible end to end — not in the claude-tail registry, not in `transcript_io`, nothing — despite
the 2026-06-28 spec claiming this path was already handled.

## Root cause (verified empirically this session)

`claude_tail`'s cold-seat discovery (`src/agent_redis_bridge/claude_tail/service.py`,
`_discover_specs()`) globs `ARB_CLAUDE_TAIL_COLD_DIR` (default `~/.claude/tasks`) for `*.output`
files. That directory currently has zero such files. A real cold-Opus subagent's transcript
actually lives at:

```
~/.claude/projects/<project-slug>/<parent-session-id>/subagents/agent-<agent_id>.jsonl
```

(confirmed on disk: a real `agent-<id>.jsonl` plus a companion `agent-<id>.meta.json` written by
Claude Code itself, containing `{"agentType":..., "description":...}`). The harness's actual
subagent-output location has drifted since the 2026-06-28 spec was written; the daemon watches
the wrong place. Nothing currently creates a `.output` file pointing at the real path.

Separately, Claude Code exposes two real hook events for subagents (confirmed against current
official docs, not assumed): **`SubagentStart`** (fires when a subagent is spawned; payload
includes `session_id`, `agent_id`, `agent_type`, `cwd`) and **`SubagentStop`** (fires on
completion; same id fields). The official docs state the hook payload's own `transcript_path`
field points at the *parent* session, not a separate subagent file — true for that field, but
doesn't contradict the empirical finding above: the per-subagent `.jsonl` file exists on disk at a
path derivable from data the hooks do provide.

## Architecture

Two new hook scripts under `scripts/claude_tail_hooks/`, wired to `SubagentStart`/`SubagentStop`
in `.claude/settings.local.json` (host-local config, same pattern as the existing
`session_start.py`/`session_end.py` wiring).

**`subagent_start.py`**
1. Reads `session_id`, `agent_id`, `agent_type`, `cwd` from the hook payload.
2. Checks `agent_type` against a new allowlist env var `ARB_CLAUDE_TAIL_COLD_AGENT_TYPES`
   (comma-separated, default `code-reviewer-report-writer`). Not on the list → no-op, exit 0.
   (Scopes this to ARB-relevant reviewer subagent types, not every subagent Claude Code spawns —
   keeps the roster from filling with Explore/Plan noise; configurable for future ARB subagent
   types without a code change.)
3. **Derives the subagent's transcript path without reimplementing Claude Code's path-slugging
   algorithm — and without dropping the session-id directory (round-4 fix, see below).** Looks up
   the *parent* session's record in the existing claude-tail registry by `session_id` (the same
   registry `session_start.py` already writes) to get its already-correct `transcript_path`
   (`.../projects/<slug>/<session_id>.jsonl` — a flat file). Claude Code nests that session's
   subagent transcripts under a directory matching the session id itself — a **sibling** of the
   flat parent file, not its parent directory: `.../projects/<slug>/<session_id>/subagents/
   agent-<agent_id>.jsonl`. The correct derivation strips only the `.jsonl` *suffix* from the
   parent transcript path (turning the file path into a same-named directory path), then appends
   `subagents/agent-<agent_id>.jsonl` — **not** the parent transcript's parent *directory*, which
   would land one level too high and miss the session-id segment entirely. (**Round-4 bug, found
   during implementation review, not either design-spec round:** the original draft of this step
   said "the subagent's transcript is `<dirname(parent transcript_path)>/subagents/agent-
   <agent_id>.jsonl`" — `dirname` of a *file* path strips the filename, landing at `.../projects/
   <slug>/`, not `.../projects/<slug>/<session_id>/`. That formula directly contradicts this
   spec's own § Root cause finding two sections above, which correctly states the real path
   includes `<parent-session-id>` as a directory segment. The implementor coded the wrong formula
   faithfully from this spec; a cold-Opus reviewer caught the contradiction by re-deriving from
   the raw on-disk evidence rather than trusting either section's prose. The bug produces a
   *dangling* symlink — `symlink_to` doesn't require the target to exist — which `service.py`'s
   `_is_recent()` then silently drops via `FileNotFoundError` on `path.stat()`, with no error
   logged: the exact invisibility this feature exists to fix, reintroduced one path-segment
   removed. The unit test "proving" the original formula correct was tautological — it built its
   expected value with the identical buggy formula — which is why both design-spec panel rounds
   and the implementation-plan panel round all missed it; only an implementation-level reviewer
   re-deriving from raw disk facts caught it.) If the parent session isn't found in the registry
   (e.g. the subagent was spawned by a non-warm/cold caller, or the registry write hasn't landed
   yet), fail soft and exit — no symlink is created, matching the existing "stay invisible"
   behavior rather than guessing a path that might be wrong.
4. **Writes the sidecar BEFORE the symlink (order matters — found during the implementation-plan
   review, a third panel round; not caught by either design-spec round).** Writes
   `<cold_dir>/<agent_id>.arb-tail.json` (a sidecar, deliberately *not* named `<agent_id>.meta.json`
   to avoid confusion with Claude Code's own same-named file in a different directory), atomically
   (temp-file-in-same-dir + `os.replace`, so a reader never observes a half-written file), containing
   `{"orchestrator": <parent seat_id, looked up from the same registry hit in step 3, or "">,
   "completed": false}`.
5. Writes `<cold_dir>/<agent_id>.output` as a symlink to the derived path. This is the file
   `service.py`'s existing glob already watches every poll tick (~1s) — **no daemon-discovery
   change needed**, real-time pickup is automatic. **Why the order is load-bearing:** discovery is
   triggered solely by this symlink's existence; the sidecar is only read *if present* at that
   moment. If the symlink existed first, a daemon tick landing in the gap between the two writes
   would create the tailer unlocked (no sidecar found yet) — and the daemon never re-creates an
   *already-existing* tailer later just because its sidecar subsequently appears (`tick()`'s
   tailer-creation check only fires for a key with no existing tailer, or one resumed after
   finishing). That seat's `orchestrator` would then stay empty for its entire lifetime, not just
   transiently. Sidecar-first, written atomically, eliminates the race: by the time discovery can
   see the symlink at all, the sidecar is guaranteed already complete.
6. fail_soft wrapped throughout (matches `common.fail_soft`) — observability plumbing must never
   block or crash the orchestrator/subagent.

**`subagent_stop.py`**
1. Reads `agent_id` from the payload.
2. If `<cold_dir>/<agent_id>.arb-tail.json` exists, rewrites it with `completed: true` (does
   **not** delete the symlink or sidecar — see § Why not delete-on-stop). No-op if the sidecar
   isn't present (matches the allowlist no-op case from start).

**`seat_id` stays at the existing default, `cold-opus-<agent_id>`** — produced today, unmodified,
by `cold_identity()`'s no-marker fallback (`src/agent_redis_bridge/claude_tail/identity.py`). This
was a deliberate simplification from the first draft (which proposed `cold-<agent_type>`) — see
§ Why not a custom seat_id below. The sidecar therefore only ever carries `orchestrator` and
`completed`, not `seat_id`/`run_id`.

### Why not delete-on-stop (P1, found independently by all three reviewers)

The first draft had `subagent_stop.py` delete the `.output` symlink, relying on the daemon's
existing "key dropped from `live_keys` → finish" path (`service.py` `tick()`) for a fast,
zero-extra-code completion signal. Two independent failure modes, both confirmed against the real
polling code:

- **Truncation**: `tick()` finishes a dropped key without a final `poll()` first
  (`service.py:83-88`) — content written in the last ≤1 poll-interval before Stop (e.g. the
  reviewer's final verdict line) would never be tailed.
- **Total loss**: if a subagent starts *and* stops within one ~1s poll interval, the daemon's
  `_discover_specs()` may never see the file between two ticks at all — not "loses the last
  bytes," loses the *entire* subagent's visibility, start to finish.

**Fix**: the daemon must do a confirming final `poll()` before finishing a tailer whose sidecar
says `completed: true`, then delete both the symlink and sidecar itself once caught up to EOF.
This requires a small, targeted change to `service.py`'s discovery/tick logic (see § service.py
change) — the one piece of this design that does touch the daemon.

### Why not a custom seat_id (P1, found by codex reading the Go frontend)

The first draft proposed `seat_id = "cold-<agent_type>"` with `run_id = <parent session_id>` so
multiple cold reviewers spawned in one session would visually group. This breaks two things in
`tools/arb-watch-go/`, found only by reading the actual frontend (not just the Python ingestion
side):

- `model.go`'s `dedupSeatRuns` collapses any two roster rows sharing the same `(seat_id, run_id)`
  pair — a guard against an unrelated phantom-duplicate bug. Two concurrent same-`agent_type`
  reviewers (e.g. a 3-reviewer panel, which this very design process used twice) would collide on
  that pair and only one would render — directly defeating the feature.
- `reduce.go`'s `agentOf()` special-cases the literal `cold-opus-` prefix to classify a seat as
  `"opus"` for filtering, with pinned tests. A `cold-<agent_type>` label would fall into a generic
  `"cold"` bucket instead, losing "filter by Opus" UX.

**Fix**: keep `seat_id = cold-opus-<agent_id>` exactly as `cold_identity()` already produces it by
default. `agent_id` is unique per spawn (no dedup collision) and the prefix is already
opus-filter-compatible. No identity override is needed for `seat_id` at all — only `orchestrator`
needs to flow through from the sidecar, which is a smaller, lower-risk surface than overriding the
full identity tuple.

### Identity-threading fix (P1, found independently by all three reviewers)

Even with `seat_id` unchanged, `orchestrator` still needs to come from the sidecar — and the first
draft's plan to set it in `service.py._discover_specs()` was in the wrong layer. The *tailer*
(`tailer.py`) re-resolves cold identity from the transcript's first user-message line
(`_resolve_cold_identity()`, called from `_is_cold()`-gated logic once text is parsed) and
unconditionally overwrites `self.identity` via `cold_identity(agent_id, session_id, marker_text)`.
A real subagent's prompt carries no `[ARB_RUN:... ARB_SEAT:... ARB_ORCH:...]` marker, so this
resolution always falls back to `orchestrator = ""`, silently discarding whatever the daemon set
at discovery time. (One reviewer additionally flagged that a naive test asserting `spec.identity`
at discovery time would have passed while the *emitted* events stayed wrong — a tautological test
that wouldn't have caught this.)

**Fix, round 2 (a single naive guard was tried and rejected — see below):** add a **new, separate**
`identity_locked: bool = False` constructor argument to `TranscriptTailer.__init__`, distinct from
the existing `_identity_resolved` bookkeeping flag. `_resolve_cold_identity()`
(`tailer.py:173-177`) gets an early-return guard on `_identity_locked` specifically (`if
self._identity_locked: return`), and only `service.py` setting `identity_locked=True` (when a
valid sidecar is found at discovery time) ever sets it — nothing inside the tailer itself sets
`_identity_locked`. `service.py` passes the sidecar-derived identity *and* `identity_locked=True`
at construction; when no sidecar is found, `identity_locked` stays `False` for the tailer's whole
lifetime and behavior is byte-for-byte unchanged.

**Why not reuse `_identity_resolved` itself as the guard (round 1 of this fix, found wrong during
spec review by a fourth, deeper read of `tailer.py`):** `_process_line()` (`tailer.py:108-122`)
calls `_ensure_identity_resolved()` **unconditionally on every line**, not just user lines — it
only no-ops if `_identity_resolved` is already `True`. If a transcript opens with one or more
`DROP_TYPES` lines (e.g. a `system` init line) before its first `user`-type line,
`_ensure_identity_resolved()` fires the empty-marker fallback (`_resolve_cold_identity("")`) and
sets `_identity_resolved=True` *before* the real first user line — possibly carrying an
`[ARB_RUN:...]` marker — ever arrives. Today's code (no guard at all) then legitimately *upgrades*
from that empty fallback to the marker-derived identity once the real first user line shows up,
because `_resolve_cold_identity` is unconditional. Gating on `_identity_resolved` directly — the
original draft of this fix — would have silently blocked that legitimate upgrade for any cold seat
not created by these new hooks (i.e. regressed existing behavior for the pre-existing marker
convention), since the empty-fallback resolution already flips `_identity_resolved` to `True`
before the marker-carrying line is reached. A separate `identity_locked` flag, set *only* by the
daemon's explicit sidecar-driven construction path, avoids this entirely — the existing
empty→marker upgrade dance is completely unaffected, because it never touches `identity_locked`.

### service.py change

The only daemon change required:

1. `_discover_specs()`'s cold branch: alongside the existing `agent_id`/`path` discovery, check for
   a sidecar `<cold_dir>/<agent_id>.arb-tail.json`. If present and valid JSON, build the spec's
   identity with `orchestrator` from the sidecar (falling back to `""` if the sidecar lacks it)
   and pass `identity_locked=True` to the tailer. If absent or malformed JSON, fall back to
   *exactly* today's behavior (`cold_identity(agent_id, session_id, "")`, `identity_locked=False`)
   — additive, not a behavior change to the existing path.
2. **Completion check goes in the active/live-key loop, not the dropped-key loop (round 2 of this
   fix — round 1 was circular and unreachable, see below).** `tick()`'s per-tick poll of each
   currently-live cold key (`service.py`, the loop that already calls `state.tailer.poll()` every
   tick — mirroring how `tailer.completed` from the `[ARB_SEAT_DONE]` marker is already checked
   there today) additionally re-reads that key's sidecar after polling. If it says `completed:
   true`: the poll that just ran already flushed any trailing content (no separate "final poll"
   step needed — it's the same poll the loop already does), so immediately call `_finish_once()`
   and delete the `.output` symlink + sidecar. Deleting the symlink there is what makes the key
   drop out of `live_keys` on the *next* tick, which then hits the existing, already-correct,
   untouched "key not in live_keys → already finished, just remove from `self._tailers`" cleanup
   path (`_finish_once` is idempotent, so a redundant call there is harmless). If the orchestrator
   process is killed without the Stop hook ever firing (no sidecar, or sidecar never reaches
   `completed: true`), the existing idle-finish backstop (300s) still applies, unchanged.

   **Why not check on key-drop directly (round 1, found circular/unreachable by two independent
   reviewers during spec review):** the first draft of this fix said "when a previously-discovered
   cold key drops out of `live_keys`, check whether its sidecar says `completed: true`." But
   `subagent_stop.py` deliberately does *not* delete the symlink (that was the whole point of the
   delete-on-stop fix above) — so a completed key never drops out of `live_keys` on its own, and
   that check is unreachable as originally written. The corrected design (above) checks
   `completed` on a key that is *still present* in `live_keys`, and the daemon's own deletion of
   the symlink is what triggers the drop — not the other way around.

## Testing

- `tests/claude_tail/test_subagent_hooks.py` (new): allowlist filter (in/out of
  `ARB_CLAUDE_TAIL_COLD_AGENT_TYPES`), parent-registry lookup success/miss (no symlink created on
  miss), symlink target path construction (derived from parent `transcript_path`, not a
  reimplemented slug), sidecar content (`orchestrator`, `completed: false` initially),
  `subagent_stop.py` rewriting `completed: true` without deleting, idempotent re-invocation,
  fail-soft on malformed payload.
- `tests/claude_tail/test_service.py` (extend): sidecar present at discovery → identity built with
  sidecar `orchestrator` + `identity_locked=True`, and the tailer's own first-line resolution does
  NOT override it (an explicit non-tautological assertion on the *emitted* identity after a
  simulated transcript line is tailed, not just the spec object at discovery time — this directly
  guards the bug the panel found). Sidecar absent → unchanged fallback to current
  `cold_identity(agent_id, session_id, "")` / `identity_locked=False` (explicit regression guard).
  **Separately, in `tests/claude_tail/test_tailer.py`**: a transcript that opens with a
  `DROP_TYPES` line (e.g. `system`) before its first `[ARB_RUN:...]`-marked user line still
  upgrades from the empty fallback to the marker identity when `identity_locked=False` (regression
  guard for the round-1 bug found during spec review — confirms the fix doesn't break the existing
  non-hook cold-seat marker convention). `tick()`: a live key whose sidecar reaches `completed:
  true` is finished and its files deleted on the same tick the poll observes it (covering the
  start+stop-within-one-tick case directly, since the symlink is never removed by the hook itself
  — see § Risks, item 1 revised).
- **Live verification**: spawn a real `code-reviewer-report-writer` subagent and confirm a row
  appears in arb-watch within ~1-2s of spawn with the correct `orchestrator` grouping, and
  finishes promptly (not after the 5-minute idle-finish fallback) on completion, with its full
  transcript content present (no truncated final lines).

## Risks / open questions carried forward

1. **Start+stop within a single poll tick — resolved by the round-2 completion-check fix, not a
   remaining risk.** The original concern was that a subagent whose entire lifetime is shorter
   than the daemon's poll interval (~1s) might never be discovered before its `.output` symlink
   disappeared. That concern no longer applies: `subagent_stop.py` never deletes the symlink —
   only the daemon does, after observing `completed: true` on a tick where it has already polled
   the key. The symlink is therefore guaranteed to exist on disk until some future tick discovers
   it, however short the subagent's actual runtime was; it will be tailed, its (possibly small,
   single-poll) content processed, completion observed, and cleanup performed in the same tick.
   (Caught during spec review: this risk was actually a symptom of the round-1 completion-check
   bug, not an independent limitation — fixing the bug resolved the risk.)
2. **Parent-registry lookup miss handling**: if `subagent_start.py` fires before the parent
   session's own `SessionStart` hook has written its registry record (unlikely in practice — the
   parent session must already be running to spawn a subagent — but not provably impossible under
   exotic timing), the hook fails soft and the subagent stays invisible for that one spawn. No
   retry is planned; this matches the existing "stay invisible on lookup failure" posture rather
   than guessing.
3. The `.arb-tail.json` sidecar is a new, project-specific file format living alongside Claude
   Code's own files in `~/.claude/tasks`-style directories — distinct naming chosen specifically to
   avoid confusion with Claude Code's own `agent-<id>.meta.json`, but it's still new vendor-adjacent
   surface to keep in mind if Claude Code's own directory layout changes in the future.
