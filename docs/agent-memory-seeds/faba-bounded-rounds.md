---
name: "faba-bounded-rounds"
description: "FABA bounded rounds: gated publish/return channel, driven phase boundaries, and failure-safe Agent-SDK bounce/non-closable rules."
metadata:
  type: reference
  origin_session_id: "seeded-by-claude-orchestrator-20260721"
  last_write_session_id: "019f8591-dad5-71a0-849e-5b60a1b2a4cc"
  source_project_key: "mark-be695e9f393d"
---

# FABA bounded per-round orchestration

FABA replaces the warm, ever-growing orchestrator session with a bounded per-round agent: born clean, pulls state from ARB Memory, does one round's work, writes its decision record, dies. Doctrine: warmth was never the asset — bounded statefulness with verified succession was. Three layers: a dumb parent (task ids, pointers, commit gate); the round agent (panel evidence + empirical verification + decision record); a separate per-round author (author ≠ verifier).

## Rules for every round agent

1. Round K+1 knows round K only through the stored decision record (evidence cells, reopen-if scopes, Basis commit), never session memory. Do not relitigate settled findings; check reopen-if scopes.
2. Return-channel rule: reply only with the exact harness pointer shape. In the bridge probe harness this is the single `FABA_EXIT` line. The body belongs in the leased workspace file and is published only by the parent gate. If the reply includes the document or extra prose, the workflow failed regardless of document quality.
3. The integrity gate belongs to the harness: it validates the record from disk, publishes it, and verifies its own receipt. A workspace draft or polished prose is never a decision record without that path.
4. Verify seat findings first-hand with filesystem/exec evidence before dispositioning them; budget turns, not tokens.
5. Author `ok=true` proves transport only. The orchestrator must publish/stage the exact subject, verify its receipt, and record store hash plus raw UTF-8 SHA-256 before panel dispatch.
6. Headless phase boundaries require designated drivers: author→stage, stage→panel, votes→adjudication, adjudication→close. Before a turn ends, the current turn completes the boundary, a live process owns it, or an explicit continuation is arranged. Seat completion never drives the next phase by itself.
7. Agent-SDK bridge bounce capability must be proven, not assumed. Calibration on authority-authentication round 2 (`faba-auth-r2c-20260722`) showed this failure sequence: the seat wrote a substantial worktree draft and executed tests, but replied with extra prose rather than the single `FABA_EXIT` line; the harness correctly refused it, then its resume bounce failed `thread-affinity-worktree-incompatible`. Result: `content_gate=failed`, `publish_phase=not_enqueued`, no content check, no receipt. Never manually publish such a draft. Preserve it as provenance and end non-closable when the retry/consult budget is exhausted.
8. Before selecting an Agent-SDK target as FABA, preflight both sides of recovery: exact return-only compliance and worktree-compatible thread resume. If resume compatibility is absent, use a harness/seat mode whose declared bounce semantics are stateless or demonstrably worktree-compatible; do not discover the mismatch after the only permitted correction bounce.
9. Long rounds run under one background event watcher. Yield immediately and let terminal notification resume the orchestrator; do not keep the chat turn alive with repeated waits or conversational polling.

## Model choice

Structural wins are model-agnostic: bounded context, durable succession, and crash recovery. Measured 2026-07-19 at panel scale: bounded peak context ≤101k vs warm 150–180k; flat marginal round cost; crash recovery from durable keys returned 8/8 seat replies after killed waiters. On identical inputs Fable beat a Sonnet synthesiser by ~27% cost, 36% lower peak, and better recall (caught 3 findings Sonnet dropped) — so Fable remains the measured cost/recall default, but the harness is model-swappable only where the selected transport satisfies the same publish, return-channel, containment, and bounce gates. A codex/GPT round agent is UNTESTED: treat its first run as a calibration datapoint, not assumed parity.

Provenance: ARB Memory `art-81438f2f5a5c4955` (ADR, v13+), `art-96faf77da9149e80` (explainer/economics, v1); 2026-07-22 panel chains `panel-polisher-interface-r1-20260722T142244Z-91060c`, `panel-authority-auth-r1-20260722T142244Z-d29116`, and failed/non-closable FABA lineage `faba-auth-r2-20260722` → `faba-auth-r2b-20260722` → consult `consult-auth-faba-r2-20260722` → `faba-auth-r2c-20260722`. See [[arb-round-convergence]] and [[faba-worktree-recovery]].

The ADR pin is a FLOOR on purpose, not sloppiness: it moved v15→v30 in ~29 hours on 2026-07-19/20 (author `faba-au-changelog`), so an exact pin would report stale within hours of being written. Read the ADR's current head, not a fixed version. Two things a reader of the changelog should know: store v16 was a mis-published author draft — a patch instruction set, not the ADR body — superseded by v17, which reproduces the last-good v15 body plus three edits and records the erratum; and the code pointer is now `AgentRedisBridge` `dev` `tools/faba/` — the old ARB `feat/faba-harness` branch is stale/history per the ADR's own v14(c) entry.
