# SPEC — ARB Observability Slice 5a-0: capture normalization

**Status:** SPEC **v11 — CONVERGED** (spec panel r10 UNANIMOUS approve / 0 P0/P1, certify quorum codex-sol@high + agy + grok; cold-Opus non-cert approve; audit run `panel-slice5a0-spec-r10b-20260714T042204Z-2e43f9`, close `emitted`). NEXT = plan stage. · **Design:**
`docs/superpowers/specs/2026-07-13-arb-observability-slice5a-0-capture-normalization-design.md` **v7**
(CONVERGED, unanimous approve / 0 P0/P1; mechanism/why authoritative there) · **Roadmap parent:**
[[arb-observability-roadmap]] Slice 5 · **Downstream:** prerequisite of
`2026-07-13-arb-observability-slice5a-design.md` (v2) · **Author:** warm-Opus (inline).

Slice 5a-0 = **capture normalization**: the ratified prerequisite of Slice 5a (the span data layer). Every
capture producer emits, into the durable eval payload, the three primitives 5a needs to project correct,
recovery-safe timing spans — a canonical **`tool_call_id`**, a deterministic per-`(run_id,task_id)` integer
**`turn_index`**, and a monotonic **`attempt_epoch`** per `(run_id,task_id)`. **5a-0 is capture-only:** it
makes NO claim about 5a's projection SQL, and hands the epoch-aware replace/fence semantics to 5a as named,
testable contract obligations (O1–O3 below). Requires a fleet bridge redeploy (covers claude-tail).

