# Changelog

Commit references below point at the private development history and will not resolve in this repository.

Notable changes to the Agent Redis Bridge. Entries are date-based (no semver yet);
each feature ships through the tri-model review workflow (codex + agy-print + cold-Opus,
per `docs/pipeline-operating-manual.md` Workflow B) before merge.

## 2026-08-03 — retrospective Workflow B over the omp/opencode arc, and its remediation

**Why retrospective.** The arc below shipped to `dev` with **no review panel**, against this
file's own header claim. Its arc-closure sweep recorded that as a true crossing. Panel
`panel-omp-opencode-arc-20260803T125825Z-570c21` is that rail, run after the fact — nothing
grandfathered by already being merged. Full record:
`docs/superpowers/reviews/2026-08-03-omp-opencode-arc-panel-record.md`.

**Verdict: needs-changes** (closed `outcome=emitted`). Zero P0 — both P0 candidates were
checked and cleared. The certify quorum voted 2 `approve` / 1 `needs-changes`; the
orchestrator owns severity and closed against the majority, because the `approve` that
carried zero findings cleared the dead-child question by citing a test subsequently shown
**vacuous by mutation**, and the other `approve` said "APPROVE WITH NOTES" in prose.

**Fixed here:**

- **The mid-turn dead-child path RAISED**, discarding everything the turn had streamed and
  skipping the terminal progress event, so a seat whose CLI died after streaming most of a
  review returned `result=""`. It now returns `TurnResult(ok=False, result=<streamed prefix>)`
  and emits `turn_completed`, matching `cursor_acp`/`grok_acp`/`cline_acp`/`devin_acp`. The
  old test asserted `assertRaises` — it was pinning the loss.
- **`grok_acp.request()` had no liveness check at all**, so `initialize` still burned the full
  timeout on a dead child — the exact defect the ACP-liveness backlog item was filed for. It
  had been closed on the claim that grok "already checks `poll()`", drawn at engine rather than
  loop granularity; grok checked only in its turn loop. Measured 2.0s vs the full timeout.
  **The false claim shipped under a "do NOT re-implement" banner, so the erroneous closure was
  itself what would have prevented the fix.**
- **Test-integrity defects.** `test_reply_then_exit_is_not_treated_as_death` preloads stdout and
  passes verbatim with the 0.5s grace drain deleted — it never pinned the grace;
  `test_grace_drain_recovers_a_line_the_reader_flushes_after_the_exit` now does.
  `test_omp_acp_engine_supported` certified `read,grep,find,ls`, a surface **omp rejects**
  (no `find`, no `ls`), so the fixture described a seat that cannot start. Six permission tests
  — including both fail-closed floor tests — sat after `test_omp_acp.py`'s `__main__` block and
  were silently dropped under direct invocation (19 ran, 25 collected).
- **Suite hermeticity.** `conftest` now scrubs `BRIDGE_ROLE_PROFILE_FILE` and
  `BRIDGE_MAX_PARALLEL`. Running the suite from a shell with a live seat's environment sourced
  false-failed 19–22 bridge tests; two reviewers independently burned time isolating the same
  phantom before their runs went green.

**Cross-turn permission residue (P1, fixed in a follow-up the same day).**
`TurnPolicyPermissionMixin` authorizes at DEQUEUE time, so a `session/request_permission` that
arrived during turn A but was still queued when turn A returned got answered under turn B's
policy — a write the agent requested under `plan` mode could be approved by an unrelated later
dispatch. Demonstrated by test: the stale ask came back `selected`. The session check could not
catch it because this family issues `session/new` once in `start()` and never retires, so
`session_id` is constant across turns.

Notably, **grok-acp is not exposed, and the mixin is derived from grok's reviewed floor.** It
copied grok's ask-time checks (GROK-1 v1.3 D2/D3b) but not the invariants that make them hold
across turns: grok retires after every turn, and a non-retiring grok seat rotates `session/new`
per dispatch *precisely so the D3b session gate correlates*. The derived code inherited the
wording of a guard without the thing that made it bite. Fix:
`_cancel_stale_permission_asks()` at turn start — a turn owns only the asks that arrive while
it is running — chosen over adding a per-turn session round trip to the whole family, and
placed at turn start rather than exit so it also covers asks arriving between turns.

**Host-portability asymmetry (found by running the suite off-host, not by the panel).** The
2026-08-03 arc made the pi-SDK entry genuinely portable (`npm root -g` + a static sweep +
`ARB_PI_SDK_ENTRY`) but left `PIEXT_ROOT` hardcoded to
`/Volumes/<workspace>/repos/pi-extensions` — a drive that lives permanently on ONE machine. So
four A5/A6 guards could only pass on that host, and the suite's "3344 green" was reproducible
nowhere else. `PIEXT_ROOT` now gets the same treatment: a search (repo-sibling, `~`,
`~/repos`, then the volume) that only accepts a root actually containing the detector, with
`ARB_PIEXT_ROOT` still overriding. **The guards still `pytest.fail` and are still never
skipped** — the change makes the red mean "not installed anywhere on this host" instead of
"you are not on one specific Mac", and the message now lists every path tried.

Worth noting how it was found: all four reviewers were asked directly whether these guards
still fail rather than skip, and all four correctly said yes. Nobody asked whether the default
path was reachable on more than one machine — and the seats happened to run while that drive
was mounted, so the environment masked it. A green that depends on an unrecorded ambient
condition is the same defect class the panel was hunting.

**Not promoted.** Both engines stay `experimental`: the live deny-proof still has no stored
artifact.

Suite: **3349 passed, 652 skipped, 0 failed** (baseline 3344 + 5 added). Every fix was
mutation-verified — each new or changed test shown to FAIL with its fix removed.

## 2026-08-03 — both filed backlog defects fixed: tool-allowlist fail-open, ACP dead-child hang

**What:** A shared `parse_tool_allowlist` in `engines/base.py` now backs pi-sdk, pi-rpc and
omp-acp; `GeminiAcpEngine` gained a child-liveness check used by both its handshake and turn
loops.

**Fail-open (1).** The guard was `pi_tools and len(pi_tools.strip()) > 0 and not parsed`. For
`"   "` the value is truthy but `.strip()` is falsy, so the guard was SKIPPED and the engine
spawned with the FULL toolset — it failed open on exactly the typo it existed to catch. The
shared condition is `csv not in (None, "")`. `tests/test_tool_allowlist_guard.py` asserts six
degenerate forms against all three call sites and pins the bug itself (old expression False for
`"   "`, new one True). **pi-rpc was worse than filed:** it had no guard at all, keeping only the
raw string, so `"   "` reached pi as `--tools "   "` *and* satisfied `if not self.pi_tools`,
making a full-tools seat read as tool-restricted to the policy check. It now guards on the
parsed list — the Tri-model P0 fix pi-sdk got and pi-rpc never did.

**Dead-child hang (2).** The ACP base's loops had no `poll()`, so a CLI that exits at spawn
reported `initialize timed out after 60s` with its own explanation stranded in the stderr drain
(live specimen: `omp --tools read,grep,find,ls` — pi's vocabulary, no `find`/`ls` in omp — exits
rc=2 instantly, seat waited 60s). Liveness is consulted ONLY when the queue comes up empty, then
a 0.5s grace drain, so "answered then exited" stays a healthy shape — pinned by
`test_reply_then_exit_is_not_treated_as_death`, with `test_live_but_quiet_child_still_times_out`
pinning that slowness is not misread as death. The turn loop was fixed too, beyond the filed
scope: the same defect there burned the whole turn timeout (up to an hour) instead of ~5.5s.

**Correction to the filing:** it claimed six adapters were exposed. They were not — grok/cursor/
devin/cline carry their own ACP implementations and already checked `poll()`. The gap was the
`GeminiAcpEngine` subclass family only (kimi-code, mini-agent, omp, opencode, plus retired
gemini-acp), all covered by the base fix.

## 2026-08-03 — close-discipline A5(ii)/A6 no longer bound to one host's paths

**What:** `tests/test_close_discipline_serving.py` resolves the pi SDK entry by asking
`npm root -g` (then sweeping Homebrew, `/usr/local`, a user npm prefix, bun, and repo-local
`node_modules`), with `ARB_PI_SDK_ENTRY` as an explicit override. The pi-extensions constants
are now derived from `PIEXT_ROOT`, overridable via `ARB_PIEXT_ROOT`; its default is unchanged.

**Why:** `SDK_ENTRY` hardcoded `/opt/homebrew/lib/node_modules/...`, but pi is installed here
under the user prefix `~/.npm-global/lib/node_modules`. A5(ii) therefore hard-failed with
"SDK entry unreachable" on a host where the SDK had been present all along — the test was
reporting an install-location mismatch as a close-discipline defect. The pi-extensions paths are
the same class and additionally sit on a mounted volume, so three more tests hard-fail whenever
it is not mounted.

**What was deliberately NOT changed:** these guards still `pytest.fail`, never `pytest.skip`.
The author's "Do not substitute a top-level-key assertion" instruction is the anti-vacuous-green
rail — turning an unreachable dependency into a skip would trade a loud failure for skip-green,
which is the exact shape ARB-B9 was filed against. Verified both directions before this entry:
with `ARB_PI_SDK_ENTRY` pointed at a nonexistent file the test still FAILS (and now prints every
path it searched); pointed at the real file it passes; left unset it resolves to the user prefix.

## 2026-08-03 — two new ACP seat engines: `omp-acp` (oh-my-pi) and `opencode-acp`

**What:** Two adapters on the shared `GeminiAcpEngine` base, both `experimental`:
`omp-acp` drives `omp acp` (oh-my-pi, a pi fork) and `opencode-acp` drives
`opencode acp`. `readonly_gate.py` admits `omp-acp` to its allowlist-engine set;
`opencode-acp` is deliberately excluded. New `TurnPolicyPermissionMixin` in
`engines/_acp.py` answers ACP permission asks from the active turn's policy.

**Why ACP over the alternatives.** omp also speaks pi's `--mode rpc` NDJSON and
`PiRpcEngine` drives it end-to-end with one flag removed, but that wire is pi's
private protocol which omp is already extending (it advertises
`supportedProtocolVersions [1,2]`) while republishing daily — 17.2.4 across 571
versions against pi's 0.83.0. Its TypeScript SDK has diverged too far for the
`pi-sdk-host` harness (`ModelRuntime` is gone). ACP is the versioned standard and
costs nothing that matters: omp's advantages are agent-side — **29 built-in tools
against pi's 7**, subagents, LSP/DAP, persistent eval — and reach through the ACP
transport unchanged. Verified by running `eval` (real Python; the returned SHA-256
digest matched an independently computed one) and `task` (a real subagent spawn)
through the ACP session. `--tools` and `--append-system-prompt` are honoured on the
`acp` subcommand, both proven behaviourally.

**Per-engine divergences, each found by a live failure, not by reading docs:**
- omp rejects `session/set_model` ("Unknown ACP ext method") — the base's call
  would fail `start()`, so it is suppressed and the model is pinned via `--model`.
- Session modes are `default`/`plan` (omp) and `build`/`plan` (opencode); neither
  has gemini/kimi's `yolo`, which both reject.
- **omp's tool vocabulary is not pi's.** The canonical reviewer allowlist
  `read,grep,find,ls` is invalid — omp has no `find` and no `ls` (use `glob`).
  omp exits rc=2, but the ACP base has no liveness check, so this surfaced as
  `initialize timed out after 60s`: a config typo disguised as a wedge. A
  pre-flight now runs omp's own validator first — the same mistake reports
  `omp rejected its spawn flags (exit 2): Unknown tool in --tools: ls` in ~2s.
- The base cancels **every** `session/request_permission`, so both agents' first
  real dispatch returned `stopReason=cancelled` with no tool results. The new
  mixin grants only inside a `trusted` turn, via the panel-reviewed
  `_select_allow_option`, and denies outside a turn, on a stale session, or when
  no allow option is offered (grok-acp's GROK-1 D2/D3b floor, applied to this base).

**Read-only gate.** omp reuses the pi-family `--pi-tools` surface and the
allowlist is enforced on the ACP path — proven with a control/limited pair where
the control wrote a file and `--tools read,grep` could not, reporting only
read/grep available. Its refusal now names omp's own fallback (29 built-ins
including browser/computer/github), because "the FULL toolset (read/bash/edit/
write)" understates it by an order of magnitude. `opencode-acp` has no allowlist
flag — its read-only posture is the ACP `plan` mode, which this gate cannot see —
so it refuses rather than appear to certify a posture it cannot check; mode-based
posture belongs to the `seat_posture_v` migration the module's docstring names.

**Verified end-to-end**, not just unit-tested: both seats registered on the live
bus and served a real dispatch through `dispatch_authority` → Redis → ACP →
agent → tools → reply. omp (read-only, `--pi-tools read,grep,glob`) enumerated the
engines directory `ok=true`/`no_changes_clean`; opencode read `support_tiers.py`
and returned the correct tier. All four read-only-gate branches were demonstrated
live (unset surface, valid subset, write-bearing surface, non-allowlist engine).

**Known-unfixed, filed here rather than left silent:** `pi_sdk.py:121`'s
degenerate-tools guard (`pi_tools and pi_tools.strip() and not parsed`) skips a
whitespace-only value — `"   "` is truthy but `.strip()` is falsy — so it fails
OPEN on exactly the typo it exists to catch. `omp_acp.py` uses the corrected
condition; pi-sdk/pi-rpc were left alone as out-of-scope for this change. The ACP
base also still lacks a child-liveness check during `initialize`, so any ACP
engine whose CLI dies at spawn waits the full timeout; omp is covered by its
pre-flight, the other five are not.

> **SUPERSEDED — both items above were fixed by the very next commit in this
> range (`fc4a6a75`, the entry immediately above this one).** Kept verbatim
> because changelog entries are historical, but flagged because a reader grepping
> for "fails OPEN" lands here first and would conclude the security posture is
> still broken. The allowlist guard is now the shared `parse_tool_allowlist` in
> `engines/base.py` backing pi-sdk, pi-rpc and omp-acp (the line reference
> `pi_sdk.py:121` no longer holds that code); the ACP base gained
> `_dead_child_error` / `_await_or_detect_death`. Do not re-file either from this
> paragraph. (Flagged by the retrospective Workflow-B panel
> `panel-omp-opencode-arc-20260803T125825Z-570c21`; two independent reviewers
> landed on it.)

## 2026-07-30 — suite front door repaired: arb_messages no longer aborts collection (ARB-B9)

**What:** The six `tests/arb_messages` modules that read `ARB_MESSAGES_TEST_DSN` via
`os.environ[...]` at import time now use `.get()` plus a module-level
`pytest.mark.skipif`, and a new `scripts/arb-messages-gate` (same contract as
`scripts/graph-sql-gate`) is the non-skippable runner: it refuses to run without the DSN,
fails on ANY skip, and pins `EXPECTED_MIN_PASSED=83`.

**Why:** A missing env var did not skip those modules — it aborted collection for the WHOLE
suite (`Interrupted: 6 errors during collection`, 3685 collected, 0 run). Plain `pytest` ran
zero tests for every human and agent in this repo: the pinned `no tests ran` shape, where the
harness reports nothing and nothing reads as nothing-wrong. Verified after the fix, no DSN
set: 3082 passed / 650 skipped / 1 failed in 6:51 (the failure is `test_doc_index`
freshness — pre-existing on dev, 16 docs landed without index entries via the Slice 3
merge and the memory-seeds mirror, filed as ARB-B20; unrelated to this change, whose diff
touches no markdown). With the DSN, the gate runs 83 passed / 0 skipped against live
Postgres. A bare skip would have traded the loud failure for skip-green — the gate is
what makes the skip honest, and all four of its branches (DSN refusal, green, skip-detect,
count-pin) were demonstrated to fire before this entry was written.

**Not fixed here:** provisioning a DSN so the DB tests run by default (ARB-B9's full ask
presumed CI, which the owner has declined for now); the suite-wide count/skip gate is ARB-B10.

## 2026-07-16 — visibility /orchestrators tests repaired for the tees response key

**What:** The four `/orchestrators` response assertions in `test_visibility_auth.py`,
`test_visibility_login.py`, and `test_visibility_orchestrators_last_seen.py` now expect the
`tees` key alongside `orchestrators`.

**Why:** `d53d127` (2026-07-11, CT-1 C) added tee heartbeat surfacing to the `/orchestrators`
payload and shipped 52 lines of new coverage in `tests/claude_tail/test_visibility_tee.py`, but
left these four exact-dict assertions on the old shape — dev has been red here since. `tees: []`
is the honest expectation for these tests (no `ARB_VIS_EXPECTED_TEES` configured ⇒ empty label
roster ⇒ empty list), not an artefact of the incomplete double: setting the env var repopulates
it to `[{label, state: missing}]` and the assertion correctly goes red. The populated `tee_states`
path stays covered by `test_visibility_tee.py`, which uses a double that implements `mget`.

**Pattern (two arcs in one week):** both this and the 07-13 consumer-hardening arc wrote thorough
tests for the behaviour they ADDED and never grepped for existing tests asserting the behaviour
they CHANGED. The new tests pass, so the arc reads as well-tested; the rot is only visible in the
older files, and both merged with the suite already red, which let it accumulate. Worth a
pre-merge "does any existing test assert the old contract?" grep on contract-changing arcs.

**Backlog (not fixed here):**
- The `FakeRedis` doubles in the three `test_visibility_*` files do not implement `mget`, which
  the real client has. `tee_states` calls it, raises `AttributeError`, and the broad `except`
  swallows it into a warning. Inert for these assertions (empty labels ⇒ `[]` either way), but a
  double diverging from the real contract is the setup for a masked regression.
- `tee_states` (`visibility.py:198`) catches bare `Exception`, so a contract/programming error is
  indistinguishable from Redis being down — both render as "all tees missing". Defensible as
  fail-soft for a real outage; as a swallow of a contract bug it is the same class as the
  `times_delivered` note above.

## 2026-07-16 — write-deadletter tests repaired to match retry-then-deadletter semantics

**What:** `test_deterministic_bad_intent_is_deadlettered_not_dropped` pins
`ARB_CONSUMER_POISON_RETRY_LIMIT=1`; `test_failed_deadletter_does_not_ack_entry` pins the same
limit and forces `_canary_deadletter_sink` to `False`, plus asserts `_deadletter_sink_open`.

**Why:** Both tests date from 2026-06-24 and encode the pre-hardening instant-drop contract —
the third and fourth casualties of the 2026-07-13 arc (70cb478 + 8038f03), alongside the poison
PEL test above. Two distinct causes behind identical `assert None is True/False` symptoms:
(1) the FK violation `hints_artefact_id_artefact_version_fkey` classifies as `poison`, so a
deterministic-bad intent now needs 5 attempts to deadletter, but the test called `_handle_entry`
once; (2) `_deadletter_failed` now forks on a live canary probe — a failed deadletter against a
HEALTHY sink is attributed to the entry and acked as `deadletter-unstorable`, so the no-ack
invariant this test is named for only holds when the sink is genuinely down.

**Open question (not settled here):** the `deadletter-unstorable` path acks and therefore drops an
entry when the sink is healthy but that entry will not store. It logs at ERROR and publishes a
terminal result, so it is not a silent drop — but the 06-24 test asserted it could never happen,
and matching the test to the code ratifies a semantic change this seat did not author. Flagged for
review by the arc's owner rather than certified here.

## 2026-07-16 — poison-ack PEL test repaired to match retry-then-deadletter semantics

**What:** `test_poison_write_missing_hint_text_is_acked_across_restart` now pins
`ARB_CONSUMER_POISON_RETRY_LIMIT=1` before constructing either consumer, polls for
`pending == 0` instead of asserting it immediately after the thread reports alive, and
asserts the entry actually reached `write_deadletter` (new `_deadlettered` conftest helper).

**Why:** The test was written 2026-06-20 (8d595ae), when a non-transient error was acked on
first attempt — `pending == 0` right after restart was trivially true. The 2026-07-13
consumer-hardening arc (70cb478 + 8038f03) replaced instant-drop with retry-then-deadletter,
but never updated this older test: a handle-stage poison entry now needs 5 failures to
deadletter, and `self._poison` is in-memory per consumer instance, so the restart resets the
counter and the test's ~1s window could never reach the limit. The sibling parse-error test
kept passing because malformed payloads deadletter at the parse stage, skipping the counter.
The deadletter assertion is the load-bearing half: `pending == 0` alone would go green if the
entry were silently dropped — the exact outcome the no-silent-drop doctrine forbids.

**Backlog (not fixed here):** the poison counter's in-memory residency means a consumer
restarting more often than ~5 ticks would never exhaust retries on a poison entry and would
recirculate it forever. Redis's `XPENDING` `times_delivered` survives restarts and would close
the gap. Consumers are long-lived in practice, so this is hardening, not an active defect.

## 2026-07-16 — graph-aware memory read tools
- Added `memory_related` / `memory_references` MCP read tools on BOTH doors (local stdio
  + connector OAuth), exposing the vault exporter's E1/E2 graph edges on demand.
  Why: MCP consumers were blind to the graph; recall now walks edges instead of
  composing searches. Shared logic lives in `src/arb_memory/graph.py` (exporter now
  imports it — one implementation, byte-identical footers). Subject-hint mode is pure
  caller intent ('live'/'as_written') — eliminates the TOCTOU class two panel rounds
  caught. Backlink prefilter is strpos (LIKE under-matches on backslash ids).
  `scripts/graph-sql-gate` refuses skip-green SQL runs.

## 2026-07-15 — ARB observability Slice 5a span layer and retention

**What:** D1 adds `eval_turn`, `eval_tool_call`, `eval_task`, and `span_deadletter` with parity DDL;
D2/D2.9 projects epoch-gated turn, tool, and task edges atomically with deadletter handling;
D3/D5 records producer-specific latency, fail-closed clock-invalid outcomes, and recovered/retracted
terminal turns; D4 adds the cold-seat durable finality watch, digest revalidation, re-arm, and explicit
eval-stream routing; D6 fixes transcript retention to use ingestion time; D7/D8 add least-privilege
span/retention grants and finality metadata allowlisting; D6 packaging adds nightly 56-day purge units.

**Why:** Slice 5a turns the durable eval event stream into replacement-safe timing spans while preserving
causal validity across retries, restarts, terminal cold-seat turns, and retention purges. The finality
watch is deliberately limited to irrevocable cold-seat completion evidence; warm-session flap markers
remain non-terminal.

## 2026-07-14 — reliable-inbox recovery validation/move atomicity (M2)

**What:** Recovery now passes the Python-computed SHA-256 claim key into one Lua script that
validates the peeked rightmost body, deletes its claim, and `LMOVE`s it to the inbox atomically.
The owner-fence probe adds the executed two-entry `peek-move-mismatch` interleave.

**Why:** The executed BODY_LOST interleave showed that a deposed predecessor could acknowledge
the peeked entry between the separate clear and move, causing recovery to move a different entry
with its claim still alive; the successor could then re-park that body and the stale predecessor
could remove it. Atomic validate→clear→move guarantees every recovered body reaches the inbox with
its claim cleared.

## 2026-07-14 — ARB observability Slice 5a-0 capture normalization

**What:** Added bounded eval capture metadata (`event_ts`, `turn_started_ts`, `turn_clock_monotonic`,
`turn_index`, `attempt_epoch`, `tool_call_id`) across claude-tail and dispatch producers, with
restart-stable composite offsets and corrected agent-sdk/pi-rpc tool-edge semantics.

**Why:** Gives 5a's downstream joins stable causal timestamps, turn/attempt ordinals, and tool
correlation without admitting raw tool I/O or free text into eval payloads.

## 2026-07-14 — reliable-inbox owner-fenced acknowledgement (M2)

**Verification gate:** `uv run --extra arb-memory scripts/verify-reliable-inbox-owner-fence`

**What:** Reliable-inbox processing entries now carry the daemon boot's owner token. Acknowledgement
uses an atomic Lua compare-token-then-`LREM`, so a stale predecessor cannot remove a successor's
re-parked envelope; missing or mismatched claims remain parked for recovery.

**Why:** The previous body-keyed `LREM` could acknowledge any matching envelope after ownership
changed, losing a request during takeover. The fence closes that request-loss defect while leaving
the non-reliable `BLPOP` path unchanged.

## 2026-07-14 — implbench: full adversarial integration and inertness proofs

**What:** Added hermetic end-to-end coverage for the isolated Open Interpreter versus Pi bakeoff:
known-good 128-cell matrix closure, deterministic sealed evidence/report validation, every close
restart point, stop/failure branches, secret and extension denial, scorer input/output denial,
scored receipt-only dispatch tripwires, and active-checkout/legacy-host inertness checks.

**Why:** Task 13 closes the implementation phase without running a live bakeoff. The operator
runbook now gives exact read-only validation, evidence/report, serialized phase, prune, and
failure-interpretation commands; provider credentials and live calibration remain outside the
tracked repository and are reserved for the separate Task 14 gate.

## 2026-07-13 — claude-tail: global auto-registration + self-bootstrapping daemon (`scripts/claude-tail-ensure`)

**What:** `scripts/claude-tail-ensure` — an idempotent, fail-soft "make sure the claude-tail daemon is
running" step called from the (host-local) global `~/.claude/settings.json` SessionStart hook. Common case
(daemon already up) short-circuits in ~20ms via `pgrep`. On a fresh host it bootstraps the per-host daemon:
**macOS** kickstarts an existing `com.example.claude-tail.*` launchd plist or generates + `launchctl bootstrap`s
one; **Linux/Ubuntu** writes a `~/.config/systemd/user/claude-tail-auto.service` unit, `daemon-reload`,
`enable --now`, `loginctl enable-linger`. Guarded by repo `.venv` + env-file presence (won't provision
secrets); any failure logs and exits 0 so a session never breaks.

**Why:** the claude-tail transcript capture previously required a hand-installed per-repo `settings.local.json`
hook AND a manually-bootstrapped daemon. The new model is: **one global SessionStart hook** registers every
Claude Code session automatically, auto-deriving the orchestrator id `claude-<repo-dir>-dev` from the session
cwd (a repo whose orchestrator id differs from its dir name sets a one-line `"env": {"ARB_CLAUDE_TAIL_PROJECT":
"<id>"}` override — single registrant, no double-registration), and the ensure step means a brand-new host
(mac or Ubuntu) stands the daemon up on first session start. Verified: short-circuit path live; the launchd
generate→bootstrap→kickstart→bootout lifecycle proven under a throwaway label (non-disruptive; real daemon
untouched); systemd unit renders correctly (its `systemctl` path unexercised on the mac host).

## 2026-07-13 — ARB Visibility: fail-loud on auth-redirect + one-command Go watcher (`scripts/arb-watch`)

**What:**
- `src/arb_memory/static/app.js` `loadOrchestrators()` now **fails loud** instead of silently blanking:
  a followed redirect (`response.redirected`), a 200 with a non-JSON body, or a network error each show
  the auth banner + navigate to `/login` (or a visible `[error]`), rather than letting `response.json()`
  throw on HTML into a swallowed rejection. New contract test
  `test_appjs_orchestrators_redirect_to_login_html_fails_loud` (red before the fix: node exits non-zero on
  the unhandled rejection; green after).
- `scripts/arb-watch` — one command to run `arb-watch-go` against the gateway **origin** over SSH
  (resolves the visibility container IP, opens a control-socket tunnel, runs the client, tears the tunnel
  down on exit). Lets a headless data-plane client reach the gateway without going through the browser-only
  Cloudflare Access layer.

