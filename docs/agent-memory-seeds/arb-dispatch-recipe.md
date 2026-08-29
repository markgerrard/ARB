---
name: "arb-dispatch-recipe"
description: "Canonical ARB dispatch and recovery: size-aware delivery, capability preflight, right-sized turn ceilings, stall staging, fast-close sequencing, and audit-safe replay."
metadata:
  type: feedback
  origin_session_id: "seeded-by-claude-orchestrator-20260721"
  last_write_session_id: "019f8a36-1443-7ab1-9924-5d7cbde71ede"
  source_project_key: "codex-rs-862ab84b364f"
---

# Canonical ARB dispatch and recovery

Use from `/Users/<user>/<workspace>`. **Free-form positional task strings were REMOVED in Slice
1d-iv (2026-07-28)** — every ordinary request/worktree_run needs the pre-minted quartet
(publish first, then dispatch):

    # 1) publish the brief (needs ARB_MEMORY_REDIS_URL in process env; receipts are TARGET-BOUND)
    scripts/arb-memory-harness-publish --target-agent-id <seat-id> \
      --brief <brief.md> --env-file envs/agent-redis-bridge-dev.env > receipt.json
    # 2) dispatch through the authority (brief MUST carry the FABA block: H1 + "## Assumptions"
    #    + ```json {"items": []}``` fence + "## Instructions" — the publish gate refuses otherwise)
    FROM_AGENT_ID=<trusted-sender> BRANCH=<branch> \
    AGENT_ENV_FILE=envs/agent-redis-bridge-dev.env \
    scripts/dispatch-dev --workspace dev --engine <engine> \
      --target-id <seat-id> --timeout 5400 --run-id "$RID" \
      --artefact-id "$(jq -r .artefact_id receipt.json)" --version 1 \
      --receipt receipt.json --brief <brief.md> \
      [--turn-timeout <seconds>] [--audit-panel] [--worktree NAME --worktree-base origin/dev]

How to apply:

- **Wave-1 (2026-07-29): 7 bridge-dev seats run `BRIDGE_TASK_REF_REQUIRED=1`** (codex
  sol/luna/terra, agy, cursor, devin, grok). A legacy string task to them is refused with exact
  `invalid-payload-task-ref` — and the sender gets NO reply envelope, just a timeout. A
  ref-required dispatch that times out with no reply: check the seat's launchd log for
  `envelope-invalid invalid-payload-task-ref` BEFORE diagnosing a hung seat.
- `--worktree` is CREATE-ONLY: it hard-fails if the directory exists. A second dispatch into the
  same worktree uses the prose pattern with a pre-staged brief, not the flag.