> **v1 → v2 fold (spec panel₀, run `panel-slice5a0-spec-20260713T195622Z-74d32c`; agy approve/none, grok
> approve/P2, cold-Opus approve/P2 non-cert, codex-sol@high needs-changes/P1).** The P1 was **verified against
> real data** before folding (a panel P1 is a candidate, not a verdict):
> - **P1 (codex-sol, CONFIRMED) — `event_ts` is replay-stable but NOT a causally valid latency clock.** The
>   transcript's own `timestamp` is not monotonic with the causal message chain: **6 of 22,347** direct
>   user→assistant pairs (`parentUuid` = user's `uuid`) have the **assistant timestamped earlier than its user
>   parent** (independently reproduced: e.g. `05e84cb7…jsonl:10-11` asst `06:26:12.073Z` < user `06:28:36.213Z`;
>   `88bf91fc…jsonl:11-12` asst `11:34:01.044Z` < user `11:37:44.232Z`; 213 backwards adjacent transitions in 47
>   files). v1's R2/O4 treated `event_ts` as a *validated clock* and its live gate checked only replay
>   *equality* — so a stable-but-negative latency passes green ("idempotently wrong"; a vacuous gate,
>   [[vacuously-green-guard-fail-loud]]). **Fold:** `event_ts` is an idempotent *timestamp*, not a validated
>   clock; O4 gains an explicit **temporal-validity contract** (5a uses it only when the turn's captured edge
>   sequence is causally non-decreasing, else latency = NULL/unknown + clock-invalid flag — never negative, never
>   a `sent_at` fallback); the gate asserts **causal validity**, not just equality (inverted-timestamp
>   deny-proof). See R2, O4, O-gate, live gate, Tests.
> - **P2s folded:** byte-0 re-read enumeration was incomplete (F1/codex — also absent inode-qualified key
>   `offset.py:19-22` + inode replacement `offset.py:10-11`); `event_ts` structural placement was ambiguous
>   (cold-Opus — pinned to the event `data`/payload so the allowlist add is load-bearing); no absent-`timestamp`
>   fail-closed rule (grok F5/cold-Opus — added, never a silent `sent_at` fallback); `promptId` is user-line-only
>   so `turn_started` must be injected at the tailer, not derived from mapped events (grok F3 — `map_line` drops
>   pure-text user lines `mapper.py:73-90`); allowlist "four additions" miscount → three new (grok F4/codex);
>   claude-tail stamps `turn_index` only on trace today (`tailer.py:368`) → must reach the eval path (grok note).

> **v2 → v3 fold (spec panel r1, run `panel-slice5a0-spec-r1-20260713T201843Z-58a826`; agy/grok/cold-Opus
> approve/P2, codex-sol@high needs-changes/P1).** The P1 was **verified against code + corpus** before folding:
> - **P1 (codex-sol, CONFIRMED) — O4 validated only the EVAL-visible edge sequence, but the causal clock is
>   not fully eval-visible.** `model_text`/`model_thinking` route **trace-only** (`tailer.py:316-318`,
>   `MODEL_EVENTS` → `_emit_trace` then `return`, never `_emit_eval`), and `map_line` drops pure-text human
>   `user` lines (`mapper.py:73-90`). Of the 6 backward direct user→assistant pairs, **5 are `text`/`thinking`**
>   (independently reproduced: `7c524525:4443`=`['thinking']`, `05e84cb7:11`=`['text']`, `88bf91fc:12`=
>   `['thinking']`). So a causally-backward pure-text/thinking assistant child is **invisible to O4**; a
>   pure-text no-tool turn yields a non-decreasing eval subset → wrong-but-green latency. v2 narrowed the
>   idempotently-wrong class but did not close it (the inverted-edge fixture assumed the inverted child was
>   already an eval edge). **Fold:** 5a-0's tailer (which already *reads* every record, incl. trace-only ones)
>   stamps a bounded **`turn_clock_monotonic`** flag + `turn_started_ts` on the turn's eval edge, computed over
>   the FULL causal record stream; O4 emits a numeric turn latency only when the flag is true, else
>   NULL/`clock_invalid`. This surfaces trace-only inversions to O4 **without routing any text to eval** and
>   without a per-record eval-volume blow-up. (codex's alternative — a per-record eval clock edge — is noted +
>   rejected on eval-volume cost in Non-goals; the flag catches intermediate inversions too, not just bookends.)
>   See R2, Deliverable 4, O4, O-gate, live gate, Tests.
> - **P2s folded (all r1 seats):** SP0-1 promoted Proposed → **PINNED** (`task:{task_id}:epoch`, no `run_id`
>   dimension + *why it is safe* stated, call-site + snapshot-once + TTL); SP0-2 pinned (isMeta/isSidechain
>   out-of-turn, `turn_completed`-before-`turn_started` precedence, `turn_completed` timestamp source);
>   synthetic/injected edges carry their source line's `timestamp` as `event_ts`, never `_now()`/`sent_at`
>   (grok P2-3, now subsumed by the flag mechanism).

> **v3 → v4 fold (spec panel r2, run `panel-slice5a0-spec-r2b-…-1b23b1` supersedes `…-r2-…-b49829`; agy/grok/
> cold-Opus approve/P2, codex-sol@high needs-changes/P1).** The P1 was **verified against the code** before
> folding:
> - **P1 (codex-sol r2, CONFIRMED) — the `turn_clock_monotonic` accumulator is lost across a mid-turn tailer
>   restart.** The flag closes the r1 hole only while ONE `TranscriptTailer` observes the whole turn — but the
>   accumulator + `turn_started_ts` are in-memory (`tailer.py:70-86`; `poll()` commits only the byte offset,
>   `:188-190`; a rediscovered session gets a **fresh** tailer, `service.py:129-132`), and nonzero-offset suffix
>   resume is a design-contract path. So: opening `user`@T=10 is read + the offset committed at EOF; the service
>   restarts before the assistant line arrives; the fresh tailer reads only the backward suffix (thinking@T=5,
>   close@T=6), baselines from the suffix, and stamps `true` (and `turn_started_ts` is gone) → wrong-but-green
>   latency, now on the **restart axis**. cold-Opus flagged the same shape (rated P2). **Fold — the fail-safe
>   inversion ([[fail-safe-when-reviewer-keeps-finding-misses]]):** three rounds found three *completeness* gaps
>   of one class (clock-validity r0, trace-only r1, restart r2), so v4 stops patching axes and **inverts the
>   default**: `turn_clock_monotonic = true` requires *proof of complete observation of the turn's full causal
>   stream from its opening record*; every non-proof (mid-restart without restored state, missing `timestamp`, a
>   drifted/unmapped causal record, suffix-only) ⇒ `false` ⇒ O4 NULLs. Recovery-completeness is achieved by
>   persisting the accumulator atomically with the inode-qualified offset (or a back-scan); its absence is safe
>   (false), never wrong-true. See the **Completeness invariant**, Deliverable 4, O4, Tests.
> - **P2s folded:** flag authoritative on `turn_completed` **only** (`turn_started` carries just
>   `turn_started_ts`; provisional-true footgun — codex F3 / grok P2-1); pin the raw clock scan **pre-`map_line`**
>   so a `DriftError`-skipped or newly-shaped causal record can't bypass the accumulator (codex F2 / agy);
>   intermediate-inversion deny-proof fixture — non-decreasing bookends + inverted trace-only middle + tools
>   (grok P2-2).

> **v4 → v5 fold (spec panel r3, run `panel-slice5a0-spec-r3-…-10de5c`; agy approve/none, codex-sol@high +
> grok + cold-Opus needs-changes/P1 — THREE verified P1s, all the same completeness class via new recovery-
> machinery mechanisms).** r3's P1s: grok — the v4 back-scan recovery arm recomputes only the opening, not the
> full prefix, so a suffix-only `true` survives; codex-sol — a **resumable idle-finish** (`service.py:202-207,
> 497-504`, a documented+tested claude-tail behavior) closes a still-open turn with a durable `true` before a
> later backward record arrives; cold-Opus — a `json.loads`-failing line hits the generic skip arm
> (`tailer.py:160-166`) and bypasses the accumulator. **Mark's Option-B scope call (2026-07-14): stop building
> recovery machinery; delete the surface the P1 class lives on.** v5 rewrites the Completeness invariant to
> **pure fail-closed single-continuous-observation** — `turn_clock_monotonic = true` only when ONE tailer
> generation observed the whole turn opening-to-irrevocable-close with every line cleanly parsed and
> non-decreasing; any restart / idle-resume / skipped-or-corrupt line / missing-ts / backward step ⇒ `false` ⇒
> O4 NULLs, **by construction, with no persistence or back-scan**. All three r3 P1s dissolve (nothing to get
> wrong). The two pins Mark set: (1) an operationally-checkable **generation-id continuity predicate** (stamped
> at `turn_started`, verified at `turn_completed`) so we don't trade recovery-machinery P1s for
> continuity-detection P1s; (2) "irrevocable close" = next human `user` line, same session — the **hard**
> version (recover latency across resumable idle / restart) + the latency-recovery itself move to 5a as named
> obligation **O5**. See the Completeness invariant, Deliverable 4, O4, **O5**, Tests, SP0-2.

> **v5 → v6 fold (spec panel r4, run `panel-slice5a0-spec-r4-…-ea7e07`; grok approve/P2, codex-sol@high + agy +
> cold-Opus needs-changes/P1).** All four seats **confirmed Option B closed the completeness-machinery class**
> (continuity predicate unforgeable for restart/idle/corrupt; O5 handoff + cost + machinery-deletion sound). The
> three r4 P1s are **targeted, adjacent, and stay pure-Option-B (in-memory, no recovery machinery)**:
> - **P1 (codex-sol) — continuity must be a *scan* generation, not an *object* generation.** The v5 predicate
>   equated one `TranscriptTailer` object with one continuous byte scan — false: the SAME object re-reads from
>   byte 0 (offset-key loss / truncate-heal / corrupt-offset heal / inode replacement — all inside `poll()`,
>   `tailer.py:88-103`, NOT via a fresh tailer) without a new generation. A replayed opening record then acts as
>   a false "next-human" close and earns a durable `true` before a later backward record (codex probe-confirmed:
>   same-object byte-0 re-read after offset-key loss). **Fix:** track expected `(inode, next_offset)` in memory;
>   any discontinuity while a turn is open sets the open turn sticky-`false`; a replayed opening record is never
>   an irrevocable close.
> - **P1 (agy) — `turn_index` resets on restart, colliding under `epoch=1`.** `self.turn_index` is in-memory
>   (`tailer.py:72`) and a fresh tailer (`service.py:129-132`) restarts it at 0; with claude-tail `attempt_epoch`
>   pinned to `1` (R1) there is no dimension to distinguish, so a partial (nonzero-offset) resume re-emits
>   `turn_index 1,2,…` colliding with pre-restart turns. **Fix:** persist `turn_index` alongside the
>   inode-qualified offset (position state, exactly like the offset — NOT the rejected latency-validity
>   accumulator); a nonzero-offset resume restores it, a byte-0 re-read resets it to 0 (idempotent re-count).
> - **P1 (cold-Opus / agy) — "irrevocable close = next human `user` line" is too narrow → the majority of turns
>   NULL.** Agentic tool-results are non-`promptId` `user` lines (`mapper.py:73-90`), so a **single-turn cold-seat
>   dispatch** (the bulk of eval traffic) and every session's **final turn** have no next-human-`user` line →
>   never earn `true` even under perfect observation. **Fix:** a **definitive terminal close** — a done-marker
>   (`completed=True`, `tailer.py:81,249-263`) or true session-end — IS an irrevocable close and CAN earn `true`;
>   only a *resumable* idle-finish stays open. Broaden O5 + the cost statement accordingly.
> - **P2s:** per-turn eager sticky-`false` on the skip/`DriftError` arm, NOT the cumulative `skipped_lines`
>   counter (agy/grok); a resumable idle-finish emits **nothing** authoritative for the open turn (remove the
>   "or false" ambiguity — grok).

> **v6 → v7 fold (spec panel r5, run `panel-slice5a0-spec-r5-…-26889a`; agy approve/none, grok + cold-Opus
> approve/P2, codex-sol@high needs-changes/P1).** All seats confirmed the r4 fixes closed (scan-continuity,
> `turn_index` mechanism, P2 folds; nobody forged a `true`). codex-sol's two last P1s are consequences of the
> r4 fixes, both **verified-buildable and pure-Option-B**:
> - **P1-1 (codex-sol) — the in-band done-marker is NOT an irrevocable close.** v6 let a `[ARB_SEAT_DONE]` text
>   marker (`tailer.py:249-263`, matches anywhere in any assistant block) earn `true`. But content is not
>   terminal: a torn trailing partial line sets `at_eof=True` **without** being parsed (`tailer.py:118-126`) and
>   a finished tailer whose file grows is **resumed** (`service.py:497-504`), so a durable `true` can precede a
>   later backward record. **Fix (uses code that already exists):** bind terminal-earn to the **external,
>   irrevocable** signal — `_cold_seat_completed` reads the sidecar `{stem}.arb-tail.json` `completed:true` and
>   then **deletes the files** (`service.py:197-201,478-486`, cannot resume), or a `draining` teardown
>   (`service.py:187-193`) — AND require a **clean EOF** (last line ended on a newline, no torn partial). Split
>   `clean_eof` from the current torn-conflating `at_eof`. The in-band marker alone never earns `true`.
> - **P1-2 (codex-sol) — `turn_index` has no legacy-offset migration.** The deployed `OffsetStore` holds bare
>   integers (`offset.py:19-26,36-40`); a natural back-compat read (legacy `"O"` → offset O, index 0) emits the
>   first post-deploy turn at a colliding index, and a later byte-0 re-count re-keys it — the "idempotent
>   re-count" breaks across the format transition. **Fix:** a **versioned atomic `{v, offset, turn_index}`**
>   value; a legacy bare **nonzero** integer must NOT resume at index 0 — force a **byte-0 replay/recount** (or
>   derive the ordinal from the prefix) to establish `{v,0,0}` before emitting any new suffix; bare `0` → `{v,0,0}`;
>   corrupt composite → reset both. Add a migration deny-proof.
> - **P2 pins:** `path|inode` (not "device/inode") to match the offset key (`offset.py:10-11`, grok); pin the
>   composite encoding + poll ordering (grok, subsumed by P1-2); keep persisted `turn_index` consistent with the
>   emit-failure prefix-commit offset (cold-Opus); enumerate which `finish()` call-sites are terminal-earning
>   (default non-terminal — cold-Opus); note continuity rests on R1's append-only + unique-`path|inode` property.

> **v7 → v8 fold (spec panel r6, run `panel-slice5a0-spec-r6b-…-5f5349` supersedes `…-r6-…-dd24ef`; agy
> approve/none, cold-Opus approve/P2, codex-sol@high + grok needs-changes/P1). Legacy `turn_index` migration
> PASSED (codex-verified).** Two terminal-close P1s + **Mark's Option-C scope call (2026-07-14) with a
> two-phase-fence tightening**:
> - **P1-2 (grok + codex-sol) — the sticky in-band marker branch shadows the sidecar.** The service checks
>   `state.tailer.completed` (set by `[ARB_SEAT_DONE]`, `tailer.py:249-263`) at `service.py:193-196` and
>   `continue`s **before** the sidecar branch `service.py:197-201` — so a normally-finishing cold seat (which
>   emits the marker) finishes via the non-earning marker path and the sidecar-earn is never reached → every
>   single-dispatch turn NULL (the regression the terminal close was meant to prevent). **Fix:** evaluate the
>   sidecar/draining terminal paths **before** the in-band marker; the marker is never a terminal earn and must
>   not suppress the sidecar.
> - **P1-1 (codex-sol) — the sidecar `completed:true` is a boolean, not a finalization fence.** `.output` is a
>   **symlink** to the real subagent JSONL (`subagent_start.py:30,38-41`); `subagent_stop.py:22-24` records only
>   `completed=True` (no final cursor); the service unlinks the **symlink**, not its target (`service.py:493`),
>   so the underlying transcript inode can receive a delayed backward record after a durable `true`. **Mark's
>   Option-C fix (keep single-dispatch turns earning `true` — they are the bulk of eval traffic; D's descope
>   would make the slice vestigial):** a **finalization fence** that is fail-closed **by construction**, not by
>   the producer being quiescent:
>   - `subagent_stop` records `{final_inode, final_size}` in the sidecar atomically (`write_json_atomic` =
>     temp + `os.replace`, `common.py:245-253`).
>   - **Two-phase earn (Mark — closes the flush/TOCTOU race):** the tailer earns `true` for a sidecar-terminal
>     close only when it reads to a `clean_eof` at **exactly** `final_size` with `inode == final_inode`
>     (⇒ *provisional*), THEN **re-stat after a short settle interval (≥1s)** and commit `true` only if inode +
>     size are **still unchanged**; else `false`. Any mismatch → `false`, `size < final_size` → **wait** (not
>     false), no partial credit, no re-read retry. This is fail-closed even if the producer writes post-stop.
>   - **Citable no-post-stop-writer (bridge side):** no bridge code writes the transcript target — only symlink/
>     sidecar `unlink`/`symlink_to` (`service.py:493`, `subagent_start.py:40-41`, `common.py:212-216`); the sole
>     transcript writer is Claude Code. The producer's post-`SubagentStop` flush ordering is NOT statically
>     citable from this repo → it is a **version-pinned live-gate canary** (below), NOT an assertion; two-phase
>     makes correctness independent of it.
>   - **Scoping (Mark):** the fence's quiescence claim is scoped to the **subagent-stop** path ONLY; the
>     resumable **idle-finish** path (`service.py:497-504`, "idle-finished mid-think, now writing its report") is
>     **out of contract** (never a terminal earn) — do not conflate them.
> - **P2 pins:** `path|inode` not "device/inode" (grok); a `FileNotFoundError`/missing-key finish is non-terminal
>   (cold-Opus); the deletion call is `service.py:486-495`.

> **v8 → v9 fold (spec panel r7, run `panel-slice5a0-spec-r7-…-5a4136`; grok approve/P2, codex-sol@high + agy +
> cold-Opus needs-changes/P1). Mark's Option-D scope call (2026-07-14), forced by a writer-identity check.**
> r7 found: the sidecar-before-marker *reorder* is insufficient (cross-poll: a `marker+at_eof` poll before the
> `SubagentStop` hook writes the sidecar still finishes a cold seat non-earning → majority NULL — agy/cold-Opus/
> grok); and **codex reproduced a forge disproving "two-phase = fail-closed by construction"** — the settle is a
> time-based *debounce*, and a producer append **after** the confirming re-stat (through a still-open descriptor)
> leaves a durable wrong-`true`; no elapsed-time check establishes producer-closure-happens-before-stat.
> **Writer-identity check (Mark's decider):** the cold-seat transcript is written by the **parent Claude Code
> process** — cold seats are native `SubagentStart`/`SubagentStop` hooks (`~/.claude/settings.json`), i.e.
> **in-process** subagents (no per-subagent process whose exit closes the fd), and `_resumed_after_finish`
> (`service.py:498-499`) proves the parent writes the transcript incrementally + post-finish. Producer flush
> ordering vs the hook is a Claude Code internal **not citable from this repo** ⇒ pessimistic branch ⇒ **Option
> D:** **5a-0 earns `turn_clock_monotonic = true` ONLY at a next-human-`user` line** (same continuous scan) —
> **no sidecar/marker/two-phase terminal earn at all.** The finalization fence, two-phase settle, version-pinned
> canary, `subagent_stop` `{final_inode,final_size}`, and the marker-vs-sidecar precedence are **all removed**
> (the marker P1s dissolve — nothing earns at a terminal stop). **Single-dispatch / last / terminal turns ⇒
> `false`/NULL** (they have no next-human-`user` boundary); recovering their latency — via a real producer-close
> handshake designed in 5a's own cycle — is **obligation O5 (5a)**, "where it always belonged in that world"
> (Mark). Honest bounded cost: single-dispatch turns (the bulk of eval traffic) get NULL turn-latency in 5a-0 —
> visible, countable, never wrong-`true`. This preserves the never-wrong-true floor **by construction** (an
> unprovable external property can't underwrite an absolute floor — Mark). Retracted: the v8 "fail-closed by
> construction" claim (a happens-before asserted without a mechanism — the v5-last-wins error class).

> **Scope boundary (Mark's option-1 call, load-bearing — hold it).** A slice must NOT own or verify claims
> about code outside its boundary. 5a-0 narrows to its **verifiable boundary** (the bridge's own capture
> code) + provides the **capture primitive**, and carries every cross-boundary concern as a **named, cited,
> testable obligation** — the M2 reliable-inbox precondition (cited to bridge code) and the O1–O3 projection
> obligations (handed to 5a). Per [[cross-slice-claims-need-citation]], every claim in this spec about code
> 5a-0 does not own is grounded in a `file:line` citation, never authorial assertion.

---

## Spec-stage P2 backlog — RESOLVED here (carried from the r7 CONVERGED design panel)

The r7 panel returned unanimous approve / zero P0/P1 and carried a P2 cluster — all on the **claude-tail
epoch** — into the spec stage to avoid authoring an unreviewed resolution at the design stage. Resolved as
follows (the spec panel verifies these):

### R1 — Pin the claude-tail `attempt_epoch` to the constant `1` (never allocated, never bumped). *[5a-0 owns; cited]*

The general `attempt_epoch` machinery (Component 2 — a task-scoped Redis `INCR` that increments at each fresh
execution boundary) applies to the **bridge-dispatch producers** (agent_sdk, pi_rpc, codex, agy, grok, cursor),
which genuinely re-execute on reliable-inbox recovery and lease-takeover. **claude-tail is different and pins
to `1`:**

- claude-tail is a **single, forward-only tailer per transcript path**: `poll()` seeks the persisted byte
  offset and reads forward, committing `new_offset` (`claude_tail/tailer.py:88-103,188-190`). One
  `TranscriptTailer` owns one transcript file.
- A Claude transcript is **append-only** — Claude Code appends turn lines; it does not re-execute and
  overwrite prior turns in place (verified against a live transcript: monotonically-appended `user`/`assistant`
  lines each with a fresh `uuid`).
- The re-read-from-byte-0 paths are all recoveries of the *same* session's existing transcript, not competing
  re-executions (exhaustive per the offset contract — F1 fold): (i) truncate-heal (`tailer.py:93-95`,
  `offset > st_size ⇒ commit 0`); (ii) corrupt-offset heal (`offset.py:27-34`, unparseable stored offset ⇒
  reset 0); (iii) **absent inode-qualified offset key** — `OffsetStore.get` returns 0 whenever the key is
  missing (`offset.py:19-22`): first observation, key loss/eviction, or restore-without-offset; (iv) **inode
  replacement/rotation** — the offset key is `path|inode` (`offset.py:10-11`, `tailer.py:90-92`), so a new inode
  selects an absent key and starts at byte 0. None is a new *attempt* — the tailer merely re-reads bytes of an
  unchanged (or newly-observed) append-only session; the constant epoch `1` is conservative for all four.

Therefore claude-tail has **no "fresh execution boundary"** of the kind Component 2 defines, and design
Component 4's "epoch increments only on a byte-0 full replay" machinery is **removed for claude-tail** and
replaced by the literal constant `attempt_epoch = 1`. This **dissolves the three P2 sub-edges at once**:
(a) a byte-0 replay re-stamping `sent_at` can never trigger an epoch-driven O2 replace (no epoch bump ever
occurs); (b) the deploy-migration "absent counter ⇒ epoch 1" edge vanishes (epoch is a compile-time constant,
no counter to migrate or find absent); (c) the "allocate epoch after `run_id` resolution" ordering edge
vanishes (no per-task epoch allocation for claude-tail at all). It also subsumes the round-6 **M1** concern
(a suffix resume must not let O2 delete the un-replayed prefix): claude-tail never emits a higher epoch, so O2
never fires on a claude-tail row.

### R2 — claude-tail derives turn latency from the transcript's own line `timestamp`, not emit-time `sent_at`. *[5a-0 owns the capture field; 5a owns consumption — named obligation O4]*

Pinning the epoch to `1` removes the epoch-driven replace, but does **not** by itself make claude-tail latency
idempotent: today the durable record stamps `sent_at=_now()` — wall-clock **at emit time**
(`tailer.py:352,384`, `_now()` = `datetime.now(timezone.utc)`). A truncate-/corrupt-heal re-read that
re-emits an earlier turn stamps a *later* `sent_at`, so any 5a latency computed from `sent_at` is
non-idempotent across re-reads.

**Resolution (verified buildable):** every Claude transcript `user`/`assistant` line carries a top-level
ISO8601 `timestamp` (verified present on 105,692/105,692 sampled `user`/`assistant` records; e.g.
`2026-07-13T19:42:50.294Z`). claude-tail carries that transcript-native timestamp into the eval **payload**
(the event `data`, so the allowlist extract is load-bearing — cold-Opus fold) as `event_ts` — a property of the
transcript, **stable across re-reads → idempotent by construction**, independent of when the tee (re-)emits.
`map_line` does not currently parse it (`mapper.py:26-34`), so this is a small, 5a-0-owned capture addition
(Deliverable 4). It creates one named 5a obligation: **O4** below.

> **`event_ts` is an idempotent *timestamp*, NOT a validated clock (codex-sol P1 fold).** The transcript
> `timestamp` is stable across re-reads but is **not** guaranteed monotonic with the causal message chain: in
> the sampled corpus **6 of 22,347** direct user→assistant pairs have the assistant timestamped *earlier* than
> its user parent (§v1→v2 fold). So `event_ts` fixes *idempotency* but not *clock validity*. 5a-0's job is to
> carry the transcript's own timestamp **faithfully** — it must NOT "correct" it (that is projection's job,
> O4). The correctness contract for turning `event_ts` into a latency number lives entirely in **O4** (5a):
> latency is emitted only when the turn's edge `event_ts` sequence is causally non-decreasing, else NULL/unknown.
>
> **Fail-closed on an absent `timestamp` (grok F5 / cold-Opus fold).** For any line that produces a turn/tool
> eval edge, `event_ts` MUST come from the line's own `timestamp`; if that field is absent, the producer
> **fails closed** — it omits `event_ts` (so 5a marks the edge's latency unknown) and increments a
> `claude_tail_missing_ts` counter. It MUST NOT silently substitute emit-time `sent_at` (that reintroduces the
> non-idempotency R2 exists to remove). Any **synthetic/injected** edge (e.g. the tailer-injected
> `turn_started`) takes its `event_ts` from the transcript line that triggered it — never `_now()`/`sent_at`
> (grok P2-3). Deny-proofs in Tests.
>
> **Full causal-clock coverage — the flag (codex-sol r1 P1 fold).** O4 can only reject an inversion it can
> *see*, but `model_text`/`model_thinking` (and dropped pure-text `user` lines) never reach the eval stream
> (`tailer.py:316-318`; `mapper.py:73-90`), so a backward pure-text/thinking child is invisible to an
> eval-edge-only check. **5a-0's tailer reads every record already** — so for each logical turn it computes,
> over the FULL causal record stream (every `user`/`assistant` record of the turn in append order, `isMeta`/
> `isSidechain` excluded), a bounded **`turn_clock_monotonic`** boolean (true iff each record's `timestamp` is
> non-decreasing along that order; false on any backward step OR any required record missing `timestamp`) and
> stamps it (+ `turn_started_ts` = the opening human-`user` record's `timestamp`) on the turn's durable eval
> edge. **This is a faithful *observation* of the transcript's own clock ordering, not a correction** (5a-0
> never reorders/alters a timestamp); the NULL-vs-number *decision* stays 5a's (O4). It catches trace-only and
> intermediate inversions without routing any text to eval.
>
> **Completeness invariant — pure fail-closed, single-continuous-observation (Mark's Option-B scope call,
> 2026-07-14; [[fail-safe-when-reviewer-keeps-finding-misses]]).** 5a-0 carries **no recovery machinery** — no
> accumulator persistence, no back-scan, no provisional close. `turn_clock_monotonic` is authoritatively `true`
> **only when ONE continuous tailer session observed the turn's ENTIRE causal record stream in memory — from its
> opening human-`user` record through its irrevocable close — with every in-turn line successfully parsed,
> timestamped, and non-decreasing.** In EVERY other case it is `false` (⇒ O4 NULLs), by construction:
> - **Continuity predicate = a SCAN generation, not an object generation (operationally checkable — Mark pin 1,
>   codex r4).** Object identity is NOT scan continuity: the SAME `TranscriptTailer` object re-reads from byte 0
>   inside `poll()` (offset-key loss `offset.py:19-22`, truncate-heal `tailer.py:93-95`, corrupt-offset heal
>   `offset.py:27-34`, inode replacement `offset.py:10-11` — none reconstructs the object, `service.py:129-132`).
>   So continuity is tracked as a **contiguous cursor**: after each poll the tailer records the expected
>   `(path|inode, next_offset)`; before the next poll, if the actual `(inode, start_offset)` ≠ that expected
>   continuation (any rewind, forward jump, byte-0 reset, or inode change), the **open turn's validity is set
>   sticky-`false`** (equivalently, the scan generation rotates) — it can never remain eligible for `true`. A
>   **replayed opening record (same `uuid`/`promptId`) is NOT an irrevocable next-human boundary**: it may
>   restart observation but cannot authoritatively close the already-open turn. A fresh tailer after restart
>   (`service.py:129-132`) likewise has no in-session opening ⇒ any turn it closes is `false`. **No persistence
>   is needed: a scan that cannot prove contiguous observation from the opening fails closed by construction.**
> - **Every in-turn line must be cleanly observed (per-turn, eager — agy/grok r4 P2).** Any of these within a
>   turn ⇒ that turn is `false`: a line that fails `json.loads`/is non-`dict` (generic skip arm,
>   `tailer.py:160-166`); a `map_line`/`DriftError` or unrecognized causal record; a required causal record
>   missing `timestamp`; a backward `timestamp` step. The tailer sets a **per-turn** sticky-`false` **eagerly on
>   the skip/`DriftError` arm itself** (reset at each turn open) — it MUST NOT infer cleanliness from the
>   *cumulative* lifetime `skipped_lines` counter (`tailer.py:86,161`; a pre-turn skip must not falsely fail a
>   later clean turn). The scan sees the raw `obj` before `map_line` (`tailer.py:207-214`).
> - **Irrevocable close = next-human-`user` ONLY (Mark's Option-D, r7).** An authoritative `true` close requires
>   the **next human `user` line observed by the same continuous scan** — the one turn boundary 5a-0 can prove
>   from its own capture (it rests only on the transcript being append-causal-ordered, cited R1). **There is NO
>   terminal earn at a stop:** the cold-seat sidecar `completed:true`, the in-band `[ARB_SEAT_DONE]` marker, and
>   any tailer `finish()` are used only for the tailer's **finish/cleanup lifecycle** — they NEVER earn
>   `turn_clock_monotonic = true`. **Decisive rationale (what actually forces this):** capture has **no citable
>   producer-close happens-before** — producer flush/close ordering vs the `SubagentStop` hook is a Claude Code
>   internal not citable from this repo, and a time-based settle is a *debounce, not a fence* (codex r7 reproduced
>   a durable wrong-`true` via a post-re-stat append) — so **no stop may underwrite `true`.** (Consistent
>   *inference*, not a proof from the cited lines: the transcript writer is the long-lived parent — cold seats are
>   native in-process `SubagentStart`/`SubagentStop` subagents `~/.claude/settings.json`, and
>   `_resumed_after_finish` `service.py:497-504` shows post-tailer-finish growth is possible — so there is no
>   process-exit fd-close to give the happens-before. Whichever way, terminal finality is unproven ⇒ Option-D.)
>   So a **single-dispatch / last / terminal turn — no next human `user` line — is `false` ⇒ NULL**; its latency
>   is **handed to 5a for recovery (a real producer-close handshake) or explicit descope under O5**. This keeps
>   the never-wrong-`true` floor **by construction** (no unprovable external property underwrites it).
>
> **The final flag is authoritative only on `turn_completed`** (`turn_started` carries only `turn_started_ts`;
> codex F3/grok P2-1). **Cost (honest, bounded — Option-D, r7):** a turn gets `false`/NULL turn-latency when it is
> not closed by a next-human-`user` line — i.e. any genuine interruption AND **every single-dispatch/terminal
> turn** (the bulk of eval traffic), whose latency is recovered by 5a via O5's producer-close handshake. NULL is
> the correct value for "5a-0 cannot prove this turn's completeness from its own capture," visible and countable,
> never a subtly-wrong number. **Recovering valid latency for the NULL turns — genuinely-interrupted ones AND
> every single-dispatch/terminal turn — is handed to 5a as obligation O5** (below), including the producer-close
> handshake; the reconstruction/finalization machinery is designed in 5a's own cycle, not speculatively in this
> capture slice (5a-0 doesn't own the producer lifecycle — the r7 lesson).

---

## Deliverables

### 1. Canonical `tool_call_id` coalescing (shared helper)
A single helper `canonical_tool_call_id(data) = first_nonempty(data.tool_call_id, data.tool_use_id,
data.item_id)` — provider ids before presentation ids — used by every producer so the SAME `tool_call_id`
lands on both tool edges (`command_started` / `command_finished`). claude-tail's `_item_id`
(`tailer.py:337-341`) currently derives a presentation id from `tool_use_id`; route it through the helper and
emit the coalesced value as `tool_call_id`.

### 2. Deterministic `turn_index` + `attempt_epoch` (bridge-dispatch capture)
- **`turn_index`** — a per-`(run_id,task_id)` integer ordinal the bridge assigns in execution order at each
  proven **logical-turn boundary** (design fold I); stamped on turn/tool edges until the next advance; scope
  `(run_id,task_id)`, **no `seat_id`** (design fold J — disjoint `task_id`s, `identity.py:38,70`).
  Deterministic ⇒ a re-run reproduces the same ordinals (stable KEYS; durability = determinism, no persistent
  Redis id-map).
- **`attempt_epoch`** — a monotonic integer per `(run_id,task_id)`, stamped on **every** eval event, that
  **increments at each fresh execution boundary** for the bridge-dispatch producers: (a) a reliable-inbox
  recovery re-queues + re-runs a parked request (`recover_processing_envelopes`, `bridge.py:870`) →
  epoch += 1 for that task before re-processing; (b) a lease-takeover where a successor claims an identity →
  the successor's epoch strictly exceeds any a still-live predecessor could stamp. Sourced from durable
  state so it survives the very restart it marks. **Guarantees 5a can rely on:** epoch is non-decreasing per
  `(run_id,task_id)`; one physical execution stamps ONE epoch on all its events; a stale predecessor's events
  carry an epoch strictly less than the current owner's.
  - **Source (SP0-1 consensus, fold):** a **task-scoped Redis `INCR` per `(run_id,task_id)`**
    (`RedisCli.incrby`, `redis_io.py:279-284` — the same durable substrate that already carries
    `:processing`), **allocated once at accepted physical-execution start and snapshotted (read-once, cached in
    the immutable/local execution context)**, so every event of one execution carries that one scalar even
    under the async eval flusher, and a stale predecessor's cached value is strictly lower. **NOT** a
    per-daemon boot token / raw UUID (unordered → fails cross-process takeover ordering). Exact key string +
    allocation call-site are the remaining spec-panel pin (SP0-1 below).
- **claude-tail:** `attempt_epoch = 1` constant (R1). It participates in `turn_index` normally.
- Out-of-turn events (`task_started`, `agent_sdk_subscription_audit`, `task_continuing`) carry no
  `turn_index`; the **terminal event is stamped BEFORE the active ordinal is cleared** (design fold K+ —
  clearing first would unstamp the terminal).

### 3. agent_sdk & pi_rpc semantic fixes (from design Component 3)
- **agent_sdk:** rename the gate `command_finished` → `tool_permission_decided`; a real `command_finished`
  comes from `ToolResultBlock` with `tool_call_id = block.tool_use_id`; a deny produces exactly one true
  finish; `agent_sdk_subscription_audit` stays out of turn scope.
- **pi_rpc:** `command_finished` only from `tool_execution_end`; demote `toolcall_end`; drop the first-wins
  dedup; add `turn_completed` on a clean `agent_end`; fix `tests/test_pi_rpc.py:472-564`.

### 4. claude-tail logical-turn lifecycle + epoch pin + transcript-timestamp latency (from design Component 4, R1, R2)
- **Logical turn:** one human `user`-prompt cycle. **`promptId` is present only on human `user` lines** (0/99
  on assistant lines in the sampled transcript — grok F3), and `map_line` **drops pure-text human `user` lines**
  (`mapper.py:73-90` returns `[]`), so `turn_started` **cannot** be derived from a mapped event — it must be
  **injected at the tailer** when a human `user` line is observed (keyed on that line's `promptId`).
  `turn_completed` with an authoritative `true` fires **only at a next-human-`user` close** observed by the same
  continuous scan (Mark Option-D, r7). **No terminal stop earns `true`** — the sidecar `completed:true`, the
  in-band `[ARB_SEAT_DONE]` marker, and any `finish()` drive only the tailer finish/cleanup lifecycle. A
  single-dispatch/last/terminal turn (no next human `user` line) is `false`⇒NULL; its latency is 5a's via O5.
  Its `event_ts` is the
  turn's **last causal record's** `timestamp` (never the next cycle's boundary, never `_now()`/`sent_at`).
  In-cycle association uses append order / `parentUuid`. Per-assistant ordinal stays trace-only.
  `_parse_line` **carries `uuid`** so edges correlate. `isMeta`/`isSidechain` records are out-of-turn: they
  neither open/close a turn, advance `turn_index`, nor contribute to `turn_clock_monotonic`.
- **Causal-clock coverage — pure fail-closed single SCAN-observation (Mark's Option-B call; R2 Completeness
  invariant; codex r4 scan-continuity):** per logical turn the tailer tracks, IN MEMORY, `turn_started_ts`, the
  expected `(inode, next_offset)` continuation cursor, a per-turn sticky-clean bit, and **`turn_clock_monotonic`**
  — a bool over the FULL causal record stream (every `user`/`assistant` record in append order incl. trace-only
  `text`/`thinking` and the dropped pure-text `user` line; `isMeta`/`isSidechain` excluded). `true` requires ALL
  of: **scan continuity** — the same scan observed the opening through the close with no cursor discontinuity
  (any rewind / byte-0 reset / inode change while the turn is open, or a fresh tailer with no in-session opening,
  ⇒ `false`; a replayed opening record does not close the open turn); **clean lines** — every in-turn line
  cleanly observed, the per-turn sticky-`false` set **eagerly** on the skip (`tailer.py:160-166`) or `DriftError`
  arm (NOT inferred from the cumulative `skipped_lines` counter); and every `timestamp` **non-decreasing**. **Any
  failure ⇒ `false`, by construction — no persistence of the clock accumulator, no back-scan, no recovery.** The
  scan sees the raw `obj` before `map_line` (`tailer.py:207-214`). **Final `turn_clock_monotonic` is stamped only
  on the closing `turn_completed`; `turn_started` carries only `turn_started_ts`** (codex F3 / grok P2-1). Both
  bounded scalars in the eval **`data`** — no message text/thinking body enters eval. (Recovering valid latency
  for genuinely-interrupted turns = obligation **O5**, 5a.)
- **`attempt_epoch = 1`** stamped on every claude-tail eval event (R1) — a constant, no allocation.
- **`turn_index` restart-stable + on the EVAL path + versioned migration (grok note; agy r4 P1; codex r5 P1-2):**
  claude-tail must stamp `turn_index` into the **eval `data`/payload** (today only the *trace* tee,
  `tailer.py:368`). Because `attempt_epoch` is pinned to `1` (R1), `turn_index` MUST be **stable across a tailer
  restart** or post-restart turns collide under `(run_id,task_id,epoch=1)`. `self.turn_index` is in-memory
  (`tailer.py:72`) and resets on a fresh tailer (`service.py:129-132`), so persist it as part of a **versioned,
  atomically-written composite position value `{v, offset, turn_index}`** in the same `path|inode` offset key
  (`OffsetStore`, `offset.py:10-11,36-40` — a single `SET`, so atomic) — position/identity state of the SAME
  *kind* CT-1 already persists, NOT the rejected latency-validity accumulator. A nonzero-offset resume restores
  `turn_index` (turn 4 stays `turn_index=4`) consistent with the committed offset (incl. the emit-failure
  prefix-commit path, cold-Opus r5); a byte-0 re-read resets to `{v,0,0}` (idempotent re-count reproducing
  identical ordinals — the design's stable-KEYS property). **Legacy migration (codex r5 P1-2):** a pre-existing
  **bare-integer** offset value must NOT be read as `{offset:N, turn_index:0}` (that collides + re-keys on the
  next byte-0). On first encounter of a legacy bare nonzero integer, force a **byte-0 replay/recount** (or derive
  the ordinal from the prefix) to establish `{v,0,0}` before emitting any new suffix; bare `0` → `{v,0,0}`;
  corrupt composite → reset both. This is orthogonal to the (unpersisted, fail-closed) clock flag.
- **Latency basis (R2):** `_parse_line` extracts the line's top-level `timestamp` and the tailer puts it into
  the eval event **`data` as `event_ts`** (a bounded ISO8601 scalar in the payload — NOT a top-level record
  field — so the `EVAL_ALLOWLIST` extract is load-bearing; cold-Opus fold). `sent_at` stays as the emit-time
  liveness/ordering signal (`_now()`/`sent_at` for live/trace tees unchanged); `event_ts` is the **idempotent
  latency basis** 5a consumes under the O4 temporal-validity contract. Absent `timestamp` ⇒ omit `event_ts`,
  fail closed (R2) — never `sent_at`.

### 5. Allowlist additions (`eval_tee.py:10-19`)
Add **five new** members to `EVAL_ALLOWLIST`: `tool_call_id`, `attempt_epoch`, `event_ts` (the R2 latency
basis), and `turn_started_ts` + `turn_clock_monotonic` (the causal-clock signal, R2/codex-sol P1). **`turn_index`
is already present** (`eval_tee.py:15`) — this slice makes it load-bearing on the claude-tail eval path
(Deliverable 4), it is not a new allowlist member. All additions are **bounded scalars** (ids / ints / ISO8601
timestamps / one bool) — no free text, consistent with the extract-only contract. Bump `EVAL_SCHEMA_VERSION`
only if the panel rules these a breaking change to the pinned correlation fields (they are additive; default =
no bump, no migration — confirm at panel).

### 6. Fleet bridge redeploy + soak
Redeploy all seats (covers claude-tail) after the capture changes land; soak against the live gate below.

---

## Contract: obligations 5a-0 hands to Slice 5a (named, testable — 5a cannot clear its panel without addressing each)

5a-0 emits `turn_index` + `attempt_epoch` (+ `event_ts` and the `turn_clock_monotonic` continuity flag for
claude-tail); **5a's span projection MUST**, using them (O1–O3 verbatim from the converged design; O4 from R2;
O5 from Mark's Option-B call):

- **O1 (was P1-1 timestamp splice):** on an event whose `attempt_epoch` exceeds the stored epoch for
  `(run_id,task_id[,turn_index])`, **REPLACE** — reset `started_at`/`finished_at`/`latency`/`outcome` to the
  new attempt's values (do NOT retain the prior attempt's `started_at`). Result: a re-run's turn latencies are
  the re-run's, never spliced across the crash gap.
- **O2 (was P1-2 tool overcount + ghosts):** on a higher `attempt_epoch`, **delete/supersede the prior
  attempt's turn AND tool rows** for `(run_id,task_id)` (tool `tool_call_id`s re-mint per attempt and won't
  self-conflict; surplus higher-ordinal turns from a shorter re-run must be removed). Result: rollups count one
  attempt, no ghosts.
- **O3 (was P1-3 lease-takeover fence):** **ignore events carrying an `attempt_epoch` strictly less than the
  stored epoch** for `(run_id,task_id)` (a reconnected predecessor). Result: no concurrent mixed rows during
  takeover.
- **O4 (claude-tail latency basis + temporal-validity contract, from R2 + codex-sol P1 r0/r1):** for
  claude-tail turns, compute latency from the carried transcript `event_ts` (transcript-native, idempotent
  across re-reads), **not** from emit-time `sent_at`. **BUT `event_ts` is a stable timestamp, not a validated
  clock** (6/22,347 direct user→assistant pairs are causally backwards, 5 of them trace-only text/thinking —
  §fold): 5a computes a numeric **turn latency** = `event_ts − turn_started_ts` **only when the FINAL
  `turn_clock_monotonic` (read from the closing `turn_completed` edge) is true**; if it is false — a backward
  step anywhere in the turn's full causal record stream (incl. trace-only records), a missing required
  `timestamp`, OR an **incomplete/non-continuous observation** per the Completeness invariant (any restart,
  idle-resume, generation mismatch, skipped/corrupt line, or suffix-only) — the latency is **NULL/unknown with a
  `clock_invalid` outcome flag** — **never** negative/understated, **never** a `sent_at` fallback. Tool-span
  latency likewise uses the eval-visible tool edges' own `event_ts` under the same non-decreasing rule and NULLs
  on a decrease. Because `turn_clock_monotonic` is `true` ONLY on a single-continuous complete observation (Mark's
  Option-B call), O4's `true`-gated latency is sound by construction; every interrupted turn is `false`⇒NULL.
  Result: no re-read, restart, idle-resume, or causally-inconsistent transcript — **including a pure-text no-tool
  turn** — ever materializes a wrong-but-green latency.
- **O5 (recover latency for turns without a next-human-`user` close — handed to 5a; Mark Option-B + Option-D):**
  5a-0 emits `false`/NULL for any turn not closed by a next-human-`user` line — i.e. (a) genuinely-interrupted
  turns (restart / scan-discontinuity / resumable-idle straddle / still in-flight) AND (b) **every
  single-dispatch / last / terminal turn** (which has no next-human-`user` boundary — the bulk of eval traffic).
  **5a MUST** either reconstruct valid latency with its own recovery mechanism — for (b) a **producer-close
  handshake** (a signal causally after the transcript writer has flushed+closed/relinquished the target;
  designed + version-tested in 5a's own cycle, NOT elapsed time) — **or** explicitly descope with rationale
  (accept NULL), tied to the span-exactness decision below. It may not silently leave the latency wrong or
  unmeasured. This reconstruction/finalization machinery is deliberately kept OUT of the capture slice: 5a-0
  does not own the producer's lifecycle, so it cannot soundly prove finality (the r7 lesson) — the handshake
  belongs where its own full cycle can ground it.
- **O-gate (cold-Opus P2 → strengthened r0–r3; Option-B form):** 5a's recovery live-gate asserts **latency
  correctness** (re-run turns show the re-run's real latency, not the crash-gap-inflated splice) **AND causal
  validity** via durable `turn_clock_monotonic = false ⇒ NULL/`clock_invalid`` for each of: (1) pure-text `user`
  + an **earlier** pure `thinking`/`text` child + no tool; (2) an intermediate trace-only inversion between
  non-decreasing bookends with tools; (3) an inversion / plain interruption **straddling a mid-turn tailer
  restart or a resumable idle-finish** (a fresh generation must never close a turn `true`); (4) a corrupt
  (`json.loads`-failing) in-turn line; (5) a **same-object byte-0 re-read** (offset-key loss / truncate /
  inode-swap mid-turn — the replayed opening must not forge `true`). Each must project **NULL/`clock_invalid`**,
  never a number; (6) **any terminal stop** (sidecar `completed:true` / `[ARB_SEAT_DONE]` marker / idle /
  session-end / `finish()`) on a turn with no next-human-`user` line leaves the durable **5a-0 capture flag
  `false`** — and **`false` is REPRESENTED AS THE ABSENCE of a `turn_completed` edge**: 5a-0 emits `turn_started`
  (with `turn_index`/`turn_started_ts`) but NO `turn_completed` for such a turn (`finish()`/idle/sidecar/marker
  drive only the finish/cleanup lifecycle, never a `turn_completed`), so **consumers/O4 MUST treat a turn with
  `turn_started` and no `true` `turn_completed` as flag = `false` ⇒ NULL** (editorial clarification folded from
  the r0 plan panel — the "flag false" and "no true close" phrasings are the SAME fail-closed semantics; not a
  scope change) — so it projects **NULL absent an O5 recovery result** (Option-D: no terminal earn in capture). Then
  the O-gate has two branches matching O5's two outcomes: **(a) if O5 is implemented,** a real terminal-turn
  latency must be supported ONLY by O5's separate causally-after-close handshake evidence and MUST NOT
  mutate/forge `turn_clock_monotonic` (no capture-side terminal earn); **(b) if O5 is explicitly descoped,** the
  terminal result stays NULL. Only a cleanly + contiguously observed turn closing at a **next-human-`user`** line
  projects a real number **from the 5a-0 capture flag**. A gate that checks only row identity, replay-equality,
  eval-visible edges, bookends, or a single object-generation run — or that expects a capture-flag number on a
  terminal/single-dispatch turn — is vacuously green
  ([[graduation-criterion-measures-what-it-claims]], [[vacuously-green-guard-fail-loud]]).

**Open decision queued for 5a's design stage (do NOT resolve here — r5-fatigue):** is crash-recovery span
*exactness* even in scope for a timing/throughput layer, or is "a crashed-and-re-run task may have imperfect
spans" an acceptable documented limitation? Depends on what consumes the latency numbers (if anything
alerts/bills on them, silently-wrong latencies are worse than missing) weighed against the O1–O3 fence-design
cost. 5a either implements **O1–O5** or **explicitly descopes them with rationale** — it may not drop them silently.

## Precondition P-recovery (M2, named + cited — a bridge-infra dependency the epoch guarantee rests on)

5a-0's guarantee that `attempt_epoch` "survives the restart it marks" is **conditional** on the reliable-inbox
recovery being **owner/attempt-fenced**. It is NOT today: `remove_processing` is a body-keyed
`LREM :processing 1 <raw-body>` (`redis_io.py:317-318`) run unconditionally in `process_request`'s `finally`
(`bridge.py:1443-1447`), and ownership-loss cleanup does not join in-flight request threads
(`bridge.py:620-653,671-682`). So a stale-but-live predecessor can `LREM` the successor's re-parked envelope
and lose the higher-epoch request: A parks R (epoch 1), loses its lease but stays live; B claims the identity,
recovers + re-parks the identical R (epoch 2), runs; A's stale `finally` then `LREM`s R, deleting **B's**
parking entry; if B crashes, recovery finds nothing → the higher-epoch request is lost. The epoch stays
monotonic but the execution carrying it does not survive the restart it marks.

**Required fix (pre-existing bridge reliable-inbox defect, contained — NOT absorbed into the capture design):**
make processing acknowledgement **owner/claim-token-owned**, not body-owned — an atomic compare-token-then-remove
(or a per-attempt processing claim) so a stale token cannot acknowledge/delete the successor's claim. This is a
distinct bridge-dispatch-infra concern (a lost request harms every dispatch, not just spans); it is carried as
a **cited precondition** that 5a-0's recovery live-gate depends on and asserts (M2 gate below). Per
[[cross-slice-claims-need-citation]], the claim is grounded in the actual code, not asserted.

## Acceptance criteria

- Every producer emits `tool_call_id` (same on both tool edges), a deterministic `turn_index`, and an
  `attempt_epoch` (constant `1` for claude-tail; INCR-sourced for dispatch producers) into the durable eval
  payload; claude-tail additionally emits `event_ts` in the payload (omitted, fail-closed, when the line's
  `timestamp` is absent — never `sent_at`).
- The **five new** allowlist members land (`turn_index` already present); the extract-only contract holds (no
  free text leaks — `turn_clock_monotonic`/`turn_started_ts` are a bool + a timestamp, never message content).
- `uv run --extra arb-memory pytest tests/` green for the touched producers (`test_pi_rpc.py`, agent_sdk,
  claude-tail tailer/mapper/offset).
- **Deny-proofs present and RED when the guard is removed** (below).
- The live gate passes (below).

## Live gate (5a-0 scope — capture, not projection)

- Producer/pin roster incl. a real interactive claude-tail session; read the durable EVAL STREAM + verify the
  pin SHA.
- **claude-tail epoch-pin + latency idempotency (R1/R2)**: emit an early turn; force a re-read from byte 0
  (each of the four paths — truncate-heal, corrupt-offset heal, missing key, new inode); the re-emitted early
  turn carries `attempt_epoch = 1` (unchanged) and the SAME `event_ts` as its first emission (proves the latency
  basis is transcript-native, not emit-time).
- **claude-tail fail-closed timestamp (R2)**: a transcript line lacking `timestamp` on a turn/tool edge emits
  NO `event_ts` (and bumps `claude_tail_missing_ts`) — it MUST NOT emit `event_ts == sent_at`.
- **claude-tail single-continuous SCAN-observation (O-gate hook, capture side; Option-B, tightened r4)**: the
  turn's eval edge carries a durable **final** `turn_clock_monotonic = false` for each of: (1) pure-text `user` +
  earlier pure `thinking`/`text` child + no tool; (2) intermediate trace-only inversion between non-decreasing
  bookends; (3) a turn interrupted by a mid-turn tailer restart (fresh generation) OR a resumable idle-finish —
  neither closes it `true`; (4) a `json.loads`-failing in-turn line; (5) **a same-object byte-0 re-read** (delete
  the offset key / truncate / inode-swap mid-turn) — the replayed opening must NOT close the open turn `true`
  (codex r4 scan-continuity). A turn observed whole by ONE contiguous scan with all clean non-decreasing lines,
  closed at a **next-human-`user` line** carries `true`; a single-dispatch/terminal turn (no next human `user`)
  is `false`⇒NULL (Option-D). All replay idempotently. The projection-side NULL/`clock_invalid` assertion is 5a's O-gate.
- **`attempt_epoch` emission (dispatch producers)**: a bridge-daemon kill + reliable-inbox recovery stamps a
  STRICTLY HIGHER epoch on the re-run's events than the pre-crash attempt; a simulated lease-takeover stamps
  the successor a higher epoch than a still-live predecessor. (The *consequences* — replace/fence/latency — are
  5a's O-gate, not asserted here.)
- **Owner-fenced recovery (M2 gate)**: takeover with the predecessor still live — B recovers + re-parks R, A
  reaches its stale `finally`, B is killed, and C must STILL recover R and allocate an epoch greater than both
  predecessors (proves the acknowledgement is owner-fenced, not body-keyed). Depends on the M2 fix landing.
- **Deterministic identity**: engine respawn / warm rotation mid-task does not collide `turn_index`.
- **claude-tail `turn_index` restart-stability + legacy migration (agy r4 P1; codex r5 P1-2)**: emit turns 1–3,
  restart the tailer at the committed nonzero offset, emit turn 4 → turn 4 carries `turn_index = 4` (NOT 1), no
  collision under `epoch=1`. **Deny-proof (restart):** don't persist `turn_index` → the restarted tailer re-emits
  `turn_index = 1` and collides. **Deny-proof (migration):** seed a **legacy bare-integer** nonzero offset (no
  `turn_index`), emit the next turn, then force a byte-0 replay → the replay MUST reproduce the same ordinal for
  that turn (i.e. the legacy read forced a recount, not an index-0 resume).
- **claude-tail single-dispatch turn ⇒ NULL, never wrong-`true` (Option-D, r7)**: a single-turn cold-seat
  dispatch (one human prompt, tool-result `user` lines, NO next human `user`) — even cleanly + continuously
  observed and ending at a sidecar `completed:true` / done-marker — emits `turn_clock_monotonic = false` ⇒ NULL
  turn-latency; **no terminal stop earns `true`**. **Deny-proof:** make any terminal stop (sidecar/marker/
  `finish()`) earn `true`, then (simulating a post-stop producer write) append a backward record through a
  still-open descriptor after the earn → a durable wrong-`true` appears (the r7 forge). A next-human-`user` close
  DOES earn `true`.
- Mint boundary (design fold I): engine startup failure produces no phantom `eval_turn`.
- claude-tail: one prompt + ≥2 tool rounds → one `eval_turn`.
- ≥2 tool calls/producer; ≥1 failing/delayed tool (finish from REAL execution); deny-proofs.

## Tests + deny-proofs

- **`tool_call_id` coalescing:** unit — provider id wins over presentation id; both tool edges carry the same
  value per producer.
- **`turn_index` determinism:** a re-run reproduces identical ordinals (all replay-key classes); terminal event
  is stamped before the active ordinal clears (deny-proof: clear-first → terminal loses `turn_index`).
- **`attempt_epoch` (dispatch):** recovery re-run → strictly higher epoch; snapshot-once → all events of one
  execution share one scalar under the async flusher; stale predecessor's cached value strictly lower.
  Deny-proof: remove the snapshot (read per-event) → events of one execution split across epochs.
- **claude-tail epoch pin (R1):** unit — all four byte-0 re-read paths (truncate-heal, corrupt-offset heal,
  missing key, new inode) keep `attempt_epoch = 1` and preserve the same captured primitives on re-read.
  Deny-proof: re-introduce a per-re-read epoch bump → the re-read prefix turn changes epoch (regression).
- **claude-tail latency basis (R2):** unit — `event_ts` equals the transcript line `timestamp`; a re-emit after
  a byte-0 re-read carries the SAME `event_ts`. Deny-proof: fall back to `sent_at` → the re-emit's basis changes.
- **claude-tail absent-`timestamp` fail-closed (R2):** unit — a turn/tool-edge line with no `timestamp` emits no
  `event_ts` and bumps `claude_tail_missing_ts`. **Deny-proof:** a silent `sent_at` substitution → the missing-ts
  test reds (proves the fail-closed guard, not a fallback).
- **claude-tail single-continuous-observation (Option-B; R2 Completeness invariant):** unit —
  (i) *trace-only, pure-text no-tool:* a turn with a trace-only (`text`/`thinking`) child whose `timestamp`
  precedes its parent, no tool → `turn_clock_monotonic = false`. **Deny-proof:** scan only eval-visible edges →
  reds (the r1 hole).
  (ii) *intermediate inversion:* non-decreasing bookends + an inverted trace-only middle + tools → `false`.
  **Deny-proof:** a bookend-only check → reds (grok P2-2).
  (iii) *unclean line ⇒ false:* an in-turn line that raises `DriftError` (unknown block, codex F2) OR fails
  `json.loads`/is non-`dict` (generic skip arm, cold-Opus r3) — the turn is `false` even if the parsed records
  are non-decreasing. **Deny-proof:** ignore the `skipped_lines`/pre-map signal → the corrupt/drift line is
  silently dropped and the turn reds to wrong-`true`.
  (iv) *fresh-generation continuity:* observe opening `user`@T=10, then **destroy + reconstruct the tailer**
  mid-turn and feed the backward suffix (thinking@T=5, close) — the fresh scan, having no in-session opening,
  closes `false`. **Deny-proof:** drop the continuity check → a fresh tailer stamps wrong-`true` and reds (r2).
  (v) *resumable idle-finish (codex r3):* observe opening + prefix, trigger a cold idle-finish, append a
  same-cycle backward `thinking`@T=5, resume → NO durable authoritative `true`. **Deny-proof:** treat idle-stop
  as an irrevocable close emitting `true` → reds.
  (vi) *same-object byte-0 re-read (codex r4 scan-continuity):* one tailer OBJECT observes opening `user`@T=10 +
  clean prefix, then its offset key is deleted / the file truncates / the inode swaps mid-turn so the SAME object
  re-reads from byte 0 and replays the opening; append a later backward `A1`@T=5. The replayed opening must NOT
  close the open turn `true` (the cursor-discontinuity sets the open turn sticky-`false`). **Deny-proof:** check
  only object-generation equality (not `(inode,offset)` contiguity) → the same object forges wrong-`true` and reds.
  (vii) *no terminal earn (Option-D, r7):* a single-dispatch turn ending at a sidecar `completed:true` / done-
  marker / any `finish()` — with NO next human `user` — emits `turn_clock_monotonic = false` ⇒ NULL. **Deny-proof:**
  make any terminal stop earn `true`, then (simulating a post-stop producer write) append a backward record
  through a still-open descriptor after the earn → durable wrong-`true` (the r7 forge — a time-settle is a
  debounce, not a fence). A next-human-`user` close earns `true`.
  (viii) *legacy `turn_index` migration (codex r5 P1-2):* a pre-seeded bare-int nonzero offset forces a byte-0
  recount before the next suffix; a subsequent byte-0 replay reproduces the same ordinal. **Deny-proof:** read
  the legacy value as `{offset:N, turn_index:0}` → the post-deploy turn re-keys on replay and collides.
  The final flag is read only from `turn_completed`; `event_ts`/`turn_started_ts` carried faithfully. (The
  NULL/`clock_invalid` projection assertion is 5a's O-gate; 5a-0 proves faithful carry + a correct
  single-continuous flag only — no persistence/recovery in 5a-0.)
- **allowlist:** the five new fields (+ `turn_index`) pass `extract_eval_payload`; a non-allowlisted sibling is
  dropped.

## Open questions for the SPEC panel

- **SP0-1 — `attempt_epoch` key + call-site (dispatch producers): PINNED (r1 — agy/grok/codex/cold-Opus
  converged; verify the pin holds).** Key = **`RedisConfig.key(f"task:{task_id}:epoch")`** via
  `RedisCli.incrby(key, 1)` — **no `run_id` dimension.** *Why safe:* `task_id` = envelope.id is a UUIDv4 minted
  unique per request (`scripts/agent-dispatch:318-324`; Go retry mints a fresh id, `tools/go-client/dispatch.go:116-121`),
  stable across a reliable-inbox recovery (`recover_processing_envelopes` moves the unchanged raw body,
  `bridge.py:870`), and the existing task substrate is already `task_id`-only (`task:{id}:events|status|result`,
  `redis_io.py:77-84`) — so a `task_id` reused across `run_id`s would already corrupt those keys, not just the
  epoch; requiring `run_id` is unnecessary and worse (`Envelope.run_id` may be `None`). Call-site: allocate once
  at accepted-worker admission **inside `process_request` (`bridge.py:1235`) before the first eval-producing
  `task_started` (`bridge.py:1264`)**, snapshot into the worker's local/immutable context, thread into every
  `build_eval_record`; the completion loop's `task_continuing` reuses the snapshot (no second INCR). Set a TTL
  aligned with `events_ttl`/`status_ttl` so counters don't live forever (hygiene, not correctness).
- **SP0-2 — logical-turn boundary + continuity: PINNED for claude-tail (Option-B; verify the predicate).**
  (i) On a human `user` line, close any open turn **before** opening the next. (ii) `isMeta`/`isSidechain`
  records are out-of-turn: no `turn_index` advance, no open/close, excluded from `turn_clock_monotonic`.
  (iii) `turn_completed.event_ts` = the turn's **last causal record** `timestamp` (never the next cycle's
  boundary — codex r1). (iv) **Continuity predicate = a SCAN generation (Mark pin 1, tightened codex r4):** the
  tailer tracks an in-memory expected `(inode, next_offset)` continuation; `turn_clock_monotonic` earns `true`
  only if the whole turn was observed by one contiguous scan (no rewind / byte-0 reset / inode change / fresh
  tailer while the turn is open — any breaks continuity ⇒ sticky-`false`; a replayed opening record does not
  close the open turn). **No clock accumulator is persisted** (Option B) — a scan that can't prove contiguity
  from the opening fails closed. (v) **Irrevocable close = the next human `user` line ONLY (Mark Option-D, r7).**
  **No terminal stop earns `true`** — the sidecar `completed:true`, the in-band `[ARB_SEAT_DONE]` marker, and any
  `finish()` drive only the finish/cleanup lifecycle. Rationale: capture has **no citable producer-close
  happens-before** (flush/close ordering vs `SubagentStop` is uncitable; a time-settle is a debounce not a fence,
  codex r7 forge), so no stop underwrites `true`; the writer is *inferred* to be the long-lived parent (native
  in-process subagents; post-finish growth possible `service.py:497-504`), leaving terminal finality unproven ⇒
  Option-D. So a single-dispatch/last/terminal turn (no next human `user`) is `false`⇒NULL; its latency + the
  producer-close handshake are 5a's (O5).
  (vi) **`turn_index` is persisted as a versioned `{v,offset,turn_index}` composite with a legacy-bare-int
  forced-recount migration** (agy r4 / codex r5 P1-2) — restart-stable under `epoch=1`. Still genuinely open:
  the agy-tmux logical-turn relocation detail (non-claude-tail).
- **SP0-3 — RESOLVED r0–r7 (Mark's Option-B → Option-C → Option-D scope calls).** Completeness *machinery*
  (r0–r3) closed by construction; r4/r5 tightened scan-continuity + `turn_index` (migration PASSED); r6/r7
  worked the terminal-close question to its conclusion: Option-C's two-phase fence was **disproven** (codex r7
  reproduced a durable wrong-`true` — a time-settle is a debounce, and a **writer-identity check** found **no
  citable producer-close happens-before** at `SubagentStop` — the writer is *inferred* to be the long-lived
  parent CC process — so no statically-provable terminal finality exists) ⇒ **Option-D: no terminal earn in
  capture** — `true` only at a
  next-human-`user` close; single-dispatch/terminal turns ⇒ NULL, latency + producer-close handshake handed to
  5a (O5). Re-panel **r8** verifies the v9 Option-D fold: is `true` now unforgeable at ANY stop (must be — nothing
  earns at a terminal); is the single-dispatch⇒NULL cost stated + correctly handed to O5; did removing the fence
  leave any dangling contradiction? Re-verify (`tailer.py`, `service.py:187-207,497-504`, `subagent_stop.py`,
  `eval_tee.py:10-19`).

## Non-goals
No span tables / consumer / retention / projection SQL (5a). No token/cost. No attempt-in-key (epoch is a
fence/replace trigger, not a key dimension). Not a general wire-protocol redesign. The M2 reliable-inbox fix is
a **precondition 5a-0 depends on and gates**, not capture-design ownership — its implementation is a distinct
bridge-infra work item. **No message text/thinking body in the eval stream** — the causal-clock coverage is a
bounded `turn_clock_monotonic` bool + `turn_started_ts`, NOT the trace-only text/thinking content (the
extract-only / `eval_io`-OFF invariant holds). **Rejected (codex r1 alternative): a per-causal-record eval
"clock edge"** — it would close the same P1 but multiply eval-stream volume by the (large) count of
text/thinking records per turn; the turn-level flag closes it at O(1) fields/turn and still catches intermediate
inversions. **No timestamp *correction* in capture** — 5a-0 observes/flags clock ordering; 5a decides
NULL-vs-number (O4). **No terminal-close earn in 5a-0 (Mark's Option-D call, 2026-07-14):** no finalization
fence, no two-phase settle, no version-pinned canary, no `subagent_stop` `{final_inode,final_size}` — `true` is
earned ONLY at a next-human-`user` close (5a-0 has no citable producer-close happens-before, so it can't soundly
prove terminal finality of a transcript it doesn't write; the producer-close handshake for terminal turns is
5a's, O5). **No recovery machinery in 5a-0 (Mark's Option-B call,
2026-07-14):** no accumulator persistence, no back-scan, no provisional-close reconstruction — the
`turn_clock_monotonic` flag is pure
fail-closed over a single continuous tailer generation; recovering valid latency for restart/idle-resume-
interrupted turns is **obligation O5, handed to 5a** (where its own design cycle can address replay-source
integrity + resumable-idle/session-boundary semantics), not built speculatively in this capture slice.

## Deploy
Fleet bridge redeploy (all seats, covers claude-tail) after the capture changes + M2 fix land. Additive
allowlist + capture fields; no consumer/schema change in 5a-0 scope. **Paused for Mark's deploy-review gate**
before any prod fleet redeploy.