**Why:** hostname-wide Cloudflare Access on `arb-visibility.example.com` was 302-redirecting the *data
plane* — both the `arb-watch-go` client (can't complete Azure SSO) and the web UI's `/orchestrators` XHR
(followed the redirect to the CF login HTML → 200 → `response.json()` threw → silent blank dropdown). The
edge was fixed operator-side by **path-scoping CF Access** (keep Azure SSO on the human HTML routes `/`,
`/login`, `/app.js`, `/journey*`; bypass it on the data routes `/orchestrators`, `/sse/*`, which stay gated
by the gateway's own passphrase+TOTP-session-or-bearer auth). This changelog entry covers the two code-side
companions: the client no longer hides an auth wall behind a blank screen, and a headless watcher has a
first-class origin path. (The gateway's own Layer-2 auth is unchanged; no data route is ever left
unauthenticated.)

## 2026-07-13 — ARB Memory AUDIT-CLOSE-2 follow-ups: consumer-loop robustness (Item 1) + write-result signal (Item 2) — SHIPPED to prod

**What:** two AUDIT-CLOSE-2 backlog items, built + tri-reviewed + deployed to prod (`abd23f3`).

- **Item 1 — consumer-loop robustness.** Extracted a shared `consumer_loop.py` used by all 5 stream
  consumers (memory-audit, audit-close, eval, transcript, writer). Adds: a consecutive-poison counter,
  a round-robin stream cursor, `threading.Event`-based backoff (bounded — no `backoff_delay`
  OverflowError), **fail-safe poison classification** (only `redis.DataError` + `psycopg.DataError`-class
  events are treated as poison and deadlettered; everything else retries — a misclassified transient must
  never be dropped), a terminal-sink **circuit breaker + canary** (the sink recovers instead of wedging on
  a bad `{}` payload), and **sanitized deadletters**. Two additive migrations:
  `audit_deadletter.stream_entry_id` and `write_deadletter.stream_entry_id`.
- **Item 2 — write-result signal.** `upsert_artefact` / `upsert_hint` now publish a receipt to a
  `write_result:<request_id>` channel; the **writer proxy owns the await** (async `redis.asyncio` blpop on
  a server-minted uuid4 `request_id`; `/publish` with `{"await": true}` blocks until the consumer reports
  `{artefact_outcome, version, hints_stored, duplicate}` or times out — never `blpop(timeout=0)`). Replay
  is idempotent via an atomic receipt stored in `idempotency_keys.receipt` (additive migration). MCP
  `memory_store` / `memory_remember` gain an internal `await_result` param.

**Why:** the audit/write consumers previously had failure paths that could silently drop a transient event,
wedge a terminal sink forever, or crash the read loop on a single bad `XACK` — and a caller had no way to
learn whether an async write actually persisted (fire-and-forget only). Item 1 makes every consumer
fail-safe and self-recovering; Item 2 gives callers a real confirmation receipt with an idempotent replay
guarantee.

**Review earned its keep:** the tri-review found **5 P1 + 4 P2 bugs inside the robustness code itself**, all
invisible to the 388 green tests because the tests mocked the failure paths (canary bare-`{}`→jsonb so the
circuit never recovers; ReadLoop dies on one transient XACK; `backoff_delay` OverflowError at ~1024;
`/publish blpop(timeout=0)` blocks forever; `_deadletter` ACK-and-loses a transient). Fix round 1
introduced a connection leak on the new WriteLoop reconnect; fix round 2 closed it. Deny-proofs were
required to drive the real failure path, not a mock.

**Deploy (2026-07-13):** prod pulled `dev`→`abd23f3`; the 3 additive columns were applied out-of-band
(`postgres:16` psql, `ON_ERROR_STOP=1`) — note `setup-schema` covers only eval/transcript tables, so these
3 live only in `schema.sql` and a container restart does NOT apply them; image rebuilt (`build memory`;
only `memory` carries the `build:` block) + `up -d --force-recreate`. **Live-gate GREEN on the real prod
plane:** an awaited `/publish` returned `artefact_outcome:"stored"` (write_result round-trip, read-back
confirmed), and zero `idle in transaction` after the new code proved the Item-1 conn-leak fix. **Scar:** the
first additive `ALTER` stalled 3+ min on `ACCESS EXCLUSIVE`, queued behind a 71-min leaked
`idle in transaction` from the *old* code (the very leak this ships a fix for); cleared with
`pg_terminate_backend`. Run future additive prod migrations with `SET lock_timeout` to fail-fast rather
than queue-and-block prod.

## 2026-07-12 — SKILL.md: verdict-close guidance → bus path (was stale DSN CLI)

**What:** rewrote the `using-agent-bridge` skill's panel-audit close step (§ "Auditing a review/design
panel", step 5 + its Postgres prerequisite) to lead with the bus recipe
(`scripts/arb-audit-close-request --run-id … --payload-file -`, needs only `ARB_MEMORY_REDIS_URL`),
document its `{outcome, gaps}` + exit-code contract (0/4/5/6/7, verified against `close_core`), and demote
the DSN-coupled `python -m arb_memory audit-close` CLI to break-glass-only.

**Why:** the old guidance told orchestrators to close via `arb-audit-emit --kind verdict` with
`ARB_MEMORY_DSN` set — a credential the orchestrator should never hold by design (the privileged
reconcile+emit lives in the arb-prod `audit-close-consumer`). That stale doc sent a peer orchestrator
hunting for a DSN it was correctly denied; the fix closes a documentation-level containment leak now that
AC2's bus close is shipped.

## 2026-07-12 — Fable 5 Agent SDK seat + Grok session-record hardening

**What:** registered `fable-5` as a Claude subscription implementor seat using
`claude-fable-5`, with explicit registry, authentication-lane, reviewer-role, and
cross-lane concurrency tests. Introduced a hardened archive derived from the
2026-07-11 Grok onboarding notes; unsafe copy-pasteable production topology, borrowed
sender identity, direct privileged writes, ad-hoc seat startup, and weak completion
rules from the local session record were not retained in the repository version.

**Why:** a model entry is incomplete when the registry contract still rejects it.
Historical operator notes must not compete with current runbooks or teach unsafe
shortcuts; current orchestration uses honest identities, supervised seats, correlated
replies with fresh output ownership, parsed `ok=true`, terminal-success state, and
bus-mediated audit closure with honest requester attribution.
Canonical pi/orchestrator operating docs use the same bus-close contract, and the
Compose shape test now includes the supervised `audit-close-consumer` service.
The deploy and audit-votes activation guides are bus-first with direct privileged
closure restricted to break-glass use. The whole bridge env-precedence test class is
isolated from an operator's ambient audit-bus URL.
Activation examples now use exact `seat:<target-id>` roster actors, and the legacy
`panel-run verdict` CLI labels itself privileged break-glass rather than normal closure.

## 2026-07-12 — audit-close structural one-verdict backstop

**What:** added a Postgres partial unique index for one `verdict` row per audit run and made the
audit consumer deadletter and acknowledge a rejected duplicate verdict.

**Why:** the prod audit bus uses `allkeys-lru`; Redis close-claim eviction could otherwise allow a
second verdict emission. The Postgres guard makes the invariant structural and the rejected event
remains recoverable instead of becoming a poison-pill retry.

## 2026-07-12 — audit-close command

**What:** added the image-shipped `arb_memory audit-close` subcommand, with stdin payload transport,
reconcile refusal exit codes, and a Redis close claim that makes same-verdict retries idempotent and
rejects different verdicts.

**Why:** audit verdict closure must be runnable inside the audit container without shell-quoting hazards,
must fail visibly when the roster or stances do not reconcile, and must not append a second verdict row
for the same run.

## 2026-07-12 — orchestration-clone audit routing → prod bus + AUDIT-CLOSE-1 design

**What:** repointed the orchestration clone's audit bus (`ARB_MEMORY_REDIS_URL` in the gitignored
`envs/agent-redis-bridge-dev.env`) from Valkey db/3 → **db/5**, so panels run/audited from the
orchestration session feed the PROD audit trail (db/5 → `deploy-audit-1` on `arb-prod` →
`arbmemory.audit_events`) instead of stranding on db/3, whose consumer was dead. Verified: an
orchestrator-emitted probe dispatch landed in prod `arbmemory`; the 7 audit-participating bridge-dev
seats (codex sol/terra/luna, grok, agy, pi-glm, pi-m3) were kickstarted to re-read the bus
(`bridge.py:191 resolve_audit_redis`). Adds the AUDIT-CLOSE-1 design (`docs/audit-close-1-design.md`)
for the still-missing verdict-close path, reviewed by codex-sol@high (needs-changes/P1 ×4).

**Why:** the audit *emit* path went live on prod today (fleet-clone seats), but the orchestration
clone emitted to a different, unconsumed bus (db/3) — so orchestration-session panels recorded nothing
durable. One prod trail across both clones requires the orchestration clone on db/5. The verdict-close
remains gapped: reconcile needs `SELECT audit_events`, which the laptop reader role is deny-proven
against, and the audit image ships `src/` but not `scripts/` — hence the AUDIT-CLOSE-1 `audit-close`
subcommand design. (Env file itself is gitignored/secret-bearing; the routing decision is recorded
here and in the design doc.)

## 2026-07-12 — configurable ARB Learn evaluation panels

**What:** `arb-learn evaluate` now supports `--panel core|full` and repeatable
`--seat` selection. Core preserves Codex Sol + agy + GLM; Full adds Fable 5 through
Agent SDK and Grok 4.5. Codex Sol is explicitly dispatched at high effort, while
Fable receives the isolated auto-cleaned worktree required by trusted Agent SDK
turns. Presets and custom seats are mutually exclusive, preventing silent preset
replacement. The new `using-arb-learn` orchestrator skill requires a structured
Full/Core/Custom question before every interactive evaluation.

**Why:** panel membership is a human decision, not a constant hidden in Python.
The CLI needs deterministic knobs for automation, while interactive orchestrators
must expose the cost, independence, and availability trade-off instead of silently
choosing a quorum.

## 2026-07-11 — truthful completion signals (boot-token leases, consumer key, escape detection)

**What:** bus-global identity is now a per-boot `owner_token` lease (`alive:<token>`,
claimed/refreshed atomically via Lua) instead of a host-local PID; a second
`agent:<id>:consumer` TTL key proves the inbox loop is progressing; a successor
waits in-process (`--identity-claim-timeout`, default TTL+interval) for a stale
hard-crash lease instead of crash-looping through `StartLimitBurst`; worktree
tasks get a base-checkout fingerprint escape detector; `--no-enforce-completion`
is restricted to diagnostic one-shot modes and the ambient
`AGENT_ENFORCE_COMPLETION` override is ignored; new `scripts/watch-go-dispatches`
silent completion watcher (ok-polarity + Redis terminal state, never pgrep).
Review fold (same branch): `agent-bridge-ping` passes healthy pre-token daemons
as `consumer=legacy` instead of failing every not-yet-restarted seat mid-rolling-
upgrade; escape detection only fails a turn on an attributable proof — overlapping
base-cwd turns, one-sided git errors, and sibling-worktree churn under
`.claude/worktrees/` read as loud warnings, not `ok=false`; escape reports name
only newly-dirty paths; a deposed daemon's heartbeat loop stops on the FIRST
ownership loss instead of riding the 3-strike transient ladder; supervision
verifier default timeout raised 90→150s (was exactly equal to the worst-case
claim wait — zero headroom).

**Why:** the recurring failure class is vacuous green — registry/heartbeat/output
signals that are technically true while the reality they imply (one healthy owner,
consuming inbox, isolated worktree, completed contract) is false. PID-based
heartbeats can't prove bus-global ownership across hosts or PID reuse; and a
truth-signal layer must not fabricate red either (false escapes, false dead
consumers), or operators learn to ignore the layer.

Second fold (r2 panel, 5 seats): base snapshot moved BEFORE worktree/engine
creation (engine build/start hooks could write pre-baseline and evade the escape
check); head/status probe failures now flag the snapshot unverifiable instead of
fabricating or masking a change, and a failed base probe warns instead of passing
silently; every unverifiable outcome is surfaced on the reply
(`completion.isolation = "unverifiable"` + reason) — never log-only; lease release
is a single atomic Lua compare-and-delete (GET-then-DEL could destroy a
successor's freshly-claimed keys); the consumer-key TTL covers a synchronous cold
`engine.start()` on the inbox thread; `watch-go-dispatches` speaks TLS/auth for
managed buses; dead `RedisCli.heartbeat()` removed; remaining 90s doc strings
fixed; `test_bridge_emit_vote`'s minimal fixture carries the new counters.

Third fold (r3 panel, sol P1, reproduced): the escape comparison now runs on
EVERY worktree-task exit via `_verify_base_isolation` — engine-setup exceptions
and failed fork/resume routing previously bypassed it, so a hook that wrote to
the base and then raised left the escape unreported; an already-failed result
keeps its own error while the completion block carries the isolation verdict.

Fourth fold (r4 panel, sol P1 + grok P2 converged): continuation-routing
persistence now happens AFTER the isolation verdict — the r3 move had left
`record_agent_sdk_continuation_workspace` running while an escaped turn still
read ok=True, leaking live continuation state for a rejected escaped agent-sdk
keep-worktree turn. The record helper is ok-guarded, so the relocation is a
no-op on every other path.

## 2026-07-10 — grok-acp: spec-correct permission answers (GROK-1)

**What:** grok-acp now answers `session/request_permission` with the ACP-spec
`selected`+offered-optionId shape (trusted) or `cancelled` (everything else),
decided ONLY by an explicitly threaded per-turn policy; adds a per-turn deny
budget (`BRIDGE_APPROVAL_DENY_BUDGET`, shared with codex) with an interrupt +
bounded grace exit; `is_healthy()` so the engine pool actually quarantines
wedged grok engines; and, for non-retiring seats, per-dispatch `session/new`
rotation with sessionId-gated asks. Shared `_select_allow_option` moved to
`engines/_acp.py` (cursor imports it; behavior unchanged).

**Why:** GROK-1 — the old reply `{"outcome": {"outcome": "approved"}}` is not a valid ACP
outcome; grok treated every permission-requiring operation as rejected and the
turn died. The probe artifact (docs/superpowers/probes/2026-07-10-grok1-v1-probe/)
pinned the root cause (controlled A/B, runs A vs B) and refuted the dead-worker
theory. Design: docs/superpowers/specs/2026-07-10-grok1-acp-permission-handling-design.md
(v1.3, 4-round panel, round-4 unanimous).

## Unreleased — dev

- PI-1: pi-sdk registers same-name guarded custom find/grep definitions whenever they
  are requested; the SDK registry adds those after its builtins, so they override the
  builtin definitions. Paths outside the session workspace fail in-band and the inner
  tool is aborted after `BRIDGE_PI_SDK_TOOL_TIMEOUT_S` (30 seconds by default), so the
  model can narrow or retry instead of waiting on a wedged crawl. WHY: a root-level
  `find` stalled for more than 40 minutes and consumed a whole review turn before the
  bridge's 3600-second timeout ended it.
- seat-preflight: add `scripts/seat-preflight` for plist or env-file seat checks, including
  Redis requirements, local MCP DSN derivation, cross-store policy, and executable paths.
  WHY: a scratch seat setting `ARB_MEMORY_LOCAL_MCP` without a derivable
  `ARB_MEMORY_LOCAL_DSN` caused loud engine-start failures and queued dispatches.

- ENG-1: codex warm-engine thread rotation (2026-07-11). WHAT: BRIDGE_CODEX_RETIRE_AFTER_TURN=0
  is now SAFE — a warm app-server rotates to a fresh thread per dispatch (grok D3b mirror,
  fail-closed quarantine), health is affirmative (True only on a clean uninterrupted
  "completed" terminal — allowlist), and BRIDGE_CODEX_MAX_PROCESS_TURNS (default 20) bounds
  process lifetime via a dynamic retire_after_turn property. Fleet default (retire ON)
  unchanged; per-seat warm adoption gated on the ENG-1 live gates. WHY: retire-after-turn
  made every dispatch pay the 2.6–13s codex spawn (DSP-1 root cause) — rotation keeps the
  thread-level contamination guarantee without the spawn tax. Design:
  docs/superpowers/specs/2026-07-11-eng1-codex-thread-rotation-design.md (v1.2, 2-round panel).

- ENG-1b: pi-sdk warm-engine thread rotation (2026-07-11). WHAT: BRIDGE_PI_RETIRE_AFTER_TURN=0
  now safely reuses a warm pi-sdk process while rotating to a fresh thread per dispatch,
  with affinity-aware continuation preservation, affirmative clean-terminal health,
  a process-turn cap, and sticky quarantine when the old thread cannot be disposed.
  WHY: warm pi-sdk seats need the same thread-level contamination boundary as ENG-1 without
  paying process startup on every dispatch, while ambiguous terminal outcomes and failed
  disposal must fail closed. Design:
  docs/superpowers/specs/2026-07-11-eng1b-pi-thread-rotation-design.md (v1.2).

- plan-fixture-smoke: executable pre-flight for implementation plans (2026-07-11).
  WHAT: `scripts/plan-fixture-smoke <plan.md> [--task=N]` extracts a plan's
  `python fixture-smoke` blocks and runs them against the target tree before each
  dispatch boundary — fixture blocks pin that every fake can satisfy the strongest
  predicate the plan's tests assert against it, and `red_claim(src, expect_fail=[...])`
  pins that every claimed-red test actually fails pre-edit (an unexpected pass = inert
  pin = plan bug, refused). Required by the operating manual for plans with fake-based
  tests. WHY: three implementor BLOCKEDs across GROK-1 + ENG-1 (fixture-vs-runtime ×2,
  red-phase-never-red ×1) were all plan bugs in the one claim-class static plan panels
  cannot falsify — runtime semantics at the fixture/real-code boundary. Each cost a
  dispatch round-trip; the smoke catches all three shapes in seconds (deny-proven by
  re-introducing both historic bugs into the ENG-1 plan's smoke and watching it refuse).

- DSP-1: dispatch auto-retry on the engine-start cold-start flake (2026-07-11).
  WHAT: go-client `dispatch` gains opt-in `--retry-engine-start` — when the matched
  reply is not-ok with `engine-start-failed: … initialize timed out …` (the ONLY
  shape observed transient), it re-dispatches ONCE with a fresh envelope id inside
  the same overall deadline, prints a loud `[retry]` stderr line, and surfaces only
  the final payload on stdout; `scripts/dispatch-dev` passes the flag by default
  (opt out: `DISPATCH_NO_RETRY=1`). Bare go-client stays byte/behavior-identical to
  the Python dispatcher unless flagged (parity rule).
  WHY: 5 dispatches across 2026-07-10..11 (pi-GLM ×2, codex-luna ×3) died on the
  cold-start initialize timeout; manual immediate re-dispatch succeeded 5/5. Each
  flake cost an orchestrator round-trip and could silently shrink a panel roster.
  Bounded to one retry so a persistent failure still fails fast and the flake's
  frequency stays visible (BACKLOG § DSP-1).

- DSP-1 init-budget raise (2026-07-11). WHAT: codex, pi-sdk, cursor-acp, grok-acp,
  and gemini-acp now use `BRIDGE_ENGINE_INIT_TIMEOUT_S`, defaulting to 60 seconds,
  for their start/initialize handshakes. WHY: the measured codex first-after-idle
  initialize took 13.2s against the old 15s budget, leaving too little margin under
  pipeline load; the client-side retry remains tail insurance. The new default takes
  effect when each seat restarts.

- CT-1: claude-tail fail-loud (spec docs/superpowers/specs/2026-07-11-ct1-claude-tail-fail-loud-design.md,
  6-round design panel). WHAT: RedisError now crashes the tee daemon (KeepAlive revives)
  while parse errors skip and offset corruption self-heals; a lock-free watchdog
  os._exit(86)s a hung loop; chunked budgeted polls with at_eof-gated finishes and
  durable draining records (SessionEnd hook hands off write-then-delete); an
  output-liveness heartbeat (tailers/failing_tailers/skipped_lines) on the live bus,
  surfaced by the visibility gateway via ARB_VIS_EXPECTED_TEES; RotatingFileHandler logs.
  WHY: the tee zombied 2026-07-06→10 (process alive, zero events, KeepAlive inert,
  discovered by a human) — every failure mode must now crash or be legible on the bus.

### feat(agent-sdk): safely resume trusted sessions in persistent worktrees (2026-07-10)
- **What:** a successful trusted stateful agent-sdk turn in a kept worktree now records its
  session ID, creating sender, and worktree name. A later trusted `thread_id` request with no
  worktree reuses only that still-registered isolated worktree; caller-supplied continuation
  paths, missing/deleted mappings, and sender mismatches refuse before an engine is acquired.
- **Why:** the SDK session store keys transcripts by cwd, so forcing every stateful trusted
  request into a new worktree made a successful `resume_thread()` path impossible. Persistent,
  owner-bound worktrees restore same-cwd continuation without allowing trusted dispatches to
  write the bridge's shared base checkout.
- **Review remediation (agy-print, P2):** the lease lock fd is now closed on non-busy `flock`
  failures, and a failed `worker.start()` releases the lease, active-request registration, and
  pool slot instead of leaking them.

### perf(e2e): batch H2 guard mutation rounds (2026-07-10)
- **What:** the H2 guard-mutation tier now batches selected guards through one isolated child
  worktree, requiring baseline-green, mutation-red, and restore-green for every guard while
  preserving individual pytest parameter cases.
- **Why:** repeated full repository copies dominated the serial tier; the measured representative
  subset fell from 11.46s to 1.42s, with all 14 guards still red-detecting and restoring green.
  (Root cause of the historical 30-min tier: untracked bulk in the repo root amplified each
  round's copytree 34x — 6.27s clean vs 212.71s with 1.2GB of .claude/worktrees present. The
  minimal copy in this change removes that entire class.)

### fix(agent-sdk): harden thread resume pre-checks (2026-07-10)
- **What:** explicit `resume_thread()` continuation now rejects non-UUID thread ids and empty
  session-store transcripts, and marks an engine unhealthy when reconnecting after disconnect
  fails.
- **Why:** the SDK cannot materialize empty or non-UUID sessions, and a failed rebuild otherwise
  returned a disconnected engine to the idle pool as healthy when retire-after-turn was disabled.

### fix(grok-acp): retire engines after each dispatch (2026-07-10)
- **What:** grok-acp engines now retire after every dispatch by default; long-lived
  seats can opt out with `BRIDGE_GROK_RETIRE_AFTER_TURN=0`.
- **Why:** grok-acp accumulates context in its ACP session and has no session-resume
  path; a self-contained probe recalled a prior dispatch's review-brief title, proving
  cross-dispatch contamination. Fresh engines isolate unrelated turns while explicit
  thread continuations continue to fail legibly as unsupported.

### fix(tests): finish the -sol seat rename in diagnose fixtures (2026-07-10)
- **What:** updated the stale `codex-bridge-dev` seat id to `codex-bridge-dev-example` in the
  diagnose/protocol-gate test fixtures (10 occurrences across `test_diagnose.py`,
  `test_diagnose_live_dispatch.py`, `test_bridge_protocol_gate.py`).
- **Why:** the seat rename (`0dc90ec`) updated `panel_constants.json` but only part of the
  test fixtures; the panel validator compares submission seats against the roster's
  target_id, so six tests failed with `unverified-without-panel` ever since. Fixtures in
  other files that use the old id as an arbitrary self-contained literal were left alone.

### fix(agent-sdk): resume subscription threads from the session store (2026-07-10)
- **What:** explicit `resume_thread()` continuation now forwards the stored session id
  for subscription seats as well as API-key seats. Both paths pre-check the session
  store for the current cwd and fail loud before reconnecting when the transcript is
  unavailable; ordinary engine startup still never auto-resumes subscription sessions.
- **Why:** subscription continuation previously dropped the explicit-resume flag and
  reconnected with a fresh conversation while reporting success. The SDK can materialize
  same-cwd transcripts from the session store across processes, so continuation should
  restore that context and reject unknown or cross-cwd sessions instead of losing it.

### fix(agent-sdk): retire engines after each dispatch (2026-07-10)
- **What:** agent-sdk engines now retire after every dispatch by default; long-lived
  seats can opt out with `BRIDGE_AGENT_SDK_RETIRE_AFTER_TURN=0`. Fresh engines no
  longer auto-resume persisted vendor sessions, explicit `resume_thread()` continuation
  remains available, and the live startup smoke test runs only once per daemon.
- **Why:** one sonnet wiki-gate seat stacked 15 unrelated dispatches into a 1.76MB
  session, contaminating a review gate across dispatches. Retiring the accumulated
  conversation removes that cross-dispatch state without charging every replacement
  engine for another real-model startup smoke turn.

### fix(codex): retire engines after each dispatch (2026-07-10)
- **What:** codex engines now retire after every dispatch by default; long-lived seats can
  opt out with `BRIDGE_CODEX_RETIRE_AFTER_TURN=0`.
- **Why:** one app-server thread served 24 unrelated dispatches and accumulated 22M tokens,
  causing panel cross-contamination and quota burn. Continuity is preserved for explicit
  `thread_id` continuations because resume across a fresh process was live-proven on 2026-07-10.

### chore(seats): rename the 7 default codex seats to carry the model tier (`-sol`) (2026-07-10)
- **What:** `codex-<project>-dev` → `codex-<project>-dev-sol` across all 7 default codex
  seats (bridge, project-g-consult, registry-x, project-d, project-h-browser, project-g,
  project-e), via the launcher's role-suffix convention (instance `codex-dev-sol` on the
  two launcher-shape seats, `--role sol` appended on the five direct-module plists). Live
  dispatch targets updated (wiki-refresh reviewer map + fallback, learn-intake quorum,
  diagnose panel blind seat, arb-memory e2e writer default), asdk trusted-sender policies
  updated (3 plist `--sender-policy` flags + 4 gitignored env files) and those seats
  cycled, living docs updated; terra/luna unchanged (already suffixed). Old plists
  archived in `.claude/plist-backups/2026-07-10-sol-rename/` for rollback.
- **Why:** a seat's model identity should be readable from its agent-id at a glance in
  rosters, dispatch calls, and audit trails, matching codex-bridge-dev-terra/-luna. The
  model was already pinned per-plist (`AGENT_MODEL=gpt-5.6-sol`); this is naming clarity
  plus belt-and-suspenders, not a behaviour change. Live-verified: canary + fleet probes
  under the new ids record `reasoning_effort: medium` (effort default intact), and the
  sonnet asdk seat accepted a dispatch from sender `codex-bridge-dev-example` end-to-end.
- **Trust-root lineage note:** `trust_root.json` still records `codex-bridge-dev` as a
  2026-07-09 certifying seat — correct as history, unchanged. The gate's
  rotation-disjointness check (`gate.py:723-728`) compares seat-id STRINGS, so a future
  rotation certified by `codex-bridge-dev-example` would pass disjointness against that root
  despite being the same lineage. Human approvers must treat `codex-bridge-dev-example` ==
  `codex-bridge-dev` when judging seat disjointness.

### feat(dispatch): per-dispatch reasoning-effort knob (`--effort`) (2026-07-09)
- **What:** `agent-dispatch`/`dispatch-dev`/go-client take `--effort
  low|medium|high|xhigh|max|ultra`, carried as `payload.reasoning_effort`. The codex
  engine sends an **explicit** effort on every `turn/start` — the per-dispatch override if
  set, else the seat default (`BRIDGE_CODEX_DEFAULT_EFFORT`, default `medium`). Unknown/
  unsupported engines warn-and-proceed like `--fresh-context`; a bad value is rejected at
  the CLI and warn-ignored at the bridge.
- **Why:** review quality scales strongly with codex reasoning effort (a low-vs-high
  seeded-defect benchmark showed terra/luna jump from 3–4/7 to 5/6 recall). A per-dispatch
  knob lets a panel request high effort for a hard review without a seat restart.
- **Two codex behaviours this had to work around, both found empirically:**
  (1) a `model_reasoning_effort` value in `config.toml` **shadows** the per-turn RPC
  `effort` param — so the codex seats must NOT pin it in config (removed from
  `~/.codex/config.toml`) or `--effort` is silently ignored; (2) codex **sticks** the last
  effort on a warm/pooled thread, so omitting the param keeps the previous value — every
  turn therefore sends an explicit effort to prevent a prior `--effort xhigh` leaking into
  later no-effort dispatches. Both verified via codex session-rollout `reasoning_effort`.
- Also: `dispatch-dev` now rebuilds the go-client binary when any `.go` source is newer
  than it (previously it only built when the binary was absent, so a source change was
  silently ignored until manual deletion).

### fix(codex): keep local-memory MCP secrets off the app-server command line (2026-07-09)
- **What:** the codex engine's `-c mcp_servers.arb-memory-local=...` override no longer
  embeds `ARB_MEMORY_LOCAL_DSN` and `OPENAI_API_KEY` values in argv; they are written to
  `~/.cache/agent-redis-bridge/arb-memory-local-mcp.env` (mode 600, regenerated from the
  daemon environment at every engine spawn) and the override carries only an
  `ARB_MEMORY_LOCAL_ENV_FILE` pointer. `arb-memory-local-mcp` loads the pointed file at
  startup and fails loud if the pointer is set but unreadable.
- **Why:** argv is world-readable in the process table — `ps` on any codex seat exposed
  the dev PG reader password and the embeddings API key. Verified empirically that codex
  spawns MCP children with a minimal environment (no daemon-env inheritance), so a
  pointer file is the only channel that avoids both argv and config-file exposure. ACP
  engines (JSON-RPC wire) and agent-sdk (in-process) were never exposed and are unchanged.
  The writer's tmp name is per-process: two seats bootstrapping in the same second raced
  a shared tmp through `os.replace` (ENOENT crash at codex-bridge-dev-luna standup).

### fix(bridge-protocol): gate git decode binary-tolerant + trust-root rotation (2026-07-09)
- **What:** `gate.git()` pins `encoding="utf-8", errors="replace"` on its subprocess so
  git output containing non-UTF8 bytes decodes with U+FFFD instead of raising; incident
  test added; `trust_root.json` re-pinned `521ade4e..`→`fbe7d3ea..` with certifying seats
  rotated to codex-bridge-dev + agy-bridge-dev (panel
  `panel-gaterepin-20260709T192027Z-b9232e`, grok absent-named).
- **Why:** committed `.enc` batteries crashed the gate's review-diff decode with an
  UNCAUGHT `UnicodeDecodeError` (`evaluate`'s except tuple doesn't cover it — a hard
  crash, not a fail-closed block); the `.gitattributes` fix covers only `.enc` paths.
  The identical edit by an out-of-scope worker was reverted for provenance (a5474e1/
  e71fa51); gate.py is in the certified object hash, so the redo required the
  subsystem's own rotation ritual. Held for Mark's ->dev review.

### fix(ci): pi-sdk host node suite runs in CI; adapter test fixed for pi-ai 0.80 (2026-07-09)
- **What:** `mcp-bridge.adapter.test.mjs` imports `validateToolArguments` from the
  `@earendil-works/pi-ai` package root (the `./base` subpath export was removed in 0.80),
  and CI gains a `node --test` step for `tools/pi-sdk-host`.
- **Why:** the node suite only ever ran locally, so the 0.80 export change broke the
  adapter test silently — three wedge-fix review seats independently rediscovered the
  breakage before anyone noticed it was pre-existing. A CI step makes host.mjs regressions
  visible at push time.

### fix(pi-sdk): retire host sessions and surface retry/compaction liveness (2026-07-09)
- **What:** pi-sdk engines now default to one fresh SDK host session per dispatch, forward
  SDK auto-retry/compaction phases as bridge progress/liveness events, and support `{pid}`
  in `BRIDGE_PI_SDK_EVENT_LOG` so parallel host processes do not interleave debug logs.
- **Why:** the overnight pi-sdk wedge diagnosis found two coupled failures: pooled SDK
  sessions re-billed accumulated dispatch history as quota-wasting request context, while
  provider retry/compaction phases were silent on the bridge wire and tripped the 30s
  no-output watchdog as false wedges.

### feat(authorbench): report-only author benchmark v1 machinery + corpus (2026-07-09)
- **What:** added `tools/authorbench/` with frozen bundle assembly, fact-key V9 validation
  including bundle-scoped `outcome_globs`, blinding/staging, jail manifests, exposure ledger,
  scoring/report walls, append-only store re-derivation, CLI routing, and the frozen v1.0
  corpus (`AB-D1`, `AB-S1`, `AB-P1`). The hermetic authorbench suite now gates V1/V2/V3/V4/V5/V6/V7/V9;
  Docker/live proofs are marked live with asserted skip reasons.
- **Why:** provides report-only evidence for future author-seat rotation decisions without
  ranking seats or feeding trust/quorum automatically. The bundle-scoped outcome globs close
  the Task-5 false-positive class where generic panel globs flagged unrelated historical
  reviews that legitimately predated a brief's base tree.

### feat(implbench): standing implementor bench harness and corpus (2026-07-09)
- **What:** Added the `bench/implbench` harness package, CLI entrypoint, hermetic V1-V4/V6 tests,
  and an encrypted C1-C7 fixture corpus for one-seat implementor bench runs.
- **Why:** Provides a report-only floor for deployed implementor seats using git/filesystem/envelope
  evidence, hidden batteries, explicit provenance, and a reporter wall that forbids rankings or
  routing/trust claims.

### feat(eval): complete Instrument 1 floor pipeline machinery (2026-07-09)
- **What:** `tools/eval` now has classified confined dispatch failures (timeout/review ⇒
  UNKNOWN infra-incomplete; canary 43 ⇒ Park), tiered boundary matching, run provenance,
  portable scenarios with CLI-only subject validation, gold matcher scoring/gating,
  format-conformance/detail split, publishable change-event artifacts, and real-corpus
  builder/lint machinery. The live docker/engine gates remain manual runbook work.
- **Why:** closes the certified Instrument 1 completion plan without weakening the core walls:
  report-only PASS/FAIL/UNKNOWN grids, no ranking/trust/seat-drop surface, infra UNKNOWN-not-FAIL,
  and no automatic downstream consumption.

### fix(learn): eval brief states the token→stance mapping (2026-07-09)
- **What:** `_eval_brief` now spells out REJECT→"block", WORTH-BUILDING→"approve",
  NEEDS-MARK→"abstain" and warns that the domain token itself is an invalid stance;
  pinned by a brief-content test.
- **Why:** live incident on the arb-bench eval: agy + GLM both concluded NEEDS-MARK and
  emitted `{"stance":"needs-mark"}` — the brief showed only an "approve" example and never
  stated the mapping, so a NEEDS-MARK seat had to guess and two of three guessed the same
  wrong way, zeroing their votes as eval-error. Same class as the 2026-07-06
  `approve-with-nits` vote re-fire: a seat-facing prompt that under-specifies the closed
  stance vocabulary converts substantive verdicts into wire errors.

### feat(codex): answer every server-initiated request — CDX-1 fixed fail-closed (2026-07-08)
- **What:** `codex.py` routes any id+method stdout message (both wait loops, BEFORE the
  params guard) to `_respond_to_server_request`: approval-shaped methods (known trio or
  name contains "approval") get a policy-decided decision — trusted ⇒ allow + WARNING
  (drift is loud), anything else ⇒ deny (`cancel`/`denied` picked from the request's own
  `availableDecisions`) + `command_denied` progress event; all other unknown methods get
  JSON-RPC `-32601`. Per-turn `BRIDGE_APPROVAL_DENY_BUDGET` (10): exhaustion ⇒ interrupt +
  bounded grace drain (`BRIDGE_APPROVAL_GRACE_S`, 10s) ⇒ legible error TurnResult; a
  wedged engine is marked unhealthy so the pool quarantines it. Daemon start names the
  path's reachability (`codex_approval_path_notice`). Inter-turn asks deny unconditionally.
- **Why:** engine-seat audit CDX-1 (P0-latent): the bridge told codex "ask before running
  commands" (`on-request` for every non-trusted policy) then dropped the ask in BOTH wait
  loops — a `human`-policy turn needing a command hung the full timeout as a fake
  "turn timed out". Latent only because the whole fleet runs bypass. Design 3-round
  ARB-panelled at v1.2 (unanimous close); wire shapes + the load-bearing
  error⇒no-execution claim pinned EMPIRICALLY on codex-cli 0.142.5 first (probe artifact
  `docs/superpowers/probes/2026-07-08-cdx1-v1-probe/`). Deny-proof verified: handler
  deleted ⇒ 8 tests red. Zero behavior change for bypass/trusted seats (V4 inertness).
  Impl panel (5 seats) faithful-to-design; P2s remediated `c9194d9` (inter-turn denies
  not budget-counted + 4 coverage pins). **Live gate V5 EXECUTED:** non-bypass wedge seat
  denied a real codex command in seconds (`command_denied` on the events stream, sentinel
  absent); bypass control seat unchanged. Fleet seats still run pre-CDX1 daemons — inert
  there by construction (all bypass); picks up on next fleet restart.

### test: CI green again — two env-divergent tests made hermetic (2026-07-08)
- **What:** (1) `test_visibility_auth`'s `redis.from_url` stub accepts **and asserts** the
  VIS-2 fail-fast socket timeouts (`socket_connect_timeout=5`, `socket_timeout=5`) that
  `build_visibility_app` now passes — the property is pinned, and deleting the timeouts
  goes red. (2) the agy-print progress-schema sequence test passes an explicit existing
  `conversations_root` (tmp_path) instead of inheriting the host-dependent
  `~/.gemini/antigravity-cli/conversations` default.
- **Why:** CI was red on every dev push since the VIS-2/AGY-2 stacks landed while the local
  full suite stayed green — the visibility tests are DSN-gated (skipped locally), and the
  agy test only announced the dark channel (`progress_channel` event, AGY-2
  working-as-designed) on hosts *without* the conversations dir, i.e. CI but not this Mac.
  Both failures were invisible to every local gate; reproduced locally (DSN un-gated +
  fake `HOME`) red→green. Same class as run-isolated-verdict: a test whose outcome depends
  on host state measures the host, not the change.

### docs(runbook): ARB Memory door access for external clients — DCR + OOB + PKCE recipe (2026-07-08)
- **What:** new `docs/runbooks/arb-memory-door-client-access.md`: how any HTTP-capable
  client (no claude.ai connector, no loopback listener) gets read/write ARB Memory access —
  dynamic client registration, the OOB code-display redirect, PKCE token exchange (with the
  RFC 8707 `resource`-parameter gotcha), and the three streamable-HTTP wire facts (root-path
  endpoint, SSE framing, `Mcp-Session-Id`). Indexed in `docs/index.json` + regenerated INDEX.md.
- **Why:** proven end-to-end 2026-07-08 from a CLI host with stdlib-only Python; the recipe
  lived only in that session. Authored on a host with a read-only deploy key, so the commit
  was delegated via ARB Memory artefact `art-032fdf938b97b8b0` v2 (repo-add request); file
  content verified byte-identical to the artefact before commit.

### feat(stall): blind-until-proven — a dark agy transcript channel no longer reads as a stall (2026-07-08)
- **What:** agy-print tasks start **blind** (bridge-side `BLIND_UNTIL_PROGRESS`, structural);
  only a real progress event proves the channel and enables stall detection. A blind task
  past threshold emits one `stall_unknown` event + `progress_blind` status field — never
  `stall_detected`/notify/`[stall]` stderr. The engine announces known-dark states
  (capture-off / root-missing at turn start; `tracker-disabled` at a single `poll()` choke
  point covering every disable site by construction) and the bridge re-blinds on them
  without retracting an active fired alarm or resetting the progress clock.
  `BRIDGE_AGY_CONVERSATIONS_ROOT` env-wires the transcript root (a missing root was fully
  silent; now warns per turn); divergent stall/turn-timeout config warns at daemon start.
- **Why:** engine-seat audit AGY-2 (P1): with the transcript channel dark, every healthy
  agy turn > threshold fired a false `stall_detected` into all four just-verified channels —
  and the dark states are an open set (a wrong-but-EXISTING conversations root triggers no
  error at all), so v1's enumerated detection was structurally unfixable; the panel
  (2 rounds, unanimous close: codex+GLM+agy certify, cold-Opus non-certifying) inverted it
  to proof-of-light. Trade-off, stated loudly: an agy wedge before first progress signals
  ONLY via `stall_unknown` on the visibility plane, bounded by `--turn-timeout`.

### fix(stall-channel): status poll decoupled from BLPOP outcome — inbox traffic no longer silences `[stall]` (2026-07-08)
- **What:** the go-client's wait loop polls the task status hash on a 5s **time cadence**
  instead of only on a BLPOP-timeout tick. The loop is extracted to `waitForReply` over a
  `redisConn` interface with a starvation regression test.
- **Why:** caught by the **first execution of the stall-detection live wedge gate**. Any
  envelope cycling through the shared orchestrator inbox — an orphan sibling reply being
  re-queued (`classOther` → RPUSH back → BLPOP returns it instantly), or notify chatter —
  meant BLPOP never timed out, so the poll behind the `[stall]` stderr channel never ran:
  a 53s live stall window produced zero lines because two stale replies from earlier
  sessions sat on `claude-bridge-dev`'s inbox. Channels 1–3 (event, status hash, notify)
  all fired; only the warm-orchestrator stderr channel was starved. The GO-1/GO-2 panel
  round reviewed the print function and the poll's failure path — the bug lived in the
  loop composition no unit touched (live-verification-catches-cli-glue).

### fix(stance): an omitted vote-fence severity no longer drops the whole vote (2026-07-08)
- **What:** `parse_stance` defaults an **absent** `severity` to `"none"` instead of raising;
  a **present-but-invalid** severity (typo like `P3`) is still rejected. Vote-fence doc
  fragment updated (both the source `docs/fragments/vote-fence.md` and the SKILL.md copy).
- **Why:** the bridge's vote auto-emit fail-soft-drops any fence `parse_stance` rejects, so
  a seat that ended with `{"stance":"approve"}` (no severity) lost its vote silently — cost
  real re-fires and manual re-emits during the engine-audit and visibility panels. The
  stance drives the merge decision; severity is advisory triage, so a missing field must
  not forfeit the vote. Malformed fences still warn (bridge guard c).


### fix(visibility hygiene): arb-watch backoff reset, dedup intent pinned, gateway-role skip made loud (2026-07-08)
- **What (GO-3):** `realStartStream` resets `streamState.backoff` to `baseBackoff` — a seat/
  orchestrator switch no longer carries a stale (up to 5s) backoff into the new target's
  first reconnect. **(GO-4):** pinned the intended dedup contract with a test + sharpened
  comment — a phantom dual-tee duplicate and a same-label sequential retry are
  indistinguishable by `(seat_id, run_id)`, so the roster shows the latest attempt (a
  task-aware key would defeat the phantom collapse; the older attempt's transcript still
  lives in the store). **(OPS-V1):** `arb-memory grants` now prints a WARNING when
  `ARB_VISIBILITY_DSN` is set but `ARB_VISIBILITY_GATEWAY_ROLE` is not (the skip fails
  closed but was silent); documented the var in `deploy/.env.example`.
- **Why:** ARB Visibility audit P2 batch (2026-07-08). No behaviour change for GO-4; GO-3
  is latency-only; OPS-V1 is a visibility/observability guard. The prod `.env` still needs
  the var set (or the out-of-band grant documented) — deferred to operator action.
  **(Done 2026-07-08:** var set on prod + `grants` re-run clean. The run exposed a second
  ad-hoc dependency — prod's MCP role is `arbmemory-mcp`, not the `arbmem_mcp` default, so a
  bare `grants` died on `apply_mcp_grants` before the visibility step; `ARB_MEMORY_MCP_ROLE`
  is now durable in prod `.env` and documented in `.env.example`.)


### fix(visibility): events:live redacted server-side; orchestrators() no longer blocks the loop (2026-07-08)
- **What:** `_redact_live_data` recursively scrubs **every** string value in the
  `events:live` payload (nested dicts, lists, all field names) via `redact()` before the
  XADD, so redaction is a server-side boundary rather than a client-JS rendering choice.
  (Initial fix used a fixed key-denylist; the certifying panel — GLM + cold-Opus — found it
  left `task_finished.summary`/`.error` and `orchestrator_committed.message` unredacted, so
  it was hardened to recursive before merge. `redact()` is a no-op on non-secret text.) `orchestrators()` moves all
  Redis I/O into a `to_thread` blocking body with a guarded fallback (clean 503 on outage,
  never a bare 500 or a hung worker) and the bus client gains socket connect/read timeouts.
- **Why:** ARB Visibility audit VIS-1/VIS-2 (2026-07-08), panel-confirmed by codex. VIS-1:
  a direct authenticated API reader received every secret ever typed into a shell command,
  violating the full-fidelity-capture design's "raw live is NOT the baseline" invariant.
  VIS-2: a single slow/black-holed Redis call on the event loop froze every concurrent
  request and open SSE stream (single-worker uvicorn).

### fix(stall-channel): dropped poll no longer duplicates [stall]; ad-hoc run_id falls back to task_id (2026-07-08)
- **What:** go-client `pollTaskStatus` returns `(status, ok)`; a failed poll leaves the
  print-state untouched instead of resetting it. The bridge stall milestone falls back
  `run_id → task_id` for ad-hoc dispatches, matching the roster/heartbeat tees.
- **Why:** ARB Visibility audit GO-1 (a transient poll error re-armed a duplicate `[stall]`
  line for one live episode on the warm-orchestrator channel) / GO-2, panel-confirmed.


### fix(pi-sdk+bridge): panel remediation — watchdog actually fires; resume-clear contained (2026-07-08)
- **What:** the new ack watchdog no longer counts host.mjs's unconditional `turn/started`
  (emitted BEFORE `session.prompt()`, i.e. before the wedge point) as output — pre-fix it
  latched `seen_any` and the watchdog could never fire in production while its test was
  vacuously green (the fixture omitted exactly that message). The wedge test now pushes
  `turn/started` like the real host and still asserts the abort. Also contained the
  stall-resume `_clear_stalled_at` Redis call inside `_record_stall_progress` (last
  uncovered blip shape in the IMP-4 wrap), and fixed two test nits (`____main__` typo,
  vacuous `if not result.ok` guard).
- **Why:** caught by the fix-stack certifying panel — cold-Opus P1 (fixture-supplies-
  what-code-lacks, confidence 95, hinge-verified against host.mjs:506) and GLM P2. The
  watchdog fix is precisely the mock-completeness-hides-bugs doctrine firing in review.

### fix(engines): steer/cancel honesty + pi-sdk wedge watchdog + legible agy-tmux failures (2026-07-08)
- **What:** pi-sdk and non-oneshot agent-sdk `steer()` now raise `EngineError` (bridge
  emits `steer_failed`) instead of silently dropping the message while reporting
  `steer_sent`; agent-sdk `interrupt()` waits (bounded 10s) on the SDK cancel and raises
  on failure instead of fire-and-forget `cancel_sent`; pi-sdk gains the
  `BRIDGE_PI_ACK_TIMEOUT` wedge watchdog pi_rpc already had (abort + unhealthy when a
  turn produces no output after start); agy-tmux non-timeout no-result turns carry an
  explicit error instead of `error=None`.
- **Why:** audit PSK-2/PSK-3/ASK-3/ASK-6/AGY-3, panel-confirmed. A phantom `steer_sent`
  makes the orchestrator believe an instruction reached the model; the pi-sdk wedge shape
  (unauthenticated provider, kevent block) held a pool slot for the full 3600s with only a
  passive stall marker. Remaining from the audit's stall cluster: AGY-2 (agy-print dark
  transcript channel ⇒ false stall positives) is deliberately deferred — its fix choices
  change stall-detection semantics and belong with the pending live wedge gate.

### fix(codex+cursor): engine health contract — dead children fail fast and never recycle (2026-07-08)
- **What:** codex gains `healthy`/`is_healthy()` (process poll + reader-thread liveness,
  flipped on turn timeout / send failure / reader death), `_process_exited` fast-fail in
  both wait loops (with the real exit code, ~2s detection), the per-side id guard in
  `request()`, `errors="replace"` decoding, a guarded stdout reader, and warnings on
  dropped malformed frames. cursor `is_healthy()` now also checks reader-thread liveness,
  its reader is guarded (flips unhealthy instead of dying silently), and its Popen decodes
  with `errors="replace"`.
- **Why:** audit CDX-3/CUR-2, panel-confirmed: codex had no health signal at all, so
  `engine_pool.release()` recycled dead subprocesses forever while every task on the slot
  timed out generically after 3600s; cursor's deaf-reader shape (child alive, reader dead
  from one bad byte) poisoned the next task the same way.

