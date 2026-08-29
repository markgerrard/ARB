---
name: "arb-overview"
description: "ARB = Agent Redis Bridge: Mark's multi-agent orchestration system on this Mac (seats, dispatch, panels)"
metadata:
  type: user
  origin_session_id: "seeded-by-claude-orchestrator-20260721"
  last_write_session_id: "seeded-by-claude-orchestrator-20260721"
  source_project_key: "unresolved:/Volumes/<workspace>/repos/codex-arb-artifacts"
---

ARB ("Agent Redis Bridge") is Mark's multi-agent orchestration system running on this Mac mini.
It turns local agent CLIs (codex, grok, agy/gemini-family, pi-hosted models) into
Redis-addressable "seats": an orchestrator LPUSHes a task envelope to a seat's inbox, the bridge
daemon runs the engine, and the reply lands back on the sender's inbox. Seats are managed as
launchd LaunchAgents (com.example.arbseat.*) and health-checked with scripts/agent-bridge-ping.
Substantial work is dispatched with scripts/dispatch-dev using per-round run-ids; multi-model
REVIEW PANELS (codex pins sol/terra, luna as implementor, grok, agy, pi-GLM, cold-Opus
subagents) review specs and implementations to zero P0/P1 with an audited vote trail. You
(codex) are frequently a seat OR the interactive orchestrator's engine in this system. Related:
[[arb-repo-map]], [[codex-fork-work]], [[operator-rules]].
