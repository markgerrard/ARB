# Backlog

Tracked future work that isn't yet specced. Each item: what, why, pointers. Promote to a
`docs/superpowers/specs/` design when picked up (brainstorm → spec → plan → build).

## pi-sdk / pi-rpc degenerate-tools guard fails OPEN on a whitespace-only allowlist

> **SHIPPED 2026-08-03 (do NOT re-implement).** Both engines now use the shared
> `parse_tool_allowlist` in `engines/base.py`, whose condition is
> `csv not in (None, "")` — the superseded `csv and csv.strip()` form skipped a
> whitespace-only value. `tests/test_tool_allowlist_guard.py` asserts every
> degenerate form (`","`, `"   "`, `"\t"`, `" , , "`, `"\n"`, `",,,"`) against ALL
> THREE call sites (pi-sdk, pi-rpc, omp-acp) so they cannot drift apart again, and
> pins the bug itself: the old expression evaluates False for `"   "` while the new
> one evaluates True.
>
> **Wider than filed.** pi-rpc turned out to have had NO guard at all — it kept only
> the raw string, so `"   "` was passed through as `--tools "   "` AND satisfied the
> `if not self.pi_tools` policy check, i.e. the seat read as tool-restricted while pi
> fell back to its full toolset. It now parses once and guards on the parsed list,
> which is the fix pi-sdk received as a Tri-model P0 and pi-rpc never did.

## A permission ask could be granted by a LATER turn than the one it arrived in

> **SHIPPED 2026-08-03 (do NOT re-implement).** `TurnPolicyPermissionMixin` authorized at
> DEQUEUE time, so a `session/request_permission` that arrived during turn A but was still
> queued when turn A returned was answered under **turn B's** policy — a write the agent
> requested under `plan` mode could be approved by an unrelated later dispatch. Affected
> `omp-acp` and `opencode-acp` (every `GeminiAcpEngine` subclass using the mixin;
> that class was renamed `GenericAcpEngine` in `engines/generic_acp.py` on 2026-08-29).
> Demonstrated by test before fixing: the stale ask came back `selected`.
>
> **Why the session check did not catch it.** This family issues `session/new` once in
> `start()` and never retires, so `session_id` is byte-identical across turns; the bridge
> re-keys only via `reset_context`, which is conditional on `fresh_context_default` (False by
> default) or an explicit per-request flag.
>
> **Worth keeping — derived code that was weaker than its source.** The mixin is explicitly
> derived from grok-acp's reviewed fail-closed floor (GROK-1 v1.3 D2/D3b) and copied its
> ask-time checks faithfully. grok is nonetheless *not* exposed, for reasons living outside
> those checks: it retires after every turn, and a non-retiring grok seat rotates `session/new`
> per dispatch **precisely so the D3b session gate correlates**
> (`grok_acp._rotate_session_if_reused`). Porting a guard is not porting its preconditions —
> when lifting a reviewed control onto a new base, port the invariants it rests on, or verify
> the new base supplies them.
>
> Fix: `_cancel_stale_permission_asks()` at turn START, before the incoming policy goes live —
> a turn owns only the asks that arrive while it is running. Turn start rather than turn exit,
> so asks arriving BETWEEN turns are covered too. Non-permission traffic is put back in order.
> Pinned by `test_ask_left_over_from_a_previous_turn_cannot_ride_the_next_trusted_turn` and
> `test_stale_drain_preserves_non_permission_messages_in_order`; both mutation-verified.
> Found by grok-acp on panel `panel-omp-opencode-arc-20260803T125825Z-570c21` (filed P2 on a
> mitigating assumption that proved false; reclassified P1).

## ACP base has no child-liveness check during `initialize`

> **SHIPPED 2026-08-03 (do NOT re-implement) for the `GeminiAcpEngine` family
> — renamed `GenericAcpEngine` (`engines/generic_acp.py`) on 2026-08-29;
> grok-acp's share of this item was RE-OPENED and then shipped 2026-08-03 (see
> "Correction to the correction" below).** `GeminiAcpEngine` gained
> `_dead_child_error` / `_await_or_detect_death`, used by BOTH the `request()`
> handshake loop and the turn loop. Liveness is consulted ONLY when the message
> queue comes up empty, followed by a 0.5s grace drain, so a child that answers and
> then exits is still handled normally — `test_live_but_quiet_child_still_times_out`
> pins that ordinary slowness is not converted into a spurious death.
>
> **Correction to this item as originally filed:** it claimed six adapters were
> exposed. That was too broad — but the correction below shows it was not as narrow
> as this entry first claimed either.
>
> **Correction to the correction (2026-08-03, panel
> `panel-omp-opencode-arc-20260803T125825Z-570c21`).** This entry previously read
> *"`grok-acp`, `cursor-acp`, `devin-acp` and `cline-acp` carry their own ACP
> implementations and already check `poll()`"*. **That was false for `grok-acp`,
> and it was asserted rather than cited.** The check was drawn at ENGINE
> granularity ("does this engine call `poll()` anywhere?") instead of LOOP
> granularity ("does the loop this item names check it?"). grok checked only in its
> turn loop and in `_exhaust_deny_budget`; `grok_acp.request()` — which serves
> `initialize`, `session/new` and `session/set_mode` — had no liveness path at all,
> so the filed defect was still live on exactly the path the item is named for.
> Measured: a dead child took the full timeout and reported
> `initialize timed out after Ns`, versus ~2s with the fix.
>
> Accurate statement: **`cursor-acp`, `cline-acp` and `devin-acp` check in BOTH
> loops.** `grok-acp` checked only in its turn loop and has now been given the same
> `_last_chance_message_after_process_exit()` treatment as its siblings
> (`engines/grok_acp.py`, `GrokRequestLivenessTest` in `tests/test_grok_acp.py`).
>
> This is the cross-slice-claim defect class in `CLAUDE.md`: a claim about code the
> change does not own, asserted instead of cited. It is worth recording *how* it
> nearly became permanent — the false claim shipped under a **"do NOT re-implement"**
> banner, so the erroneous closure was itself the thing that would have prevented
> the remaining fix. A wrong claim inside a closure banner is more durable than a
> wrong claim anywhere else in this file.
>
> The turn loop was included beyond the filed scope because it is the same defect with
> a far larger cost: a child dying mid-turn previously burned the entire turn timeout
> (up to an hour) rather than ~5.5s. **Return contract corrected 2026-08-03 by the
> same panel:** that path originally *raised*, which discarded everything the turn
> had streamed and skipped the terminal progress event. It now returns
> `TurnResult(ok=False, result=<streamed prefix>)` like `cursor_acp`/`grok_acp` do.
>
> **Test-integrity note.** `test_reply_then_exit_is_not_treated_as_death` was cited
> here as pinning the answered-then-exited path. It does pin "message before
> liveness", but it does **not** pin the 0.5s grace drain: it preloads stdout, so the
> reader always finishes before liveness is consulted, and the test passes verbatim
> with the grace deleted (verified by mutation). `test_grace_drain_recovers_a_line_
> the_reader_flushes_after_the_exit` now covers the grace itself.

## Cold-Opus subagents can never get a run-id label in arb-watch

> **SHIPPED 2026-07-01 (do NOT re-implement / re-plan):** implemented exactly per
> `docs/superpowers/plans/2026-07-01-cold-opus-run-id-label.md` (single-task, TDD), merged to
> `dev` at `a46a5ae` ("merge(dev): cold-Opus run-id label + seat-name bridge parity"), and
> **E2E-verified live against the real daemon** per that merge commit's own message — the
> post-merge live-verification step the plan calls out (spawn a real cold-Opus subagent with an
> `[ARB_RUN:...]` marker, confirm `run_id` patches in `events:live` while `seat_id`/`orchestrator`
> stay untouched) was completed, not deferred. Current `tailer.py::_resolve_cold_identity`
> carries the patch; `tests/claude_tail/test_tailer.py` carries all three plan-specified tests
> (`test_locked_cold_identity_run_id_is_overridden_by_marker_but_seat_and_orchestrator_are_not`,
> `test_locked_cold_identity_without_marker_is_fully_unchanged`,
> `test_locked_cold_identity_marker_mid_paragraph_still_overrides_run_id`) — confirmed by direct
> read + a live `pytest tests/claude_tail/` run (124 passed) on 2026-07-06, correcting a handoff
> that had incorrectly still listed this as an open residual. Kept here only so a future session
> doesn't re-plan or re-build already-shipped work.

**What:** A cold-Opus reviewer subagent (Claude Code's native Agent/Task tool) always shows a
raw GUID (its `agent_id`) in arb-watch's Run column, never a meaningful label — unlike
bridge-dispatched seats (codex/agy-print/etc.), which can carry `--run-id`. The
`[ARB_RUN:<id> ARB_SEAT:<seat> ARB_ORCH:<orch>]` marker mechanism in
`src/agent_redis_bridge/claude_tail/identity.py` (`parse_marker`/`cold_identity`) exists to solve
exactly this but is dead code on the real path.

**Why:** `service.py:_discover_specs()` (lines ~144-167) constructs the cold seat's identity at
*discovery* time via `cold_identity(agent_id, session_id, "")` — an **empty** marker string,
before any transcript content is ever read. Because `SubagentStart` writes the sidecar
(`<agent_id>.arb-tail.json`) *before* creating the `.output` symlink, the sidecar is already
present by the time the daemon's poll loop discovers the new file, so `identity_locked=True` is
set on that same first pass. Once locked, `TranscriptTailer._resolve_cold_identity()` returns
immediately and never looks at the transcript again (`tailer.py`) — so no later marker in the
subagent's actual prompt/transcript can ever change `run_id` or `seat_id`. Confirmed empirically
2026-07-01: a live cold-Opus dispatch with `[ARB_RUN:panel-cold-opus-livetest-... ...]` embedded
as the very first line of its prompt still showed `run_id=<raw-agent-id>` on every `events:live`
entry.

**Pointers:** `src/agent_redis_bridge/claude_tail/service.py:144-167` (`_discover_specs`, the
empty-marker-text discovery call + sidecar-triggered lock), `identity.py` (`parse_marker`,
`cold_identity` — the mechanism that's currently unreachable),
`docs/superpowers/specs/2026-06-30-cold-opus-subagent-visibility-design.md` (the feature this
gap lives in). Design questions for a fix: where would a caller even inject a run-id for a
subagent spawned via the native `Agent` tool (no CLI flag exists for that path, unlike
`agent-dispatch`/`go-client`)? Likely needs either (a) a way to pass a run-id hint alongside the
subagent prompt that `SubagentStart` writes into the sidecar (sidecar already carries
`orchestrator`; extend it to carry `run_id` too, sourced from parsing the *prompt* at
`SubagentStart` time rather than the transcript), or (b) delaying the identity lock until the
first line of real transcript content is available. Ties `dispatch-run-id-discipline` memory
(the parallel gap for bridge seats, which the CLI enforcement now catches).

## ARB dispatch-queue — queue work to agents instead of bouncing

> **SHIPPED 2026-06-24 (do NOT re-implement):** capacity-gated BLPOP (Option B) — a busy seat now
> *queues* work instead of replying `bridge busy`. See CHANGELOG.md § "2026-06-24 — Bridge dispatch
> queue (capacity gating + control lane + FIFO)" (~line 647) and the converged design at
> `docs/superpowers/specs/2026-06-24-arb-dispatch-queue-design.md`. Kept here only so a future session
> doesn't re-plan or re-build already-shipped work.

## MCP OAuth token refresh — auto-renew connector sessions

**What:** Implement the OAuth **refresh-token** grant for the ARB Memory MCP-OAuth door so connector
sessions (claude.ai / Codex / ChatGPT) auto-renew expiring access tokens instead of forcing re-auth.

**Why:** Access tokens are short-lived; without a refresh flow, long-lived connectors break when the
token lapses and the user must re-authorize. Refresh keeps the public read-only door usable across
sessions without manual re-pairing.

**Pointers:** `docs/decisions/arb-memory-architecture.md` (Phase 3 public read-only MCP-OAuth door,
shipped + connector-canary GREEN), the `mcp_auth` schema / `src/arb_memory/mcp/` OAuth provider. Design
questions: refresh-token issuance + rotation; storage + revocation; expiry/refresh windows; whether DCR
(dynamic client registration) clients get refresh tokens; reuse-detection on a rotated refresh token.

## arb-watch observability — live-verify the built liveness + agy-text fixes (not a build item)

> **Status (verified against source 2026-06-30):** the two arb-watch gaps earlier filed as backlog are
> **already BUILT + MERGED** (all ancestors of current HEAD, dated 2026-06-28). What remains is
> **end-to-end live verification + an ops kickstart**, not new code. Recorded here so a future session
> doesn't re-build already-shipped work.

**What's done (do NOT re-implement):**
- **Turn-liveness heartbeat** — `e61ba0f` tees a periodic `turn_heartbeat` to `events:live` for any
  active-but-quiet turn (`bridge.py:_emit_turn_heartbeats`, ~548-587), so a seat blocked on the model
  API (e.g. agy-print) keeps a fresh `last_event_ts` instead of reading stale. The fd-leak that the
  prior handoff said "defeated" this is also fixed (`01376ef`, `contextlib.closing`). run_id→task_id
  fallback keeps un-tagged seats fresh too (`bridge.py:568-571`).
- **agy `model_text` / `model_thinking`** — `agy_print.py` now extracts and emits `model_thinking`
  (`7e50d1b`, protobuf field 20.3) and `model_text`, **plus** a turn-end fallback that tees the final
  reply text as `model_text` if none streamed (`agy_print.py:583-591`). The earlier "agy = tool-calls
  only" gap is closed in code.

**What's genuinely open (verify / ops, ranked):**
1. **Live-confirm in arb-watch** that agy's `model_text` (not just tool calls) actually renders, and
   that the turn-heartbeat keeps a quiet seat fresh — observe a real agy dispatch on the prod gateway
   (`https://arb-visibility.example.com`) or `events:live` directly.
2. **DONE 2026-07-06 — Kickstart the mac-mini launchd seats** onto current code: env-file change +
   kickstart landed in the `panel-pi-ext-hardening` run. The bridge/agy fleet runs on the mac-mini
   (launchd), NOT the arb-prod droplet (which is only the MCP door), so these fixes shipped by
   kickstarting seats, not by a droplet redeploy.
3. **Residual seat-tee DNS resolver flap (unconfirmed).** The fd-leak masqueraded as Errno-8/DNS and is
   fixed, but `[[seat-tee-dns-flap-root-cause]]` notes a *genuine* resolver flap may still exist
   separately. Watch for a lone Errno-8 that does NOT pair with "Too many open files" — that would be a
   real flap, not the fd leak. Open until ruled out by observation.

**Why:** so bursty/quiet engines (agy-print, cursor-acp, grok-acp during model-wait) read live-and-fresh
in arb-watch and agy's actual review prose is visible, not just its tool calls.

**Pointers:** `src/agent_redis_bridge/bridge.py:548-587` (turn-heartbeat); `src/agent_redis_bridge/engines/agy_print.py:320-347,583-591`
(model_text/thinking + fallback); memories `[[agy-print-heartbeat-followup]]`, `[[agy-tmux-streaming]]`
(Known-gap section), `[[agy-fd-leak-masquerades-as-dns]]`, `[[seat-tee-dns-flap-root-cause]]`,
`[[roster-run-id-gate]]`; fleet ops in `[[bridge-dev-fleet-launchd]]` (kickstart, not bootout, for
code-only changes).

## Per-dispatch seat re-roling — run any model under any role without standing up new seats

**What:** Let `agent-dispatch` carry a role-profile in the request envelope; on a `--fresh-context`
dispatch (which already re-threads), the bridge calls pi `thread/start` with that `appendSystemPrompt`,
re-roling the seat *for that dispatch* — no new seats, no restart.

**Why:** Today a seat's role is bound at **launch** (the bridge reads `BRIDGE_ROLE_PROFILE_FILE` once at
boot; `role_profile_for_turn` always returns `self.role_profile`; the envelope carries no role override —
verified 2026-06-24). So a judgment-oracle seat (M3/GLM, scoped to security-posture classes) can't serve
as a design/debug panelist without standing up a separate seat. Per-dispatch re-roling lets any model
serve any role on demand — e.g. add GLM/M3 to a design panel as a decorrelated seat.

**Pointers:** `bridge.py:165-174` (boot-time role load), `:1338-1343` (`role_profile_for_turn`); pi
`thread/start` accepts `appendSystemPrompt` per-thread (`host.mjs` / `pi_sdk.py:200`). Memory
`[[bridge-seat-role-bound-at-launch]]` records it as "achievable and small — a bridge-plumbing gap, not a
pi limit." Caveats: re-role needs a fresh thread (resets that dispatch's context — fine for stateless
panel calls); the read-only tool list + trusted-sender policy still apply; a full-tools pi seat refuses
non-trusted turns.