### fix(agent-sdk): permission gate always returns an explicit decision (2026-07-08)
- **What:** `_gate`'s telemetry emission (`_turn_on_event` → bridge Redis writes) is now
  guarded; any failure logs a warning and the gate still returns its computed
  `PermissionResultAllow`/`Deny`.
- **Why:** audit ASK-2, panel-confirmed: only `decide()` was wrapped, so a Valkey blip
  during the allow/deny telemetry raised out of the SDK permission callback — the CLI then
  receives an error-response whose allow/deny interpretation is unverifiable, breaking the
  fail-closed contract the whole PATH-2 design rests on.

### fix(engine-pool): failed engine start is torn down instead of leaked (2026-07-08)
- **What:** `EnginePool.acquire()` best-effort `stop()`s an engine whose `start()` raised,
  then re-raises; pool accounting (`_started`/`_busy`/`_idle`) stays intact.
- **Why:** audit PSK-1/ASK-4, panel-confirmed: pi-sdk/pi-rpc/agent-sdk spawn a child (node
  host.mjs / claude CLI) plus reader threads before their startup RPCs; a failure after the
  spawn (undeclared model, auth wedge, connect timeout) left the engine unregistered and
  unreachable by `stop_all()` — one live orphan per dispatch, unbounded on a misconfigured
  seat.

### fix(bridge+codex): a steer/cancel against a dead engine can no longer crash the daemon (2026-07-08)
- **What:** codex `_send` converts `OSError` (write to a closed pipe) into `AppServerError`;
  `handle_control` catches `Exception` and answers `<kind>_failed`; the control-lane drain
  is extracted to `_drain_control_lane()` with the same per-envelope guard the request lane
  already had.
- **Why:** audit CDX-2, panel-confirmed: a late steer/cancel to a crashed codex child raised
  raw `BrokenPipeError` through the unguarded control-lane `handle_raw`, reaching `main()`'s
  exit — killing every other in-flight parallel task on the daemon.

### fix(bridge): engine and observability failures can no longer lose a task's reply (2026-07-08)
- **What:** `run_engine` now converts ANY engine exception (not just `EngineError`) into a
  failed `TurnResult` and marks the engine unhealthy; `handle_progress` wraps its Redis
  emissions so an observability failure degrades to a logged warning instead of killing
  the engine callback that raised it; `_check_stalls` unmarks a stall episode when its
  emissions fail (new `StallWatch.unmark`) so the next tick retries instead of dropping
  the episode permanently.
- **Why:** audit 2026-07-07 (ASK-1/AGY-1/IMP-4) + panel GAP-1: a `ProcessError` from the
  agent-sdk CLI, a `UnicodeDecodeError` from agy stdout, or a Valkey TLS blip inside
  agy-print's poll thread each made the dispatch vanish with no reply, no `task_finished`,
  and (post-`finally`) no stall visibility — the orchestrator's worst failure shape. The
  2026-06-30 subscription-seat ProcessError incident was this class; this closes the class
  rather than the trigger. At-least-once stall emission beats silently-never.

### fix(acp-engines): per-side JSON-RPC id guard propagated to every response-wait loop (2026-07-08)
- **What:** the `eee0b15` rule ("a message with `method` is a request regardless of id")
  now also guards `cursor_acp.request()`, `gemini_acp` (prompt loop + `request()` —
  inherited by kimi-code/mini-agent), and `grok_acp` (both). Collision tests ported to all
  three test files.
- **Why:** audit CUR-1/LT-1, panel-confirmed: the same id-collision that killed cursor
  turns was live in every sibling — gemini-family reported a colliding permission request
  as a SUCCESSFUL early turn completion (silent wrong answer) and left the agent wedged
  on an unanswered request; grok discarded all streamed chunks. One rule, all loops.

### fix(learn+arb-memory): external proposal text withheld from every search surface (2026-07-07)
- **What:** learn index hints now carry a metadata summary (title line, status, target,
  `memory_get` pointer) instead of the verbatim body (`hint_summary()`); `retrieve()`
  additionally withholds the artefact attachment for learn-proposal hits so `memory_search`
  never ambiently delivers the raw body — explicit `memory_get` only. Existing prod hints
  sanitized in place (embeddings recomputed). Bonus catch by the pin test: `propose` could
  clobber an `eval-approved` id with a fresh `proposed` version — it now refuses ANY
  existing id without `--force`.