- Mint one run-id per round and reuse it verbatim. Emit the exact roster manifest as audit seq 1 before dispatch.
- Preflight with raw `scripts/agent-dispatch --check`; `dispatch-dev --check` is not the preflight surface in this checkout.
- Non-trivial tasks use a brief file or staged inputs. For isolated FABA/author seats, every input must be inside the leased worktree and repo-relative; host-absolute evidence paths cause `worktree_escape`.
- Do not inline a large artefact into every bridge task. Polisher v13 embedded 133 KB into five prompts; all five dispatchers hit the 3600-second ceiling and the run became manifest-only/non-closable. Prefer staged bytes or an authenticated pointer with capability preflight.
- A pointer is safe only if each seat actually exposes the required read tool. Preflight tool capability—not just heartbeat—before choosing pointer delivery. The v13 Opus/Fable abstain cause is now fixed: every ARB-Memory-enabled bridge seat, including asdk seats, exposes the read-only `arb-memory-local` MCP (`memory_get`, `memory_search`, `memory_recent`) against the PROD store. Pointer delivery is viable for every lens seat. If availability ever differs, stage the immutable body instead.
- The `dev` local-read tier is near-empty and has no panel subjects; all seat plists were flipped to `prod` on 2026-07-22. If `memory_get` returns `null` for a known artefact, suspect a tier/store mismatch before anything else. Tier resolution lives in `local_memory_mcp_config()`, with a venv-anchored server command (<workspace> dev `e56f1c1` plus follow-ups).
- Capability preflight is a convergence gate, not just hygiene. Losing a certifying lens can make approval unreachable for the round; compensating FABA must execute that lens firsthand, is slower and single-perspective, and can support only conservative block/needs-changes outcomes—not approval.
- Right-size `--turn-timeout` to the work: about 1800 seconds is a sane review ceiling; reserve 3600 seconds for author/FABA-scale turns. Keep client `--timeout` above the requested turn ceiling. Trusted senders may request below the seat default.
- Treat a persistent `[stall]` with no progress for roughly 10–15 minutes as a preparation signal: stage the mechanical `timed-out` vote and prepare the FABA workspace/inputs while the lease runs, so closure can fire immediately at the ceiling. Do not consume the inbox or kill the dispatcher to abort early; stall-abort is not an available bridge feature.
- Prepare the staged-input manifest and FABA workspace during the seat wait. Fast-close target: when the last roster outcome lands, emit/reconcile the mechanical outcome, dispatch the already-prepared FABA run, confirm its receipt, and close without further setup latency.
- Record three wall-clock timestamps in every orch log: panel dispatch, last roster outcome, and audit close. Their deltas separate seat-wait cost from close-ceremony cost.
- Pin both the store content hash and raw staged-file SHA-256. The 2026-07-22 live gates proved the dual-pin path: `probe-asdk-memtool-r3-20260722T120056Z` fetched v13 on `asdk-bridge-dev-opus48` and matched the store content-hash pin `0043f809…5236`, while its fail-closed tool ceiling blocked local recomputation; `probe-asdk-memtool-fable-r3-20260722T120101Z` fetched v13 on `asdk-piext-dev-fable5` and recomputed raw SHA-256 `004abb71e01b7aa28451d6e190dbb720c515125be6aec5fba3e841a1112c5d7d`, an exact pin match. Either verified pin is sufficient; STOP only if both fail.
- Sender identity and continuation are one decision: a headless turn dispatches under its own seat identity, never a borrowed `FROM_AGENT_ID`, because replies route by sender identity and need either the dispatcher process to remain alive as the completion wait or a bounded-rounds continuation arranged before the turn ends. “Dispatch running” followed by end-of-turn is not a workflow. Use one persistent event-driven watcher; never poll Redis or conversationally poll seats. Single-shot task `status`/`result` is allowed only for an explicit progress request or terminal recovery.
- If the waiter dies, recover from the durable, bridge-written `task:<id>:result` key (multi-reader), then inspect status and audit once. Never recover from or consume any inbox. Never duplicate an existing actor vote.
- If a seat returns a valid literal fence but auto-audit misses it, confirm the actor has no audit row, then replay the exact captured full reply through `arb-panel-vote`. Do not interpret or rewrite.
- Record mechanical absence via `arb-panel-vote --timed-out`. For invalid/missing fences, re-fire rather than manufacture a stance.
- FABA lease TTL must exceed the harness lower bound; for 3600-second child/turn ceilings, 14400 is known-good.
- Author tasks must explicitly forbid repo edits when the deliverable belongs in the staged artefact. Asking an isolated author to materialize a helper script can dirty the worktree and fail the content gate.
- Do not claim dispatcher stall-abort exists. Until implemented, wait to the ceiling and preflight/stage content around actual seat capabilities.

Calibration: Polisher v13 r1b ran about 68 minutes. All other seat outcomes were known by 08:10 BST; GLM reported no progress since 08:14:50, consumed the 3600-second ceiling until 09:04, and the staged FABA path reached receipt-confirmed close at 09:10. The dead stall dominated the round; the prepared close path added about six minutes.

Why: these rules preserve immutable evidence, append-only votes, responsive orchestration, and approval reachability while minimizing transport-generated gaps and avoidable round latency.