## ARB panel-to-panel sandboxed encrypted messaging (filed 2026-07-02, not specced)

**What:** A peer-to-peer addressed-message primitive letting one panel/review agent send a
sealed message that only one *specific other* named agent can decrypt — sandboxed from the rest
of a concurrent panel (e.g. other independent reviewers). Raised while generalizing ARB Messages
from Cloudflare-only minting to a generic privileged-action relay (Codex app on Mark's Mac
fulfilling *any* requested action — Cloudflare, DO managed-DB admin creds, DO API, or non-token
actions — for a remote coding agent, sealed request in, sealed reply out).

**Why:** Explicitly a *different* primitive from what ARB Messages already is, not an extension
of it — worth tracking separately rather than conflating. ARB Messages today is
*requester → one privileged fulfiller → reply addressed only to the requester's own identity*
(`agent_id` bound to the caller's own OAuth identity, never a target parameter). Panel sandboxing
needs *arbitrary sender → arbitrary named recipient*, which is a materially different addressing
model: every participant needs its own keypair (not just the requester), and the schema needs an
explicit recipient field (today there isn't one — the implicit "recipient" is always whoever
polls as the same `agent_id` that enqueued). The PyNaCl `SealedBox` sealing approach carries over
cleanly; the table/schema/tool surface does not.

This maps onto a real, already-documented gap: review independence today (CLAUDE.md § "Review
hygiene") is enforced by *convention* — "write reports outside the repo until all reviewers
finish" — not by encryption. A genuine sealed point-to-point channel would make that a structural
guarantee instead of a discipline one.

**Pointers:** `src/arb_messages/store.py` (`enqueue`/`claim`/`read_and_mark_delivered` — the
current requester-scoped addressing to generalize away from), `src/arb_messages/keys.py`
(PyNaCl `seal`/`unseal`, reusable as-is per-recipient), `docs/superpowers/specs/2026-07-02-arb-messages-design.md`
(the converged design this would diverge from, not extend), CLAUDE.md § "Independent review
stays independent" (the convention-level guarantee this could replace with a structural one),
`docs/multi-model-consensus.md` § "Review hygiene" (the incident that motivated the current
convention-based approach). Design questions for whoever specs this: does every panel seat need
a *persistent* registered keypair, or an ephemeral one minted per panel run; does the schema need
a `recipient_agent_id` column or a separate table entirely; how does this interact with the
existing bridge dispatch/inbox mechanism (`agent_scratch:agent:<id>:inbox`) rather than
duplicating it; is Postgres even the right store for something this short-lived and high-churn,
versus the existing Redis-backed dispatch bus.

## cursor-acp hardening pass — two P2 follow-ups (non-blocking, filed 2026-07-01)

Filed after the code-review round for `docs/superpowers/specs/2026-07-01-cursor-acp-hardening-design.md`
(merged `dev` — commit merges `b029cf7`). Panel verdict was unanimous SHIP WITH NITS, no P0/P1; these two
nits were judged worth tracking rather than blocking merge.

1. **`_set_fast_mode` failure leaves the operator blind to fast-toggle state.** When
   `session/set_config_option` errors, `cursor_acp.py`'s `_set_fast_mode` logs via
   `logging.getLogger("agent_redis_bridge.engines.cursor_acp").warning(...)` and continues — but nothing
   queryable records that the toggle didn't take (no instance flag, no bus event). An operator relying on
   default-off (or an explicit `--cursor-fast`) has no way to know the server rejected the call short of
   scraping logs. Two of three code reviewers (pi-GLM, cold-Opus) independently flagged this. Cheap fix:
   store the failure (`self._fast_set_failed = True` or the warning string) on the engine instance so a
   future health check can surface it — even if nothing reads it today.
2. ~~`engines/gemini_acp.py` (now `generic_acp.py`) / `grok_acp.py` `refusal`-omission~~ — **RESOLVED 2026-07-05:**
   `"refusal"` added to both failure sets (gemini-acp is dead as a seat but is the live BASE CLASS
   for kimi-code/mini-agent), with refusal tests in `test_{gemini,grok,kimi_code,mini_agent}_acp.py`,
   inject-revert-verified in both directions. The gemini-bridge launchd plist was removed 2026-07-04
   (engine hard-deprecated). Item 1 (`_set_fast_mode` blind spot) remains open.

## ARB Memory read-model export — one-way markdown vault for human browsing (filed 2026-07-05, from ARB-2026-07-OBSVAULT-R1)

> **Status (2026-07-06): SHIPPED to `dev`+`main` (do NOT re-implement).** Merged at `89e07f9`
> (`src/arb_memory/vault_export.py`, `scripts/arb-memory-vault-export`, `run_grants()` wiring,
> 12 tests) — codex TDD implementation, cold-Opus independent verification (1 P2 fixed:
> explicit `utf-8` encoding on file writes), full `tests/arb_memory/` suite green (522
> passed/1 unrelated skip). Spec:
> `docs/superpowers/specs/2026-07-06-arb-memory-read-model-export-design.md`. Plan:
> `docs/superpowers/plans/2026-07-06-arb-memory-read-model-export.md`.
>
> **Prod role standup: DONE 2026-07-06.** `arb_vault_export` role created in prod (via the ARB
> Messages relay to the Codex app, which holds the DigitalOcean admin credential — see
> `docs/superpowers/plans/2026-07-02-arb-messages-deployment-checklist.md`'s grants-drift
> incident note for a real bug found+fixed along the way), `apply_local_reader_grants` applied
> and privilege-shape-verified (SELECT-only on `hints`/`artefacts`, zero on sensitive tables),
> and the resulting credential end-to-end verified (connects as `arb_vault_export`, `SELECT`
> works, `INSERT` correctly denied). DSN stored at `~/.arb-memory-prod/vault-export.env` on the
> prod droplet (mode 600): `ARB_VAULT_EXPORT_DSN` + `ARB_VAULT_EXPORT_ROOT=/home/claude/arb-memory-vault`.
>
> **Code deploy: DONE 2026-07-06.** Pulled `main` (`1432792` → `f0fd594`), `docker compose
> build memory`, `up -d --force-recreate` on the 7 services sharing the `arb-memory:phase3`
> image (`cloudflared` untouched — separate image); all 8 services healthy afterward. **Real
> end-to-end proof against live prod data**: ran the exporter via
> `docker compose exec memory python3 -c 'from arb_memory.vault_export import main; main()'`
> with the provisioned env — **91 real artefacts exported successfully.**
>
> **Fixed along the way:** the stored `ARB_VAULT_EXPORT_DSN` was originally written in
> psycopg's space-separated `key=value` conninfo form (`make_conninfo`'s default), which broke
> plain shell `source` at the first unescaped space (silently truncated to just
> `dbname=arbmemory`). Rewritten as a proper percent-encoded `postgresql://` URI — shell-safe,
> no quoting fragility — using the same already-stored secret (no need to re-request Codex;
> delivery already held the correct value, only the `.env` shell-consumption format was wrong).
>
> **Real constraint discovered, left for the cron-wiring step:** the export directory
> (`ARB_VAULT_EXPORT_ROOT=/home/claude/arb-memory-vault`) only exists inside the `memory`
> container's ephemeral filesystem — no volume mount in `deploy/docker-compose.yml` for this
> path, so the 91 exported files will vanish on the next container recreate. Whoever wires the
> actual nightly cron must choose: (a) add a volume mount so the path persists on the host, or
> (b) run the exporter from a host-side Python venv (with `psycopg`/`pgvector` installed)
> instead of via `docker compose exec`, writing directly to a host path. Neither is done yet.
>
> **Graph edges: SHIPPED + DEPLOYED + LIVE-PROVEN 2026-07-06 (do NOT re-implement).** Spec
> `docs/superpowers/specs/2026-07-06-arb-memory-vault-graph-export-design.md` and plan
> `docs/superpowers/plans/2026-07-06-arb-memory-vault-graph-export.md` each 2-round
> panel-confirmed; codex TDD (3 commits, merged `2450fe0`), cold-Opus verified
> character-for-character; 533 tests green; deployed to prod at `37e27f4` and the rerun export
> matched the calibration predictions exactly: 91/91 files with `aliases`, 35 with
> `## References`, 73 with `## Related`, 306 wikilinks total (host copy at
> `/home/claude/arb-memory-vault` via `docker cp` pending the persistence decision).
>
> **Cron + persistence: DONE 2026-07-06.** Persistence resolved via option (a): env-interpolated
> host mount on the `memory` service (`deploy/docker-compose.yml`, commit `2681866`;
> `ARB_VAULT_EXPORT_HOST_DIR=/home/claude/arb-memory-vault` in prod's `deploy/.env`), mount
> verified via `docker inspect`. Nightly cron installed for user `claude` (03:30 UTC,
> `/home/claude/arb-memory-vault-export.sh` → `docker compose exec memory`, logging to
> `/home/claude/arb-memory-vault-export.log`), and the wrapper was run manually through the
> exact cron path: exports land on the host and survive container recreates. Install gotcha
> recorded: `crontab -l | grep -v` exits 1 on an empty crontab — guard with `|| true` under
> `pipefail`; and any `docker compose exec` inside a heredoc-fed remote script needs
> `< /dev/null` or it eats the script's stdin.
>
> **What's still genuinely open:** follow-ups filed separately — viewer choice (Quartz panel:
> prefer Python-native), "Graph-aware ARB Memory read tools", "ARB Wiki" (pilot DONE, see that
> section). Concept panel-reviewed
> 2026-07-05 as part of a 4-pattern adoption review of `claude-obsidian`
> (`ARB-2026-07-OBSVAULT-R1`) — 3-of-4 seats (cold-Opus, agy, codex) recommended ADAPT; pi-GLM's
> REJECT rested on a premise verified false post-panel (`src/arb_memory/visibility.py` serves
> live seat/task activity, not memory content). Design: round 1 (codex + agy-print + cold-Opus,
> independent) found 8 P1/P2 findings, no P0, all addressed; round 2 (targeted confirmation)
> unanimous approve/none-to-P2. Design reuses `apply_local_reader_grants` under a dedicated
> `arb_vault_export` role, hash-suffixed collision-free filenames, deterministic multi-hint
> resolution, and places the nightly cron on the MCP-host box (not the mac-mini, for
> reliability/credential-footprint reasons). Plan: round 1 found 2 cross-validated cosmetic
> findings (unquoted YAML frontmatter, vestigial `ORDER BY id`) plus one spec-wording fix, all
> addressed; round 2 unanimous approve/none, three seats. Three-task TDD plan (grants CLI
> wiring, `vault_export.py` module, CLI wrapper), 10+ tests specified. Next: dispatch to
> codex-bridge-dev-example for implementation (worktree, TDD, cold-Opus verify, merge dev+main — same
> protocol as every other build cycle in this repo).

**What:** a nightly, one-way export job projecting ARB Memory's artefacts/decisions into plain
markdown files, so a human can `grep`/browse accumulated ARB knowledge offline without an LLM
client in the loop. Distinct from ARB Visibility (arb-watch), which stays exactly as-is for live
seat/task observability — this fills a different, currently completely unserved need.

**Why:** no non-LLM-mediated way exists today to browse ARB's accumulated memory content; SSH+psql
is already discouraged for casual reads (memory `arb-memory-read-via-mcp`). All three ADAPT
reviewers independently estimated ~1 day of effort (nightly cron + a read-only DB role) — cheap
relative to the value, especially once the "we already have this" counter-argument is off the
table.

**Non-negotiable guardrails** (converged across all three ADAPT votes; hard preconditions, not
nice-to-haves):
- Exporter runs as a **read-scoped DB principal with no write grant** to Memory/Files — verified via
  a grant audit at deploy time, not assumed.
- The vault host holds **no ARB-write credential** at all.
- **Never** wire `/autoresearch`-style ingest tooling (or any future importer) at the vault path —
  the one change that would flip this from P2-advisory to a real Memory-plane trust-boundary issue
  requiring re-panel.
- A standing test asserting the exporter's grants stay read-only, so drift is caught rather than
  assumed away.

**Pointers:** `src/arb_memory/visibility.py` (the live-observability surface this does NOT
replace); `src/arb_memory/mcp/read_tools.py` (`memory_search`/`memory_get`/`memory_recent` — the
only current access path); `docs/multi-model-consensus.md` § Worked Example 2 (the panel-doctrine
lesson from the same review round — GLM's premise error here is itself an instance of the exact
failure class WE2 documents: an unverified claim about the CURRENT system tipping a verdict).
Design questions for whoever specs this: markdown export schema (one file per artefact vs. per
topic); whether frontmatter carries provenance (source artefact ID, export timestamp) for
staleness detection; cadence (nightly vs. on-write); and whether the read-only DB role should be
shared with or kept separate from the existing MCP read-only role.

## ARB Messages — scope agent_id per session/project instead of the shared OAuth identity (filed 2026-07-05)

> **Status:** the immediate symptom (registration always failing for any session after the first)
> is FIXED — rotate-on-register shipped in `src/arb_messages/keys.py::register_key` (revoke the
> prior live row + insert the new one, atomically via `conn.transaction()`), tests updated
> (`tests/arb_messages/test_keys.py`), 83/83 `arb_messages` tests pass. This item is the deeper,
> not-yet-built fix the incident also exposed — filed per Mark's explicit request, not yet specced.

**What:** `agent_id` for ARB Messages is derived entirely from the OAuth connector's access-token
identity (`src/arb_messages/mcp/door_tools.py::_actor`) — there's no way for a caller to scope it
per-project or per-session. Every concurrent Claude Code session authenticated under the same
underlying connector gets the *identical* `agent_id`, so they all share one row in
`arb_agent_keys` (enforced one-live-key-per-agent_id by `arb_agent_keys_one_live_key_idx`).

**Why:** the rotate-on-register fix makes registration always *succeed*, but it doesn't remove the
collision — it just makes the most-recently-registered session's key win, silently invalidating
whatever session registered before it. A session that registered a key, then didn't touch ARB
Messages for a while, can have its decrypt capability pulled out from under it with zero
notification the next time a sibling session (different project, different host, same connector
identity) calls `messages_register_key`. Scoping `agent_id` per session/project would let
concurrent sessions hold independent keys with no collision at all — the actual root cause, per
the incident writeup (ARB Memory `art-944f558d412df42b`, 2026-07-05, project-d session).

**Pointers:** `src/arb_messages/mcp/door_tools.py::_actor` (the sole identity-derivation point —
whatever scoping mechanism gets added has to compose with or replace this); `src/arb_messages/keys.py`
(`register_key`/`live_key`, both currently keyed purely on `agent_id`); `src/arb_messages/store.py`
(uses `agent_id` throughout for request/delivery addressing — any compound-identity change has a
blast radius here, not just in `keys.py`). Design questions for whoever specs this: does the client
supply a scope label (e.g. project name) that gets composed into a compound `agent_id`, or does the
door derive scope some other way (e.g. from the MCP client's declared `cwd`/project metadata, if
available at the transport layer); does this require a migration for existing `arb_agent_keys` rows
(their `agent_id` values would need reinterpreting under a new compound scheme); and whether
`arb_messages`' request/delivery routing (which also keys everything on `agent_id`) needs the same
scoping or can stay coarse-grained while only key custody gets finer-grained.

## ARB Memory — full RFC 8628 device-code login (filed 2026-07-05)

> **Status:** the simpler variant — an out-of-band code-DISPLAY flow (register with the sentinel
> `urn:ietf:wg:oauth:2.0:oob` redirect_uri, login-success page shows the code instead of
> redirecting) — is SHIPPED (`src/arb_memory/mcp/redirect_policy.py`, `login.py`). This item is the
> fuller, not-yet-built alternative: real polling-based device-code, no copy-paste required.

**What:** proper RFC 8628 Device Authorization Grant — a client calls a new
`/device_authorization` endpoint, gets back a `device_code` + short human-typeable `user_code` +
`verification_uri`, shows the user "go to X, enter code ABC-123", and polls a token endpoint until
the user completes verification elsewhere (any device, not necessarily the requesting one).

**Why not built now:** the installed MCP Python SDK (`mcp.server.auth`) only implements the
standard `authorization_code`/`refresh_token` grants — its token-endpoint request model is a
Pydantic discriminated union hardcoded to exactly those two `Literal` grant types
(`mcp/server/auth/handlers/token.py`). Device-code can't be expressed inside that abstraction; it
needs a genuinely new, parallel HTTP surface sitting in front of (not inside) the SDK's routing.
Chosen instead: the OOB code-display variant above, which reuses the existing
authorization_code+PKCE+login flow entirely and only changes how the code is delivered (rendered
vs. redirected) — see the commit for the full comparison of effort between the two.

**Pointers:** `src/arb_memory/mcp/oauth.py` (`ArbMemoryOAuthProvider` — the SDK-abstraction class
device-code would need to sit alongside, not inside), `login.py` (`login_routes` — the
passphrase+TOTP page a device-code verification step would reuse), `oauth_store.py`
(`put_access_token`/`put_refresh_token` — the minting helpers a device-code token endpoint would
call once approved, same as `exchange_authorization_code` does today), `redirect_policy.py`
(`OOB_REDIRECT_URI` — the simpler variant already shipped, useful reference for the design). Design
questions for whoever specs this: a new DB table for device_code/user_code state (pending/
confirmed/denied), the poll-response semantics (`authorization_pending`/`slow_down` per RFC 8628),
user_code alphabet/length (short, human-typeable, avoiding ambiguous characters), and whether the
new `/device_authorization` + polling `/token` routes are added as Starlette routes ahead of the
SDK's own routing (intercepting the device-code grant_type before it reaches the SDK's fixed
two-grant handler) or via some other integration point in the `mcp` package.