- **Why:** the injection-screening `/learn` candidate was REJECTED (screening is the wrong
  mechanism for a mistakes-not-malice model), but GLM's dissent identified the real surface:
  external, pre-eval text stored verbatim, served fleet-wide, persisting across rejection.
  Structural fix (don't index/attach raw foreign text) beats configurational (scan it).
  Ops note: the first sanitize pass silently ROLLED BACK — psycopg3's `conn.transaction()`
  after prior statements is a savepoint in an uncommitted implicit txn, and in-transaction
  verification is vacuous; redone with autocommit + fresh-connection verification.

### feat(arb-memory): journey snapshot export and visibility pane (2026-07-07)
- **What:** added `arb-journey-export` to build and verify a graph snapshot from latest
  artefacts and live hints, plus `/journey` and `/journey/graph.json` visibility routes,
  a self-contained graph/table client, and compose wiring for the shared snapshot volume.
- **Why:** operators need a low-friction journey view that exposes artefact relationships,
  free-hint inventory, and graph health without leaking raw hint text or adding DB access
  to the visibility surface.
### feat(arb-learn): proposal intake CLI with panel evaluation (2026-07-07)
- **What:** added `arb-learn` for proposal intake, evaluation, promotion, and human
  resolution. Proposal state is stored as versioned learn artefacts with JSON first-line
  headers plus replacement index hints, guarded by write-visibility polling, terminal-state
  transition checks, dedupe, pinned three-seat evaluation dispatch, fenced verdict parsing,
  and drift-aware promotion.
- **Why:** creates the `/learn` path for turning candidate lessons into ARB/project work
  without bypassing the memory write loop, panel audit surface, or Mark-only project gate.

### feat(wiki): naming a sibling repo requires citing one of its pages (2026-07-07)
- **What:** `validate_pages` now takes a `sibling_repos` map (name → page ids, built from
  the config in `main`): a page whose prose names a sibling repo (word-boundary match, so
  `project-a` inside `project-a-service-gui` doesn't false-trigger; backticked ids stripped
  first so a citation isn't itself a "mention") must cite at least one of that sibling's
  page ids, or validation fails the run. The brief's sibling section upgraded from "may
  cite" to the same MUST, with an anti-gaming note (don't name siblings gratuitously).
- **Why:** two refreshes in a row kept the cross-repo boundary human-readable but not
  machine-followable — project-a v3 named `project-a-service-gui` with zero id citations under
  the optional wording. Mark's call: mandatory when named.

### fix(arb-memory): a replacement artefact_index hint retires its predecessors (2026-07-07)
- **What:** `write_artefact_and_hints` soft-deletes older `kind=artefact_index` hints for
  the same artefact, in the same transaction as the new version's hint insert. Scoped:
  pinned evidence/audit hints keep faithful history (`test_two_step_faithful` unchanged);
  a version write with no replacement index hint retires nothing (the artefact can never
  go search-invisible). 3 new tests; full arb_memory suite 538 green; codex review APPROVE
  (zero findings — checked single-writer/locking, dedup-path, blast radius on vault export
  and memory_recent).
- **Why:** live incident via claude.ai reading the wiki: after a page refresh,
  `memory_search` kept serving v1 while `memory_get` showed v2 — *permanently*, not as
  re-embed lag. Superseded index hints were never retired, the lexical tie-break
  (`ORDER BY ts_rank DESC, h.id`) deterministically favors the oldest hint on
  near-identical text, and `retrieve()` pins the artefact at the hint's version. Requires
  a one-off prod cleanup of already-stale index hint layers (retirement only acts on new
  writes).

### feat(wiki): boundary rule + sibling-repo hints in the generation brief (2026-07-07)
- **What:** the generation brief now carries a Boundaries section: any page touching a
  system whose code is NOT in the repo must say so and name where it lives — plus, when
  refreshing, the brief lists the *other* onboarded repos' page ids as citable siblings
  (cross-repo backticked citations were already validator-legal; the generator just was
  never told they exist). Threaded through the revision brief too.
- **Why:** claude.ai reader feedback on the live wiki: `wiki-project-a-external-service`
  describes project-a-side external-service control endpoints without saying the service node
  itself is a separate repo — exactly the trap this wiki exists to prevent (an orienting agent
  burning context grepping the Laravel tree for node code that isn't there). Highest-leverage
  addition per that feedback.

### fix(wiki): review task hands over the repo path and forbids filesystem searching (2026-07-07)
- **What:** the reviewer dispatch task now states the repo path explicitly and instructs the
  reviewer to use only the given page paths and paths under the repo — never to search or
  glob the filesystem. Pinned by a `main()`-level test.
- **Why:** the task used to give page paths but not the repo location, so a grounded
  reviewer had to *find* the repo. On the project-g-consultant onboarding the Sonnet seat (cwd
  `/Users/<user>` since the workdir widening) fired four successively broader `bfs` sweeps of
  the whole home directory, each hanging (likely on a stat-blocking path like
  `~/Library/CloudStorage`), wedging the review turn ~25 minutes until killed. project-a's
  review only worked because the model guessed `~/project-a` by name. Handing over the path
  removes the search incentive; the instruction is the belt to that suspender.

### fix(wiki): generation output under the state dir, so the reviewer seat can read it (2026-07-07)
- **What:** the CLI now creates per-run generation output dirs under `<state-dir>/tmp/`
  (i.e. `~/.arb-wiki/tmp/…`) instead of the system temp dir, via the already-injectable
  `mkdtemp_fn`. Cleanup semantics unchanged (`refresh_repo` still removes the dir).
- **Why:** first out-of-family onboarding (project-a) exposed that the asdk reviewer seat has
  no shell and reads only under its workdir — pages in `/var/folders/…` and repos outside
  the workdir were unreachable, and Sonnet correctly fail-closed with "cannot verify against
  the actual source" (the gate refused to bluff — but the run could never succeed). Paired
  ops changes outside the repo: the reviewer seat's workdir widened to `/Users/<user>`
  (read-only tool ceiling unchanged), and a 5-day-old ghost daemon holding the same
  `asdk-bridge-dev-sonnet5` id (the PB-4 mutation-tooled seat) was killed — two consumers
  on one inbox were racing dispatches, which is what made the revision-round review dispatch
  fail with an opaque exit 1. One-active-daemon-per-agent-id, re-learned.

### feat(wiki): default review gate is now the Sonnet-5 subscription seat (2026-07-07)
- **What:** `_REVIEWER_MAP` for codex-generated pages now points at `agent-sdk` /
  `asdk-bridge-dev-sonnet5` (launchd-persisted via
  `com.example.asdk-sonnet-bridge.bridge-dev`, read-only tool ceiling `Read,Grep,Glob,LS`,
  plan-authenticated via a dedicated 0600 env file the plist sources — the OAuth token is
  process-env-read, not env-file-read, so the plist wraps the launch in
  `set -a; source …`). `agent-sdk`-generated content defaults to a codex reviewer;
  agy-print→codex unchanged. Terms-change fallback documented in-map: swap the target to
  the pi-GLM seat.
- **Why:** Mark pulled trust from agy-print as the gate. A same-pages head-to-head on the
  live run-1 output: Sonnet (REQUEST-CHANGES, found the in-repo ground truth that
  `gemini-acp` is retiring) made the safer fail-closed call than Opus (APPROVE with the
  same fact rated non-blocking), at lower plan cost — the desired gate temperament from
  the cheaper model. Seat smoke-tested over the exact dispatch shape the pipeline uses
  (`--engine agent-sdk --target-id asdk-bridge-dev-sonnet5`, sender `claude-bridge-dev`).

### feat(wiki): revise-and-resubmit — review rejections feed back into a bounded revision loop (2026-07-07)
- **What:** a REQUEST-CHANGES with revisions remaining no longer aborts: `refresh_repo` writes
  a revision brief (the reviewer's reasons verbatim + revise-in-place instruction + the full
  original format rules) into the output dir, re-dispatches the generator, then re-validates
  and re-reviews the revised pages. Bounded by `max_revisions` (CLI `--max-revisions`,
  default 1; `0` = the old reject-aborts behavior; negative fails loud — a negative bound
  would skip validation *and* review entirely and store unvalidated content). The reviewer's
  prompt is identical every round. (Amended same day: that guard is prompt-level only — a
  session-continuous reviewer seat still remembers round 1, live-observed as "previously-
  flagged issues have been corrected". Kept deliberately per Mark: the objector verifying
  its own fixes against source is the useful behavior, round-1 coverage of unchanged pages
  stands, and `fresh_context` on the review dispatch is the one-line stricter option.)
  Structural validation failures still abort immediately (contract bugs,
  not factual disputes) — including one *introduced by* a revision. 7 new tests including a
  `main()`-level rejected-then-revised-then-stored round trip.
- **Why:** the live v1.1 `--add` proof showed rejection-with-actionable-reasons is the common
  case (agy-print's REQUEST-CHANGES named the invented envelope keys and the deprecated
  gemini-acp page — a competent generator can fix both), but those reasons only went to
  stderr for a human. Discard-and-retry restarted blind; feeding the feedback back converts
  most rejections into approved content with zero human steps — the zero-touch goal.

### fix(wiki): roll back the config entry on a rejected/failed `--add` (2026-07-07)
- **What:** `rollback_add()` — when a freshly-added repo's first refresh fails (review
  rejection, validation failure, dispatch error), the `--add` path now removes the config
  block `add_repo` persisted, restoring the config to its pre-add state. Guarded two ways:
  only fires for the repo added *this run* (a routine refresh failure of a pre-existing repo
  never touches config), and refuses when `pending-<repo>.json` exists (that batch is durable
  and possibly partially stored; resume-from-pending needs the config entry — rolling back
  would orphan it). Runs under the same run lock as `add_repo`. Plus the suite's first two
  `main()`-level integration tests (faked `subprocess.run` covering discovery/generation/
  review/store), pinning both the rejected-add rollback and the approved-add happy path.
- **Why:** the v1.1 live `--add` proof left the review-REQUEST-CHANGES'd agentredisbridge
  repo *configured-but-unstored*, needing a manual config revert — and a later plain
  refresh (review-off) would have completed it, landing the exact content the gate rejected.
  `--add` is now atomic: either configured *and* stored, or neither. The `main()`-level tests
  exist because all three v1.1 live-caught bugs were in exactly this untested CLI glue
  (memory: `live-verification-catches-cli-glue`).

### fix(tests): acknowledge wiki_refresh.py in the env-reader co-move tripwire (2026-07-07)
- **What:** `test_arb_memory_dsn_and_redis_url_readers_co_move_with_env` allowlists
  `src/agent_redis_bridge/wiki_refresh.py` with the reason: the `ARB_MEMORY_REDIS_URL`
  reference lives in `STORE_SCRIPT_TEMPLATE`, a script executed on the droplet inside the
  memory container — the local wiki process never reads that env.
- **Why:** dev CI had been red on this tripwire since the v1.1 merges — the test exists to
  force exactly this conscious ack whenever a new `src/` file mentions the memory env names,
  and v1.1 shipped without it.

### feat(wiki): ARB Wiki v1.1 — zero-touch repo onboarding (2026-07-06)
- **What:** `arb-wiki-refresh --add <repo-path>` onboards a virgin repo with no human step but
  the path: a discovery dispatch has a seat read the repo and propose the page set as strict
  JSON, `parse_discovery` validates it (2–8 pages, `wiki-<repo>-<slug>` id format, reserved
  `manifest` slug, cross-config id disjointness, non-empty fields), the loop atomically merges
  the config block under the run lock (config re-read after lock), recomputes `all_ids`, and
  runs the normal refresh. A `review_fn` pre-store hook in `refresh_repo` (the only change to
  the shipped v1 loop; `None` = v1 behavior) drives an opt-in decorrelated review gate
  (`--review`/`--no-review`, **default-on for `--add`**): a reviewer seat of a *different*
  engine than the generator factually sanity-checks the first generation before store;
  reject aborts all-or-nothing. `resolve_reviewer` fails loud rather than silently reviewing
  with the same engine. 24 new tests (47 total in the loop suite).
- **Why:** Mark's steer — "I don't want a human step." Only the repo path is human input; page
  curation and first-generation quality checking are both seat dispatches. Spec and plan each
  2-round independently panel-confirmed (codex + agy-print + cold-Opus); the panels caught two
  spec P1s (stale `all_ids` failing a new repo's own sibling validation; the review gate
  needing a `refresh_repo` hook), a deterministic-ulid `manifest`-slug collision that would
  silently drop the batch marker, and two plan P1s (test rows tripping the count guard; a
  same-engine reviewer override defeating decorrelation). codex-TDD'd, cold-Opus verified
  faithful (2 P2 nits fixed: reviewer no longer shown the brief; clean error on partial seat
  override); suites green independently.

### feat(wiki) + fix(arb-memory): ARB Wiki generation loop; upsert_artefact rollback fix (2026-07-06)
- **What:** `scripts/arb-wiki-refresh` + `src/agent_redis_bridge/wiki_refresh.py` +
  `configs/arb-wiki.json` — the automation loop the pilot proved manually: per-repo HEAD
  change detection (exclusive run lock, atomic state), read-only seat dispatch generating the
  configured page set, strict structural validation (final-line backticked See-also against
  the whole config, bare/typo'd `wiki-*` refs rejected, all-or-nothing), and a batch-safe
  store (full validated intent batch persisted per-repo before any enqueue, deterministic
  `(nonce, artefact_id)` idempotency ulids, resume-from-file never regenerates, manifest last
  as the completion marker) enqueued on the prod bus from the droplet — single-writer
  preserved, zero new credentials. Plus the prerequisite **shipped-bug fix**:
  `store.upsert_artefact` now dedups against the LATEST version only (and drops the
  `(artefact_id, content_hash)` UNIQUE constraint) — previously A→B→A content reverts
  silently no-op'd, stranding every latest-version consumer (the vault export) on stale
  content forever. 26 new tests (2 store + 24 loop).
- **Why:** Mark's steer: the wiki-generation side feeds the knowledge graph; no human step in
  steady state (v1.1 zero-touch onboarding filed). Spec and plan each 2-round independently
  panel-confirmed (codex + agy-print + cold-Opus; the panels caught a real shipped P1, two
  plan P1s that made the literal plan un-executable, and the store's mixed-generation resume
  hole). Implementation codex-TDD'd, cold-Opus verified faithful; suites green independently
  (535+1skip arb_memory, 24 loop).

### feat(arb-memory): vault graph export — typed wikilink edges (2026-07-06)
- **What:** the vault exporter now emits each artefact's relationship graph: `aliases`
  frontmatter, a generated footer with `## References` (explicit textual artefact citations,
  matched with panel-refined lookaround boundaries — bare matching for `art-<hex>` and
  underscore ids, backtick-required for hyphen-only ids, URL/path/prefix collisions excluded
  by construction, sentence-final periods allowed) and `## Related` (pgvector
  min-pairwise-hint cosine similarity over stored embeddings, top-5 under an env-tunable
  threshold `ARB_VAULT_EXPORT_SIMILARITY_THRESHOLD`, default 0.35 calibrated on the real
  corpus: 73/91 artefacts connected, hub blowup starts at 0.45). Links target filename stems
  (portable in Obsidian/Quartz/any viewer); bodies stay byte-verbatim above the generated
  marker; no new privileges, no OpenAI dependency (stored vectors compared server-side).
  11 new tests.
- **Why:** the Quartz-viewer panel unanimously found the vault's graph value inert (no links
  emitted, hash-suffixed filenames unresolvable by name); Mark's steer: the graph is the goal,
  the viewer a rendering choice. Spec and plan each 2-round independently panel-confirmed
  (codex + agy-print + cold-Opus); implementation codex-TDD'd, cold-Opus-verified
  character-for-character against the plan. E1 yield measured on prod: 55 directed edges,
  zero precision loss vs a loose scan.

### ops(arb-messages): fix prod grants drift on arb_agent_keys, expand provider allowlist (2026-07-06)
- **What:** Live-fixed prod — `GRANT UPDATE ON arb_agent_keys TO "arbmemory-mcp"` (was missing;
  `SELECT`+`INSERT` only) and expanded `ARB_MESSAGES_ALLOWED_PROVIDERS` from `cloudflare,azure`
  to `digitalocean,cloudflare,azure,office365` in `deploy/.env`, recreating the `mcp` container
  to pick it up. No code change — both are live database/config fixes. Full incident + fix
  recorded in `docs/superpowers/plans/2026-07-02-arb-messages-deployment-checklist.md`.
- **Why:** `messages_register_key` was silently broken in prod since the rotate-on-register fix
  shipped in `src/arb_messages/keys.py` (that fix added an `UPDATE` statement ahead of the
  `INSERT`; the corresponding grant was never re-applied to prod). Found while standing up the
  `arb_vault_export` role for the read-model export feature above, which needed a working
  ARB Messages channel to reach the Codex app's DigitalOcean admin access.

### feat(arb-memory): read-model export — one-way markdown vault for human browsing (2026-07-06)
- **What:** `src/arb_memory/vault_export.py` projects the latest version of every `artefacts`
  row (plus deduplicated, sorted tags from any linked, non-deleted `hints`) into one markdown
  file per artefact under a configurable vault directory, so a human can `grep`/browse ARB
  Memory's accumulated knowledge without an LLM client in the loop. Filenames are
  hash-suffixed (`slug-idhash.md`) for guaranteed collision-freedom, with an in-run fail-loud
  check as a backstop; frontmatter fields are JSON-escaped for YAML safety. Reuses the
  existing `apply_local_reader_grants` under a new dedicated `arb_vault_export` role (wired
  through the `grants` CLI via a new `ARB_VAULT_EXPORT_ROLE` env var — same privilege shape as
  the local MCP reader, separate credential). Ships with a thin CLI wrapper
  (`scripts/arb-memory-vault-export`) intended for a nightly cron on the MCP-host box. 12 new
  tests (`tests/arb_memory/test_vault_export.py`, `test_vault_export_grants.py`).
- **Why:** no non-LLM-mediated way existed to browse ARB Memory's accumulated artefacts
  (specs, decisions, handoffs) — only `memory_search`/`memory_get`/`memory_recent` MCP tools,
  all requiring an LLM client. Concept converged via a 4-pattern `claude-obsidian` adoption
  panel (`ARB-2026-07-OBSVAULT-R1`, 2026-07-05); design and implementation plan each went
  through 2 independent panel rounds (codex + agy-print + cold-Opus) before dispatch, per
  `docs/BACKLOG.md` § "ARB Memory read-model export" and
  `docs/superpowers/specs/2026-07-06-arb-memory-read-model-export-design.md`. Deploy-time role
  standup, actual cron wiring, and real env values are explicitly out of scope for this
  commit — ops steps for whoever has MCP-host-box access, not code.

### docs(lint): harden doc recipe dispatch scanning (2026-07-06)
- **What:** `scripts/check-doc-recipes` now catches `agent-dispatch` / `dispatch-dev`
  invocations after shell operators, scans tilde fences and CommonMark-indented fenced
  blocks, and checks each dispatch command independently so one labelled command cannot
  shield a bare one in the same block. Added red-first probe tests for all three cases.
- **Why:** cold-Opus verifier nits from the 2026-07-06 docs-drift cycle found blind spots
  the current corpus did not trigger, but future docs could regress silently.

### feat(claude-hooks): versioned context-cost lifecycle hooks (2026-07-06)
- **What:** `scripts/claude-hooks/` — three Claude Code hooks + README: `precompact-preserve.sh`
  (compaction keeps task-ids/run-ids/SHAs/worktrees/TaskList verbatim), `context-nudge.sh`
  (Stop hook blocks once per transcript-size band — default 25MB then +15MB — with a
  write-a-handoff instruction), `handoff-hint.sh` (fresh sessions get a pointer to the newest
  `.claude/handoffs` file). Wiring is per-operator in `settings.local.json`.
- **Why:** heavy warm-orchestrator sessions accumulate 20–60MB of transcript, so every
  prompt-cache miss re-reads a large context at full price; these make the
  handoff→`/clear`→resume loop self-prompting instead of remembered.
- **Merge-fix note:** the docs-drift merge surfaced a stale `docs/reviews` directory entry in
  `docs/index.json` — green in the codex worktree only because `git mv` left the empty source
  dir on disk there (environment-masked green); removed on dev, all checks green.

### docs: repo-wide drift fixes from the 2026-07-06 four-agent docs review
- **What:** corrected stale dispatch recipes to include run labels, added the
  `scripts/check-doc-recipes` lint, single-sourced the audit vote-fence vocabulary, refreshed
  quorum/routing guidance, marked deprecated `gemini-acp` references, reconciled pi-sdk/pi-rpc
  worker docs, archived legacy review briefs, and marked `SPEC.md` as historical.
- **Why:** three P1 findings mapped to real panel failures on 2026-07-06: unlabelled dispatches
  lost audit/run identity, a non-canonical stance (`approve-with-nits`) forced a vote re-fire,
  and missing daemon-side audit Redis / undrained audit stream state produced silent vote gaps
  until verdict reconciliation.

### feat(pi-extensions): arb-dispatch-monitor hardening — six fixes from the Fable review (2026-07-06)
- **What:** (1) the default auto-synthesis prompt now enforces the anti-laundering contract
  (roster-vs-returned accounting, per-seat stance table, hinge-claim verification, named vote
  gaps) — the code default contradicted the operating guide; (2) `deadlineMinutes` on the
  auto-synthesis barrier — on expiry with missing/non-terminal seats it queues a vote-gap
  follow-up (named absent seats + re-fire instructions) instead of hanging forever, and the
  poller stays alive while an unfired deadline pends; (3) `worktree`/`worktreeBase`/
  `worktreeCleanup` pass through to `agent-dispatch --worktree` (hard isolation); (4) task-id
  window expiry warns of a possible misroute and the widget shows `no task-id (misroute?)`;
  (5) vote-fence recovery for >256KB replies via tail slice + JSON-escape decoding (the fence
  sits at the END of the reply; the head-capped read truncated mid-JSON and silently dropped
  the vote); (6) status-bar counting treats queued/quiet-alive/stale as active. Dead code
  removed (`pad`, `renderWatchLines`, `recentEvents`). Eight new tests.
- **Panel:** `panel-pi-ext-hardening-20260706T025015Z-2a718b` — codex + agy-print + pi-GLM
  certifying, cold-Opus non-certifying (Anthropic-lineage author). Convergent P1 (3 seats:
  vote parsers matched the FIRST fence, so a quoted example fence in a large reply could mask
  the real vote — worse than the prior no-vote baseline) confirmed and fixed in `93a34ac`
  (last-fence matching, both parsers). agy's claimed P1 (async rollback on `sendUserMessage`
  rejection) refuted by evidence (`ExtensionAPI.sendUserMessage` is typed `void`; the Promise
  variant is `ReplacedSessionContext`) — defensive thenable handling added anyway. Verdict
  audit-closed (6 rows: manifest, 4 votes, self-verified verdict).
- **Ops:** bridge-seat `--audit-panel` vote auto-emit was inert on this host — the daemon env
  lacked `ARB_MEMORY_REDIS_URL` (fail-soft by design; the verdict reconciler caught it).
  Added to `envs/agent-redis-bridge-dev.env` + daemons kickstarted, so future panels emit
  votes natively. Also: instruct seats with the CANONICAL stance vocabulary
  (`abstain|approve|block|needs-changes|timed-out`) — a non-canonical label (`approve-with-nits`)
  cost a vote re-fire.

### docs(pi-orchestrator): operating guide + canon promotions from the 2026-07-05 pi run review (2026-07-06)
- **What:** new `docs/pi-orchestrator-operating-guide.md` — the pi-harness counterpart of
  CLAUDE.md's orchestrator role layer, inlining the panel canon a pi warm orchestrator can't
  auto-load (roster composition incl. author-non-quorum and the one-Opus certify cap,
  independence via per-reviewer `--worktree`, hinge-claim verification, vote-gap re-check +
  named-absent + re-fire, audit-emit wiring, auto-synthesis barrier caveats with an
  anti-laundering `synthesisPrompt` template, destructive-git guards on merge). Plus two
  promotions out of the run's lessons artefact: the empty-log re-check procedure into
  `docs/multi-model-consensus.md` (new "Vote gaps" section) and the raw-model-id-as-engine
  failure shape into `docs/fragments/failure-shapes.md` (synced to README + skill;
  drift-check green). `pi-extensions/README.md` now points at the guide and its example
  `synthesisPrompt` carries the roster/stance-table requirements.
- **Why:** the first pi-orchestrated ARB run (artefact `arb-pi-orchestration-lessons-2026-07-05`,
  reviewed 2026-07-06) re-derived the panel discipline from a single run and needed heavy live
  human steering — the ~20% it missed were exactly the rules born from incidents pi never saw
  (report-leak echo chamber, double-Opus lapse, silently-shrunk panel). Lessons docs from one
  run should be deltas against the canon, not standalone re-derivations; this makes the canon
  reachable from inside the pi harness. The run's future-improvement "standard panel manifest"
  was dropped in review: `arb-audit-emit`'s roster manifest already exists — the extension
  should wire into it, not rebuild it. v2 of the artefact (ARB Memory) integrates the review
  edits and cites this guide as canon.

### feat(arb-memory): out-of-band login for clients without a loopback listener (2026-07-05)
- **What:** a client that registers the sentinel redirect_uri `urn:ietf:wg:oauth:2.0:oob`
  (`src/arb_memory/mcp/redirect_policy.py::OOB_REDIRECT_URI`) completes the existing
  authorization_code+PKCE+passphrase/TOTP login flow unchanged, but the login-success page
  **displays** the authorization code instead of doing a `Location` redirect — for a native
  app/extension that can't or doesn't want to run a local HTTP listener to catch a loopback
  redirect. Zero changes to token minting, PKCE validation, or the login gate itself; the only
  change is how the already-issued code is delivered to the client (rendered vs. redirected).
- **Scope note:** this is deliberately the smaller of two options considered. The other — full
  RFC 8628 device-code (poll-based, no copy-paste, verify on any device) — can't be expressed
  inside the installed MCP SDK's `OAuthAuthorizationServerProvider` abstraction (its token-endpoint
  request model is hardcoded to exactly `authorization_code`/`refresh_token`); it would need a
  parallel HTTP surface sitting in front of the SDK's own routing. Filed as a backlog item
  (`docs/BACKLOG.md` § "full RFC 8628 device-code login") for if the copy-paste step ever matters.
- **Security note (recorded, not resolved by code):** this is the OOB pattern Google deprecated in
  2022 for phishing resistance. Accepted here because every ARB Memory login already requires a
  personal passphrase + TOTP regardless of redirect_uri — a materially higher bar than the
  consumer-OAuth case that motivated the deprecation — but it is a real, non-zero trade-off, not a
  free simplification.
- **Verified:** new tests in `test_redirect_policy.py` (exact-match allowlisting, no prefix/case
  matching), `test_login_route.py` (code-display response shape), and a genuine end-to-end
  register→authorize→login→exchange round trip in `test_dcr.py` extracting the code from the
  rendered page text (codes are stored only hashed — there is no plaintext copy anywhere else to
  assert against, matching what a human actually copies). Full `arb_memory` suite: 510 passed.

### fix(arb-messages): rotate-on-register fixes cross-session key-registration lockout (2026-07-05)
- **What:** `messages_register_key` always failed with `UniqueViolation` for any Claude Code
  session after the first, because `agent_id` is derived from the shared OAuth connector identity
  (`door_tools.py::_actor`) — every concurrent independent session shares one `arb_agent_keys` row.
  A session that never registered before could hit the same failure a sibling session caused
  minutes earlier, with delivered messages it could never decrypt. `register_key` now revokes any
  prior live key for that `agent_id` before inserting the new one, atomically via
  `conn.transaction()` — registration always succeeds, the most recently registered key is always
  live. Trade-off (accepted, not solved here): an earlier session's key silently stops working with
  no notification — see `docs/BACKLOG.md` § "scope agent_id per session/project" for the deeper fix.
- **Found via:** a live incident from a concurrent project-d session (ARB Memory
  `art-944f558d412df42b`), surfaced while reviewing ARB Memory's most recent entries.
- **Verified:** `tests/arb_messages/test_keys.py` updated (the old test asserted the broken
  behavior as correct — replaced with rotation + revoked-not-deleted + atomicity assertions); full
  `arb_messages` suite 83/83 passing against a real Postgres.

### fix(bridge): live_redis stale-alias bug — CI's first real regression catch (2026-07-05)
- **What:** `Bridge.live_redis` was assigned once at `__init__` time (`self.live_redis = self.redis`
  when no `ARB_LIVE_REDIS_URL` is configured) — a snapshot, not a live reference. Any later
  reassignment of `self.redis` (exactly what test fixtures do: `bridge.redis = fake`) left
  `live_redis` pointed at the stale original client. `live_redis` is now a property with a
  private backing field, dynamically falling back to `self.redis` at read time whenever no
  dedicated live client was configured.
- **Why this surfaced now:** CI run 28737782361 failed
  `test_empty_structured_reply_attaches_null_without_warning` — `assertNoLogs` caught a real
  `ConnectionRefusedError` from the stale client trying to reach a port nothing was listening
  on. Locally this was invisible: a real dev-fleet `redis-server` happens to listen on the same
  test-fixture port (6390), silently absorbing every stray write. This is very likely also why
  that CI run took 17 minutes instead of the normal ~5 — plausibly dozens of other tests hit the
  same doomed connection attempt without an assertion watching for it; runtime returned to ~5
  minutes locally after the fix.
- **Also:** `tests/test_bridge_handle_raw.py::test_request_fans_out_events_status_result_and_reply`
  used `fields["type"]` unconditionally over `fake.events`, which now correctly also receives
  live-tee entries (`event_type` key, not `type`) now that `live_redis` resolves to the same
  fake — switched to `.get("type")`.
- **Verified:** direct exercise of `_tee_live_event` confirms it now reaches the fake; the fix
  holds even with the configured Redis port pointed at nothing (reproducing the exact CI
  condition); full non-e2e suite 1,730 passed / 0 failed in 5:04 (vs the prior CI run's 17:07
  before failing).

### refactor(logging): daemon print()→logging conversion; ACP refusal fix; kimi/mini engine tests (2026-07-05)
- **What:** `src/agent_redis_bridge/bridge.py`'s 79 daemon-diagnostic `print(..., flush=True)`
  calls converted to leveled `logger.{error,warning,info}` by prefix
  (`[bridge-error]`→error, `[bridge-warning]`/`WARNING`→warning, rest→info), plus the same
  conversion for `engines/{agent_sdk,pi_rpc,grok_acp,pi_sdk}.py` daemon prints. Bracket-prefix
  message text is byte-identical — only the emission mechanism changed. `main()` now installs
  `_configure_daemon_logging()` (message-only stdout handler at INFO, no-op if an embedder
  already configured handlers) — previously the daemon had NO logging config at all, so the
  27 pre-existing module loggers silently dropped INFO under Python's last-resort handler.
  CLI-facing prints (ctl.py, ACP `__main__` demo blocks, arb_* CLIs) deliberately untouched.
- **Also:** `"refusal"` added to gemini_acp.py's and grok_acp.py's ACP failure sets (cursor_acp
  already had it from the 2026-07-01 hardening pass) — a refused turn is now `ok=False`
  instead of a success whose result is refusal prose. kimi-code and mini-agent inherit the fix
  via `GeminiAcpEngine`. New `tests/test_kimi_code_acp.py` + `test_mini_agent_acp.py` (the two
  previously-untested engines): command shape, start handshake, each engine's real
  `set_session_mode_for_policy` delta, and refusal — inject-revert verified.
- **Panel-reviewed** (`panel-daemon-logging-acp-tests-20260705T100245Z-f0e9dc`, audited
  seq 1-6): certify quorum cold-Opus (approve) + agy-print (needs-changes, P2) + pi-GLM
  (needs-changes, P2); codex non-certifying contributor (authored the test-migration commit).
  All findings P2 — the substantive catch (codex+agy independently) was that the initial
  AST-conversion script's hardcoded line numbers silently no-op'd on two `grok_acp.py` prints
  once an earlier edit shifted line numbers; remediated along with orphan `import sys` and
  cosmetic whitespace. Reports: `docs/superpowers/reviews/2026-07-05-daemon-logging-review-*`.
- **Verified:** full non-e2e suite green (1,730 passed / 0 failed) against a real
  pgvector-Postgres + Redis backend at the merged SHA.

### feat(ci): GitHub Actions workflow + dev dependency group — the suite now runs somewhere other than session memory (2026-07-04)
- **What:** `.github/workflows/ci.yml` runs the full non-e2e suite (~1,718 tests) on every
  push to `dev`/`main` and every PR, against real pgvector-Postgres + Redis service
  containers; `pyproject.toml` gains a PEP 735 `dev` group (pytest, anyio) and
  `testpaths = ["tests"]` so a fresh `uv sync --all-extras` checkout collects cleanly
  (previously pytest/anyio were undeclared anywhere — a clean clone got 95 collection errors).
- **Why now:** the repo had ~1,700 tests and zero automation; verification lived entirely in
  session discipline. Setting this up immediately caught a real regression nobody had seen:
  `test_doc_index_is_complete_and_generated_index_is_fresh` had been failing since 06-30.
- **Test-infra fixes required to run the suite in ONE invocation (never possible before):**
  missing `__init__.py` in `tests/{arb_email,arb_files,arb_secrets,claude_tail}` caused
  basename collisions (`test_config.py`, `test_protocol.py`, …) that errored collection;
  `tests/arb_crypto` deliberately stays package-less (its real code lives in the src package's
  own `__init__.py`, so the `__path__`-graft convention would shadow it).
- **Linux-runner remediation (first live runs, 43 reds → taxonomy → fixes):** `fetch-depth: 0`
  (defect_hunts replays historical SHAs via `git show`; the protocol gate resolves
  `target_branch=main` — both break on depth-1 single-ref clones); engine stubbed via the
  existing `RecordingEngine`/`build_engine`-patch convention in
  `test_{reliable_inbox,bridge_notify_inbox,bridge_handle_raw}` (the pool spawns the engine
  binary before `run_engine`'s dry-run short-circuit, so these unit tests silently required a
  real `codex` on PATH); `skipif(no sandbox-exec)` on the 8 containment-dependent diagnose/gate
  tests (`skills/diagnose/containment.py` is macOS `sandbox-exec` by design, fail-closed
  elsewhere); dropped the workflow's global `ARB_MEMORY_REDIS_URL` export (the bridge reads the
  same var as its AUDIT bus URL — every test bridge grew a real audit client, breaking
  `from_url` call-count asserts and the transcript hotpath latency budget). Round 2: a local
  `main` ref is created post-checkout (the protocol gate's `merge-base main HEAD` fail-closes
  with `setup-error` when only `origin/main` exists — reproduced in a fresh clone), and the
  hotpath 50ms budget relaxes to 5s under `GITHUB_ACTIONS` only (shared-runner scheduler noise;
  the guarded regression — a wedged consumer blocking the hotpath — hangs forever, so a loose
  bound still catches it).
- **Environment assumptions made explicit (encoded in the workflow, verified clean-room):**
  the DB superuser must be named `arb_memory` or the three MCP-role deny-proof tests silently
  run as superuser and fail (the tests derive the MCP DSN by rewriting the username);
  `arbmem_mcp` needs LOGIN+password pre-created; `schema.sql` must be applied to `public`
  because the visibility app's own connections resolve unqualified tables there (this was the
  `test_revoked_visibility_token_closes_seat_sse_stream…` failure — not a product bug).

### feat(visibility): history-only date filter, moved server-side (2026-07-04)
- **What:** the History filter bar gained a Date picker (hidden in Live mode — live seats are a
  stream, not dated rows). First cut (`675451b`) matched client-side against a UTC-day string
  computed per row; the follow-up (`06ea549`) moved the bound server-side — `/seats`/`/history`
  now take UTC-day bounds directly, cursors carry `since` (with legacy-cursor compatibility), and
  the SPA reloads history with `date=` on every filter change instead of filtering an
  already-fetched page.
- **Cleanup (`191eb27`):** once the filter went server-side, the client-side `utcDateOf`
  date-matching helper (function, export, and its unit test) was dead code — removed.

### fix(visibility): auth-failure routing + orchestrator-dropdown gaps found after the login-gate deploy (2026-07-04)
- **`fc9dd98`:** a 401/403 from the SPA now redirects to the login gate, with loop protection, and
  replaces the stale "token" banner wording — found live once the passphrase+TOTP gate below was
  actually deployed and real sessions started expiring/rejecting.
- **`2793a9b`:** `loadOrchestrators()` is now called unconditionally on init, so a
  session-authenticated browser (cookie, no bearer token) still gets the header's orchestrator
  dropdown populated — it previously only ran when the hidden token field was non-empty.

### feat(visibility): passphrase+TOTP login gate for arb-visibility-web (2026-07-04)
- **What (`d3e0577`):** browser access to arb-visibility now reuses the MCP door's login form,
  session cookie, TOTP verifier, and login-throttling counters, with bearer-token auth preserved as
  a fallback — one credential, one throttle bucket, shared by the door and the visibility UI.
- **Hardening (`6f9a722`):** sessions rotate their id on verify, a verified session gets its own
  visibility-scoped TTL extension, the global lockout state is isolated from the door's, and
  expiry/rotation/isolation/session-SSE behavior is now test-covered.
- **Grants (`fb649ed`):** the visibility DB role gets DML (not just SELECT) on `login_sessions` and
  `login_attempts` only; `oauth_clients`/`refresh_tokens` stay denied, other read surfaces stay
  SELECT-only.
- **Deploy (`4131f41`):** `ARB_MEMORY_MCP_LOGIN_SECRET`/`ARB_MEMORY_MCP_TOTP_SECRET` (already in
  prod `.env` for the MCP door) are now passed through to the visibility service so the gate
  verifies against the same credential as arb-memory; if either is unset the gate stays off
  (bearer-token only).
- **UI (`366e81b`):** the header's token input is hidden (kept for `app.js`'s `#token` reference)
  and the orchestrator label moved inline, now that login is the primary auth path.
- Memory: `arb-visibility-two-layer-auth` (hostname-wide CF Access in front of this
  passphrase+TOTP gate; grant delta is `login_sessions`+`login_attempts` only).

### feat/fix(arb-visibility-web): orchestrator panel — toggle, live-bug fixes, tab isolation, anchoring, last-seen (2026-07-04)
- **What shipped:** a "Seats" / "Orchestrators" segmented toggle in the left pane (`3e91048`),
  reusing the header dropdown's already-fetched orchestrator IDs (no new fetch) rather than
  mirroring arb-go's approach of sorting the orchestrator into the bottom of the seat list —
  investigation showed this deployment's orchestrator never emits a `seat_appear` event (confirmed
  via the live SSE stream: zero occurrences despite real worker seats appearing), so the Go
  mechanism wouldn't have worked here. Also added a copy-transcript button in the same commit.
