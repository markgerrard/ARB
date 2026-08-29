# Authoring brief: arb-watch-history design (reconstructed v1)

Author a design for adding historical seat visibility to the ARB watch surface. Ground the design in the bridge's existing eval event stream and the Go TUI's live seat model. The design must keep live state and historical state conceptually separate, avoid trusting stale warm-orchestrator memory, and name verification gates that would catch dead vote branches, missing eval allowlist fields, and history pagination/cursor regressions.

Inputs to read: `src/agent_redis_bridge/bridge.py`, `src/agent_redis_bridge/eval_tee.py`, `src/arb_memory/visibility.py`, and `tools/arb-watch-go/model.go`. Output a design only, not a task plan.
