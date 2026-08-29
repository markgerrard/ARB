"""ARB warm orchestrator — Agent SDK warm session with typed dispatch and gates.

Test-drive slice of the buzz control-plane pilot (design record:
ARB Memory art-4a69641bf02ec070). The warm orch is a second consumer of the
agent_redis_bridge engine parts with inverted defaults: workers shed context
between dispatches (retire-after-turn); the orchestrator keeps context as its
value. Drive it from a terminal now, from an ACP client later — same object.
"""