- **Live-found bugs (Playwright, invisible to the DOM-harness pytest suite):** `5f7c2fe` fixed
  `renderOrchestratorRows()` calling `.map()` directly on a real `HTMLCollection` (works only
  against the test harness's fake array-like element) and a stale header `<select>` value after a
  left-pane click; `cc47efd` disabled Cloudflare edge caching on `index.html`/`app.js` (the default
  4h max-age had made the toggle/copy-button deploy invisible on the live site for hours after a
  successful origin deploy — a single-operator internal tool doesn't need CDN freshness
  trade-offs); `a25f186` fixed `openOrchestrator()` never resetting `seatPanelMode` back to
  `"seats"`, so opening an orchestrator from the new list left the panel stuck on the orchestrator
  list; `ce541cd` then corrected `a25f186`'s fix after further live testing — an ID-selector CSS
  rule (`#filter-bar{display:flex}`) beat the native `[hidden]` UA rule so the filter bar never
  actually hid in Orchestrators mode (same bug class as a pre-existing `#auth-banner[hidden]`
  override), and forcing the mode back to `"seats"` on every orchestrator click broke viewing the
  orchestrator's own tool-call telemetry. Review brief for these four:
  `docs/superpowers/reviews/2026-07-04-arb-visibility-web-live-fixes-brief.md` (`88bfcd9`).
- **Design churn on where the orchestrator lives:** `8d1ec61` partitioned orchestrator
  self-telemetry behind a trailing divider within the Seats tab (mirroring arb-go); `f045319`
  reverted that — this UI has a dedicated Orchestrators tab, so self-telemetry
  (`agentOf(seat_id) === "claude"`) was excluded from the Seats tab entirely instead (row list,
  filtered/total count, and the Agent-filter options, which previously offered a useless "claude"
  option that filtered to nothing); `b887f6e` then removed the toggle altogether in favor of a
  single Go-parity list with warm orchestrators behind a trailing divider; `4e45d8a` split that
  live orchestrator group into a pinned anchor below the worker scroller and repointed history
  pagination at the worker list only — the shape that stuck. `c411e38` added a `[hidden]` display
  override for the seat panel along the way.
- **Data freshness:** `be2618c` — `/orchestrators` now reads from a Redis last-seen hash
  maintained since visibility startup, falling back to the old tail scan only when the hash is
  empty.
- **claude-tail hardening:** `63e562f` tees orchestrator state to `eval:events` for live
  seat-state branches (preserving run-id gating, isolating `xadd` failures); `fd1dfdb` skips warm
  registry records whose transcript file is gone, prunes stale Redis hash entries best-effort, and
  finishes a vanished tailer cleanly instead of a traceback.
- Panel-reviewed per commit message references (codex P1 + agy P2 findings against `ce541cd`
  fully resolved by `f045319`'s exclusion approach).

### fix(arb-watch): stable seat-pane height on scroll + dd/mm history dates (2026-07-03)
- **Filter bar jumped on scroll (reported):** `seatViewportRows()` returned `vp.Height-2` and
  forgot to subtract the filter bar / header / hint lines (its comment claimed it did), so a full
  history list overflowed the pane and the `↑/↓ more` affordances toggling on scroll made the
  layout oscillate. Fixed: correct row budget, a FIXED-line-count `renderSeatTable` (reserved
  hint + status lines, blank-padded rows), and a `normalizeHeight` clamp in `View`.
- **History dates:** a history seat older than 24h now shows its `dd/mm` date instead of a useless
  `768h`; live seats keep the relative age. Age column widened 4→5.
- **Bonus alignment bug:** `incomplete` (10 chars) overflowed the width-9 State column and shifted
  the Age column on those rows — widened State to 10. Age is now padded ANSI-aware (`padState`) so
  styling can't skew Run.
- Tests: constant-height-across-scroll + dd/mm-after-a-day regressions; full suite green.

### deploy(prod): systemd boot-autostart unit for the docker-compose stack (2026-07-03)
- **What:** `deploy/systemd/arb-memory-stack.service` (+ install README) — a oneshot/RemainAfterExit
  unit running as `claude` that does `docker compose up -d` on boot and `stop` on teardown. Installed +
  enabled on prod (`arb-prod`).
- **Why:** the stack's containers already carry `restart: unless-stopped`, but nothing reconciled a
  *left-stopped* container or gave a single stack lifecycle handle. The unit makes systemd the boot
  authority on top of Docker's per-container policy, closing the prod reboot-survival gap.
- **Gotcha documented:** all 8 services share the `arb-memory:phase3` tag, so a bare `docker compose
  up -d` after a `build` recreates *every* service on the old image — name the service
  (`up -d visibility`) to limit blast radius. Bit this during the arb-watch-history prod deploy (rolled
  the whole stack onto 9c24ccc; benign, all merged code).

### chore(engines): deprecate gemini-acp — Google killed the gemini CLI (2026-07-03)
- **Why:** Google deprecated the `gemini` CLI and it stopped working, so the `gemini-acp` engine
  (which drives `gemini --acp` as a subprocess) can no longer run a real turn. The project-g-consult
  gemini seat was already in a crash state before it was `launchctl disable`d + booted out.
- **What:** removed `gemini-acp` from the launcher's known-engines list
  (`scripts/agent-redis-bridge-systemd`); `agent-dispatch` and `agent-bridge-ping` now reject
  `--engine gemini-acp` with a deprecation error at the operator entry point; added a DEPRECATED
  banner to `src/agent_redis_bridge/engines/gemini_acp.py` (module retained ONLY for its
  ACP-protocol unit tests — no runtime `build_engine` raise, so the existing gemini-acp bridge
  wrapper tests still pass); updated README + the `using-agent-bridge` skill. Dated historical
  design docs/plans/reviews left intact (point-in-time record). Memory: `gemini-cli-deprecated`.

### fix(arb-messages): messages_request no longer polls inline — enqueue and return (2026-07-02)
- **Why:** fulfillment is human-attended via Codex App -- minutes, not seconds -- so the 15s
  inline window could never win and only delayed the response while holding a DB connection.
- **What:** `messages_request` now does one immediate status check after enqueue so a retried
  `request_id` still recovers an already-fulfilled result; result retrieval is via
  `messages_poll`, and the e2e test now follows that poll-based flow.

### fix(arb-messages): messages_request exhaustion reports claimed vs pending (2026-07-02)
- **What:** `messages_request` now passes through the last observed non-terminal status instead
  of hardcoding `pending`, and removes the trailing no-op sleep after the final poll check.
- **Why:** requesters could not distinguish an untouched request from one an operator was actively
  fulfilling; this was a 2026-07-02 review-panel finding in the same ARB Messages series.

### fix(arb-messages): idempotent re-delivery — already_delivered returns the sealed body (2026-07-02)
- **Loss window:** `delivered_at` committed before the response reached the client, so any
  post-commit transport failure permanently stranded the payload; the 2026-07-02 incident was
  one instance of that class.
- **Safety:** re-delivery is scoped by `(agent_id, request_id)` at SELECT time, and the stored
  body is NaCl SealedBox ciphertext sealed to that agent's registered key.
- **Change:** `already_delivered` now carries `body_b64` through the door; `delivered_at` still
  records first delivery only.

### fix(arb-messages): sealed delivery is now body_b64 — JSON-safe, fail-loud (2026-07-02)
- **Incident:** ARB Messages returned raw NaCl SealedBox ciphertext bytes through an MCP JSON-RPC
  tool result. JSON serialization crashed after `read_and_mark_delivered()` had already committed
  `delivered_at`, so the first real prod credential became unrecoverable through the API and had
  to be recovered manually from the DB.
- **Initial fix:** `1cbb7cb` base64-encoded sealed bodies at the door boundary so delivered MCP
  tool results became JSON-safe.
- **Follow-up:** renamed the door-level field to `body_b64` before any client ships against the
  messages API, made encoding unconditional and fail-loud for non-bytes-like bodies, updated the
  E2E pipeline test to decode before unsealing, and documented the decode step in the tool
  descriptions remote agents see.

### chore(agent-sdk): sonnet implementor subscription model upgraded to Sonnet 5 (2026-07-01)
- `agent_sdk_models.py`'s `MODELS["sonnet-4.6"]` (slug `sonnet46`, `claude-sonnet-4-6`) renamed to
  `MODELS["sonnet-5"]` (slug `sonnet5`, `claude-sonnet-5`) — Sonnet 5 released yesterday.
  `subscription=True` unchanged; only the model key/slug/`model_id` moved. No seat is currently
  running this model (dev-only, not deployed — see `agent-sdk-subscription-seat` memory), so no
  daemon kickstart was needed; the next dispatch/launch using `--model sonnet-5` (or a target-id
  like `asdk-bridge-dev-sonnet5`) picks it up.
- Updated `tests/test_agent_sdk_models.py` and `tests/test_agent_sdk_subscription.py`'s
  `sonnet-4.6` references to `sonnet-5` throughout. 85/85 agent-sdk tests green.

### fix(agent-dispatch): require --run-id or --adhoc on every dispatch (2026-07-01)
- **Found live:** a full design → panel → spec-panel → plan-panel → codex-TDD → tri-model pipeline
  (the cold-Opus subagent visibility feature above) was dispatched entirely without `--run-id` —
  confirmed by grepping the orchestrating session's own transcript for every `agent-dispatch`
  invocation. `bridge.py`'s intentional `run_id = envelope.run_id or task_id` fallback
  (`dea2ec2`/`b28a5f4`, "un-tagged seats are first-class in the roster") silently degraded every
  seat's arb-watch Run column to a raw task-id GUID instead of a readable label, and dropped all
  audit/vote evidence for the whole pipeline since `--audit-panel` requires `--run-id` to do
  anything.
- **Fix:** `scripts/agent-dispatch` AND `tools/go-client`'s `dispatch` subcommand now both
  hard-refuse any dispatch that has neither `--run-id` nor a new `--adhoc` flag (an explicit,
  conscious "this one-off needs no label"); `--dry-run-envelope` is exempt on both (it never
  touches the bus), and `--check` is additionally exempt on the bash tool (the Go client has no
  `--check` flag at all — caught by a cold-Opus live-test review, P2). Mirrors the existing
  `--audit-panel`-requires-`--run-id` precedent (already present in both implementations). Also
  fixed `scripts/dispatch-dev`'s flag-translation loop, which would have mishandled a
  passed-through `--adhoc` as a two-arg flag and swallowed the task text as its value.
- **Also discovered:** `scripts/dispatch-dev` (the preferred wrapper, defaults to the Go client
  edge) already auto-defaults a meaningful `--run-id` from the task's brief filename or
  `<target>-<branch>-<HHMMSS>` — a more complete fix than the hard-refuse gate, and it predates
  this change. The regression happened because the pipeline dispatched via raw
  `scripts/agent-dispatch` directly instead of `scripts/dispatch-dev`.
- Updated every internal caller that previously omitted both: `scripts/arb-memory-seat-e2e` and
  `tools/eval/arb_eval/pipeline.py`'s `BridgeDispatcher` now thread their own pre-existing per-run
  identifiers (`run_tag` / `scenario.id-<hex>`) through as `--run-id` instead of minting a second,
  disconnected id; `skills/diagnose/panel.py` passes `--adhoc` (diagnose keeps its own
  `dispatch_log.jsonl` audit trail, so each seat call is a genuine one-off from agent-dispatch's
  perspective); `tests/test_grok_acp_e2e.py` / `tests/test_pi_rpc_e2e.py` (opt-in live e2e) pass
  `--adhoc`. The canonical recipe in `docs/fragments/dispatch-recipe.md` (mirrored into
  `README.md`/`SKILL.md`, drift-checked) now shows `--run-id "$RID"` by default.
- **Verified:** full `tests/test_agent_dispatch*.py`, `tests/test_diagnose*.py`,
  `tests/arb_memory/test_seat_e2e_script.py`, and `tools/eval/tests/test_pipeline.py` green;
  `scripts/check-doc-drift` clean. See memory `dispatch-run-id-discipline`.

### feat(claude-tail): cold-Opus subagent visibility in arb-watch (2026-06-30)
- Cold-Opus reviewer subagents (Claude Code's native Agent/Task tool, e.g.
  `code-reviewer-report-writer`) never appeared as a seat in arb-watch — `claude_tail`'s cold-seat
  discovery globbed the wrong directory (the harness's actual subagent-output location had drifted
  since the prior 2026-06-28 spec).
- **Fix:** two new hooks, `SubagentStart`/`SubagentStop` (designed to be wired in
  `.claude/settings.local.json`, host-local — that wiring is operator/live-verify work, not part of
  this code change), register/deregister each allowlisted subagent
  (`ARB_CLAUDE_TAIL_COLD_AGENT_TYPES`, default `code-reviewer-report-writer`) by symlinking its real
  transcript — derived from the already-known parent-session registry entry's `transcript_path`,
  not a reimplemented path-slugging algorithm — into the directory the daemon's existing `.output`
  glob already watches. Zero daemon-*discovery* change needed; real-time pickup within one poll
  tick (~1s).
- Two small, targeted daemon changes carry the `orchestrator` field through correctly
  (`identity_locked` guard on `TranscriptTailer`) and finish+clean up promptly on completion
  instead of the 5-minute idle-finish fallback (`service.py`'s `tick()` checks each live cold key's
  sidecar after polling it).
- Panel-reviewed four times (codex + agy-print + cold-Opus) across design → plan → implementation:
  round 1 found two P1 design bugs (an identity-overwrite-wrong-layer bug, a same-`seat_id` dedup
  collision in the Go frontend); round 2 (re-reviewing the round-1 fixes) found two more — a
  circular/unreachable completion-check placement, and an identity-guard regression of an existing
  marker-upgrade code path; round 3 (reviewing the derived implementation plan) found a
  symlink-before-sidecar write-order race; round 4 (reviewing the implemented diff) found a P0 that
  rounds 1-3 all missed — the subagent transcript path formula dropped the session-id directory
  entirely, producing a silently-dangling symlink that made the whole feature do nothing in
  production, caught only because cold-Opus re-derived the real path from raw on-disk evidence
  instead of trusting the spec's own (wrong) prose; the unit test "proving" the formula was itself
  tautological. All five fixed, each independently re-verified against the real code/disk rather
  than trusted from reviewer prose. See
  `docs/superpowers/specs/2026-06-30-cold-opus-subagent-visibility-design.md`.
- **Verified:** TDD red→green throughout, including a red→green cycle specifically proving the
  round-4 path fix (the rebuilt non-tautological test fails against the old formula, passes against
  the new one) and a cross-check of the corrected formula against this session's own real subagent
  transcript files on disk. Full `tests/claude_tail` suite green (105 tests). Full repo suite
  collection is currently blocked by pre-existing duplicate test module basenames under
  `tests/arb_email` and `tests/arb_files` (unrelated to this change).

### fix(claude-tail): evict stale same-seat registry entries on SessionStart (2026-06-30)
- **Found live:** a `/clear` + resume cycle left an old resumed Claude Code process (`--resume
  <jsonl> --reply-on-resume`) running idle in the background, still registered in the claude-tail
  registry under the same seat (`claude-bridge-dev`) as the new session. Both fed `transcript_io` in
  prod PG concurrently, surfacing as duplicate "live orchestrators" in observability — confirmed via
  `claude:registry` HGETALL (2 live entries) and PG (`run_id=77d7a0f0-...` logged exactly 1 event then
  went idle while the new session kept growing). Resolved manually this time with `kill <pid>`.
- **Fix (Mark's call — registry-only, no process kill):** `SessionStart` now evicts any other live
  registry record sharing the new session's `seat_id` before registering itself — a warm orchestrator
  seat is one logical thread at a time, so a fresh start supersedes a stale prior record. The other
  option considered (also `kill`ing the stale process's pid) was rejected as too invasive: a future
  session legitimately running two windows on the same project would get silently killed. The orphaned
  OS process, if any, is left alone — just no longer tailed/double-counted.
- Logs one line to stderr per eviction (`[claude-tail] evicted stale registry entry <id> for seat
  <seat_id>`) for visibility.
- **Verified:** TDD red→green, `tests/claude_tail/test_hooks.py` (3 new tests: file-registry eviction,
  redis-registry eviction, different-seat records left untouched); full `tests/claude_tail` suite
  82 pass. No daemon restart needed — `session_start.py` runs per-session via the SessionStart hook.

### fix(doc-index): collection-exempt superpowers plans/specs (2026-06-30)
- `check-doc-index` was red on 6 pre-existing `docs/superpowers/plans` and `docs/superpowers/specs` files
  (arb-files, arb-email, agent-sdk-subscription specs/plans) that predate this session and were never given
  individual `docs/index.json` entries.
- Mark's call (surfaced, not picked silently): collection-exempt both directories rather than author 6
  individual entries, mirroring the existing `docs/superpowers/reviews` exemption — these are dated working
  docs, not curated reference material.
- **Fix:** added `docs/superpowers/plans` and `docs/superpowers/specs` to `COLLECTION_EXEMPTIONS` in
  `scripts/doc_index_lib.py`, plus a collection-stub entry for each in `docs/index.json` (status `archive`,
  audience `any`), matching the `reviews` pattern. Regenerated `docs/INDEX.md`.
- **Verified:** `scripts/check-doc-index` exits 0; `tests/test_doc_index.py` (6 tests, includes the
  end-to-end `check-doc-index` run) passes.

### fix(agent-sdk): startup smoke-test is read-only — no base-checkout litter (2026-06-30)
- **Follow-up to the cwd-anchor fix:** the live startup smoke-test asked the model to "write
  `ARB_AGENT_SDK_DENY_PROBE.txt`, which must be denied". That premise only holds for a read-only seat; a
  Write-capable implementor's gate correctly *allows* the write, so the probe dropped a file in the pooled
  engine's cwd — the base checkout — on every boot (made reliable once the seat could find its cwd). Not a
  containment break (Write is legitimately in-ceiling), just litter, and the deterministic deny-proof
  (`_run_startup_gate_checks`) already covers denial.
- **Fix:** the probe (`_startup_probe_prompt()`) now drives a harmless read-only directory listing — it still
  round-trips the live model through the gate without touching the filesystem.

### fix(doc-index): exclude symlinked markdown from the index requirement (2026-06-30)
- A symlinked markdown alias (`GEMINI.md -> AGENTS.md`, added in the AGENTS.md migration) was flagged as
  "tracked markdown missing from docs/index.json" — but an alias is not an independent document.
  `tracked_markdown_paths()` now skips symlinks, so the alias no longer demands its own entry.
- This cleared `GEMINI.md` only; the remaining 6 pre-existing `docs/superpowers/` specs/plans were closed
  separately — see "fix(doc-index): collection-exempt superpowers plans/specs" above.

### fix(agent-sdk): subscription implementor no longer crashes on cross-dispatch resume (2026-06-30)
- **Bug:** a subscription *implementor* seat (stateful, not `--agent-sdk-oneshot`) crashed on every dispatch
  after its first with `claude_agent_sdk._errors.ProcessError: exit code 1`. The real CLI stderr (surfaced via
  `_handle_stderr`) was `No conversation found with session ID: <id>` — `--resume` couldn't *find* the session.
- **Root cause (3 compounding, any one fatal):** (a) the per-seat `CLAUDE_CONFIG_DIR` is randomised per process
  (`config_dir` uses `uuid4`), so a restart orphans all prior conversations; (b) the claude CLI scopes `--resume`
  by cwd/project, but each trusted implementor dispatch runs in a *fresh per-dispatch worktree* (new cwd); (c)
  worktrees are recreated from HEAD, so resuming would restore conversation memory into an empty tree anyway.
  The cold reviewer was immune (already `resume=None`). Asymmetry with codex: codex carries its own OS sandbox
  so it's safe in a stable shared cwd (→ in-process thread = cross-dispatch memory); agent-sdk has no sandbox,
  so the bridge isolates it with a per-dispatch worktree, and that moving cwd is what breaks resume.
- **Fix:** subscription seats connect with `resume=None` always (mirrors the cold reviewer). The session id is
  still loaded/persisted for observability; the within-dispatch completion loop is unaffected (it reuses the
  live client and never reconnects, so it never needs resume). Git is the cross-dispatch memory, as for every
  other bridge worker.
- **Verified:** unit test red→green (`test_implementor_subscription_seat_does_not_autoresume_persisted_session`),
  full agent-sdk suite 69 pass; live — haiku seat restarted on the fix, 3 cross-dispatch dispatches, zero
  `No conversation`/`ProcessError`, file written crash-free on a fresh worktree with the stale id still on disk.

### fix(agent-sdk): announce the cwd so a no-shell seat can find where to write (2026-06-30)
- **Bug (found during the resume-fix live verify):** a no-Bash implementor wrote its file *outside* the
  worktree — it had no way to learn its working directory. A raw-string `system_prompt` REPLACES Claude
  Code's default prompt, which is what normally injects the working-directory line; with `Write`/`Edit`
  needing absolute paths and no shell for `pwd`, the seat was blind and guessed (`/`, `/root`, `/tmp`, …).
- **Fix:** the engine now composes `system_prompt` as the role profile + an explicit cwd anchor
  (`_system_prompt()`), so every agent-sdk seat is told its working directory. This is information, not
  privilege — the deliberate no-Bash containment (why the bridge commits on the seat's behalf) is unchanged.
- **Verified:** unit test (`test_subscription_system_prompt_announces_cwd`); live — haiku then wrote
  `HAIKU_E2E.md` *inside* the worktree and the orchestrator committed it (`committed_clean`, `af3aa14`).

### fix(bridge): completion gate fails loud on a missing-artifact no-op (no more vacuous green) (2026-06-30)
- **Bug (found during the same live verify):** a worktree dispatch carrying an `expected_artifacts` contract
  returned `ok=true` when the artifact was **never produced** (state `no_changes_clean`) — e.g. a worker that
  wrote outside its cwd or did nothing and claimed "done". The contract only *drove* the re-prompt loop;
  `orchestrator_commit` deferred a missing artifact "to the gate/loop", and `apply_completion_gate` only
  bounced *dirty* trees — so an unmet contract on a clean tree passed green.
- **Fix:** `apply_completion_gate` now also bounces (`ok=False`, worktree preserved) when expected artifacts
  are missing on an otherwise-clean tree — the no-op masquerade fails loud instead of reporting success.
- **Verified:** unit test red→green (`test_clean_tree_with_missing_artifact_bounces_not_vacuous_green`),
  full completion-loop + agent-sdk suites green (106 pass together).

### docs: harness-agnostic AGENTS.md migration + candid deprecation (2026-06-30)
- **Migration (panel-approved 4/4 SHIP):** moved the harness-agnostic operational orientation
  (doc-routing map, dispatch-vs-seat-standup recipes, memory-access + `agent_id` facts) out of the
  Claude-Code-self-selecting `CLAUDE.md` into `AGENTS.md`'s locally-authored Bridge-repo layer, so a cold
  non-Claude-Code orchestrator (codex/pi/glm/agy) reaches it without the `~/.claude/skills` install.
  Added `GEMINI.md → AGENTS.md` symlink (codex reads `AGENTS.md` natively). `CLAUDE.md` slimmed to its
  Claude-Code layer (skill-install + orchestrator-role); role layer intact. Panel nits folded:
  `agent_id` shown as `{tool}-{project}-{workspace}[-{role}]`, honest per-harness coverage, no-drift
  seat-standup pointer, seat-scoped memory bullet. check-doc-drift clean (fragments untouched).
- **candid deprecated in place:** `tools/candid` existed to dodge "the multi-minute floor the bridge path
  imposes" — but that floor was a bug (per-token Redis writes throttling codex over managed-bus TLS RTT),
  fixed in `9da7761` the same afternoon candid was added (`ada513f`, 6 min later). The bridge prose path is
  now fast. DEPRECATED headers added to `tools/candid` + README § candid; note removed from `CLAUDE.md`.
  File kept (not deleted) so existing `~/bin/candid` symlinks survive.


### agent-sdk: tool OUTPUT actually reaches the transcript (real root cause) (2026-06-30)
- The prior tool-output change put content on the AssistantMessage `ToolResultBlock` branch — which is
  DEAD for built-in tools: the SDK returns tool results in a **UserMessage**, a type the turn loop never
  handled. So no output ever reached the transcript. Now the loop handles `UserMessage` and emits the
  result via `_emit_tool_result`.
- The result is emitted as `command_output` under a **distinct `:output` item_id** (codex's pattern). The
  visibility gateway merges frames by item_id, so reusing the `command_started` id (tool_use_id) collapsed
  the output INTO the started frame (rendered without content). The separate id keeps it its own
  command_output frame → arb-watch renders the `⎿` line. Verified live via the gateway SSE.


### agent-sdk: tool-call output in the transcript (2026-06-30)
- `command_finished` now carries the tool RESULT content (`_tool_result_text` flattens str|list result
  blocks, capped at 16k), so arb-watch renders the `⎿ <output>` line under each `⏺ Read(...)` (operator
  decision: full output, parity with codex). Transcript-only: `content` is NOT in the eval allowlist, so
  raw tool output never reaches the eval tee — the eval contract holds. Verified in prod arbmem:trace.


### agent-sdk: panel fixes (eval-leak, asdk normalize, tool-label cap) (2026-06-30)
- **Eval-tee leak (P1):** the tool-call label was set on `tool_name`, which the eval tee allowlists
  (eval is contracted to exclude raw args) — a Bash command with an inline token would surface in eval.
  The label now lives in `command` ONLY (eval excludes it); the arb-watch transcript still renders it via
  transcript_flusher's `tool_name or command` fallback.
- **`asdk` normalize centralized (P1/robustness):** `normalize_engine_name` is now called in
  `Bridge.__init__` and `build_engine`, not just `main()`, so a non-CLI caller can't reach an
  `engine_name == "agent-sdk"` gate with an un-normalized `asdk`. Added an alias test.
- **Tool-label cap (P2):** every `_tool_command_label` branch now caps at 120 chars (the named-key branch
  was uncapped — transcript bloat + max secret exposure for long Bash/URL args).
- Fixes a red `test_engine_progress_schema.py` (it expected the old `kind="Read"`). 101 passed.


### agent-sdk: tool calls render with detail in arb-watch (2026-06-30)
- agent-sdk `command_started` events set `kind` to the *tool name* (e.g. `Read`) instead of
  `"command_started"`, so arb-watch's transcript renderer (which branches on `kind == "command_started"`)
  fell through to a bare `⏺` bullet. They also dropped `ToolUseBlock.input`, so even when shown there was
  no file/arg detail.
- Now emits `kind: "command_started"` (matching codex) and a readable label folding the salient arg
  (`Read(src/foo.py)`, `Grep(pattern)`, …) into `command`/`tool_name` via `_tool_command_label`. Tool calls
  now render `⏺ Read(src/foo.py)` in arb-watch. Live-verified in prod events:live.

### agent-sdk: short engine/seat name "asdk" (2026-06-30)
- agent-sdk seats now register under the short tool/seat name **`asdk`** (e.g. `asdk-bridge-opus-opus48`
  instead of the verbose `agent-sdk-bridge-opus-opus48`) via `ENGINE_TO_TOOL["agent-sdk"] = "asdk"`.
- **`asdk` is also accepted as a `--engine` alias**, normalized to `agent-sdk` after parse so all internal
  `engine_name`/`build_engine` logic is unchanged. `--engine agent-sdk` still works and also yields the
  short seat name. Flag/env names (`--agent-sdk-oneshot`, `BRIDGE_AGENT_SDK_TOOLS`) are unchanged.

### arb-watch-go: fix clipboard copy over ssh/mosh (2026-06-30)
- `copyToClipboard` tried `pbcopy`/`xclip`/`xsel`/`clip` first and only fell back to OSC-52 when no
  clipboard tool existed. On a host that always has a clipboard tool (e.g. the mac-mini) the copy landed
  on the *server's* clipboard, never the client's — so `c` / `Ctrl+C` silently failed when the TUI was
  viewed over ssh/mosh. "pbcopy succeeded" is not "I'm local."
- Now emits OSC-52 **always** (the only path that reaches the client's clipboard, local iTerm2 or remote
  alike) *and* writes the local OS clipboard best-effort (helps a local terminal with OSC-52 disabled).
  OSC-52 still goes to `/dev/tty` in one shot so it can't interleave with the bubbletea renderer.

### agent-sdk: subscription seats for Claude plan OAuth (2026-06-30)
- Adds the agent-sdk subscription lane for Claude plan OAuth seats (`opus-4.8` reviewer, `sonnet-4.6` /
  `haiku-4.5` implementors), with prefix-sweep neutralization of inherited `ANTHROPIC_*`/`AGENT_SDK_*`
  shadow keys, per-seat `CLAUDE_CONFIG_DIR`, OAuth-token scrubbing, cold reviewer resume suppression, and
  subscription audit events for post-hoc double-Opus detection.
- Subscription turn concurrency is capped in-process at **1 Opus / 2 implementors**. This deliberately uses
  `threading.BoundedSemaphore`; a Redis/cross-process cap is future hardening if seats are scaled across
  bridge processes or hosts. `SEAT_ENABLED` is checked at turn start as the operator kill switch.

### arb-watch: cap the orchestrator's transcript backfill to a recent window (2026-06-29)
- **The orchestrator is one long-lived task** (the warm session, `seat_id == orchestrator`), so its
  PG transcript backfill grew unbounded, while short-lived seats are naturally small. The gateway's
  `_backfill_transcript` now caps the **orchestrator only** to a recent window —
  `ARB_VIS_BACKFILL_HOURS` (default 3) AND `ARB_VIS_BACKFILL_LINES` (default 2000), whichever hits
  first (`ts >= now() - Nh` + `ORDER BY ts DESC LIMIT N`, re-sorted chronological). Seats keep full
  history; the live-tail is unchanged (separately maxlen-bounded), so new content still streams in
  full. Verified against real Postgres (orchestrator trimmed by time + line cap; seat uncapped).


### agy-print: surface the model's thinking in arb-watch (2026-06-28)
- **agy's reasoning now appears in the transcript.** agy's `step_type=15` model step multiplexes
  field 20 — `.1` is the answer text (already captured), `.3` is the model's **thinking**, and `.7` is
  the tool call (already surfaced via the dedicated tool steps). The granular SQLite poller only read
  `.1`, so the reasoning was dropped entirely — arb-watch showed agy's tool calls and sparse answer
  text but none of its thinking. The poller now also extracts `.3` and emits a `model_thinking` event
  (which the bridge's `handle_progress` already routes, phase "thinking"). Verified by driving a real
  agy turn: `model_thinking` now flows alongside `model_text`. Root-caused via systematic debugging —
  the decode of `.1` was never broken (42/42 on a real DB); the gap was the un-read `.3` field.

### Claude-layer visibility: warm orchestrator + cold-Opus seats in ARB Visibility (2026-06-28)
- **The warm Claude orchestrator and each cold-Opus reviewer now appear as live, bridge-symmetric
  seats in arb-watch**, closing the last black box in the orchestration loop (you could already watch
  codex/agy/pi; now the Claude layer too). A standalone `claude-tail` daemon tails the warm session
  `.jsonl` + cold-Opus `.output` transcripts, maps each line to the bridge event contract, and tees to
  `events:live` (roster + lifecycle/command boundaries) + `arbmem:trace` (full redacted transcript).
- **New package `agent_redis_bridge.claude_tail`**: `mapper` (line→events + fail-loud drift self-check),
  `lifecycle` (synthetic task_started/finished), `identity` (warm/cold seat identity; cold-Opus
  `[ARB_RUN:.. ARB_SEAT:.. ARB_ORCH:..]` marker correlation), `offset` (Redis offset store, commit-after-
  publish + rotation reset), `tailer` (routing: model_text/thinking→trace only, redaction before BOTH
  sinks, turn_index, drift threshold), `service`+`__main__` (discovery daemon: warm registry + cold dir,
  per-tailer + loop error isolation, separate live/trace clients).
- **`visibility_tee`**: standalone `live_tee`/`trace_tee` extracted from `Bridge` (shared `build_trace_fields`
  redact/cap/drop contract); the bridge's local tee now delegates to it (wire-unchanged).
- **Hooks** (`scripts/claude_tail_hooks`): SessionStart/End register/deregister the warm session into an
  atomic Redis-hash registry (fail-soft — a hook crash never blocks the Claude session).
- **arb-watch (Go)**: `agentOf` recognizes `cold-opus-*`/`claude-*`; the warm orchestrator renders in an
  isolated bottom section; the seat list scrolls/pages when it exceeds the pane.
- **Containerised** (`deploy/claude-tail/`): read-only `~/.claude` + cold-source mounts at identical host
  paths, offsets in Redis (no writable mount), separate live/trace clients with keepalive.
- Built via 12-task TDD with a decorrelated review panel (codex + cold-Opus + pi-GLM) per task; a live E2E
  caught a P0 (live_tee passed `ttl` to redis-py `xadd`, which a permissive fake had hidden) that all
  panels missed — fixed + deny-proven. Known follow-ups ticketed (agy-print runaway-turn engine fix; a
  handful of P2 nits).
- **Prod deploy (2026-06-28): the warm orchestrator transcript now renders end-to-end on
  `https://arb-visibility.example.com`.** Two daemon-side fixes closed the gap the deploy exposed:
  - **Trace tee routed to `ARB_TRACE_PREFIX`, not the events prefix.** `_emit_trace` wrote
    `{AGENT_REDIS_PREFIX}arbmem:trace` (`agent_scratch:arbmem:trace`), but the visibility gateway reads
    trace from `{ARB_TRACE_PREFIX}arbmem:trace` — empty in prod, so `arbmem:trace`. Backfill-less Claude
    seats therefore showed empty transcripts while PG-backfilled bridge seats looked fine. A distinct
    `trace_prefix` (env `ARB_TRACE_PREFIX`, default `""`) now threads `Service`→`TranscriptTailer`;
    `events:live` unchanged. Verified empirically against managed Valkey (fresh trace lands in
    `arbmem:trace`) and on prod (live `event: transcript` frame delivered via a controlled injected marker).
  - **`ARB_CLAUDE_TAIL_PROJECT` env override** for the seat project half, symmetric with the existing
    `ARB_CLAUDE_TAIL_WORKSPACE`. `project_workspace` inferred project from the cwd basename, so
    `/Users/<user>/<workspace>` slugged to `workspace-dev` (→ `claude-workspace-dev-*`) instead of the canonical
    `claude-bridge-dev` that the trusted-sender ACLs and sibling engine seats already name.
- **Launch surface**: `scripts/claude-tail-daemon` (loads the bridge env file via the bridge's own
  `read_env_file` parser, then runs the service loop; process env wins so a plist can pin keys). Deployed
  as a per-host launchd agent (`com.example.claude-tail.bridge-dev`); SessionStart/End hooks wired in the
  project-scoped `.claude/settings.local.json` (gitignored, machine-local).
- **Seat lifecycle made correct for long-lived/long-quiet seats** (a live seat was flipping to `done`
  with a frozen transcript when it paused). Three fixes: (1) a **warm orchestrator never idle-finishes**
  — it is bounded by its SessionEnd hook, not by a 5-min idle (a finished tailer was skipped forever, so
  a >5-min think froze it mid-session); (2) **resumable finish** — any finished seat whose file grows
  past the finish point is recreated (re-emits `task_started`, resumes from the committed offset, no
  replay), so an idle-finish is a reversible "looks done" signal, not a terminal abandon; (3) an
  **explicit completion signal** — a cold-Opus reviewer ends its output with `[ARB_SEAT_DONE]` (symmetric
  to the brief's start marker), which the tailer detects (cold seats only, assistant text only) to finish
  the seat promptly and accurately. Idle-finish (now cold-only + resumable) remains the backstop for a
  missing marker.

### arb-watch (Go): clipboard copy no longer leaks base64 to the screen (2026-06-28)
- **Copy (`c` / copy-range) now pipes to the OS clipboard** (`pbcopy`/`xclip`/`xsel`/`wl-copy`/`clip`)
  instead of writing a raw OSC-52 escape to `os.Stderr`. The stderr write interleaved with bubbletea's
  alt-screen stdout renderer (and silently failed on terminals without OSC-52 support), leaking the
  base64 **payload** — the copied text — as visible garbage at the bottom of the screen. A subprocess
  pipe can't leak an escape sequence. OSC-52 is kept only as a remote/SSH fallback (no local clipboard
  tool), written in a single `/dev/tty` write so it can't interleave. Tests: copy writes nothing to
  stderr; pbcopy round-trips on darwin.

### arb-watch (Go): transcript word-wraps to the pane (2026-06-28)
- **Transcript/detail pane now word-wraps long lines to the viewport width** instead of overflowing to
  the terminal edge (which wrapped mid-word, edge-to-edge). `wrapTo`/`wrapBlock` wrap each block to
  `vp.Width` (ANSI-safe via lipgloss, trailing pad stripped). Wrapping is **display-only** — the copy
  overlay keeps numbering and extracting **logical** lines, so a wrapped line still counts as **1**:
  copy-by-range pastes the clean, unwrapped original (the numbered copy view shows one number per
  logical line, the wrapped continuation aligned under the gutter).
- **Re-wraps on terminal resize and the `f` fullscreen toggle** — both now call `rerenderTranscript`
  after `resizeViewport`, so the content re-wraps to the new pane width (honouring copy mode) instead
  of staying wrapped to the old width. Tests: copy counts a wrapped entry as 1 + extracts clean text;
  display wraps to width; rerender honours copy mode.

### arb-watch (Go): seat-list pane — wider columns + order by activity (2026-06-28)
- **Seat list now orders by last activity, most recent at top** (`visibleSeats` stable-sorts by
  `last_event_ts` desc; seats with no timestamp keep their order at the bottom). The seat pane widened
  34%→40% to fit roomier columns: **Seat** truncates to 20 in a width-22 field (always ≥2 trailing
  spaces to breathe), **State** 8→9 (so `completed` no longer overflows + misaligns Age), **Run** cap
  10→18 (long run-ids like `arb-…-20260628` were cut to `arb-ctr-va`). Pinned by a new ordering test.
- **Seat-list columns now fit the pane on one clean line (responsive)** + bottom padding. The wider
  fixed columns overflowed the ~40%-width pane on normal terminals, so lipgloss wrapped rows mid-table
  (the `Run` header and run-ids spilling to column 0). `seatColWidths` now sizes Seat/Run to the pane —
  wide on wide terminals (e.g. seatW 26 / runW 23 at 170 cols), graceful on narrow — so every row is a
  single line; `PaddingBottom(1)` keeps the last row off the bottom border.
- **Cursor follows the selected seat across reorders** — since the list now re-sorts by live activity,
  `syncCursorToSelection` (run after each seat-list frame) re-finds `selectedTask`'s new index so the
  highlight stays on the seat you picked instead of a fixed row; no-op when nothing is selected. Tested.

### Go client-edge (Track 1) — dispatch + ctl complete (2026-06-28)
- **Review-panel parity hardening** (codex + cold-Opus + pi-GLM): `--audit-panel` now requires
  `--run-id` (Python hard-errors the combo — silent zero-audit/zero-eval otherwise); the task body is
  joined from ALL positional args (no first-token truncation); a bare `--worktree` defaults
  `cleanup:"keep"` like Python; U+2028/U+2029 in a task body stay literal (Go's `encoding/json` escapes
  them even with `SetEscapeHTML(false)` — undone, backslash-run-aware so a literal ` ` is
  preserved); `AGENT_REDIS_TLS` precedence is fill-missing (first source wins, env-file can't flip a
  process-env `0` on); the orphaned-sibling-reply re-queue backs off 100ms (no hard Redis spin); reply
  payload is pretty-printed (Python `jq` parity); `ctl result` guards its type assertion. New `build.go`
  (`buildEnvelope`, unit-tested) + golden `with-worktree-bare`; U+2028 fix mutation-verified; both edge
  cases re-confirmed byte-identical to Python via `--dry-run-envelope`, and a live round-trip still
  returns `{ok:true,result:ACK}` exit 0.
- **The Go client edge is now a working `agent-dispatch` + `ctl`** over the bridge's envelope/Redis
  contract, zero external dependencies (hand-rolled RESP2 → static binary). Builds on the envelope
  foundation: `resp.go` (minimal RESP client — connect/TLS/AUTH/SELECT + LLEN/HSET/EXPIRE/RPUSH/BLPOP/
  HGETALL/GET, encode+decode unit-tested), `config.go` (env + `--env-file` fill-missing resolution +
  the structural guards: `--from` required, `--branch` non-empty, env-file existence-checked),
  `dispatch.go` (observable-queue pre-write → RPUSH → BLPOP-reply loop with strict filtering
  `kind==reply && in_reply_to==self`, notify-drop, sibling-reply re-queue, exit 0/1/124), `ctl.go`
  (`status`/`result`), `main.go` (typed-flag CLI; task body is an argv string → the `\n`/backtick
  trap is impossible by construction).
- **Verified end-to-end:** `go test ./...` green (envelope corpus + RESP + config guards + reply
  classification); `dispatch --dry-run-envelope` is **byte-identical to the Python tool**; and a
  **live round-trip** through the binary to a real `codex-bridge-dev` seat returned `{"ok":true,
  "result":"ACK"}` (exit 0) with `status` reading back `state:completed`. Completes 2b.

### Go client-edge (Track 1) — envelope contract foundation (2026-06-28)
- **Froze the bridge envelope contract as a golden corpus + a byte-matching Go builder** (the design's
  step-zero "first dividend: a tested protocol spec"). `tools/go-client/`: 11 golden envelopes captured
  from Python `agent-dispatch --dry-run-envelope` across the full flag matrix; `envelope.go` builds the
  request as Go structs (field order == the Python dispatcher's insertion order) with `Marshal()`
  producing BYTE-IDENTICAL wire output (compact, `SetEscapeHTML(false)` to match `ensure_ascii=False`,
  no trailing newline); `envelope_test.go` asserts all 11 reproduce byte-for-byte + an HTML-escaping
  guard. `go test ./...` green. Merged on definitive byte-match verification (no full panel — the corpus
  *is* the contract proof for serialization). **Remaining (scoped in the package README):** CLI typed
  flags + structural guards, Redis LPUSH/BLPOP dispatch with strict reply filtering, `ctl`, parity test.
  `parse_stance`/reconcile/search stay Python (go-python-boundary).

### Seat-host container (Track 2) (2026-06-28)
- **Run bridge seats from a baked image** instead of the per-host clone / venv / `npm i -g` /
  launchd ceremony. `deploy/seat-host/`: a `Dockerfile` (node:22-bookworm-slim → Node 22 + Debian
  Python 3.11 + the bridge venv + `codex`/`gemini`/`pi-coding-agent` CLIs + the pi-sdk-host harness),
  an env-driven `entrypoint.sh` (pins `HOME` for engine auth dirs, defaults `AGENT_WORKDIR` to the
  `/repos` mount and warns on host-absolute paths, hands off to `agent-redis-bridge-systemd`), a
  `docker-compose.seat-host.yml` (repo bind-mount `REPO_ROOT→/repos`, engine-auth named volumes,
  `--user $(id -u)`, per-engine services), and a README (build, device-code first-run auth, the
  container-path footgun, UID/ownership). Implements Track 2 of the go-client-edge design.
- **Auth-volume path works for any host UID:** the image pre-creates `/home/seat/.codex|.gemini|.pi`
  world-writable (0777) so a fresh named volume inherits a writable mountpoint — otherwise Docker
  initialises it `root:root` and the non-root `--user $(id -u)` seat hits `EACCES` on device-code
  login (the review panel's P0). Auth commands run as the host UID with `HOME=/home/seat` (cmd-override,
  not `--entrypoint`, so the HOME-pinning shim runs). Verified: an arbitrary UID writes + persists a
  token file on a fresh volume.
- **No host-env bleed:** compose reads `SEAT_`-prefixed overrides (`SEAT_AGENT_WORKDIR`, …) so the
  launchd bridge's ambient host-absolute `AGENT_WORKDIR` can't silently defeat the `/repos` convention
  (verified via `docker compose config`). Engine versions pinned (codex 0.142.3, gemini 0.49.0, pi 0.79.0).
- **Verified headless:** image builds (~389MB); `python -m agent_redis_bridge --help`, `codex`,
  `gemini`, `pi`, `node` all resolve inside; the entrypoint warns on a host-absolute workdir and the
  launch chain reaches the bridge (stops at `AGENT_REDIS_HOST required` — needs the mounted env/bus, an
  ops step). NOT prod-deployed. **agy is not baked** (no clean in-repo install; documented, not guessed).
### Go client-edge (Track 1) — envelope contract foundation (2026-06-28)
- **Froze the bridge envelope contract as a golden corpus + a byte-matching Go builder** (the design's
  step-zero "first dividend: a tested protocol spec"). `tools/go-client/`: 11 golden envelopes captured
  from Python `agent-dispatch --dry-run-envelope` across the full flag matrix; `envelope.go` builds the
  request as Go structs (field order == the Python dispatcher's insertion order) with `Marshal()`
  producing BYTE-IDENTICAL wire output (compact, `SetEscapeHTML(false)` to match `ensure_ascii=False`,
  no trailing newline); `envelope_test.go` asserts all 11 reproduce byte-for-byte + an HTML-escaping
  guard. `go test ./...` green. Merged on definitive byte-match verification (no full panel — the corpus
  *is* the contract proof for serialization). **Remaining (scoped in the package README):** CLI typed
  flags + structural guards, Redis LPUSH/BLPOP dispatch with strict reply filtering, `ctl`, parity test.
  `parse_stance`/reconcile/search stay Python (go-python-boundary).

### agy-print schema-guard test hardening (R2) (2026-06-28)
- **Pinned the two `_schema_ok` sibling guards that had no red-flipping test** (the carried P2-2 / R2
  residual cold-Opus noted): a TOOL step whose `_tool_call` fails to decode, and a USER_INPUT step
  missing field 19, must each disable granular capture (fail loud). Test-only; both non-vacuity
  **mutation-verified** (each test reds when its target guard is removed). No production change.

### Turn-liveness heartbeat for events:live — burst-engine seats no longer read stale (2026-06-28)
- **The bridge now tees a periodic `turn_heartbeat` to `events:live` while a turn is active**, so a
  still-running but event-quiet seat keeps a fresh `last_event_ts` in the arb-watch roster instead of
  reading stale. **Why:** arb-watch freshness tracks event cadence on `events:live`, not process
  liveness. Burst-emitting engines — chiefly `agy-print` (one-shot `agy --print`, granular events in
  SQLite-poll bursts) — go quiet while blocked on the model API, so the seat read stale despite being
  alive (observed live: agy 10 events vs codex 112 in the same panel). codex (continuous app-server
  stream) never had this.
- **Engine-agnostic, throttled, fail-soft.** `_emit_turn_heartbeats()` runs on the existing
  `heartbeat_loop` tick (no new thread); emits only when no real live event flowed within
  `heartbeat_interval` (so chatty turns self-throttle to zero heartbeats); gated on `run_id` like every
  other live tee; and runs in its own try/except so a tee failure can't trip the registry-heartbeat
  failure counter that kills the bridge at 3. New `_last_live_tee_ts` per-task map, wiped in the
  per-task `finally` alongside `_last_stream_heartbeat`.
- Pinned by `tests/test_bridge_turn_heartbeat.py` (4 tests, non-vacuity mutation-verified). Design:
  `docs/superpowers/specs/2026-06-28-agy-print-heartbeat-design.md`.

### agy-print granular — P2 follow-up: fail-loud on schema drift + drop low-printable fallback (2026-06-28)
- **Schema drift now disables (fails loud) instead of retrying forever.** The earlier hardening wave
  broadened `_schema_ok`/`poll` to treat every `sqlite3.Error` as transient (retry, never disable).
  But `OperationalError` covers both genuinely transient conditions ("database is locked", "disk I/O
  error") **and** permanent schema drift ("no such column", "no such table"). A new
  `_is_schema_drift_error()` discriminator now disables granular capture on drift while still
  retrying genuine transients. **Why:** against a future agy schema (e.g. one lacking the `status`
  column) the granular path would otherwise retry at the 0.2s poll interval for the whole turn,
  emitting only log spam — failing *open* rather than *loud*. Restores the fail-loud-on-drift guard.
- **Unknown-tool result fallback no longer emits low-printable garbage.** `_best_result_text`'s
  final `_decode_text(blob)` fallback now passes through the same `_printable_ratio > 0.85` gate the
  rest of the path uses; a UTF-8-decodable-but-low-printable blob (ASCII control bytes) returns `""`
  and the `command_output` event is omitted. **Why:** closes the round-1 P2-1 case the prior wave's
  brief claimed closed but had not — the empty/non-decodable guard did not cover decodable control
  bytes. Caught by the cold-Opus re-review reading source vs. the brief's claim.
- Pinned by 3 new tests (covering both drift handlers + the decodable-low-printable case); engine
  surface exercised by `test_agy_print.py` + `test_engine_progress_schema.py`, all green.

### ARB Memory local read MCP for seats (2026-06-26)
- **Local read-only MCP server for bridge seats** — `arb-memory-local-mcp` exposes only
  `memory_search`, `memory_get`, and `memory_recent` over stdio, backed by the dedicated
  `arbmem_local_reader` Postgres role. **Why:** seats can inspect ARB Memory locally without the
  public OAuth door and without any write-capable database role.
- **Engine injection is flag-gated and per-launch** — ACP engines receive an in-memory `mcpServers`
  launch spec, Codex receives a per-invocation `-c mcp_servers.arb-memory-local=...` override, and
  agent-sdk receives `ClaudeAgentOptions(mcp_servers=...)` plus a reconciled `can_use_tool` ceiling
  so the three read tools are usable at runtime. No global `~/.codex/config.toml` mutation.
- **Read target policy is explicit** — `ARB_MEMORY_LOCAL_DSN` is required and, when `ARB_MEMORY_DSN`
  is present, must point at the same store as the writer DSN unless
  `ARB_MEMORY_LOCAL_ALLOW_CROSS_STORE=1` is set. This prevents silent prod/dev mixing while still
  allowing deliberate production reads.
- **Tombstone behavior documented** — local `memory_get`/`memory_recent` mirror the public door and
  surface artefacts through the shared store functions; `memory_search` continues to exclude
  tombstoned hints.

### arb-watch: Go is now the maintained watcher; Python deprecated (retained)
- **Decision (2026-06-26): the fleet watcher goes Go.** The Go/Bubble Tea TUI at
  `tools/arb-watch-go/` is now the canonical, maintained implementation; the Python/Textual
  `arb-watch` (`src/arb_memory/watch/`) is **deprecated but kept** so the original design/work isn't
  lost. Both are pure clients of the same visibility gateway, so the Python one still runs — its
  entry point now prints a deprecation notice on launch (module docstring + stderr banner). **Why:**
  the side-by-side spike answered the question — Lip Gloss is nicer enough, and the Go version has
  grown features past Python parity (full-width header, `e` expand, `s`/`a` status/agent filters,
  Ctrl+C copy-range with line numbers, `←`/`→` pane-focus). New work lands in Go.
- **Go watcher README** added (`tools/arb-watch-go/README.md`): build/run, full key map, test
  commands (incl. the read-only live-gateway E2E), and the zero-gateway-logic boundary.

### ARB Observability — Slice 1 (eval → prod)
- **Eval event schema frozen** with `schema_version` end-to-end (producer stamps `"1"`, consumer
  stores it on `eval_event_raw` + `eval_deadletter`). Why: pin the wire/storage shape before prod
  volume so Visibility (Slice 4) and span tables (Slice 5) can rely on it.
- **Fixed eval-Redis env resolution** (`resolve_eval_redis`): a URL/DB/prefix set only in the bridge's
  `.env` file now arms the tee (was `os.environ`-only → silently disarmed). Why: prod config lives in
  the `.env`, not the process env.
- **Dispatch fails loud** on `--audit-panel` without `--run-id` (was warn+exit-0), and stamps
  `audit_vote_expected:true` when both are set. Why: a panel dispatch with no run_id silently yields
  zero audit AND zero eval — the core mistake this slice prevents.
- **`grants` subcommand** applies the eval consumer grants + the `arbmemory-mcp` eval-REVOKE on prod.
- **Prod `eval` service** added to compose, consuming prod eval db-6 → `eval_event_raw`.

## 2026-06-24 — ARB Memory MCP write paths (memory_store / memory_remember) + OAuth-seam fixes

- **Write tools on the public MCP door** — `memory_store` (documents/artefacts) and `memory_remember`
  (searchable memories/hints) let claude.ai / ChatGPT / Codex connectors *write* to ARB Memory, not just
  read. The door validates + `memory.write`-scope-gates + rate-limits, then makes one authenticated HTTP
  POST to an internal **writer** service that owns the bus credential and publishes to the existing
  single-writer consumer. **Why:** the door is internet-exposed, so it must not be able to reach the bus
  *by construction* — it holds **no redis client** (a tested invariant: `test_mcp_readonly_import.py`); the
  bus credential lives only in the non-exposed writer. Fail-loud is structural (synchronous POST; writer/
  bus down ⇒ "not accepted"; no buffer ⇒ no silent loss).
  - **Why publish-proxy, not the sidecar it started as** — the design first used a local sidecar Valkey +
    relay (DO managed Valkey has no per-user ACLs, which forced topology-based containment). Implementation
    revealed the door was deliberately built redis-free (3 guard tests); the sidecar broke that *structural*
    guarantee for a weaker *configurational* one. Pivoting to the proxy restored the invariant **and** deleted
    the whole sidecar/relay/heartbeat silent-loss class. See memory `structural-not-configurational-containment`.
  - **Provenance** — `artefacts` gains `source`/`author` columns (symmetric with `hints`); MCP-origin writes
    stamp `source=mcp`, `author=<connector client_id>`.
  - **No silent drops** — the consumer's deterministic-bad path is upgraded from log-and-ack to a
    `write_deadletter` table; it acks only after the deadletter row is durably committed, else leaves the
    entry pending for retry (the deadletter's *own* failure path — caught by codex on review).
  - **Auto-index** — `memory_store` also emits an index hint over the (capped) content, auto-linked to the
    artefact, so a stored document is findable via `memory_search` (search runs over hints; artefacts carry
    no embedding). Without it a stored doc was fetch-by-id only.
  - Single `memory.write` scope (default-granted, resolved via `get_access_token().scopes`, anonymous =
    deny); size/mime/id-charset/tag-count validation; content-hash-derived ids; linked-hint preflight.
  - **Pipeline:** spec (6 iterations, 3 decorrelated design/transport panels) → codex-TDD impl → tri-model
    code review (1 P1 + P2s folded) → real-component E2E. Merged to dev, deployed live; live e2e smoke
    through writer → bus → consumer → DO Postgres.

- **Three OAuth-seam fixes (connector-enabling; each found under REAL connector use, not the suite):**
  - **`get_client` returned a hardcoded `scope="memory.read"`** → the SDK `/authorize` rejected any
    `memory.write` request (`invalid_scope`) and bounced the flow before `/login` (looked like a CF Access
    problem; wasn't). Now honours the client's registered scope. *Surfaced by the live login test.*
  - **Protected-resource metadata advertised only `memory.read`** (the SDK ties RFC-9728 `scopes_supported`
    to `required_scopes`, kept minimal so reads don't carry write). RFC-9728 clients (claude.ai) request
    exactly the resource's advertised scopes → never asked for write → read-only token. Now advertises the
    full `valid_scopes` at the resource; `required_scopes` stays read, write stays per-tool-gated. *Surfaced
    by claude.ai writes failing while ChatGPT's worked — different client scope-discovery behaviour.*
  - **Connector-facing tool docstrings** — MCP tool descriptions now carry usage, notably `memory_remember`'s
    both-or-neither `artefact_id`/`artefact_version` rule.
  - **Methodology:** all three lived in protocol *seams* (the SDK consulting `get_client`; RFC-9728 scope
    discovery; an agent's "store ⇒ searchable" assumption) that green unit tests can't see — each was caught
    by a real connector round-trip and is now pinned by a test. Both claude.ai and ChatGPT verified writing
    end-to-end.

- **Operator at deploy:** new `writer` compose service (internal; holds the bus URL + `ARB_MEMORY_WRITER_TOKEN`;
  no published port); the `mcp` door gets `ARB_MEMORY_MCP_WRITER_URL` + token and **no redis env**; `schema.sql`
  migration (`artefacts` source/author + `write_deadletter`, idempotent `ALTER … IF NOT EXISTS`); connectors
  must re-authorize to acquire `memory.write`. CF Access on `/authorize`+`/login` (Entra/Azure + MFA) per
  `deploy/cloudflare-access-setup.md` — path-scoped only; server-side connector paths (`/`, `/token`,
  `/register`, `/.well-known/*`) stay open.

## 2026-06-24 — Bridge dispatch queue (capacity gating + control lane + FIFO)

- **Bridge dispatch queue** — a busy seat now *queues* work instead of replying `bridge busy`. The main
  loop waits for engine-pool capacity (`EnginePool.wait_for_capacity`, stop-aware) before popping a
  request, so un-popped work stays durable in Redis `:inbox` (zero new durability surface); `handle_raw`'s
  synchronous busy reply is kept as a safety net. `agent-dispatch` writes a `queued` task status on enqueue
  and defaults `--timeout` to 3600s so a deep queue is observable, not an opaque wait. **Why:** orchestrators
  can fan out beyond `--max-parallel` without hand-managing retries — backpressure becomes "queued", not
  "rejected".
  - **Control lane** — `cancel`/`steer` get their own ungated key (`agent:<id>:control`), drained *before*
    the capacity gate, so a running task can still be interrupted at full capacity (controls used to share
    the request inbox, where the gate would starve them). Fail-soft drain; only `steer`/`cancel` accepted.
  - **FIFO ordering** — request producers enqueue to the tail (`RPUSH`) while the bridge pops the head, so
    oldest-first; the reliable-inbox `BLMOVE`/recovery machinery is unchanged. (The inbox was accidentally
    LIFO — `LPUSH` + left-pop — which would have starved old tasks under load; found by *running* it, not by
    reading the spec's "FIFO" claim.)
  - **Operator at deploy:** migrate fleet clients off the `bridge busy` fast-reject contract (single clean
    flip, no opt-in shim); the real-bus FIFO / cancel-during-run check is the deploy-time verification.
  - **Methodology note:** the decorrelated panels caught four load-bearing defects pre-merge — a
    queued-status race, the control-lane starvation, a daemon-crash on a Redis blip in the new control drain,
    and the LIFO-not-FIFO ordering — each surfaced by the cross-vendor (non-Opus) seat and deny-proofed.

## 2026-06-23 — ARB Memory: arb-audit-emit panel wiring

- **arb-audit-emit panel wiring** — real review/design panels now record an audit trail (roster
  manifest + per-seat votes + verdict) keyed by one `run_id`. Votes are parsed from a uniform stance
  block by `stance.py`; the verdict emit is gated by `panel_audit.reconcile` (seq-1 manifest +
  precedence + roster + stance-match, `incomplete→refuse`) so doneness-laundering fails loud.
  `agent-dispatch --audit-panel` auto-emits bridge votes fail-soft. **Why:** the audit path was built
  but dormant (zero real emitters); this makes the audit log record live panel decisions.

## 2026-06-23 — ARB Memory: audit-eval slice 1 (eval-trace correlation spine)

- **Audit + eval-trace correlation spine** — the foundation the panel wiring above is built on (backfilled
  2026-06-24). Adds the `run_id` envelope field (`agent-dispatch --run-id`) threading a per-panel
  correlation id; `audit_events` gains a first-class `kind` column (nullable→backfill→NOT-NULL migration);
  a new **eval** data class — `eval_event_raw` + `eval_deadletter` schema, `EvalConsumer` (idempotent insert
  keyed on `stream_entry_id`, run_id-missing dead-letter, crash-recovery drain) on its own Valkey db
  (db4, `eval:events`); `apply_eval_grants` (least-privilege: consumer `INSERT`/`SELECT` only, MCP read role
  `REVOKE`d from eval tables); the `arb-audit-emit` CLI (dispatch/vote/verdict); and an **extract-only eval
  tee** (`push_task_event` → `eval:events`, allowlisted metadata only, fail-soft, opt-in via
  `ARB_EVAL_REDIS_URL`). Built codex-TDD over 12 tasks, tri-model reviewed, live-canaried on DO dev
  (3×+2× green + deny-proof); the eval-role grant guard runs green on the real substrate via
  `ARB_EVAL_TEST_ROLE`. **Why:** join "what was decided" (audit) to "how each seat behaved" (eval) by one
  `run_id` — the spine every later panel-audit/eval feature builds on. Producer tee ships dormant until
  `ARB_EVAL_REDIS_URL` is set.

## 2026-06-22 — H2 producer: flip from dormant to operative (shadow mode)

- **What:** The H2 producer follow-slice (branch `feat/h2-producer`) makes the H2
  environmental-assumption gate operative. The bridge-protocol gate now derives candidate
  environmental assumptions from the review diff (`h2_derive.derive(files, diff)` — thin call-based
  AST heuristics, H1-analog), forces a reviewer disposition per candidate
  (`answered`/`not_load_bearing`/`flag`, all evidence-anchored), runs in **shadow mode by default**
  (`H2_MODE="shadow"`), and graduates to BLOCK only via a falsifiable, un-gameable-by-mistake
  criterion (`is_graduation_ready`: ≥10 complete runs, ≥20 disposed, discrimination present,
  FP=Σnot_load_bearing/Σdisposed<0.10, complete-runs-only). Ships shadow-safe — nothing gates live.
  Built codex-TDD over 10 tasks with warm-seat verify-from-git; full Workflow B.
- **Why / what the methodology caught (the load-bearing part):** the graduation criterion's
  integrity took **seven review rounds** (uncomputable → gameable-by-silence → unstable-id →
  empty-run → presence-not-validity → no-discrimination → token-discrimination), terminating not by
  finding the last hole but by **diagnosing the threat-class** of the remaining holes
  (mistake-class = closeable; adversarial-disposition = §9 named non-goal, out of threat model for a
  trusted operator). The final whole-producer panel then found **two P1 graduation-integrity
  defects in the BUILT code** — most sharply, the slice's *own* deny-proofs (`f`, `h`) were hollow
  against their named guard (hard-set `complete=False` so the mutated validity/anchor field was
  never read — the exact fixture-masks-reality defect the slice exists to prevent, in its own
  tests). Fixed (`ad2ba0c`): `is_complete` rejects unknown-id rows; `f`/`h` rewritten to *compute*
  `complete` through `is_complete`; **mutation-verified both directions** (delete the guard clause →
  the deny-proof reds).
- **The re-panel's headline catch (`f543681`):** the re-panel asked whether a *duplicate*
  disposition row was rejected. **Three decorrelated analytical seats (cold-opus, m3, glm) all said
  handled-or-out-of-scope; none ran it; the empirical check disproved all three** —
  `validate_h2_section` and `is_complete` both returned `True` for a section with two valid rows for
  one candidate (no cross-row uniqueness check; `rows⊆derived` passes both copies). It mattered
  because `_disposition_counts` counts *rows*, so a duplicate `answered`/`flag` row inflates the
  disposed denominator → lowers FP → makes graduation *easier* (false-pass skew, the dangerous
  direction). It is **mistake-class** (an honest reviewer disposing a candidate twice), so in threat
  model — the *deliberate* duplicate is the §9 non-goal. Fixed with a uniqueness clause in
  `is_complete` (sibling of the unknown-id fix) → a duplicate-row run is incomplete → **excluded in
  its entirety from the graduation denominator** (complete-runs-only; verified by execution).
  Deny-proof with a positive control; mutation-verified both directions. This is the live
  justification for the planned E2E-after-panel gate: decorrelated *analytical* review shares a
  blind spot (it reasons about the representation, not the running system); the empirical check is
  decorrelated *across modes* and caught what three structural reads missed — GLM even *saw* the
  honest-mistake denominator inflation and filed it into a comfortable category anyway.
- **Verification record (honest roster, not laundered):** the P1 fix `ad2ba0c` went through a
  decorrelated re-panel. Result: **cold-opus APPROVE, m3 no-P0/P1, glm approve — agy TIMED OUT
  (exit 124 at 1800s, no verdict produced).** This is **3/3 non-blocking + 1 no-verdict**, NOT a
  laundered 4/4: a timeout is absence-of-verdict, not approval (same discipline as
  E2E-didn't-run ≠ E2E-passed). Proceeded on (a) accept-3/3 — the P1 property is over-confirmed
  (3 seats + warm-seat mutation run), the headline duplicate-id catch was fixed and
  empirically verified *beyond* any panel verdict, the slice is shadow-safe, and the missing 4th
  verdict was a missing instance of the verification mode that mattered *least* here. The duplicate
  fix `f543681` is warm-seat-authored on user direction and mutation-verified but **not
  panel-reviewed** (the panel saw `ad2ba0c`); covered by the user's →dev review. Held for the
  user's →dev review before merge.

## 2026-06-22 — ARB Memory Phase 3 go-live: connector canary GREEN

- **Phase 3 deployed; claude.ai connector canary GREEN** (backfilled 2026-06-24) — flips the pre-go-live
  canary noted in the Phase 3 build entry below from pending to verified: the public read-only MCP door is
  deployed (DigitalOcean + Cloudflare tunnel) and a real claude.ai MCP-connector round-trip is GREEN.
  **Why:** confirms external connector compatibility end-to-end on the live edge, not just in local e2e.

## 2026-06-22 — ARB Memory: DigitalOcean least-privilege role model

- **DO-managed-Postgres least-privilege adaptation** (backfilled 2026-06-24) — the owner role on DO managed
  Postgres can't `CREATE ROLE`, so `schema.sql` is now **DDL-only** (no role creation) and the read-only MCP
  role is platform-pre-created with a per-env name (`arbmemory-dev-mcp` / `arbmemory-mcp`) instead of the
  hardcoded `arbmem_mcp`. The role name is configurable (`ARB_MEMORY_MCP_ROLE`, default `arbmem_mcp` for
  back-compat); a shared `apply_mcp_grants(conn, role)` helper (psycopg `sql.Identifier`-quoted) applies the
  least-privilege grants; conftest creates the role locally or assumes it pre-created on DO. **Why:** run
  ARB Memory on managed Postgres without owner-superuser assumptions.

## 2026-06-21 — Defect-detection skill (held-axis hunts): H1 operative, H2 gate dormant

- **What:** A new skill that promotes two held-axis defect classes from corpus *reference* into operative
  panel procedures, merged to `dev` (merge `f97fae4`). **H1 config-drift** (`skills/defect_hunts/`) is an
  AST kernel keyed on *literal == env-default* — it FLAGs a hardcoded sibling left behind when a constant
  becomes env-derived (the `audit.PREFIX=""` shape) — wired as a **live standing check in the
  bridge-protocol gate** (`BLOCK_H1_STANDING_CHECK`), gated behind a purpose-built deterministic eval
  (`skills/defect_hunts/eval/runner.py`): recall=precision=1.0, scope hard-labelled *"class-seed
  validation, not broad certification."* **H2** (environmental-assumption schema gate) is built and tested
  but **dormant** — no producer emits `h2_section`, so absence reports `h2_status: "dormant-no-producer"`
  loudly, never a silent pass. Its producer (a review-brief composer) is a deferred follow slice.
- **Why / what the methodology caught (the load-bearing part):** the skill's own premise — *don't trust a
  green test* — was applied to itself. Built codex-TDD per task with warm-seat verify-from-git; the two hard
  tasks each got a **5-seat decorrelated panel** (cold-Opus + agy + M3 + GLM certifying, codex
  author-contributor) that caught a P0 the solo verify passed green. **Task 4 (sealing):** the production
  `seal()` discovered no anchors and archived the whole repo, so the anti-circularity sealing was inert on
  the real path (a fixture-masks-reality bug the unit tests hid) — fixed + inject-revert-proven.
  **Task 8 (eval harness, HARD GATE A):** the 7-adversary deny-proof was genuine, but the panel found
  **three distinct hollow-cheat shapes** the eval's *coverage* let through (a value-ignoring detector, a
  positional/path cheat exploiting an unsealed eval positive, a could-not-analyze spammer) — remediated to
  **13 adversaries + out-of-process detector execution** (real sandbox), and cold-Opus re-confirmed *no
  hollow-1.0 path remains*: you cannot pass the gate without running the real algorithm. **HARD GATE B**
  (every config suspect closed by execution or logged as a finding — incl. `BRIDGE_MAX_PARALLEL`, the
  skill's own first real catch) holds. The final whole-skill panel surfaced the H2 dormancy (the skill's
  own held-axis blind spot — a gate whose enforcement path is never exercised); the warm seat ruled to ship
  H1 operative and make H2 honestly dormant. 115 skill tests; dev green at 638 passed.

## 2026-06-21 — GLM-5.2 judge seat runs on pi-sdk (not agent-sdk); pi upgraded to 0.79.9

- **What:** Added GLM-5.2 as a read-only judge seat on the **pi-sdk** engine (`--model zai/glm-5.2`,
  z.ai Coding-Plan endpoint) — a decorrelated sibling to the M3 seat, restoring the review panel's 5th
  independent model. Upgraded `@earendil-works/pi-coding-agent` 0.79.3 → 0.79.9 (native `zai/glm-5.2`,
  1M context). Documented the engine-routing rule in `docs/agent-role-routing.md`,
  `docs/decisions/m3-judgment-seat.md` §D4, and a forward-correction banner on the agent-sdk-engine plan.
- **Why:** GLM on the `agent-sdk` engine (z.ai `/api/anthropic`) hung on every real dispatch — that
  endpoint's time-to-first-token scales steeply with input size, and agent-sdk's full Claude Code system
  prompt + tool schemas push each request past the dispatch timeout. pi-sdk's lean prompt + the
  Coding-Plan endpoint answer in seconds (trivial ~10s; review-sized agentic task ~121s). The rule is
  engine-level and platform-independent (launchd on macOS, systemd on Linux), promoted from session-local
  memory into version-controlled docs so other clones inherit it. (Related lane bug noted for the record:
  the agent-sdk GLM model code must be plain `glm-5.2`, never `glm-5.2[1m]`, which z.ai 400-loops.)

## 2026-06-21 — ARB Memory Phase 3: public read-only MCP door (on `feat/arb-memory`)

The public MCP host for ARB Memory: a read-only FastMCP server with our own OAuth 2.1 authorization
server, pinned connector redirect allowlist, hashed auth persistence, refresh rotation with reuse-family
revocation, public-base-url proxy trust, and one-image three-service deploy shape.

- **What:** `src/arb_memory/mcp/` now contains the OAuth provider, login route, DCR hardening, token
  verification, read-only MCP tools (`memory_search`, `memory_get`, `memory_recent`), metadata pinning, and
  readiness/liveness. `python -m arb_memory memory|audit|mcp` dispatches the three services. `deploy/`
  contains the Dockerfile, compose files, Cloudflare tunnel example, and go-live runbook. The MCP package has
  no Redis/Valkey import path; the database role is the write boundary.
- **Why:** Phase 3 opens ARB Memory to external MCP connectors without putting the write bus or Valkey on the
  public edge. The security surface is proven through direct provider tests, adversarial stored rows, hostile
  proxy headers across PRM/ASM/authorize/token/401, and a local DCR→2FA login→S256 token→authenticated search
  e2e. Real claude.ai/ChatGPT connector compatibility remains a pre-go-live canary documented in
  `deploy/README.md`.
- **Why this shape (what the panels caught — this is the one public trust boundary):** the design panel
  (cold-Opus+agy+codex+M3) reframed the OAuth as **read-only + a pinned redirect allowlist as the central
  control** — the load-bearing attack (M3) is open-DCR + phishing, which **2FA and audience-binding do not
  stop**; only an authorize-time allowlist of the real connector callbacks does (pinnable because Claude Code
  uses the bus, not the door). cold-Opus (source-verified) corrected a systemic over-credit of the SDK: it
  enforces **only** S256-PKCE + the metadata envelope — **audience-binding (RFC 8707), auth-code single-use,
  refresh rotation, and redirect policy are provider code**, so the security tests must drive the provider
  **directly** and seed adversarial rows (the SDK rejects easy violations before the provider runs, so
  end-to-end-only tests prove nothing). The **code-review panel returned 4/5 BLOCK** on persistence/config
  defects the build's own tests hid: **production connections never committed** (masked by an autocommit test
  fixture — a fixture-masks-reality bug that would have silently lost all OAuth state); an **in-memory
  client-secret cache that failed OPEN on restart** (SDK skips the secret check when the secret is absent →
  fixed by registering connectors as **public PKCE clients**, no secret at all); the **DCR GC evicting the
  owner's live connector** (`last_used_at` declared but never written); **`/token` not enforcing the RFC 8707
  resource**; and a **per-session-only login rate-limit** (fresh sessions reset the budget). All nine findings
  fixed with red-first regression tests; every P0 fix **inject-revert deny-proven** (autocommit→False → red;
  global-throttle removed → red; `last_used_at` write skipped → red; resource check removed → red). One fix's
  deny-proof was itself **found hollow during verification** (the `/token` resource check passed even when
  removed, because no test seeded a foreign-resource code) and a real deny-proof added. Suite **125 passed /
  1 skipped**. Held on `feat/arb-memory` pending the `→ dev` review; go-live (DO managed-PG, CF tunnel, DNS,
  secrets, the real connector canary) is the operator's hands.

## 2026-06-21 — ARB Memory Phase 2: audit consumer (on `feat/arb-memory`)

The audit pipeline — a separate consumer group draining `arbmem:audit` into `audit_events`, joining
orchestrator decisions + seat positions by `run_id`+`seq` so every panel auto-produces the disagreement
corpus. Full pipeline (design → spec-panel → plan-panel → build → 4-certifier review → fix), codex
implementor-contributor throughout.

- **What:** `src/arb_memory/audit.py` — `audit_emit`/`AuditRun` (fire-and-forget, payload-capped),
  `AuditConsumer` (one loop, XACK-after-commit), the **Valkey-INCR per-run seq allocator** (`(run_id,seq)`
  unique-by-construction), a length-one **sink seam**, and an additive `audit_deadletter` table. Decision-
  grain enforced by a structural payload cap; lag alarm via `XINFO GROUPS` pending.
- **Why / what the pipeline caught (all *silent-loss* paths, the cardinal sin for an evidence store):** the
  spec-panel caught **4** — seq ambiguity × `ON CONFLICT DO NOTHING` (false-dedup), `MAXLEN` trimming
  undrained events, unbounded payload drift, no fail-loud on collision; the plan-panel caught the **collision
  deny-proof itself was writable-as-drop**; and the code-review caught the deepest: a **sink-side
  `ProgrammingError` (e.g. a missing table) was misclassified as a bad event and silently ack-dropped — a
  schema bug would have lost *every* audit event**, plus a malformed event silently dropped (evidence!). The
  unifying fix: **for an evidence store there is NO silent-drop — all infra/DB errors retry, deterministic
  bad-event errors deadletter (preserved, recoverable), nothing is dropped.** (The Phase-1 ack-and-drop
  pattern is correct for a lossy *cache*, wrong for *evidence*.) Every fix deny-proven by inject-revert
  (narrowed-retry → ProgrammingError dropped → red; malformed → not in deadletter → red; lag without `XTRIM`
  → `XLEN`-only passes → red; collision without the deadletter write → red). Suite (Phase 0+1+2): **43
  passed / 1 skipped**; `audit_deadletter` additive (existing tables untouched); scoped. Held on
  `feat/arb-memory` pending the `→ dev` review.
- **Re-review hardening (focused cold-Opus, post-integration):** an independent stake-free seat re-verified
  all four deny-proofs are *genuine, not hollow*, then found one residual: the `_handle_entry` **handle path**
  (`except Exception`, after a clean parse) still **ack-and-dropped** — the parse path had been upgraded to
  deadletter but the handle path had not. Unreachable through today's only sink (Postgres errors are
  `psycopg.Error`, caught by the retry branch) but it's exactly the seam where a future object-store/training
  sink's serialize bug would leak evidence. Fixed: deadletter-before-ack on a fresh connection (the handler's
  conn may be poisoned), mirroring the parse path. New deny-proof `test_handler_error_deadlettered_not_dropped`
  (TDD red `'dropped' == 'dead-lettered'` → green). Suite **44 passed / 1 skipped**. Lesson reinforced: *walk
  **every** `except` for the silent-drop, not just the parse path.*

## 2026-06-20 — ARB Memory Phase 1: Valkey-bus transport (on `feat/arb-memory`)

The transport that lets seats use ARB Memory with no second endpoint — auth = bus membership. Built through
the full pipeline (design → spec-panel → plan-panel → build → 4-certifier code-review → 3 fix cycles), codex
as implementor contributor on every panel; **GLM 5.2 added as a 4th decorrelated judge** (though it timed out
on the longer reviews — the agent-sdk judge is slow).

- **What:** `src/arb_memory/bus.py` — `memory_write` (fire-and-forget `XADD` to `arbmem:writes`),
  `memory_query` (`XADD` to `arbmem:reads` + `BLPOP` the per-correlation-id reply, **timeout→grep**), and a
  `MemoryConsumer` running **two concurrent loops** (write loop: embed + idempotent atomic write into the
  Phase 0 store; read loop: search + reply). All on the existing `agent_scratch:arbmem:*` keyspace (db 12),
  disjoint from bridge traffic.
- **Why / what the pipeline caught:** a **silent-data-loss** idempotency bug (the key insert + write were
  two transactions → a crash between them lost the write; now one transaction, savepoint-nested); a **hollow
  PEL deny-proof** (the recovery test passed with a no-op drain) → rewritten to split read/handle/ack with an
  `XPENDING==0` assertion; a **single-loop HOL block** (a slow embed froze reads) → separate concurrent
  loops; a **bytes hazard** (`decode_responses` undocumented) ; and a **poison-pill crash-loop** found in
  review (a malformed write was never acked → re-crashed on every restart) — fixed across two layers
  (parse errors *and* deterministic content errors ack-and-drop; only infra errors retry). Every fix
  **deny-proven** by inject-revert (two-tx → red; no-op drain → red; BLPOP-0 → red; single-loop → red;
  poison no-catch → red; content-retry → red). Suite (Phase 0+1): **28 passed / 1 skipped** against local
  pgvector + redis db 15 (tests never touch db 12); whole-repo collection green; scoped to `arb_memory`.
  Held on `feat/arb-memory` pending the `→ dev` review.

## 2026-06-20 — ARB Memory Phase 0: two-lane store + write-library (on `feat/arb-memory`)

First slice of consolidating the standalone `ai-brain` into ARB as **ARB Memory** (architecture:
`docs/decisions/arb-memory-architecture.md`). Built through the full ARB pipeline, dogfooding the tool on
itself — design → spec-panel → plan-panel → build → 4-certifier code-review, with codex as the implementor
contributor on every panel and **GLM 5.2 added as a 4th decorrelated judge**.

- **What:** new scoped package `src/arb_memory/` (imported only by the future MCP-host services) — a
  **two-lane Postgres+pgvector schema** (`artefacts`: faithful, PK-keyed, **versioned**; `hints`: fuzzy,
  embedded, the semantic index over artefacts) plus the write-library: `embed()` (OpenAI
  `text-embedding-3-small`, the single embedding owner), `write_artefact_and_hints()` (atomic dual-write in
  one transaction), version-on-changed-content / no-op-on-identical, **two-step version-pinned retrieve**,
  and **real RRF hybrid search** (reciprocal-rank fusion over separate vector + lexical ranked lanes,
  live-only). `pyproject` gains an optional `arb-memory` extra; tests `importorskip` it so they never break
  core-suite collection. Audit table is a schema **stub** (Phase 2).
- **Why / what the pipeline caught before merge:** the panels found **nine load-bearing defects pre-code**
  (version-unaware hint hash → new artefact versions undiscoverable; two hollow deny-proofs; a missing
  `CREATE EXTENSION`; a suite-breaking import; a missing pgvector adapter; a `NameError`-at-import default)
  and the code-review caught a **degraded-to-vector-only "hybrid" search** with a dead lexical lane. All
  fixed and deny-proven: dual-write atomicity (strip the transaction → red), version-pinning (fetch-latest →
  red), hybrid fusion (vector-only → red), the single-embedding-owner AST drift-guard, and the
  content/content_bytes XOR guard. **Suite: 16 passed / 1 skipped** (smoke gated on `OPENAI_API_KEY`) against
  a local `pgvector/pgvector:pg16` dev DB; whole-repo collection green; diff scoped to `src/arb_memory/**` +
  `tests/arb_memory/**` + `pyproject` (no bridge-core touched). Stops at the Phase 0 boundary on
  `feat/arb-memory` pending the `→ dev` review.

## 2026-06-19 — fix: lazy-import the agent-sdk engine (unblocks hosts without `claude_agent_sdk`)

### `bridge.py` + `pyproject.toml` — `claude_agent_sdk` is now optional, not load-bearing for every seat

- **What:** `bridge.py` no longer imports `AgentSdkEngine` at module scope — it's imported **lazily**
  inside `build_engine()`'s `engine == "agent-sdk"` branch. `claude-agent-sdk` is declared as an **optional
  extra** (`pip install -e '.[agent-sdk]'`), only needed by hosts that run the agent-sdk engine
  (M3/Kimi/GLM seats). Added a subprocess-isolated regression guard
  (`tests/test_agent_sdk_lazy_import.py`) that imports `bridge.py` with `claude_agent_sdk` blocked, and the
  agent-sdk integration tests now patch the engine at its source module (the lazy-import resolution point).
- **Why:** the agent-sdk engine (merged June 18) imports `claude_agent_sdk` eagerly at its module top; since
  `bridge.py` imported `AgentSdkEngine` at module scope, **loading the bridge at all required the package** —
  so **codex/agy-print/pi seats couldn't start on any host without `claude_agent_sdk` installed**, even
  though they never construct an agent-sdk engine, and the dep wasn't declared in `pyproject`. Found running
  `dev` on a second host. The fix decouples the optional heavy dep from the core load path. **Deny-proven:**
  the guard fails if the eager import is re-added; suite **502 passed**; the real agent-sdk path still
  constructs with the package present.

## 2026-06-19 — Tier-1 reliability batch (#8 / #9 / #11) — make ARB trustworthy to use

Right-sized (light TDD + single cold-reviewer, APPROVE, no findings). Three reliability fixes:

- **#11 flaky `test_bridge_notify_inbox`** — `test_env_var_overrides_default` / `test_cli_flag_overrides_env`
  called `importlib.reload(bridge_module)` under a patched env and never restored it, polluting the global
  parser default → `test_default_routes_notifies_to_inbox` failed depending on run order. Fix: `setUp` pops
  `BRIDGE_NOTIFY_INBOX`, `tearDown` reloads with the var unset then restores. **Why:** green-means-green —
  an order-dependent flake undermines the whole suite's signal once you rely on it.
- **#8 `bridge_dispatch` swallowed failures** (`skills/diagnose/panel.py`) — returned bare `None` on a
  non-zero `agent-dispatch` exit, discarding stderr (this bit the #13 live dogfood, masking a relative
  env-path error as an undebuggable None). Fix: surface `argv[:-1]`/`returncode`/`stderr` to `sys.stderr`
  before returning `None`; the None contract `run_panel` relies on is preserved, the sender token rides in
  `env=` and is never printed. **Why:** live dispatch failures must be debuggable, not silent.
- **#9 contamination test-seams moved out of production** (`diagnose.py` + `diagnose-steer/diagnose_steer.py`)
  — the `contamination` kwarg + its injection branches (and `build_predicates(observations, contamination)`)
  were production-resident test seams: production code carrying branches that only fire for tests, a cousin
  of fixture-masks-reality. Fix: removed the kwarg + all branches; the contamination scenarios now corrupt a
  **genuinely-clean real record** (after `assert_real_integrated_record` proves a valid hash-chained dispatch
  log) and assert the real validator blocks, with a passing clean twin per scenario. **Why:** production code
  should *be* the production path — no test-only behavior in it; and the controls are now stronger (real
  record corruption, not an inert branch flipped by a kwarg). Equivalence proven (branches were inert at
  `contamination={}`); noisy-OR monotonicity coverage preserved test-side; no test dropped (+1 added).

## 2026-06-19 — diagnose live-panel: orchestrator-forwarded dispatch (corrects #7's never-dispatched-live) [#13]

### `skills/diagnose/panel.py` + `briefs.py` + `_diagnose_common/neutral_validators.py` + `panel_constants.json`

- **What:** `run_panel` now takes an **injected dispatcher** (`run_panel(sealed_briefs, dispatch, work_dir)`) —
  the skill authors+seals briefs and runs the recompute gate; the orchestrator forwards each sealed envelope.
  Voting seats route over the bridge via the **real** `agent-dispatch` interface
  (`--engine/--target-id/--role` + positional `<task>`, sender via `FROM_AGENT_ID`); the roster is pinned to
  three verified, vendor-decorrelated live seats (`blind=codex-bridge-dev`, `alternative=agy-bridge-dev`,
  `open=pi-sdk …minimax-m3`). The descriptive `scribe=claude-haiku` runs as an in-process Agent-tool cold
  subagent and is **excluded from the verdict basis by construction** (filtered from the certifier+collation
  post-briefs on both skill and gate sides — the certifier has no `bus_reply_ref` path to it). Certifier
  selection is stable-order with a typed `CertifierStarved` fail-loud. Seat-identity decorrelation is
  enforced end-to-end by `_panel_blocks` (submission `seat` == roster `target_id`).
- **Why:** #7 shipped a `run_panel` that python-self-dispatched via flags `agent-dispatch` never had
  (`--model/--ceiling/--work-dir`) — green tests, **zero live dispatch** (the test mocked the broken
  function: the 4th fixture-masks-reality). The full review pipeline (design+spec+plan panels, tri-certifier
  code review, codex contributor) caught five more: the answered-model was recorded-not-checked; the scribe
  could reach the verdict via the certifier post-brief; the argv omitted the positional task; and **the sender
  was set via `AGENT_DISPATCH_SENDER`, which `agent-dispatch` never reads** — `FROM_AGENT_ID` is the real
  field (the 5th fixture-masks-reality, caught by the M3 reviewer). The adversarial "which model answered"
  limit (no immutable bus `from→task` ledger) is named honestly and backlogged (#14). **Live-verified:** a
  real round-trip to all three seats accepted the sender and returned distinct vendors (`ACK codex` /
  `ACK Gemini 3.5 Flash` / `ACK MiniMax-M3`) with `from`=the real seat; the Haiku Agent-tool scribe channel
  returned a clean description. Suite: **110 passed**; both deny-proofs verified load-bearing by inject-revert;
  gate self-test 49 passed; no gate logic touched (no trust-root re-pin).

## 2026-06-18 — `agent-sdk` per-engine CLAUDE_CONFIG_DIR isolation

### `engines/agent_sdk_models.py` + `agent_sdk.py` — each engine gets its own claude config/state dir

- **What:** `isolated_env` now sets `CLAUDE_CONFIG_DIR` to a per-engine, non-repo path
  (`<session_root>/<agent_id>/claude-config/<uuid>`), created before connect. Previously unset, so every
  `claude` subprocess the seat spawned shared the default `~/.claude`.
- **Why:** under concurrent connects, sharing `~/.claude` is the credible cause of a connect-time
  `ProcessError: exit code 1`, and concurrent engines shared session-mirror/transcript state. Per-engine
  isolation removes both. Auth is unaffected — the seats authenticate via env-provided vendor vars
  (`ANTHROPIC_BASE_URL` + `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`), not config-dir creds; **live-verified**
  (seat connects, live M3 smoke test fires, the isolated dir is populated, mutation + respawn-resume both
  still work). Found while investigating intra-seat parallelism (the broader EnginePool admission-thread
  flaw is filed separately: `docs/enginepool-admission-thread-flaw.md`). Suite: **373 tests OK**.

## 2026-06-18 — `agent-sdk` SessionStore: respawn-resume fixed + non-repo session state (T8-2/T8-4)

### `engines/agent_sdk_session.py` + session-root default — durable resume now actually works

- **What:** (1) session storage now defaults to a **non-repo state dir**
  (`$XDG_STATE_HOME/agent-redis-bridge/agent-sdk-sessions`, else `~/.local/state/...`), agent-id
  namespaced; `--agent-sdk-session-root`/`BRIDGE_AGENT_SDK_SESSION_ROOT` still win. The completion gate is
  **untouched**. (2) `ScrubbedSessionStore` no longer defines the optional SessionStore methods on its
  class — it proxies them via `__getattr__` only when the inner store *truly* implements them (mirroring
  the SDK's class-level `_store_implements` detection, including the inner-inherits-the-Protocol-default
  case), raising `AttributeError` otherwise. (3) `FileSessionStore` implements `list_subkeys`,
  `list_sessions`, and `delete` per the SDK 0.2.104 contract.
- **Why:** the gated live integration (Task 8) caught two SessionStore defects the 365-test unit suite
  missed because it mocked the store. **T8-2:** session-root defaulted inside the per-dispatch worktree →
  transcripts committed with code + the post-commit flush bounced the completion gate (`dirty_after_commit`).
  **T8-4 (durability):** cross-client resume — what a genuine respawn does — fatally failed:
  `FileSessionStore` lacked `list_subkeys`/`list_sessions`, while `ScrubbedSessionStore` *advertised* them
  (class-level definition defeats the SDK's optional-method detection) then delegated to an inner that
  couldn't perform them → `RuntimeError: ... failed during resume materialization`. So
  `supports_continuation=True` was a false-positive capability claim. A unanimous tri-model design panel
  (codex+agy+cold-Opus, SDK-source-verified) settled the fix; see `docs/escaped-defect-journal.md` ED-001
  (whole-panel miss) for the lesson: the mock was more protocol-complete than the real store. The
  **load-bearing regression test** drives the SDK's real `materialize_resume_session` against the real
  `ScrubbedSessionStore(FileSessionStore)` (main transcript + a subagent subpath) — it fails on the
  pre-fix code and passes after. Suite: **370 tests OK**.

## 2026-06-18 — `agent-sdk` startup gate: deterministic, model-independent (T8-1)

### `src/agent_redis_bridge/engines/agent_sdk.py` — the fail-closed startup gate no longer depends on live-model behavior

- **What:** `assert_serveable()` now proves the gate with NO live model call. It checks the structural
  fail-closed fields on the exact options object passed to the client (`allowed_tools==[]`,
  `setting_sources==[]`, `permission_mode=="default"`), asserts the callback is wired into the
  **connected** client (`self.client._query.can_use_tool is self._options.can_use_tool`), and exercises
  the gate by directly calling `can_use_tool` with an in-ceiling tool (expect Allow) and an
  out-of-ceiling sentinel (expect Deny), using a real `ToolPermissionContext`. `parse_ceiling` now
  rejects unknown tools at parse time. The live model probe is demoted to a **non-fatal, logs-only,
  seat-startup-only** smoke test (`live_smoke_test`, set true only when the engine cwd is the primary
  workdir, never for per-dispatch worktree engines) — it never raises and never gates.
- **Why:** the gated live integration (Task 8) caught that the original positive leg required the live
  model to *choose* to invoke an in-ceiling tool during the probe. Non-Anthropic models don't do that
  reliably, so the gate raised non-deterministically (fail-CLOSED — safe, but the mutation seat became
  unusable), and it re-ran a live model round-trip on every worktree dispatch. A tri-model design panel
  (codex+agy+cold-Opus, all SDK-0.2.104-source-verified, unanimous) confirmed the live "model called a
  tool" proof is not load-bearing — under the fail-closed options the SDK forces
  `--permission-prompt-tool stdio` and routes every ask-path call through the callback — and caught that
  a direct callback call alone proves `decide()` works but **not** that the connected client carries the
  callback. The wiring-identity assertion closes that gap; it is itself proven by a test that breaks the
  wiring and confirms the gate refuses to serve. Suite: **365 tests OK**.

## 2026-06-18 — `agent-sdk` bridge engine: PATH-2 mutation seat (the probe, productionised)

### `src/agent_redis_bridge/engines/agent_sdk.py` + bridge/readonly-gate wiring — a mutation-capable AgentEngine over `claude-agent-sdk`

- **What:** a new `AgentSdkEngine` that drives non-Anthropic models (MiniMax-M3, Kimi, GLM-5.2)
  through the bridge as a **mutation-capable** seat, wrapping a persistent `ClaudeSDKClient`
  subprocess on a dedicated background asyncio loop/thread (all control via
  `run_coroutine_threadsafe`, lock-free vs the turn lock). Adds `ENGINE_TO_TOOL["agent-sdk"]`,
  the `build_engine` branch, the agent-id slug, the worktree hard-guard, and the `readonly_gate`
  allowlist branch. Routing table + auth isolation ported from the de-risking probe. Stateful
  implementer is the core; one-shot oracle mode is a flag.
- **Why fail-closed by construction:** the seat runs third-party models with write tools on a host
  holding three vendors' keys, so the gate is the whole safety story. `can_use_tool` is the **sole**
  authoritative gate, enforced ONLY there — the SDK bypasses it for `allowed_tools`, ambient
  `setting_sources`, and non-`default` `permission_mode`, so the engine builds `ClaudeAgentOptions`
  with `allowed_tools=[]` + `setting_sources=[]` + `permission_mode="default"` and `assert_serveable()`
  **raises** (refuses to serve) if any of those drift. A startup self-probe proves both a positive
  (in-ceiling tool routes through the gate live) and a negative (an out-of-ceiling sentinel returns
  `PermissionResultDeny`). Silent-death is a PASS-shaped failure, so `run_turn` tracks `saw_result`
  and marks the engine unhealthy + `ok=False` if the stream ends without a `ResultMessage`. Auth is
  isolated by blanking every inherited `ANTHROPIC_*`/`AGENT_SDK_*` var (the SDK merges the full parent
  env) and setting only the selected vendor's; events, stderr, and the SessionStore are secret-scrubbed.
- **Review:** spec → panel → plan → panel → codex TDD build → **tri-model implementation review**
  (codex + agy + cold-Opus) → fix → merge. The panel confirmed no real-run gate fail-open and correct
  silent-death handling, and caught four functional defects, all fixed pre-merge: (1) `isolated_env`
  leaked non-enumerated sensitive vars (now blanks by prefix); (2) the startup probe hard-required a
  `Write` denial, which would have **blocked the mutation seat from ever starting** (now a
  config-independent sentinel denial — codex lone-correct, see `docs/disagreement-corpus.md` DC-005);
  (3) stateful resume was unwired through the bridge (now exposes `session_id`, implements
  `resume_thread`, persists last session id for durable respawn); (4) the startup probe could
  false-PASS on silent death (now raises). Suite: **360 tests OK**. Specs/plan:
  `docs/superpowers/specs|plans/2026-06-18-agent-sdk-engine*.md`. The live mutation-on-worktree
  integration (Task 8) is the gated final test, run by the orchestrator.

## 2026-06-18 — agent-sdk mutation probe: PATH-2 de-risked (M3/Kimi/GLM 3/3)

### `tools/agent-sdk-probe/` — proves claude-agent-sdk drives Chinese models through real mutations

- **What:** a committed, secret-free de-risking probe (`models.py`/`spike.py`/`probe.py`/`verifier.py`
  + fixture + hidden held-out oracle) that drives MiniMax-M3, Kimi, and GLM-5.2 via `claude-agent-sdk`
  through a multi-step code mutation in a throwaway repo, judged by an anti-false-PASS verifier. Live
  result: **3/3 PASS** (each genuinely Read→Write/Edit→Bash-verified). Keys live in gitignored
  `envs/agent-sdk-models-dev.env`. Per-vendor routing resolved from pi's catalog + vendor docs (M3:
  `api.minimax.io/anthropic`; Kimi: `api.kimi.com/coding/`; GLM: `api.z.ai/api/anthropic` via Claude
  tier-lane mapping). Full pipeline: spec→panel→plan→panel→codex-impl→tri-review→run, then a delta
  re-review.
- **Why:** PATH 2 (deferred in `docs/decisions/m3-judgment-seat.md`) is the future *mutation* harness —
  agent-sdk = the Claude Code harness, stronger than pi for write-heavy work. The probe retires the
  unproven risk (M3's read-only probe never proved *write* tool-use) and green-lights an `agent-sdk`
  engine build-spike. The verifier is **un-cheatable-by-construction**: the held-out oracle ships only
  `sha256(expected)` so the process under test can't read the answers off disk (delta-review P0, agy;
  recorded as disagreement-corpus DC-004). Reusable for when the engine is built. Design/plan:
  `docs/superpowers/specs|plans/2026-06-18-agent-sdk-mutation-probe-*.md`; results:
  `docs/agent-sdk-probe-results.md`.

## 2026-06-18 — read-only launch gate: close the BRIDGE_PI_TOOLS fail-open

### `enforce_readonly_tool_surface` — a seat declared read-only refuses to serve if its surface isn't

- **What:** new `src/agent_redis_bridge/readonly_gate.py` + wiring in `Bridge.run()` (before
  `register()`). When `ARB_REQUIRE_READONLY_TOOLS=<csv>` is set, the bridge refuses to register/serve
  unless the effective pi `--pi-tools` surface is **non-empty and a subset** of that allowlist;
  otherwise it raises → `main()` prints `[bridge-error]` and exits 1 (launchd `KeepAlive` crash-loops,
  visibly refusing to serve). Opt-in per seat; applied to the M3 oracle plist. `Bridge.__init__` also
  now resolves `pi_tools` **and** the gate marker through the env-file (CLI/process-env > env-file),
  written back onto `args` — without this an env-file-configured `BRIDGE_PI_TOOLS` (the documented
  `.env.pi-dev.example` read-only-seat shape) reached neither the engine (full-tool fallback) nor the
  gate (a pre-existing latent fail-open). Verified: 17 unit + Bridge-integration tests, full suite
  324 OK, attack-verified via BOTH the process-env and env-file config paths.
- **Why:** the M3 judgment oracle is read-only by *tool absence* (`--pi-tools read,grep,find,ls`), but
  that posture had a verified **fail-open**: drop `BRIDGE_PI_TOOLS` and `pi_sdk` silently falls back to
  pi's DEFAULT full toolset (read/bash/edit/write) — a read-only oracle would then serve write-capable,
  silently. A tri-model design panel (codex/agy/cold-Opus) unanimously identified this as *the* threat;
  the chosen fix is cold-Opus's launch gate (prevention at the root, fail-closed, portable) over a
  macOS `sandbox-exec` jail (deferred — it adds only speculative, non-oracle-specific RCE containment
  on an Apple-deprecated mechanism; revisit if pi-sdk RCE becomes credible or a non-deprecated sandbox
  primitive lands). The existing `pi_sdk.py` guard only caught a *non-empty* value that parsed empty
  (e.g. `","`); the unset/None case was uncovered. Gate semantics mirror `tools/seat_deny_proof` (the
  post-launch behavioural deny-proof) — this is the pre-serve counterpart.
- **Follow-up (deferred):** `pi_rpc.py:371` lacks the parse-empty guard pi-sdk has, so a degenerate
  `BRIDGE_PI_TOOLS=","` is truthy there yet pi ignores it (full toolset) — the launch gate catches it
  when active, but the engine's own guard should be aligned with pi-sdk's. Tracked, not done here
  (separate engine; the project-g-consult pi-rpc seats use it, so it warrants its own change + test cycle).

## 2026-06-17 — M3 judgment-tier bridge seat + deny-proof harness (merged from `feat/m3-judgment-seat` + `feat/m3-deny-proof-harness`)

### MiniMax-M3 stood up as the read-only judgment-tier oracle seat (PATH 1, pi) (`524bc1b`, `a229cbe`, `ee685e0`)

- **What:** added `roles/judgment-oracle.md` (a read-only [J]-class role profile scoping the seat to
  input-trust / authorization / PII-classification judgment) and the decision log
  `docs/decisions/m3-judgment-seat.md`. The seat is `pi-agentredisbridge-dev-minimax-m3`, launched
  `--engine pi-rpc --model minimax/MiniMax-M3 --role minimax-m3 --pi-tools read,grep,find,ls
  --role-profile-file roles/judgment-oracle.md` — read-only by tool-allowlist, certified by a live
  multi-vector deny-proof (5 write vectors unavailable, sentinels absent, surface unchanged).
- **Why:** the autonomous-mode oracle (merged `70f8ade`) requires a *decorrelated* judgment tier —
  a real non-quorum bridge seat outside the codex/agy/cold-Opus voting family — or its judgment-class
  posture checks *park* instead of *deliver*. This seat flips the host from `decorrelated-seat: no`
  to `yes`. PATH 2 (Anthropic Agent SDK driving M3) was explored and live-probed but **not built**:
  panel-unanimous it's dominated (same model + key → zero added decorrelation; read-only by *policy*,
  not *absence*) — reframed as the future *mutation* harness, not waste. Decision D3 (GO WITH PI)
  taken by the user. The agent-id convention `pi-<project>-<workspace>-<model>` (model via `--role`)
  prevents seat-id collisions when pi runs multiple models.

### Deny-proof classifier — reusable read-only certifier (`4c8bcd8`, `201d46c`)

- **What:** `tools/seat_deny_proof/` — a TDD classifier (`validate_tool_surface` + `classify_deny_proof`)
  that certifies a seat read-only from three independent signals: write-vector outcomes
  (refused/unavailable/succeeded), filesystem sentinel presence, and exclusive tool-surface match.
  14 tests green.
- **Why:** the D1 lesson — a prior attempt (`mini-agent-acp`) self-reported a refused write while the
  file was *actually created* — proved config-assumption is not acceptance. The classifier makes
  certification adjudicate filesystem truth over self-report, and (P0 fix, tri-model reviewed) treats
  an *empty* vector list as INCONCLUSIVE rather than PASS — zero write attempts is absence of evidence,
  not proof of read-only. Reusable for this and future judgment seats.

## 2026-06-17 — eval Instrument 1, P-3 fixture corpus (branch `feat/p3-fixture-corpus`, not yet merged)

### Repeat-pooling supersedes the under-T guard (decision panel B, unanimous) (`3d92665`)

- **What:** removed the hard under-T guard; repeats are now POOLED (the trial unit is the cluster,
  Wilson n = #clusters, not clusters × repeats; a cluster's outcome pooled conservatively across
  repeats). No hard gate — the wide small-n CI self-limits. Class-level *eligibility* now keys on
  distinct *mechanisms* (seed clusters; untagged = location, so duplicate locations and
  one-mechanism-many-locations collapse), not raw locations. `cluster_key` is location-defaulted and
  `tag:`/`loc:` namespaced.
- **Why:** a unanimous tri-model decision panel (re-run with full information; cold-Opus reversed its
  own earlier guard recommendation) found the guard required ≥T≈19 independent seed *mechanisms* for a
  class PASS — which posture classes (~5–12 mechanisms) don't have *in reality*, making them
  structurally un-PASSable on any fixture, ever (real bases don't fix it). Pooling closes the
  over-claim the same structural way the cluster fix does (count real independence, no estimated
  parameter) WITHOUT over-blocking: verified that the canonical one-mechanism/five-location over-claim
  stays UNKNOWN while a clean seat earns an honest class PASS on the existing synthetic fixtures
  (caught 5/5 lo≈0.57 > noise 0/11 hi≈0.26). This unblocks the first class-level milestone on the
  current corpus; real-base sourcing demotes to a coverage follow-on. ICC/design-effect deferred (it
  reintroduces a mis-estimable parameter). Panel detail: `docs/decisions/eval-p3-panel-log.md` Panel 5.

### Effective-N + symmetric under-T rail: CIs on *cluster* counts, both axes (`eb65dd6`, `804a940`, `be8f85c`, `7d4778a`)

- **Also (seed axis + the rail):** the same disease lives on the *caught* side — correlated seeds
  (one mechanism, many locations) inflate `caught_n` and, with repeats, manufacture a false class
  PASS. Fixed symmetrically via a shared `cluster_key` helper: `Seed.cluster` (= mechanism), caught
  CI on the seed-cluster count, a caught cluster "detected" only if ALL member seeds are (the
  anti-over-claim mirror of noise's any-flagged). And a **symmetric under-T rail**: if effective
  control **or** seed clusters < T, the verdict is forced to UNKNOWN — clustering alone is
  insufficient because repeats re-inflate the Wilson n past T on either axis. An execution-primary
  panel *constructed* the residual false PASS (5 locations of one mechanism) that the
  effective-N-controls-only cut left open; the rail closes it (regression-tested). Moving the
  class-level gate `I_min` from distinct locations → distinct mechanisms is now genuinely optional
  (the rail closes the hole) and remains a human fork. (Original entry below.)

### Effective-N: ν_s confidence interval on why-clean *cluster* count (`eb65dd6`, `804a940`)

- **What:** each `ControlLocus` now carries a `cluster` (its "why-clean" idiom). The viability
  oracle aggregates noise to the cluster — trial unit `(seat, class, repeat, cluster)`, a cluster
  "flagged" in a repeat if ANY member control is flagged — and computes ν_s's Wilson CI on the
  **cluster (effective) count**, not the nominal control count. The power budget's `>= T`
  requirement and the `plan`/run warnings are now measured against effective clusters; the cluster is
  written into the control `matcher_decision` event so the NDJSON stays authoritative. Singleton
  cluster keys are namespaced (`__id__:<id>`) so an explicit tag can never silently merge independent
  controls. Untagged scenarios are unchanged (each control its own singleton).
- **Why:** controls clean for the *same reason* are correlated, not independent samples; computing
  ν_s's CI on 19 controls that are effectively ~12 makes the interval falsely narrow → a false PASS.
  This is `measurement-principles.md` **P1 instance 5** — the oracle was blind to correlation along
  the why-clean dimension it didn't measure. The fix makes the suite *structurally* incapable of
  over-claiming via correlated controls, on any fixture: a real codebase can also have correlated
  controls, so more/realer fixtures reduce how often the hole bites but only cluster-based CI closes
  it. A 3-model panel (execution-primary) confirmed the control side sound and demonstrated it bites
  (same data: 19-in-1-cluster → UNKNOWN vs 19 singletons → PASS). Human decision: effective-N FIRST,
  then real-codebase bases; reject lowering the power target to fit the fixture. Panel detail in
  `docs/decisions/eval-p3-panel-log.md`.

### P-3 corpus: secrets-in-logs + correctness fixtures at full power (`821a787`, `1f78459`, `f76575d`)

- **What:** two full-power floor scenarios — secrets-in-logs (5 distinct leak mechanisms) and
  correctness (5 distinct logic-error kinds, deliberately non-security to test generalization), each
  with 19 control loci, built from marker-free source by deterministic builders + signature-resolving
  scenario generators (`tools/eval/fixtures/`). Also fixes a matcher P0 found by the corpus panel: the
  ±window fallback no longer crosses function boundaries (a false positive on a clean control within
  10 lines of a seed was being scored as a seed detection — a bug in the merged-main matcher).
- **Why:** P-1's live run proved the dispatch path on one seed (instance-level UNKNOWN); P-3 builds
  the multi-seed, multi-control corpus needed for an actual class-level PASS/FAIL. Scoped to three
  classes first to prove the verdict path before expanding the taxonomy. The corpus panel found the
  synthetic fixtures top out at ~11-13 *effective* control clusters (< T=19) — generalizing the authz
  "needs a real base" flag to all classes — which drove the effective-N fix and the decision to move
  the corpus to real-codebase bases. Synthetics retained as relabeled pipeline-validation scenarios.

## 2026-06-11

### Codex thread/fork exposure for tree-of-thought dispatch (`b93b7c2`)

- **What:** new request payload field `fork_from_thread_id` (CLI:
  `--fork-thread-id` on `agent-dispatch` and `ctl send`). Codex engines call App
  Server `thread/fork` before the first turn: the base thread is loaded from disk and
  forked into a NEW child thread; the turn runs on the child and the reply's
  `thread_id` is the child id, ready for further continuation (`thread_id`) or deeper
  branching (`fork_from_thread_id`). Codex-only; engines without a fork hook fail
  loudly (`thread-fork-unsupported`), fork errors fail before any turn
  (`thread-fork-failed`). Contradictory combinations (`thread_id`,
  `fresh_context: true`) are rejected at envelope validation; fork suppresses
  `--fresh-context-default`; fork + worktree is valid on codex (forks on the worktree
  engine).
- **Why:** the bridge could resume one codex conversation but never branch it, so
  exploratory orchestration ("run the base analysis once, then try A, B, and C in
  parallel from that exact context") required re-running the shared prefix N times —
  wasteful and divergence-prone. codex 0.130.0's app-server added `thread/fork`,
  making disk-cheap branching possible. Design fixed by a unanimous tri-model panel
  (codex/agy/cold-Opus): a separate field rather than a `fork: true` flag keeps the
  input (base id) and output (child id) dataflow unambiguous in tree-of-thought
  loops; the reply carries only the child id because the caller already knows the
  parent and App Server persists `forkedFromId` for lineage; all three consultants
  independently flagged that fork must suppress the daemon fresh-context default or
  the forked child would be reset away before its first turn.

### Engine affinity / warm-pinning for session-in-process engines (`591493e`)

- **What:** `payload.thread_id` now works on engines whose conversation lives inside a
  pooled engine process (gemini-acp, cursor-acp, grok-acp, pi-sdk, …): the pool routes
  the request to the live engine instance whose `thread_id`/`session_id` matches,
  scanning idle and busy engines under the pool lock. Failures are loud and distinct —
  `thread-affinity-miss` (owner evicted/restarted), `thread-affinity-busy task=<id>`
  (owner mid-turn; fail fast, caller retries), `thread-affinity-ambiguous` (duplicate
  id claims fail closed), and `thread-affinity-worktree-incompatible` (worktree turns
  run on a fresh single-use engine, so continuation is unsatisfiable). Codex keeps its
  instance-independent disk-resume path and never participates in affinity scanning.
- **Why:** multi-turn workflows only worked on codex; on every other engine a
  `thread_id` request failed `thread-continuation-unsupported` even when the engine
  holding the conversation was sitting idle in the pool, because `acquire()` popped
  LIFO with no notion of context ownership. SPEC had documented this gap explicitly
  ("there is no engine affinity"). Design fixed by a unanimous tri-model panel
  (codex/agy/cold-Opus, ADOPT-WITH-CHANGES): ownership is derived from live engine
  attributes rather than a side table that could go stale, there is no queueing (a
  blocking acquire would freeze the inbox loop and stall steer/cancel), and per the
  lenient-vs-loud rule a continuation that cannot be satisfied fails loudly rather
  than silently running fresh. Implicit warm context (no `thread_id`) remains
  best-effort, and session affinity cannot cross daemon boundaries.

### Registry self-heal + pid-guarded cleanup (`a3a4524`)

- **What:** the heartbeat loop now re-asserts the full `registry:<agent_id>` hash
  (with the daemon's original `registered_at`) on every beat instead of only
  refreshing the status key, and `cleanup()` deletes the status/registry keys only
  when their recorded pid still matches the exiting process.
- **Why:** a shutdown/startup race was permanently deleting registry keys on manual
  restarts. SIGTERM only sets the stop event; the old daemon stays blocked in
  `blmove` for up to `--blpop-timeout` (30s) before its deferred `cleanup()` runs —
  and that cleanup deleted keys *by name*, clobbering a successor that had already
  registered. The status key healed via heartbeat, but the registry hash was
  write-once, so it stayed gone and `agent-bridge-ping` reported
  `registry=missing` for every ad-hoc seat after the 2026-06-10 fleet restart.
  Launchd seats were immune because `kickstart -k` serializes old-exit → new-start,
  which is what isolated the cause. Either guard alone closes the hole; together a
  lost key now heals within one heartbeat interval and a dying predecessor can no
  longer delete a successor's registration.

## 2026-06-10

Five features landed on `main` (`bc0be11..8fa684d`) and were deployed to all live
daemons on this host the same evening. Test suite grew 211 → 269, all green. The first
three re-implement the ideas from the stale April branch `feat/phase-1-7-roles`
(deleted after extraction); the last two grew out of gaps found while porting.

### Docs routing and lifecycle cleanup

- Added `docs/index.json` as the machine-readable documentation routing layer and
  generated `docs/INDEX.md` from it. `scripts/check-doc-index` now enforces tracked
  Markdown coverage, closed `status`/`audience` values, existing paths, and generated
  index freshness.
- Split the old root orchestration document: current bridge operations quick reference
  moved into `docs/orchestrator-patterns.md`; historical Phase 1 / 1.5 / 1.6
  commissioning notes moved to `docs/archive/orchestration-phase-validation.md`.
- Standardized runbook placement by moving the Claude Code channel corruption runbook
  to `docs/runbooks/cc-channel-corruption.md` and adding
  `docs/runbooks/fleet-restart.md` for the 2026-06-10 restart lessons.
- Extracted README / `skills/using-agent-bridge/SKILL.md` verbatim atoms into
  `docs/fragments/` and added `scripts/check-doc-drift` to prevent recipe, override,
  and failure-shape drift.

### Structured replies (`6b675ac`, fixes `b9c0af7`)

- Request envelopes may set `payload.expect_structured: true` (CLI:
  `--expect-structured` on `agent-dispatch` and `ctl send`). The bridge appends a
  directive asking the worker to end its reply with a fenced JSON status block
  (`status` ∈ DONE / DONE_WITH_CONCERNS / BLOCKED / NEEDS_CONTEXT, plus optional
  `summary`, `concerns`, `next_steps`, `questions`, `artifacts`).
- The parsed, validated object is attached as `structured` on both the reply payload
  and the persisted `task:<id>:result` — orchestrators and scripts can branch on
  status instead of reading prose.
- Lenient by design: a missing/invalid block logs `structured-reply-parse-failed` and
  yields `structured: null`; it never fails the task. Extraction is end-anchored
  (candidates validated last-first) so replies that quote JSON earlier don't
  false-positive; optional fields are type-checked and dropped when mistyped.
- New module: `src/agent_redis_bridge/protocol.py`. Spec: `SPEC.md` payload shapes.

### Per-request fresh context (`1e47d76`, docs `2f2f4c8`)

- The engine pool reuses engines with conversation context intact; this makes the
  bleed controllable. `payload.fresh_context: true` (CLI: `--fresh-context`) resets
  the acquired engine's thread/session before the task's first turn; explicit `false`
  forces warm; absent defers to the new daemon flag `--fresh-context-default`
  (env: `AGENT_FRESH_CONTEXT_DEFAULT=1` via the systemd wrapper).
- `reset_context()` implemented for codex (new App Server thread) and gemini-acp
  (new session). Other engines log `fresh-context-unsupported` and run warm.
- The continuation loop and `steer` never re-reset (they depend on the live session).
- SPEC documents that warm reuse under the pool is best-effort in both directions
  (no engine affinity).

### Role-profile injection for all engines (`0a1a475`, fix `e8e1374`)

- `BRIDGE_ROLE_PROFILE_FILE` (or `--role-profile-file`) now reaches every engine:
  pi engines natively (as before, via `--append-system-prompt`); all others get the
  profile wrapped around each request's first-turn task in a `<system_guidance>`
  block. Engines that consume the env var natively are marked
  `consumes_role_profile = True` so they are never double-injected.
- Profile content lives in `roles/` (lead, reviewer, team-seat) — unchanged.
- Fix: restored dry-run-without-engine semantics regressed by the refactor.

### Thread continuation (`05ca11f`)

- `payload.thread_id` (CLI: `--thread-id`) resumes a prior codex App Server thread
  via `thread/resume` — threads are disk-backed, so any pooled engine process can
  resume any thread. Replies and persisted results now always carry `thread_id`
  (id or null) so callers can capture and continue.
- Loud failure modes (unanimous 3-model design panel): `thread_id` +
  `fresh_context: true` is rejected as `envelope-invalid contradictory-context`;
  an unsupported engine or a failed resume fails the task (`ok=false`) rather than
  silently running without the requested context. An explicit `thread_id` suppresses
  a daemon-level fresh default. Codex-only this round.

### Reliable inbox — shutdown envelope loss fixed (`b192f1e`, fixes `8fa684d`)

- Previously, SIGTERM/SIGKILL during the inbox BLPOP window could silently lose one
  in-flight request (observed live). The inbox consumer now uses
  `BLMOVE inbox → agent:<id>:processing` (server-side atomic), acknowledges with
  `LREM` after handling, and drains parked envelopes back to the inbox at startup
  (`recovered in-flight envelope id=...`).
- Delivery is now **at-least-once**: a daemon killed mid-turn re-runs the request on
  restart. Result keys are envelope-id-keyed so re-runs overwrite; side-effectful
  re-runs (worktree creation, orchestrator-commit) are NOT idempotent — see SPEC.
- Redis < 6.2 (no BLMOVE): warn-once fallback to the old BLPOP path.
- Live-verified: SIGKILL mid-turn parked the envelope; restart recovered and re-ran
  it; the still-waiting dispatcher received the full reply.

### Deployment & operational notes (2026-06-10)

- All 11 live daemons on this host restarted onto `8fa684d`. Three project-g seats are
  launchd-managed (`com.example.{codex,gemini,agy}-bridge.project-g-dev`) — restart those with
  `launchctl kickstart -k`, never kill+respawn (launchd resurrects, creating duplicate
  consumers racing one inbox).
- Before restarting shared seats, check EVERY `task:*:status` entry with
  `state=running` for a fresh `updated_at` — the bus hosts multiple orchestrator
  sessions and stale entries make sampling misleading.
- pi-sdk engine per-clone setup: run `tools/pi-sdk-host/install.sh` (symlinks the
  global pi package; not an npm install). The pi-sdk host is catalog-gated on models;
  providers without built-in catalog entries (e.g. minimax) need a
  `~/.pi/agent/models.json` entry per model.

### Known gaps

- `codex-bridge-dev` / `agy-bridge-dev` seats write no `registry:` key (pre-existing,
  cosmetic — affects `agent-bridge-ping` only; cause unidentified).
- `thread/fork` (conversation forking) exists in codex ≥ 0.130.0 but is not exposed.
- Engine affinity / warm-pinning under the pool: not implemented; warm context
  remains best-effort.

## 2026-06-19 — `bridge-protocol` skill (the build pipeline as an executable, fail-closed merge-gate)

### `skills/bridge-protocol/` — invokable pipeline contract + runnable gate (stdlib, 39 dogfood tests)

- **What:** the standard build workflow (design→panel→spec→panel→plan→panel→TDD→tri-review→merge-gate→merge)
  codified as an executable skill: a `gate.py` that consumes a `phase_input`, recomputes its own git ground
  truth, and emits a `gate_result` with fail-closed BLOCK conditions — the cheap-fake manifest rule
  (production-by-default classification + load-bearing-component manifest with dimension-preserving tests),
  correctness-basis transitions, trust-root coupling (logic-set tree hash, self-cert base case), and
  verified-vs-judged. The skill passes its OWN gate (dogfooded).
- **Why / how it was built:** spec through 3 review rounds + a confirm round; plan-panel caught a fatal
  self-referential hash-domain P0; the **build-review caught a fail-open the 31 green tests hid** (the gate
  treated its committed artifacts as optional caller params → skipped its own §5 checks under the documented
  call shape) — the cheap-fake lesson recurring at the build layer, and the gate catching its own build's
  violation one level up. Fixed in two rounds (wire evaluate() to self-load + recompute, fail-closed; close
  the multi-commit under-scope + the partial-injection carve-out) with integration tests that exercise REAL
  derivation (real git + on-disk artifacts) and assert block-FIRES, not just correct-block-given-input.
  Panel: codex (build) + cold-Opus + GLM-judge (review; codex non-certifying as author). Validated the
  GLM-5.2 read-only judge as a sharp decorrelated seat. Suite **39 tests**. Self-cert root pinned externally
  (certifying seats ≠ author). Known v-next gap recorded (§1.4a): gate enforcement of author-non-quorum.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