## ARB Wiki — openwiki-inspired repo→wiki generation into ARB Memory (filed 2026-07-06)

> **PILOT DONE 2026-07-06 — the whole pipeline is proven end-to-end.** codex (read-only adhoc
> dispatch) generated 5 wiki pages from the real <workspace> source (`wiki-workspace-dev-overview`,
> `-dispatch-protocol`, `-engines`, `-arb-memory-plane`, `-observability`; 300–500 words each,
> real file paths cited, sibling cross-references as backticked ids per the graph-spec
> boundary rule); stored to prod ARB Memory via `memory_store` with explicit artefact ids
> (auto-indexed → embeddings); export rerun through the nightly cron wrapper → **96 files,
> 351 wikilinks**. The wiki cluster came out fully connected: every page's `## References`
> links its cited siblings, `## Related` connects the cluster at distances 0.17–0.22, AND the
> cluster integrated **bidirectionally** with the pre-existing corpus purely via embeddings —
> 7 pre-existing artefacts gained semantic edges into wiki pages, and the overview page picked
> up an existing artefact at 0.31. What the pilot deliberately did NOT build (the remaining
> feature): the generation *loop* — refresh-on-change detection, page-set curation per repo,
> which seat/model generates at what cadence, and versioned re-store on refresh. Pilot
> artifacts: brief + pages in the 2026-07-06 session scratchpad (throwaway); the stored
> artefacts and their graph are the durable output.

**What:** generate and maintain wiki-style documentation pages for the repos ARB works in —
taking inspiration (selected mechanisms, not wholesale adoption) from
[`langchain-ai/openwiki`](https://github.com/langchain-ai/openwiki), an LLM-agent CLI that
writes/refreshes a markdown `openwiki/` docs directory from a codebase (TypeScript,
provider-agnostic, one-shot `-p` mode, GH-Actions daily-refresh pattern). The ARB shape:
generation runs as ARB work (bridge seats / agents), pages are **stored as ARB Memory
artefacts** through the existing single-writer path, and the vault graph exporter
(`docs/superpowers/specs/2026-07-06-arb-memory-vault-graph-export-design.md`) emits them into
the browsable vault with cross-references materialized as wikilinks.

**Why:** Mark's steer (2026-07-06, during the vault-graph design): the goal is a genuinely
useful knowledge graph of how reviews, specs, essays, and repo documentation relate — the
wiki-generation side supplies dense, well-linked content; the exporter supplies the graph;
the viewer is a swappable rendering choice.

**Boundary conditions already settled by the vault-graph spec (§ Pipeline context):**
generated pages cite other artefacts by backticked id (or stem-targeted `[[stem|id]]` links) —
never bare `[[artefact-id]]` (Obsidian doesn't resolve alias-only links); ingestion goes
repo → ARB Memory via the write path, **never** via the vault directory (base-spec guardrail
#3: the vault stays one-way outbound; wiring any importer at the vault path requires a
re-panel).

**Pointers:** `src/arb_memory/vault_export.py` (output half, shipped);
`docs/superpowers/specs/2026-07-06-arb-memory-vault-graph-export-design.md` (graph edges, in
panel); `src/arb_memory/store.py::write_artefact_and_hints` + the bus write path (where pages
would land); openwiki repo for the generation-loop ideas (agent-driven page authoring,
refresh-on-change, AGENTS.md/CLAUDE.md pointer injection). Design questions for whoever specs
this: page granularity and artefact-id naming scheme (per-repo prefix? `wiki-<repo>-<page>`);
refresh cadence and change detection (openwiki's daily-PR pattern vs ARB's dispatch model);
which model/seat generates (cheap implementor lane vs frontier); and how page updates
interact with artefact versioning (new version per refresh — fits the existing
latest-version-only export).

## Graph-aware ARB Memory read tools — memory_related / memory_references (filed 2026-07-06)

> **SHIPPED + PROD-DEPLOYED 2026-07-16 (do NOT re-implement) — merged to dev `a404d47`, pushed `f3ecfea`.**
> Spec `docs/superpowers/specs/2026-07-16-graph-aware-memory-read-tools-design.md` (5 design
> rounds, r4 unanimous) and plan `docs/superpowers/plans/2026-07-16-graph-aware-memory-read-tools.md`
> (3 rounds); luna@high TDD implementation (8 commits), impl-review r0 approve 0 P0/P1, e2e
> live gate PASS over the real MCP wire (both doors). Shared logic hoisted to
> `src/arb_memory/graph.py`; both gates green post-merge (no-DB 33 passed 0 skipped,
> `scripts/graph-sql-gate` 40 passed). Prod deploy executed same day (Mark-authorized):
> `arb-prod` pulled `fe4a5ac`, `memory` image rebuilt, stack recreated, both tools confirmed
> registered on the live prod door (`build_server().list_tools()`), public door healthy
> (Claude-UA → 401); `tools/pi-sdk-host/install.sh` re-run (symlinks only, NO seat restarts);
> no connector reconnect needed (no new scope). Remaining: codex-sol P2 follow-up on
> graph-sql-gate matrix coverage (`%`-id backlink, NULL-content backlink candidate, real
> v2-write retirement path).

**What:** extend the read MCP surface (local read MCP + connector door) with graph queries over
the same relationships the vault graph exporter materializes: `memory_related(artefact_id)`
(E2 — pgvector min-pairwise-hint similarity, top-k under threshold) and
`memory_references(artefact_id)` (E1 — explicit textual citations, both directions:
outgoing mentions and backlinks). Computed on demand with the exporter's exact SQL/matching
logic (`src/arb_memory/vault_export.py` once the graph feature merges) — no new tables, no new
privileges, same read-only tables (`hints`+`artefacts`).

**Why:** Mark's question (2026-07-06): "how do we make it so models can query it?" The graph
currently materializes only in exported markdown — coding agents with a synced vault directory
can follow wikilinks as files (the openwiki model, maximally accessible for repo-resident
seats), but every other consumer (claude.ai, pi-sdk seats via the Node MCP-client bridge,
agent-sdk seats via mediation) reads ARB Memory through the MCP tools, which are blind to
edges. Adding these two tools makes recall graph-aware — search finds an entry point, then the
model walks edges instead of composing more searches. psql stays ops-only per
`arb-memory-read-via-mcp` and structural-containment: seats get read tools, never DSNs.

**Pointers:** `src/arb_memory/mcp/read_tools.py` (ReadMemoryTools — where the two methods
land), `src/arb_memory/mcp/local_server.py` + `server.py` (tool registration, both doors),
`src/arb_memory/vault_export.py` (`_related_artefacts` + `_reference_targets` — reuse, don't
duplicate: consider hoisting shared logic into `store.py`), `tests/arb_memory/`
(read-tool test conventions). Design questions: rate limiting (reuse `_check_search_allowed`'s
pattern?); whether `memory_references` scans bodies on demand (O(corpus) per call) or the
export tees edges somewhere cheap; pi-sdk/agent-sdk wrapper propagation (re-run
pi-sdk-host/install.sh per `pi-sdk-mcp-client-coverage`).

## AGY-4 — agy-tmux dark-channel stall analogue (PARKED 2026-07-09 with a standup gate)

> **STANDUP GATE (Mark, 2026-07-09): no agy-tmux seat may enter service until this ships.**
> Parked, not dropped: zero agy-tmux seats run today, so every line of this fix is inert, and
> agy-print is already fully covered by AGY-2 (blind-until-proven, shipped + live-gated
> 2026-07-08). Whoever stands up the first agy-tmux seat must build this FIRST, then run the
> deferred live gate (below) before the seat takes real work.

**Label collision, read carefully:** the engine-seat audit
(`docs/superpowers/reviews/2026-07-07-arb-engine-seat-audit.md:173`) uses "AGY-4" for a
*different* finding (P2, `Popen(text=True)` encoding hygiene, grouped under IMP-8). THIS item is
the agy-tmux follow-up that the AGY-2 spec (`2026-07-08-agy2-dark-channel-design.md`, § B′ and
§ Non-goals) also calls "AGY-4". Same name, unrelated work.

**What:** agy-tmux's only mid-turn progress source is the `transcript.jsonl` tail
(`engines/agy_tmux.py::tail_transcript`/`_map_line`) — same class as agy-print's AGY-2 gap: a
dark channel makes every healthy turn past `BRIDGE_STALL_AFTER_SECS` fire a false
`stall_detected`. Fix = (1) add `"agy-tmux"` to `BLIND_UNTIL_PROGRESS` (`bridge.py:49`) — the
structural correctness core, blind-arming is engine-name-keyed at `bridge.py::_start_stall_watch`
so the whole AGY-2 mechanism (blind default, `stall_unknown`, `progress_blind`, startup config
warning) comes free; (2) drift legibility — `_map_line` today drops non-JSON lines, unknown
shapes, and empty model content silently (no latch, no warning, unlike agy-print's D3), so a
stale parser after an Antigravity format change is invisible.

**Scoping already decided (Mark, 2026-07-09 brainstorm — don't re-litigate):**
- Scope = blind conformance + drift legibility. Full AGY-2 parity (env-wire `brain_root`,
  `progress_channel` re-blind plumbing) explicitly declined as YAGNI at parking time.
- Verification = hermetic tests only at build time; the AGY-2-style live temp-seat wedge gate is
  DEFERRED to first real seat standup (that's part of the gate above).
- Still open at pickup: drift warning engine-local only (one `logger.warning` per turn when
  non-ignored transcript lines flow but zero events map) vs. also emitting `progress_channel
  dark` with a new closed-enum reason (`transcript-drift`) for visibility-plane refinement.
  Leaning engine-local; decide at spec time.

**Analysis already done (2026-07-09, verified against source — reusable):** agy-tmux's dark-state
taxonomy differs from agy-print's. Pre-bind failures are LOUD (missing/wrong `brain_root`,
transcript never appearing, ambiguous nonce → `EngineError` at `startup_timeout_s`; turn fails,
no false stall) — so no D2a/D2b analogue exists. Post-bind, the only realistic dark state is
parser drift, which arrives with an agy version bump BETWEEN turns — so a drifted turn is dark
from line one and blind-by-default already covers it; there is no mid-turn dark *transition*
(no capture flag, no disable latch), which is why the AGY-2 re-blind machinery has nothing to
trigger it here. Drift-detection condition needs care: the transcript always contains ≥1
legitimately-ignored line (the task prompt echoed as `USER_INPUT`, in `TOOL_IGNORE_TYPES`), so
"line consumed, nothing mapped" is normal at turn start.

**Pointers:** `bridge.py:49` (`BLIND_UNTIL_PROGRESS` + the follow-up comment), `bridge.py:1919`
(`_start_stall_watch`, blind arming), `engines/agy_tmux.py:151-250` (`tail_transcript`/`_map_line`
— the silent drops), `docs/superpowers/specs/2026-07-08-agy2-dark-channel-design.md` (mechanism +
§ Non-goals naming this follow-up), memories `agy2-blind-until-proven-design`,
`agy4-agy-tmux-parked-standup-gate`. Pipeline when picked up: brainstorm → spec → design panel
(AGY-2 precedent: certify quorum codex + pi-GLM + agy-print, cold-Opus non-certifying) → TDD →
hermetic tests → live gate at standup.

## ARB Wiki v1.1 — zero-touch repo onboarding (filed 2026-07-06, Mark's explicit steer: no human step)

> **v1.1 SHIPPED + DEPLOYED + LIVE-PROVEN 2026-07-06 (do NOT re-implement).** Spec + plan each
> 2-round panel-confirmed; codex-TDD'd, cold-Opus-verified; merged dev+main (loop `d5f9432`,
> then three live-caught glue fixes `41ff154`/`3fe88cc`/`da00af0`); deployed. **Live `--add`
> proof against `/Users/<user>/AgentRedisBridge`:** the full pipeline ran — discovery (codex
> proposed 7 pages) → config write → generation → **decorrelated agy-print review** — and the
> review gate **correctly REQUEST-CHANGES'd**, catching genuine factual errors in codex's
> output (it read the real `envelope.py` and flagged that the protocol page invented envelope
> keys `sender`/`recipient`/`timestamp` for the real `from`/`to`/`sent_at`, and a page
> claiming `gemini-acp` operational when deprecated). Store correctly aborted — 0
> agentredisbridge artefacts in prod. **The gate works with teeth: a decorrelated seat kept
> factually-wrong content out of the graph — the exact safety property, demonstrated live.**
> Live verification earned its keep by catching THREE glue bugs, all in the CLI "prose
> carve-out" the plan panels flagged as untested (dispatch-envelope unwrap; reviewer target
> `agy-bridge-dev` not derived `agy-print-bridge-dev`; verdict searched not `startswith`) —
> each fixed with a pinning test. See memory `live-verification-catches-cli-glue`.
>
> **v1.2 wrinkle FIXED 2026-07-07:** `add_repo` writes the config block BEFORE `refresh_repo`
> runs, so a review-REJECTED `--add` used to leave the repo *configured* but *unstored* (the
> rejected agentredisbridge entry was manually reverted; a later plain review-off refresh
> would have landed the flagged content). Now `rollback_add()` compensates: any first-refresh
> failure of the just-added repo removes its config entry — unless `pending-<repo>.json`
> exists (batch durable, possibly partially stored; resume needs the entry). `--add` is
> atomic: configured *and* stored, or neither. Pinned by the suite's first `main()`-level
> integration tests (the untested-CLI-glue lesson applied).
>
> **Revise-and-resubmit ADDED 2026-07-07 (same session):** review rejections now feed the
> reviewer's reasons back into a bounded revision dispatch (`--max-revisions`, default 1) —
> revise in place, re-validate, re-review with an unchanged reviewer prompt (the reviewer
> never knows it's round 2; anchoring it with "feedback addressed" would soften the gate).
> Converts the common rejection case (actionable factual reasons, competent generator) into
> approved content with zero human steps.



> **v1 (the generation loop) SHIPPED + DEPLOYED + LIVE-PROVEN 2026-07-06 (do NOT re-implement).**
> Merged `451f567`, dev+main `5e9aa35`; 559 tests green; prod redeployed at `5e9aa35` with the
> `upsert_artefact` latest-version-dedup fix applied (constraint drop verified gone). Live
> end-to-end run: `scripts/arb-wiki-refresh --repo workspace-dev --force` → codex generated 5
> pages → validation passed → "enqueued 6 intents" via the ssh hop → all five pages versioned
> to v2 in prod, `wiki-workspace-dev-manifest` v1 created, vault re-export shows the full cluster.
> v1.1 below is the next increment.

**What:** `arb-wiki-refresh --add <repo-path>` makes a virgin repo fully automatic: a
**discovery dispatch** (seat reads the repo, proposes the page set as structured JSON —
repo name + 3–8 pages with `wiki-<repo>-<page>` ids/titles/scopes, openwiki-style), the loop
**writes the config block itself** (config stays the storage format, stops being
hand-authored; proposals structurally validated — id charset, global uniqueness across all
configured repos), then the normal v1 refresh runs. Optional `--reviewed` gate for virgin
repos: one decorrelated reviewer-seat dispatch sanity-checks the first generation for factual
howlers before the store — replaces the "human eyeballs the first page set" caveat with the
ARB pattern itself.

**Why:** Mark (2026-07-06): "I don't want a human step." The only irreducible input is the
repo path; page-set curation and first-generation quality checking are both seat-shaped jobs.
Sequenced AFTER the v1 generation loop merges (in flight at filing time) — discovery drives
v1's config/refresh core unchanged, so nothing in v1 is wasted or blocked.

**Pointers:** `docs/superpowers/specs/2026-07-06-arb-wiki-generation-loop-design.md` (v1; its
"page-set auto-discovery" non-goal is exactly this item), `src/agent_redis_bridge/wiki_refresh.py`
(once merged: `load_config`/`all_page_ids` validation to reuse for proposals). Design
questions: discovery brief shape + JSON contract; whether `--add` also commits the config
change (config is a tracked file — probably yes, warm-seat commit); page-set *evolution* for
already-onboarded repos (re-discovery cadence vs manual re-init); reviewer-gate default
(on for virgin repos, off for refreshes?).

## GROK-1 — adapter-side ACP permission handling for grok-acp (filed 2026-07-10)

**STATUS: CLOSED 2026-07-10 — shipped dev `635c398`, V5 live gate GREEN (edit-tool
out-of-cwd write executed + `end_turn` on restarted grok-bridge-dev), skill rules
relaxed (`9a7bf4f`), ARB artefact corrected (`art-5777b7eb0afbad16`). V5b remains
gated on any opt-out seat standup.** Design:
`docs/superpowers/specs/2026-07-10-grok1-acp-permission-handling-design.md`.

**Root cause (probe-verified 2026-07-10, controlled A/B):** the adapter answered
`session/request_permission` with `{"outcome": {"outcome": "approved"}}` — not a
valid ACP outcome — so grok treated the reply as non-acceptance: the operation
never executed and the turn died. The `worker quit with fatal: ...
Auth(AuthorizationRequired)` stderr line is BENIGN (present on successful runs);
the original dead-worker attribution here was wrong. Out-of-cwd READS never ask
at all. No bypass layer exists (`--always-approve`, yolo mode, `allow_always`
grants — all probed inert over ACP). Evidence:
`docs/superpowers/probes/2026-07-10-grok1-v1-probe/` (runs A–I).

**Remaining to close GROK-1:** orchestrator-run V5 live gate (restart
grok-bridge-dev onto the new code; out-of-cwd write task must show the
trusted-allow callback fired + file written + `end_turn`), then relax the
cwd-only/inline grok brief rules in `skills/using-agent-bridge` and correct ARB
Memory `art-d893502c280b1740`. V5b (opt-out isolation gate) only if an opt-out
seat is ever stood up.

## CT-1 — claude-tail must fail loud on live-bus emit failure (filed 2026-07-10; CLOSED 2026-07-11)

**CLOSED — shipped dev `58b7794`, live gate GREEN 2026-07-11 ~07:15.** Full pipeline:
design v1.6 (6 panel rounds, operator-closed at r6 with the last P1 folded) → plan rev 2
(panelled, 5 embedded-test contradictions folded) → codex-luna@high 10 tasks + fix commit,
per-task cold-Opus gates, all first-pass → tri-model final review (certify agy +
cold-Opus approve; terra's reproduced P1 — os.path.exists masking PermissionError as
"missing" — reconciled in `03a85f8`) → merged, daemon bootstrapped, live gate: heartbeat
8-field payload fresh with tailers=1; tee_states reduction fresh; black-hole live bus →
rc=1 in 4s (the incident's exact exception class now crashes instead of zombie-ing);
events flowing; rotating log wired; 54MB stderr archived+removed. Spec:
`docs/superpowers/specs/2026-07-11-ct1-claude-tail-fail-loud-design.md`.
**Residuals:** prod visibility-gateway deploy (merged `tee_states`/route/chip +
`ARB_VIS_EXPECTED_TEES=claude-tail.bridge-dev` env) — the dev-side reduction is proven,
the prod chip needs the normal prod deploy; V5b-style re-check on grok binary upgrades
unrelated. Original filing below for the record.
**→ Promoted 2026-07-16 to its own open item: § "VIS-1 — tee staleness chip is INERT on prod
until ARB_VIS_EXPECTED_TEES is set". Track it there, not here** — this residual sat unactioned
for five days because a live deploy step was buried under a CLOSED heading.

**Problem.** The orchestrator visibility tee (`scripts/claude-tail-daemon`, launchd
`com.example.claude-tail.bridge-dev`) ZOMBIED on 2026-07-06 12:44: a
`redis.exceptions.ConnectionError` (broken pipe — DO live-bus / home-IP firewall flap
shape) killed its emit path, but the process stayed alive, so launchd `KeepAlive=true`
never fired. Four days of orchestrator sessions never reached the visibility plane;
discovered only when Mark noticed "no orchestrators" (2026-07-10 21:3x).

**Discriminator.** Process liveness lies: launchd shows a pid while the stderr/log
mtime is frozen days old and `agent_scratch:events:live` carries seat events but no
orchestrator entries. Liveness = OUTPUT freshness, not process existence (same class
as the vacuously-green-guard rule). Recovery: `launchctl kickstart -k
gui/501/com.example.claude-tail.bridge-dev` (verified: orchestrator events flowed within
seconds).

**Fix shape.** In `claude_tail` (tailer/service): an emit failure must either crash
the process (so KeepAlive revives it) or enter a bounded retry-reconnect loop that
crashes after N failures — never swallow-and-idle. Add a heartbeat key the gateway
can surface as "tee stale since <ts>". Also rotate/cap the 56MB stderr file.

## DSP-1 — dispatch auto-retry on engine-start cold-start flake (filed 2026-07-11; root-caused same day; retry SHIPPED, timeout raise SHIPPED `f1a4fd3` + `202fd2f`)

**CLOSED 2026-07-11: merged dev `1e9d0d1`; panel `panel-dsp1rev-20260711T155632Z-7b9591`
UNANIMOUS approve (certify cold-Opus + agy; one P2 on record: env <=0 unvalidated).
LIVE-PROVEN same hour:** the 15s class took out GLM ×3 + grok ×1 during the day's review
panels; after merge + seat restarts, both seats' fold-review dispatches cold-started
cleanly on the 60s budget (recorded in verdict `panel-pi1rev-r2b-20260711T163907Z-1b5247`).
Plan-fold scar worth keeping: the smoke only exercised PiSdkEngine, so a wrong
`popen_factory` claim about CodexEngine traveled undetected → luna BLOCKED; rule = one
smoke block per FAKED CLASS (plan rev 1.1).

**Problem.** Five dispatches across 2026-07-10..11 (pi-GLM ×2, codex-luna ×3) failed with
`engine-start-failed: initialize timed out after 15s` on a dispatch to a cold seat; an
immediate manual re-dispatch succeeded **5/5**. Each occurrence costs an orchestrator
round-trip and a manual retry, and inside a panel an unnoticed one silently shrinks the
roster (the named-absent-vote rule exists precisely because of this failure mode).

**Mechanism.** The bridge replies `TurnResult(ok=False, error="engine-start-failed: ...")`
when `pool.acquire` raises `EngineError` (`src/agent_redis_bridge/bridge.py:941`). The
seat is healthy afterwards — the engine subprocess just missed its 15s initialize window
on first spawn after idle (cold caches). This is a latency flake, not a wedge: the very
next dispatch starts the engine fine.

**ROOT CAUSE (probed live 2026-07-11 ~10:30, Mark's steer: don't band-aid, diagnose).**
Not a rare flake — the 15s initialize budget sits ~2s above codex's NORMAL first-after-idle
latency, and the failures are the tail of that distribution under pipeline load. Evidence:

- Direct probe (spawn `codex app-server`, send `initialize`, time the response): **13.20s**
  on the first spawn after ~3.5h seat idleness on an otherwise-quiet machine, then
  6.18s → 2.73s → 2.59s on immediate re-probes (classic cache-warming decay); a later
  probe under ambient session load drifted back up to 6.79s. Steady-state is 2.6–7s;
  budget is 15s. Margin on a GOOD day: ~2s.
- `codex` is a node wrapper (`@openai/codex` `bin/codex.js`) — a bare `codex --version`
  costs 3.4s wall. Spawn cost alone eats a fifth of the budget.
- `~/.codex/models_cache.json` (276KB, `fetched_at`/`etag`) was rewritten DURING the
  13.2s probe and untouched by the fast probes — app-server startup refreshes the model
  catalog when stale, and the models manager does it by spawning ANOTHER child. Luna's
  stderr proves that child can hang outright: `codex_models_manager … failed to refresh
  available models: timeout waiting for child process to exit`, 8× at ~185s cadence
  (2026-07-10 00:13–00:38Z).
- Occurrence timing (luna launchd log `/tmp/arbseat.codex-bridge-dev-luna.launchd.log`,
  lines 187/698): both luna failures were the FIRST dispatch after an idle gap
  (2026-07-10 21:01:01, hours idle, GROK-1 task 1; 2026-07-11 06:59:18, ~49min idle,
  CT-1 fix task); the re-dispatch 45–66s later succeeded. Both windows had heavy
  pipeline IO underway (parallel pytest, 1.2GB worktree copytree churn) — exactly what
  pushes a 13s first-spawn tail past 15s. The child emits NOTHING to stderr during the
  15s (silent, not erroring).
- Cross-engine consistency: pi-sdk/GLM shares the shape — node must cold-load a 30MB
  `node_modules` tree before `host.mjs` can answer anything; its initialize does no
  network. Same 15s literal (`pi_sdk.py:248`), same first-spawn page-in tail.

Why retry-once is 5/5: attempt 1 pays the warming (binary/node page-in + models-catalog
refresh now on disk) even though the bridge kills it at 15s (the refresh child writes the
cache anyway); attempt 2 lands in the 3–7s range.

**Timeout raise (SHIPPED in `f1a4fd3` + `202fd2f`):** the engine
initialize/start budget is now env-tunable through `BRIDGE_ENGINE_INIT_TIMEOUT_S`,
with a 60-second default across codex, pi-sdk, cursor-acp, grok-acp, and gemini-acp.
The client-side retry stays as tail insurance and covers un-restarted seats; the new
default takes effect per seat at its next restart.

**Discriminator.** Retryable = the reply's `error` starts with `engine-start-failed:` and
contains `initialize timed out` — transient by observation (5/5). Other engine-start
failures (bad config, auth, missing binary) are NOT known-transient; keep the retry
bounded to ONCE and always print a loud stderr line when it fires, so the flake's
frequency stays visible instead of being masked (if one retry stops being enough, that's
the consecutive-clean-failure circuit-breaker ticket's territory, not more retries).

**Fix shape (client-side, preferred).** Retry-once at the dispatch client edge: a
go-client `dispatch` flag (e.g. `--retry-engine-start`: on a matched not-ok reply with
the retryable error shape, re-send with a FRESH envelope id and wait again within the
remaining timeout), passed by default by `scripts/dispatch-dev` with an env opt-out.
Client-side ships without touching the 25 running daemons; a bridge-side retry inside
`handle_raw` would need a fleet restart and would double the 15s stall on genuinely-bad
seats for every caller. Keep the go-client flag opt-in so bare `go-client dispatch`
stays byte-identical to the Python `agent-dispatch` contract (parity rule).

**Pointers:** `scripts/dispatch-dev` (wrapper + flag default), `tools/go-client/dispatch.go`
(`dispatch`/`waitForReply`/`classifyReply` — the retry seam), `bridge.py:941` (emit site).
Related unfiled idea (Mark's): consecutive-clean-failure engine circuit breaker.
Structural successor: [ENG-1] — warm-engine context rotation would remove the
per-dispatch spawn entirely, making this retry a rarely-fired backstop.

## ENG-1 — warm-engine context rotation across engines (generalize GROK-1's session/new; retire becomes the backstop) (filed 2026-07-11; codex v1 SHIPPED + live-gated same day; ENG-1b pi-sdk OPEN)

**STATUS: codex v1 CLOSED 2026-07-11 ~12:50 — merged dev `805736b`, live gates GREEN.**
Full Workflow B arc in one session: design v1.2 (2 panel rounds, 4/4 remediation
confirmation) → plan rev 2.2 (plan panel caught the 4/4-convergent LOGGER defect;
2 luna BLOCKEDs mid-build were both genuine plan bugs, folded as rev 2.1/2.2 with an
explicit post-certification drift record) → luna@high 4 tasks, per-task cold gates all
PASS → tri-model final review UNANIMOUS approve (certify: cold-Opus + agy + GLM; terra
non-certifying; run `panel-eng1final-20260711T111338Z-45ef99`) → merge → live gates on
scratch seat codex-bridge-dev-warmgate (retire=0, merged code, 51 dispatches, zero
failures): **G2 contamination PASS** (prose + tool plants; direct + summarize asks; the
summarize answer contained ONLY current-dispatch context — also the empirical proof that
warm rotation pays retire-mode's flat token floor, no accumulation gradient);
**G3 RSS PASS** (bounded 146–176MB, no trend — AND the D9 cap live-proven: child pid
recycled at exactly turns 20 and 40); **G5 latency PASS** (warm median 10.6s flat across
halves; the only elevated dispatches were the predicted post-cap cold spawns, 16–21s —
the run contains its own A/B). Byproduct of the build: `scripts/plan-fixture-smoke`
(see CHANGELOG) after 3-for-3 luna BLOCKED precision on plan bugs.
**Fleet flip is Mark's call** — no seat runs retire=0 yet; flipping any seat =
`BRIDGE_CODEX_RETIRE_AFTER_TURN=0` in its plist (bootout+bootstrap+kickstart), cap
defaults to 20.

**ENG-1b (pi-sdk) CLOSED 2026-07-11 ~16:20 — merged dev `fca9a50`, live gates GREEN.**
Full Workflow B arc: design v1.2 (2 rounds; sticky dispose-quarantine latch the r2
keeper) → plan rev 2.1 (panel + re-fired GLM's self-defeating-wiring-test catch; born-in
fixture/world-claims smokes at every boundary) → luna@high 4 tasks + T3-fix + ok-conjunct
reconciliation d04c665, per-task cold gates all PASS (T3 = first undeclared-deviation
adjudication: accepted as superior, discipline corrected) → tri-model final UNANIMOUS
(certify cold-Opus + agy; run `panel-eng1bfinal-20260711T142252Z-4a5e04`) → all six
design-G4 deny-proofs on record (sixth run orchestrator-side) → live gates on scratch
seat pi-sdk-bridge-dev-warmgate: G2 contamination clean cross-mode (summary probe
enumerated only its own context); G3 host RSS bounded 77-92MB across a live cap-recycle
(pid changed mid-series at turn ~10 of process 2's window) AND token context DEAD FLAT
(70 turns, ~5.7k each, min 5651 max 6016 — the 8.4k→58.8k wedge signature absent by
measurement); G5 warm median 6.1s, halves flat, 50/50 + 20/20 dispatches zero failures.
`BRIDGE_PI_RETIRE_AFTER_TURN=0` is now SAFE; **no pi seat flipped — Mark's call**, and
one gate-day scar for the recipe: a scratch-seat env missing ARB_MEMORY_LOCAL_DSN while
setting ARB_MEMORY_LOCAL_MCP fails engine starts loudly (queued dispatches) — the
seat-provisioning recipe wants its own world-claims pre-flight (5th world-claim failure
of the day, 2nd orchestrator-side).
SHIPPED: `scripts/seat-preflight` is now the seat-provisioning world-claims pre-flight
(merged dev `357b982`; panels `panel-preflightrev-20260711T155632Z-d71e0c` REQUEST
CHANGES → fold `5d8a72a` → r2 `panel-preflightrev-r2-20260711T162312Z-4194a5` approve
with orchestrator reconciliation `12b816e`; live gate rc=0 on real luna/registry-x/pi-glm
plists, both config shapes).

**FLEET FLIPPED 2026-07-11 ~16:14 (Mark's order, all seats):** all 7 codex seats
(luna, terra, 5× `-sol`) at `BRIDGE_CODEX_RETIRE_AFTER_TURN=0` and pi-glm at
`BRIDGE_PI_RETIRE_AFTER_TURN=0`; caps default 20; `BRIDGE_MAX_PARALLEL=4` and
`BRIDGE_NOTIFY_INBOX=0` set fleet-wide (6 seats were missing the notify split — the
max_parallel>1 startup WARNING flagged it live). Fleet clone pulled `635c398`→`132b007`
first (pyproject delta was comment-only; no venv refresh needed). Live smoke: dispatch
pairs on luna (2s/7s), registry-x-sol (3s/4s), pi-glm (4s/3s), all ok=true; warm rotation
positively confirmed via `[codex] rotated thread ... (fresh context per dispatch)` on
BOTH clones; pi host survived across turns with no respawn.

Original recon note below. the pi SDK has a FIRST-CLASS
fresh-context primitive — `ctx.waitForIdle()` + `ctx.newSession({parentSession})`
(a pi extensions repo's `extensions/clear.ts`, cloned locally) — so the
host.mjs change is "expose newSession semantics over the host protocol", not a
hand-rolled dispose-and-replace. Design must confirm the headless surface
(`createAgentSession`/`startSessionWithBridge`, `SessionManager.inMemory`) exposes it or
wrap equivalent (new session + old-session dispose), keep the wedge-scar RSS gate
mandatory (the GLM accumulation lived in host memory), and mirror codex's
counters/health/cap. Certify-quorum note for ENG-1b panels: GLM is NON-certifying on its
own engine harness (grok-precedent rule), so certify = codex pin + agy, with GLM +
cold-Opus admissible.

Original filing below for the record.

**What.** Instead of retire-after-turn (kill + respawn the engine process per dispatch —
today's default on all four engines), keep the engine process WARM and clear its
conversational context per dispatch, the way grok already does for opt-out seats:
`_rotate_session_if_reused()` issues a fresh `session/new` on the live process and
quarantines the engine if rotation fails (`engines/grok_acp.py:250`, probe run H).
Retirement demotes from default to backstop (idle-TTL and/or every-N-dispatches, plus
quarantine-on-rotation-failure).

**Why now (evidence).** Retire-after-turn was the correct accumulation fix
([[pi-sdk-glm-wedge-root-cause]] family, all four engines closed 2026-07-10) — but DSP-1's
root-causing shows what it costs: EVERY dispatch pays a full spawn (codex initialize:
2.6–7s warm, ~13s first-after-idle, vs a 15s budget → the engine-start-failed tail), and
the grok retire spec explicitly booked per-dispatch cold start as an accepted residual
("a few seconds") that is now measured at 3–13s with a live failure mode. Rotation keeps
the contamination guarantee while deleting the spawn tax — grok's shipped implementation
is the existence proof.

**Per-engine mechanism map (verified in code 2026-07-11):**
- **grok-acp:** DONE — session/new rotation on warm process, sessionId-gated asks,
  quarantine on failure. The template.
- **codex:** `start_thread()` already exists (`engines/codex.py:161`) — rotation = new
  `thread/start` on the warm app-server, drop the old thread_id. UNKNOWNS to probe: does
  the app-server process hold per-abandoned-thread memory (RSS over N rotations), and
  does any cross-thread state leak (contamination probe)? Retirement was chosen because
  the pool re-served the SAME thread (22M-token accumulation 2026-07-08) — rotation
  attacks that directly; nothing in the retire records rejects rotation on the merits.
- **pi-sdk:** host.mjs is single-thread-per-process BY DESIGN — a second `thread/start`
  returns `thread already started` (host.mjs:255). Rotation needs a host change
  (dispose-and-replace or `thread/dispose`), and the disposal must PROVABLY free the old
  session object — the GLM wedge was session accumulation, so an RSS-bounded-over-N probe
  is mandatory, not optional. Host is our code; change is contained.
- **agent-sdk:** retire there is SDK-session-level, not a big subprocess spawn; evaluate
  whether the spawn cost is even material before touching it (asdk never exhibited the
  DSP-1 flake).
- **agy-print:** spawns a fresh `agy` per turn by design — no persistent process to keep
  warm; out of scope.

**Hard requirements for any engine that flips (from the scars):**
1. GROK-style contamination probe per engine: dispatch A plants a fact, dispatch B must
   not know it (probe run H analogue) — decorrelated across modes per
   [[cross-mode-decorrelation-empirical-check]].
2. RSS bounded over N≥50 rotations (the wedge + agy fd-leak class); watchdog backstop.
3. Explicit `--thread-id` continuation semantics unchanged (codex rollout resume; pool
   affinity; asdk continuation store).
4. Rollout behind a per-seat env flag, default OFF until both probes are green live.
5. Deny-proof: disable rotation → contamination probe must go red.

**Relationship to DSP-1:** complementary, not competing. ENG-1 removes the cold-start
class at the source for pipeline-cadence dispatches; the DSP-1 retry (+ recommended
15s→60s init budget raise) still covers daemon-start and post-idle-TTL cold starts.

**Design class:** concurrent-mechanism (pool, rotation, quarantine, affinity interplay) —
budget multiple panel rounds per [[review-depth-for-concurrent-mechanisms]].

## PI-1 — scope + timeout guard on the pi host's builtin find/grep tools (filed 2026-07-11)

**STATUS: SHIPPED (2026-07-11; `6a6dc75`, `daec45f`; merged dev `aac8b0f`).**
Panel record: round 1 `panel-pi1rev-20260711T155632Z-38fd28` REQUEST CHANGES — cold-Opus
P0 (excludeTools deleted the guarded customTools before the same-name override;
orchestrator-confirmed against the SDK source; the fake-createSession tests were blind);
fold `ae19a69` (excludeTools removed + real-SDK registry-layer integration test,
red-on-defect → green). Round 2 `panel-pi1rev-r2b-20260711T163907Z-1b5247` UNANIMOUS
(re-fired GLM+grok + fresh cold-Opus; r2b = re-mint after the duplicate-vote guard
correctly refused a daemon-auto-emit/fence-recovery collision).
**Live-gate note:** the deployed GLM seat's default session post-ENG-1b carries only
read/bash/edit/write — NO find/grep — so the incident vector is closed by the default
tool set, and the guard covers any future seat that requests find/grep (registry-level
proof: the non-vacuous integration test). RESIDUAL: a live seat-level guard exercise
needs a seat launched with find/grep in its tools; do it with the next such seat rather
than standing one up specially. Adjacent observation: the `bash` builtin can still run
`find /` — different tool, out of PI-1 scope, noted for a future BASH-guard filing if
it ever bites.

**Problem.** During the ENG-1b design-r2 panel (2026-07-11 13:07:43), the GLM reviewer
seat issued `find` with `pattern: '**/pi-ai/package.json'`, `path: '/'` — a filesystem
crawl from root. The call blocked in the OS (node child at 0% CPU for 40+ minutes —
dead-mount or TCC-stall shape, not directory crunching) and never returned; the turn
died only at the bridge's 3600s timeout. Evidence: `/tmp/pi-sdk-events-66635.ndjson`
(tool_execution_start with no end), design-r2 audit run
`panel-eng1bdesign-r2-20260711T120322Z-c5d52b` (GLM stance timed-out, evidence in the
vote body). Cost: the seat missed a panel round; a dispatch round-trip; 40 min of a
wedged slot.

**Why structural, not brief-hygiene.** The seat was being diligent, not disobedient (it
wanted the pi-ai type definitions and reached for the widest search that would find
them). A "keep searches in the repo" brief line is configurational and re-fails with
every creative future reviewer; [[structural-not-configurational-containment]]: an
exposed process should be UNABLE to crawl `/` by construction. Same reasoning that made
retire-by-default a code fix, and ENG-1b's clean-terminal check engine-side.

**Fix shape.** In the pi-sdk host's builtin tool layer (`tools/pi-sdk-host/host.mjs` /
wherever the builtin find/grep handlers resolve paths): (1) reject or clamp any `path`
outside the session's `cwd` subtree with a legible tool ERROR ("path outside workspace:
<path>") — the model reads it and narrows, in-band; (2) wall-clock timeout on the crawl
(e.g. 30s) returning "find timed out after Ns" as a tool error, never a hang. Both
convert a silent 40-minute wedge into a self-correcting tool error. Check whether the
tools are our code or pi-ai builtins passed through — if pi-ai's, the guard wraps at the
customTools/toolArgs seam instead.

**Sequencing.** AFTER ENG-1b merges — it touches host.mjs, whose fences are
panel-certified for the rotation arc; do not smuggle it into that plan. Small enough
for Workflow A.

**Pointers:** `tools/pi-sdk-host/host.mjs` (builtin tool wiring), `mcp-bridge.mjs`
(`BUILTIN_TOOL_NAMES`), the GLM ledger note in [[eng1-codex-rotation-shipped]]-adjacent
memory, ENG-1b design D-B2 (the toolArgs seam).

## VIS-1 — tee staleness chip is INERT on prod until ARB_VIS_EXPECTED_TEES is set (filed 2026-07-16)

**What:** CT-1's tee-staleness surfacing (`d53d127`, 2026-07-11) is merged and dev-proven, but its
prod status is **unverified** and it is **inert without an env var**. Two separable things, only one
of which a code deploy delivers:
1. **Code** — `tee_states` + the `/orchestrators` `tees` key + the UI chip. Probably already on prod:
   the CT-1 residual naming this deploy was written 2026-07-11, BEFORE the 07-13 and 07-16 prod
   deploys (`arb-prod` now at `fe4a5ac`), either of which would have carried `d53d127`. **Not
   confirmed — check before acting.**
2. **Config** — `ARB_VIS_EXPECTED_TEES` (e.g. `claude-tail.bridge-dev`). A code deploy does NOT bring
   this. The roster is CONFIGURED, not discovered, by deliberate design (`tee_states` docstring: SCAN
   can only enumerate keys that EXIST, so it could never report "missing" for a tee that never
   started — CT-1 spec §C, panel r2 codex P1). Empty roster ⇒ empty label list ⇒ `tees: []` ⇒ the
   chip renders nothing and a dead tee is indistinguishable from a healthy one.

**Why it matters:** the whole point of CT-1 was the 2026-07-06 incident where the claude-tail tee
zombied for four days and nobody noticed. The chip is the detection surface for a repeat. Shipped but
unconfigured, it provides zero detection while *looking* deployed — the same vacuously-green shape the
repo's own doctrine names: liveness = OUTPUT freshness, not process existence. `visibility.py:691`
already warns `ARB_VIS_EXPECTED_TEES is empty — tee staleness surfacing is INERT` at startup, so the
gateway's own logs will answer step 1 and 2 together.

**Verified locally 2026-07-16** (incidental to repairing four stale `/orchestrators` test assertions,
`945a577`): with no `ARB_VIS_EXPECTED_TEES`, `/orchestrators` returns `tees: []`; set it to
`claude-tail.bridge-dev` and the same call returns `[{label: claude-tail.bridge-dev, state: missing}]`.
The mechanism works — it is purely roster-gated.

**Steps when picked up:** (1) check the prod visibility gateway's startup log for the
`ARB_VIS_EXPECTED_TEES is empty` warning — its presence answers both whether the code is deployed and
whether the roster is set; (2) if the code is absent, normal prod deploy; (3) set the env var to the
real expected tee roster and recreate; (4) confirm the chip renders a real state (a deliberately
stopped tee should read `missing`/`stale`, not vanish) — a chip that shows nothing is the failure mode
this item exists to prevent.

**Pointers:** `src/arb_memory/visibility.py:188-215` (`tee_states`, MGET over the configured roster),
`:691` (the INERT warning), `_expected_tee_labels()`; `tests/claude_tail/test_visibility_tee.py`
(fresh/stale/missing coverage, double implements `mget`); CT-1 § Residuals (line ~623 above — this item
promotes that buried residual out of a CLOSED section, which is why it sat unactioned); memories
`[[ct1-shipped-live-gated]]`, `[[arb-visibility-cf-scope-and-claude-tail-global]]`,
`[[arb-observability-prod-live]]`. Note the gateway host is NOT necessarily `arb-prod` (that droplet is
the MCP door); check `[[claude-vis-gateway-trace-tail-deploy]]` for the deploy target before assuming.

## GOV-1 — succession replay test: do the stores transfer the rulings? (filed 2026-07-11, Mark's design; RUN + PASSED same day)

**STATUS: PASSED with one filed finding, 2026-07-11 ~15:15** (run
`gov1-replay-20260711T140208Z`; report
`docs/superpowers/reviews/2026-07-11-gov1-replay-report.md`). Sonnet-5 10/12, GLM
12/12 + one novel finding — cross-lineage replication. The single divergence (Sonnet
mis-timing the report-path world) was input-provenance, not reasoning: the stores were
current-state, remediation had overwritten the evidence. Yield = the `world_at:`
capture rule (REQUIRED, co-signed, landed BEFORE this closure so the record is
self-consistent).

**RATIFIED 2026-07-11 ~16:50 by Mark** — the platform claim stands: knowledge
externalized, execution judgment delegated and calibrated, constitutional judgment
reserved and audited. No re-run directive issued; a replay after the next NOVEL
incident remains available (stores now carry `world_at:`, so point-in-time
reconstruction would be tested properly). Original filing below.

**What.** Replay the 2026-07-11 four BLOCKED incidents as COLD briefs — state snapshot
+ ARB Memory access, nothing else — to a different warm seat (Sonnet-class or GLM), and
score its rulings against the Fable orchestrator's actual ones (plan rev 2.1's six-site
fix; rev 2.2's red-phase expectation; the worktree provisioning diagnosis; the ellipsis
path fix). Divergences are not failures; they are the map of what the stores still
don't say.

**Why.** The 2026-07-11 tape proves the rulings are TRACTABLE in-loop (a frontier
orchestrator handled all four autonomously, fixes always landing outside the worktree)
and that the knowledge was EXTERNALIZED (every ruling is in a commit, rule, or memory).
It does NOT prove the externalization is SUFFICIENT for succession — that requires a
different seat reconstructing the rulings from the stores alone. Status: succession
test PREPARED, not passed. Settles the bus-factor/platform question with data.

**Pointers.** Specimens: ENG-1 T2/T3 BLOCKEDs + ENG-1b T1 BLOCKED (reports in the
2026-07-11 session scratchpad — durable copies: the fix commits d075fad, rev-2.2
commit, d3554d5 + provisioning; luna replies quoted in the day's audit runs);
doctrine: [[luna-high-implementor-regime]] BLOCKED calibration,
`docs/pipeline-operating-manual.md` § implementor-BLOCKED triage; day artefact
`art-4ec04c4f689c4ce4`. Design questions: scoring rubric (same-fix vs same-diagnosis vs
same-layer); whether the replay seat gets the triage rule (tests rule-following) or not
(tests derivation).

## Memory write result: signal stored-vs-deduped (filed 2026-07-12)

> **SHIPPED 2026-07-13 + PROD-DEPLOYED (do NOT re-implement):** `upsert_artefact`/`upsert_hint`
> now publish a `{outcome: stored|deduped, artefact_id, version}` receipt to a
> `write_result:<request_id>` channel; the writer proxy owns the await (`928d18a`, spec
> `docs/superpowers/specs/2026-07-13-item2-write-result-SPEC.md`, changelog + prod-deploy
> record `edcd434` — live plane proof: an awaited `/publish` returned `artefact_outcome:"stored"`).
> Kept here only so a future session doesn't re-plan already-shipped work.

**What.** The memory write path (`arbmem:writes` → `MemoryConsumer` → `store.upsert_artefact`)
silently no-ops when incoming content is byte-identical to the artefact's current latest version
(`store.py:37-41`: same `content_hash` as latest → return existing version, no new row). The write
is ACKed (correctly — a dedup is a no-op, not a drop; `write_deadletter` stays empty) but the caller
gets no signal distinguishing **stored-as-vN+1** from **deduped-against-vN**. Same gap in the MCP
write tools (`memory_store`/`memory_remember`) and the publish-proxy (bare `200 OK`).

**Why.** 2026-07-12: a project-a peer spent an afternoon polling `memory_get` for a "v3" that would
never appear — its republish was byte-identical to v2, so it deduped; its content was already at v2.
A `{outcome: stored|deduped, artefact_id, version}` result would have told it immediately (a) it
deduped and (b) where the content already lives. Cost was hours of shadow-boxing over a non-bug.

**Pointers.** `src/arb_memory/store.py:37-45` (dedup + `COALESCE(max(version),0)+1`); the writer
publish-proxy (200 OK, no version outcome); the MCP write path. The write is fully async through a
stream, so surfacing the outcome needs a **result channel** back to the caller — the SAME pattern
the audit-close-2 bus-driven close needs (`close_result` per request_id). Design the result-channel
once and reuse for both. Ideal shape: caller publishes with a `request_id`, reads
`arbmem:write_result:<request_id>` = `{outcome, artefact_id, version}`.

## Consumer-loop robustness: deterministic-infra PEL recirculation + Redis-down tight-loop (filed 2026-07-12)

> **SHIPPED 2026-07-13 + PROD-DEPLOYED (do NOT re-implement):** shared `consumer_loop.py`
> extracted and adopted by all 5 stream consumers — bounded retries on a PEL head →
> deadletter-with-reason, plus backoff on the outer loop (`70cb478`, spec
> `docs/superpowers/specs/2026-07-13-item1-consumer-loop-SPEC.md`, shared-prep slice
> `2026-07-13-shared-prep-slice-SPEC.md`, changelog + prod-deploy record `edcd434`).
> Kept here only so a future session doesn't re-plan already-shipped work.

**What.** Two pre-existing consumer-loop robustness gaps flagged by cold-Opus in the AUDIT-CLOSE-2
fold-review (non-gating, NOT introduced by that slice; likely present in `AuditConsumer` too):
1. A **deterministic infra-typed** error makes an entry re-drain the PEL head forever — infra errors
   intentionally don't ack (so they retry), but a *deterministic* one never succeeds, so the loop
   re-reads the same head every cycle without progress. Transient errors (the intended case) are fine.
2. `run()`'s top-level `except: log` **tight-loops if Redis is down** — no backoff between iterations.

**Why.** Bounded-blast (single stuck consumer; self-heals when infra recovers) but wastes CPU / masks a
stuck request. Fix shape: bounded retry count on a PEL head → deadletter-with-reason after N deterministic
failures; a backoff sleep in the `run()` except. Apply to `AuditConsumer` too if it shares the pattern.

**Pointers.** `src/arb_memory/close.py` `run()`/`drain_pending()`; mirror in `src/arb_memory/audit.py`.
Fold-review: `docs/superpowers/reviews/2026-07-12-audit-close-2-foldreview-brief.md`.

---

# PISDK-1 — `pi-sdk-host` dead on `@earendil-works/pi-coding-agent` 0.80.9 (`AuthStorage` removed); every pi-sdk seat pings ALIVE while inert

**Filed** 2026-07-17 during the kimi seat arc, when `pi-sdk-bridge-dev-glm` was rostered onto design
panel r2 and never voted. NOT fixed here: the blast radius crosses projects and this arc did not own
it. Named as a roster deviation instead.

**What.** Every dispatch to a `pi-sdk` seat dies at engine start:

```
[bridge-error] engine-start-failed: pi-sdk-host exited (returncode=1) while waiting for initialize
SyntaxError: The requested module '@earendil-works/pi-coding-agent' does not
provide an export named 'AuthStorage'    at tools/pi-sdk-host/host.mjs:59
```

Installed package is **0.80.9**, which exports 143 names; `AuthStorage` is **not** among them. The
nearest surviving export is **`readStoredCredential(providerId, authPath = join(getAgentDir(), "auth.json"))`**,
which returns `data[providerId]` from `auth.json` (or `undefined`). Two usage sites:
`tools/pi-sdk-host/host.mjs:59` (import) and **`:380`** (`const authStorage = AuthStorage.create();`).

Same drift family as the note already in that file ("pi-ai >=0.80 removed the getModel() export;
`ModelRegistry.find()` is the sole resolution path now") — cf. memory `pi-sdk-pi-ai-080-fix`. The
package keeps removing exports across minors and nothing pins or asserts them.

**Why it matters more than one dead seat — it lies about being alive.** The seat still
registers, heartbeats, passes its read-only gate, and reports healthy:

```
[readonly-gate] pi-sdk-bridge-dev-glm surface certified read-only (<= read,grep,find,ls); serving.
[bridge] pi-sdk-bridge-dev-glm online at 2026-07-16T11:53:59+01:00 (pid=880)
```

and `agent-bridge-ping` returns `heartbeat=alive consumer=alive`. **A ping proves the daemon's inbox
loop is progressing, not that the engine can boot.** This is the "looks configured, is inert" class
(cf. the four-day zombie tee, the inert VIS chip) sitting live in the fleet — and it silently
shrinks any panel that rosters a pi-sdk seat, which is the failure `dont-silently-shrink-the-panel`
exists to prevent. pi-GLM is a **doctrinal certifying seat** for authored stages, so this degrades
every design/spec/plan panel on every project.

**Blast radius (why it wasn't fixed inline).** ≥6 seats across 3 projects depend on `host.mjs`:
`pi-sdk-bridge-dev-glm`, `pi-sdk-bridge-dev-minimax-m3`, `pi-sdk-arb-codex-dev-{glm,k3,minimax}`,
`pi-sdk-project-h-browser-dev-glm`, `pi-m3-bridge.bridge-dev`. A shared-tool change needs its own review
round, not a drive-by during someone else's arc.

**Fix shape.** Replace the `AuthStorage.create()` construction at `:380` with `readStoredCredential`
(read the call site first — `AuthStorage` may have carried write/refresh behaviour that
`readStoredCredential` does not, in which case find the real replacement rather than forcing this
one). Then **add a guard that fails LOUD at seat start**: assert the exports the host needs are
present and refuse to register if not, so a future removal produces a dead seat that *says* it is
dead instead of one that pings healthy. Pin the dependency while you are there.

**Gate.** A real dispatch that returns a reply — NOT a ping, which is green today on a seat that
cannot run a single turn. Cf. memory `live-verification-catches-cli-glue`.

---

## ACP-1 — `_select_allow_option`'s deny-marker fallback can select a DENY option (latent)

**Filed 2026-07-17.** Found by the pi-sdk k3 seat and the kimi CLI seat **independently**, during a
harness-decorrelation control for the kimi arc. **All three findings verified by EXECUTION**, not by
re-reading (`PYTHONPATH=src .venv/bin/python`, calling the real function):

| Input (`options`) | Returns | Expected |
|---|---|---|
| `[{"optionId": "do_not_allow"}]` | **`do_not_allow`** | `None` |
| `[{"optionId": "not_allowed"}]` | **`not_allowed`** | `None` |
| `[{"optionId": "shallow_clone"}]` | **`shallow_clone`** | `None` |
| `[{"optionId": "allow_always"}, {"optionId": "allow_once"}]` | **`allow_always`** | `allow_once` |

Controls PASS: the kind-based path still prefers `allow_once` over `allow_always`, and a lone
`reject_once` still yields `None`. `dont_allow` and `disallow` are correctly skipped — which is
**why this survived review**: it behaves correctly on the spellings a reader spot-checks.

**F1 [P1-latent] — snake_case defeats the deny markers.** `_DENY_MARKERS` (`_acp.py:11-25`) holds
space-separated `"do not"` and `"not allow"`, but optionIds are snake_case — the underscore breaks
the substring match, then `"allow" in token` (`_acp.py:51`) fires and the deny option is **returned
as the allow option**. Underscores are the idiomatic separator here; the file's own authoritative
kinds are `allow_once`/`reject_once`. This is the likely spelling, not an exotic one.

**F2 [P2] — the fallback inverts least-privilege.** `_acp.py:43-46` deliberately prefers `allow_once`
over `allow_always`. The fallback (`:47-52`) returns the first allow-ish option in **server payload
order**, so a persistent grant can be auto-selected when a one-shot was available.

**F3 [P2] — the docstring names the hazard, then commits it.** `_acp.py:35-36` refuses to match on
`name` *because "shallow" contains "allow"* — and `:51` applies the identical substring test to
`optionId`. `shallow_clone` is selected as an allow. The cited hazard was **relocated, not
eliminated**.

**Severity bound — honest.** This is the FALLBACK, reached only when NO option declares a `kind`.
All **104** ask payloads logged in `2026-07-17-kimi-behaviour-matrix.md` carried a `kind`, so against
kimi this is **latent, not live**; it needs a non-conforming ACP server. But `_acp.py` is shared by
**grok, cursor and any future ACP engine** (that sharing is its stated purpose, `_acp.py:3-5`), and
this is the function whose entire job is *"'do not allow' can never be selected"* (`:37`).

**Fix shape.** Normalize separators before matching (`token.replace("_"," ").replace("-"," ")`), and
better: split on non-alphanumerics and match **whole words**, which fixes F1 and F3 together
(`shallow` ≠ `allow` as a word). For F2, run two fallback passes: prefer allow-tokens without
`always` before accepting `always`-bearing ones. Add table-driven tests for every row above —
including the passing controls, so a future "fix" can't silently break the kind-based path.

**Why it is filed, not fixed.** Widening scope is Mark's call, not the panel's, and `_acp.py` is
shared by seats outside this arc — a shared-contract change needs its own review round, not a
drive-by during the kimi arc. Cf. PISDK-1's identical reasoning.

**Gate.** The table above as tests, RED before the fix and GREEN after — plus an adversarial check
that deleting the normalization makes them go RED again
(`[[deny-proofs-need-adversarial-verification]]`).

## KSP-1 — kimi runtime-surface probe: build `driver.py` + run it live (filed 2026-07-17; LOW PRIORITY, PARKED)

**Priority: LOW.** The design is done (blocked-but-endorsed), the testable machinery is built and
green, and the remaining work is a self-contained orchestrator plus a gated live run. Nothing depends
on it; the kimi seat already works under `plan` (`af85422`). Picked up when the sandboxed-tools kimi
seat is actually wanted, not before.

**What:** Finish the kimi surface-characterization probe — the instrument that produces kimi's observed
file/exec/network surface so a Seatbelt profile can be authored against measurement, not reasoning. Two
pieces remain: (a) write `docs/superpowers/probes/2026-07-17-kimi-sandbox/driver.py` (orchestrates
instrument A discover→measure turns + instrument B class-drop-one, scoring each turn with
`oracle.delivered()`; reuse `docs/superpowers/probes/2026-07-17-kimi-spike/kimi_spike.py` to drive the
ACP); (b) run it live — **gated on Mark**.

**Why LOW / why parked:** the live run needs three things only Mark grants: `sudo fs_usage` for the
trace window, a **quiesced host** (the 25-seat fleet + parallel sessions stopped, because the
contamination gate defaults to `contam_max=0`), and ~2.5–3h of kimi runtime (~10–12 turns). Not worth
scheduling until the sandboxed kimi seat is on the critical path.

**Built + TDD-green already (do NOT rebuild — 22 tests, `.venv/bin/python -m pytest
docs/superpowers/probes/2026-07-17-kimi-sandbox/tests/`):** `oracle.py` (exec-anchored DELIVERED
scorer; defeats the `.git/logs/HEAD` proof-of-git bypass), `sbprofile.py` (Seatbelt bootstrap renderer;
deny-proofs vs real `sandbox-exec`), `fsusage.py` (parser + pid-tree attribution + fail-closed
contamination gate). The evidence checker `docs/superpowers/probes/2026-07-17-kimi-spike/derive_evidence.py`
(`5a9f95f`) is non-vacuous (byte-for-byte block compare + adversarial self-test).

**Open design question for Mark (from the r3 panel, unanswered):** run only under a scheduled
fleet-quiet window (`contam_max=0`, clean data) OR make `contam_max` a runtime parameter so a first
exploratory run measures-and-reports contamination rather than aborting? The gate supports both.

**Pointers:** design `docs/superpowers/specs/2026-07-17-kimi-runtime-surface-probe-design-v4.md`
(`8dc304e`); the last full handoff `.claude/handoffs/2026-07-17-1211.md` (gitignored — read while it
exists); behaviour matrix `27f7465`; three closed review panels (RIDs in the v4 design's supersede
chain). **Do NOT edit `/Volumes/<workspace>/repos/ARB/bench/implbench/harness/sandbox.py`** (parallel
session, read-only reference). Ties memory `[[wrong-defaults-beat-discipline]]` and
`[[characterize-before-designing-external-behaviour]]`.

## Bridge-appended admissibility preview at result delivery (filed 2026-07-27, owner-designed, not specced)

**What:** When the bridge delivers a panel/dispatch result, it resolves the result's claim refs
against `claim_admissibility_v` using the `arb_gate_reader` credential it already holds, and appends
to the **reply frame** a short statement of what the gate *will do*:

    C-114: unconfirmed  -> a remediation dispatch referencing it will be refused (unconfirmed_claim)
    C-115: confirmed, unattested -> will be refused (unattested_claim)

**Why here rather than anywhere else — placement is the whole point.** Verification guidance keeps
failing not because it is absent but because it is far from the decision. Memory entries sit far
away; pinned `CLAUDE.md` loads at session start and competes with everything after it. This text
arrives *attached to the artefact being accepted, on the exact turn of the acceptance decision* —
co-located with the moment the trigger fails to fire. Best real estate available to a prompt-layer
intervention.

**Three design constraints, each load-bearing:**

1. **Bridge-authored, in the FRAME, never in the body.** Generated deterministically from the
   envelope and appended outside anything the seat wrote, visibly bridge-provenance. An instruction
   inside the seat's result text is self-defeating twice: the seat could shape it (verification
   theatre, or steering attention away from its own soft spot), and models are increasingly
   injection-hardened to treat tool-result text as data rather than commands — so an "instruction"
   in the untrusted body is exactly what should be discounted. The seat never sees or touches it.
2. **Specific, not boilerplate.** "Verify any claims" stapled to every result is wallpaper within a
   day — constant stimuli stop being stimuli, manufacturing the exact low-salience state it was
   meant to fix. Enumerate instead: named refs, marked asserted vs relayed. An enumerated list is
   also script-food: ticking through named items is a worn groove, feeding context-triggered
   diligence a shape it reliably executes rather than exhorting a behaviour it does not schedule.
3. **Strongest form is NOT an instruction — it is information from the authority.** A preview of
   machinery behaviour has nothing to obey or ignore; it makes early verification the path of least
   resistance rather than a virtue. It fires **conditionally** — silent when everything is
   admissible — so it is risk-shaped by construction and immune to habituation, because its
   appearance is itself the signal. And it cannot be gamed by the seat, because it derives from the
   store, not from the result's prose. It is the refusal-code gaps-naming pattern moved one step
   earlier: instead of bouncing the dispatch after the orchestrator commits, the bounce is previewed
   while the decision is still open. Same information, cheaper turn.

**Cost:** one extra view read per result delivery. Negligible at current volume.

**Required discipline — file it as a NUDGE in the spec's honest-limits register**, alongside the
pre-store re-read and the CLAUDE.md pinning. Watch for the failure mode of controls that look like
controls: six weeks from now, *"the bridge already tells the orchestrator what's unconfirmed"* must
never appear as an argument for deferring Slice 2. **Nothing may read compliance from it; the gates
still define confirmed.** Belt and braces — just keep clear which one is holding the trousers up.

**Depends on:** Slice 1c (the bridge holding a live `arb_gate_reader` connection). Cheap to add once
1c lands; pointless before it.

## Engine spawn-scrub: make it structural, not guard-matched (from Slice 1d-i close, 2026-07-27)

The 1d-i AST guard (`tests/test_engine_spawn_env_ast_guard.py`) catches un-`env=`'d DIRECT
`subprocess.run/Popen/check_output` calls but is blind to the injected `self.popen_factory(...)`
/`self.run_command(...)` form that is 8 of 12 real engine spawn sites (and to `os.system`/`exec`,
alias/`getattr`, asyncio spawns, off-root helpers). No live leak today — every current site is
scrubbed — but a future factory-form spawn could ship unscrubbed silently. Structural fix (owner-
chosen deferral): make `popen_factory`/`run_command` apply `scrubbed_child_env` BY CONSTRUCTION so
no call site can spawn unscrubbed regardless of syntax; the guard then shrinks to a thin 'no raw
subprocess in engines/' backstop. See `docs/superpowers/reviews/2026-07-27-slice1d-i-closure-residuals.md`.

## CD-1 — close-discipline block is served to CLI seats only; the Pi-orchestrated route gets nothing (filed 2026-07-28)

> **Owner steer at filing (Mark, 2026-07-28): to be sorted on the mac-mini**, which is where the
> Pi orchestrator and the launchd seat fleet actually run. Filed rather than built for that reason —
> not deferred on merit. Nothing about the design is open enough to need a brainstorm; the open part
> is the injection point (below).

**What:** `prompts/arb-close-discipline.md` reaches a warm seat's system prompt on exactly one
route — a `claudeya` shell function in `~/.zshrc` passing `--append-system-prompt-file` to the
Claude Code CLI. **Pi-orchestrated warm turns bypass the shell entirely and therefore carry no
close-discipline block at all.** The bridge harness (the PiExtensions `/arb-orch` wizard, and the
dispatch path behind it) has to inject the same file by direct repo path.

**Why it matters more than a normal gap.** The deployment note (ARB Memory `art-fe4f19c5ab1e0d87`
v2) requires the two serving routes to be byte-identical **by construction**; today they are not,
and the difference is invisible from inside a session. A Pi-orchestrated warm seat makes acceptance
and close decisions — the exact moments the block exists to catch — while looking entirely normal.
That is the same quiet-failure shape the note itself warns about for dangling symlinks, except no
guard can fire because nothing is even attempting to read the file on this route. `ee89a04b` shipped
a readability guard for the CLI route precisely to convert that silence into a loud failure; the Pi
route has no equivalent because it has no reader.

Serve it from the **repo path, not `~/.claude/`** — the harness knows its own checkout, and routing
through the per-machine symlink would add a failure mode the harness does not otherwise have. Note
the wizard itself lives in the PiExtensions volume, *outside* this repo
(`tests/test_arb_orch_panel_subagents.py:273` skips when that volume is unreachable), so the repo
side must supply the path rather than expect the wizard to know it.

**Candidate mechanism, not yet chosen — the role-profile passthrough already does this shape.**
`--role-profile-file` loads once at boot (`src/agent_redis_bridge/bridge.py:4693-4708`,
`load_role_profile` → `append_system_prompt`) and reaches pi engines natively
(`engines/pi_rpc.py:316` `--append-system-prompt`; `engines/pi_sdk.py:283-284`
`appendSystemPrompt`), while non-pi engines get the same content as a first-turn
`<system_guidance>` wrapper (`skills/using-agent-bridge/SKILL.md:749`). **Open design question:** a
role profile answers *what job this seat is doing* (reviewer, implementor) and is per-seat;
close discipline is orthogonal to role and should hold for every warm orchestrating seat regardless.
Concatenating it into the role profile would work mechanically but couples two independent axes, and
`role_profile_for_turn` binds at launch (see § "Per-dispatch seat re-roling"). Decide whether this
rides the existing passthrough, gets its own channel, or is composed at load time.

**Scope caution:** the block is **orchestrator-tier** (CLAUDE.md § role layer — acceptance and close
are orchestrator judgments; workers never read CLAUDE.md). Do not blanket-inject it into dispatched
worker seats without deciding that separately — a worker told to await a close-consumer reconcile it
has no part in is being taught a claim it cannot act on.

**Do NOT promote the three staged bullets as part of this work.** served ⊆ artefact is deliberate
while the gates are pending; the staging rule and its rationale are in
`prompts/arb-close-discipline.NOTES.md` § "Why the served file is a SUBSET of the artefact". Wiring
a second route is not a trigger for any staged bullet.

**Verification when built** (the CLI route's own evidence bar, from `ee89a04b`): a sentinel token
through the real Pi path, returning; the seat answering a question whose answer is only in the block
("what does your close discipline say about polished claims?"); and a demonstrated *loud* failure
when the file is unreadable. A route that silently serves nothing is the defect, so a check that
cannot fail is worthless here.

**Pointers:** `prompts/arb-close-discipline.md` (the served copy, 3 live bullets, sha256
`a0b1019265490e761bdcc7057de50478456a53c77e5cfff30aff3ed768f2aa82` at filing);
`prompts/arb-close-discipline.NOTES.md` § "Not yet done — the Pi-orchestrated route" (the same gap,
stated from the deployment side); `git show ee89a04b` (what the CLI route does and exactly what was
verified); `scripts/arb-orch-panel` + `docs/pi-orchestrator-operating-guide.md` +
`docs/agent-memory-seeds/pi-organic/pi-orch-startup.md` (the Pi orchestration surface). ARB Memory
reference copies: `art-cb57c7a0faa7e32a` v2 (prompt block, live + staged) and
`art-fe4f19c5ab1e0d87` v2 (deployment note) — **fetch v2 or later; both v1s name a repo that does
not exist** and would bootstrap the failure mode they document.

## FABA-1 — an author round that never dispatches its author is indistinguishable from one that authored nothing (filed 2026-07-28, from the v5 fold debugging)

> **FIXED 2026-07-28, same day (do NOT re-implement the diagnosis half).**
> `run_author_round.author_dispatch_observed()` + `never_fired_reason()` now name which of the
> two failures happened, and the undispatched case names its likely trigger (`--task` carrying
> instructions rather than a pointer). Pinned by 7 tests in
> `tools/faba/tests/test_author_round_guard.py` (`test_author_dispatch_observed_*`,
> `test_never_fired_reason_*`); both mutants — helper always-True, reason always-generic — were
> injected and confirmed to kill tests, so the tests can fail. Validated against all five REAL
> workspaces from the incident: the two hijacked rounds, the timeout-killed round, and both
> healthy rounds all classify correctly.
>
> **One refinement the real-data check forced, worth keeping visible:** the gate log alone was
> not sufficient. An author killed by the turn timeout mid-write emits no SubagentStop, so the
> first implementation called the 900s-killed round "never dispatched" when it had in fact
> written a complete 98KB draft — a confidently wrong diagnosis, i.e. the same defect class this
> entry exists to remove. `artefact.md` on disk now counts as proof the author ran, checked
> before the log.
>
> **STILL OPEN — the early abort.** The fix improves the diagnosis; it does not make it cheap.
> A hijacked round still burns the full `--turn-timeout` before reporting. Killing the child once
> no `faba-author` dispatch has been seen for ~90s needs the blocking `subprocess.run` to become
> a polled `Popen`, which is a real change to a spawn path that had just been stabilised — left
> deliberately for a session that can give it its own verification rather than bolting it on
> mid-incident.

**What:** `tools/faba/subagent/run_author_round.py` spawns `claude -p` with `ORCHESTRATOR_PROMPT`,
whose whole job is "dispatch the `faba-author` agent via the Task tool". If the child orchestrator
never makes that dispatch, the round ends `content_gate: never-fired` / `publish_phase:
not_enqueued` — **byte-identical to the outcome where the author WAS dispatched and simply produced
nothing publishable.** The driver already has the discriminator in hand: `gate-log.jsonl` records
`hook_input.agent_type`, which reads `faba-author` on a healthy round and `''` when some other
(untyped) subagent stopped instead. Nothing reads it.

**Why it matters — measured, not hypothetical.** Diagnosing this cost **five failed rounds at
~15 minutes each** on 2026-07-28. Every run looked like "the author didn't write anything", so the
whole investigation was aimed at the author's *inputs* (staged prior, spec availability, session-env
leakage) when the author had never run at all. Four successive input-shaped hypotheses were
confirmed wrong by experiment before a bisect against a known-good control isolated the real
trigger. A fail-fast that said *"no faba-author dispatch observed — the orchestrator did not
delegate"* would have ended it in seconds.

**Fix shape:** after the child exits (or times out), read `gate-log.jsonl`; if no entry carries
`agent_type == "faba-author"`, set `gate_reason` to something like `author-never-dispatched — the
child orchestrator did not delegate; check the --task string` instead of the generic `content gate
never-fired`. Cheap, local to the driver, no new state. Worth pairing with an early abort: if no
faba-author dispatch is seen within the first ~90s, kill the child rather than burning the full
turn budget.

**The trigger that caused it, worth recording as the motivating instance:** a `--task` of ~2.4KB of
imperatives ("Write artefact.md EARLY", "DO NOT pick a side", "PRIORITY ORDER", "REQUIRED: …") is
interpolated into the brief the ORCHESTRATOR reads, and the orchestrator follows those instructions
itself rather than delegating. A ~300-byte task delegates correctly every time. **Bisect evidence:**
holding the 40KB staged record, `--prior-artefact-id` and `--max-turns 100` constant and shrinking
ONLY the task flipped the round from fail to pass. This is the repo's own pointers-not-bodies rule
(`CLAUDE.md § "Briefs are authored off-cockpit"`) applying to `--task` — the durable fix is to keep
`--task` a pointer to an instructions file the author reads, and the driver could plausibly refuse
or warn on a `--task` over some size.

**Related defect, FIXED the same day:** `subprocess.run(..., timeout=900)` was hardcoded while
`--turn-timeout` was parsed and threaded into the lease calculation but never reached the child, so
every value passed was silently ignored. This independently killed a round that HAD produced a
complete 98KB v5 draft, at exactly 900s, before the gate could fire. Patched to
`timeout=args.turn_timeout`.

**Pointers:** `tools/faba/subagent/run_author_round.py` (`ORCHESTRATOR_PROMPT` ~line 90; the
`subprocess.run` child spawn ~line 596; `content_gate` resolution after the `finally` block),
`tools/faba/subagent/subagent_stop_gate.py` (writes `gate-log.jsonl`), memory
`faba-author-round-recipe` (the operating notes this episode should be folded into).
