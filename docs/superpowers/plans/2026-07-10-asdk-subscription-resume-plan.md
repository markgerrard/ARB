# Agent-sdk subscription thread-continuation — implementation plan

Date: 2026-07-10 · Workflow A · Spec: `docs/superpowers/specs/2026-07-10-asdk-subscription-resume-design.md`

Scope: `src/agent_redis_bridge/engines/agent_sdk.py` ONLY (+ tests + changelog). No bridge.py
changes, no engine_pool changes.

## Task 1 — TDD

**Step 1 — failing tests.** New file `tests/test_agent_sdk_resume.py` per the spec's five
test groups. Reuse the FakeClient / captured-options patterns from
`tests/test_agent_sdk_engine.py` (see `test_completed_session_id_persists_without_autoresume_for_next_engine`
for the client_factory capture idiom). For the store-seeded tests, write the JSONL at
`<session_root>/<agent_id-safe>/<project_key(cwd)>/<sid>.jsonl` — compute the project key with
`from claude_agent_sdk import project_key_for_directory` and reuse
`agent_redis_bridge.engines.agent_sdk_session._key_path` if convenient. Use a valid UUID for
`sid` (the SDK's materializer rejects non-UUIDs).

Run `pytest tests/test_agent_sdk_resume.py` — the explicit-resume and pre-check tests must
FAIL first (flag currently dropped; no pre-check exists).

**Step 2 — implementation, all in `engines/agent_sdk.py`:**

(a) Signature + forwarding:

```python
    def _build_options(self, *, explicit_resume: bool = False) -> ClaudeAgentOptions:
        if self.spec.subscription:
            return self._build_subscription_options(explicit_resume=explicit_resume)
```

(b) `_build_subscription_options(self, *, explicit_resume: bool = False)` — replace the
hardcoded `resume=None` and REWRITE the stale comment block (agent_sdk.py:322-332) to state
the real contract:

```python
            # Subscription seats never auto-resume at connect (fresh context per
            # engine is the retire-after-turn contract). Explicit resume_thread()
            # continuation DOES resume: the SDK materializes the transcript from
            # session_store into a temp CLAUDE_CONFIG_DIR (session_resume.py), so
            # it works across processes — live-proven 2026-07-10. The store key
            # includes the cwd's project key, so resume requires the same cwd;
            # resume_thread pre-checks that and fails loud on a miss.
            resume=self._last_session_id if explicit_resume else None,
```

(c) Pre-check at the top of `resume_thread`, after the oneshot/empty-id guards:

```python
        from claude_agent_sdk import project_key_for_directory

        store = FileSessionStore(self.session_root, self.agent_id)
        key = {"project_key": project_key_for_directory(self.cwd), "session_id": thread_id}
        if self.loop_thread.loop is not None:
            entries = self.loop_thread.submit(store.load(key)).result(timeout=15)
        else:
            entries = asyncio.run(store.load(key))
        if entries is None:
            raise EngineError(
                f"thread-resume-unavailable: session {thread_id} not in the session store "
                f"for cwd {self.cwd}"
            )
```

(import `FileSessionStore` at module top — it is already imported for the stores; check.
Put the `project_key_for_directory` import at module top too if the SDK export is stable.)

Run the new file → all pass; then
`pytest tests/test_agent_sdk_retire.py tests/test_agent_sdk_engine.py tests/test_agent_sdk_resume.py tests/test_engine_pool.py`
→ no regressions; then full suite, comparing failures against the 6 known pre-existing
diagnose-fixture reds.

**Step 3 — commit:**
`fix(agent-sdk): explicit thread continuation resumes via session store (fail-loud on miss)`

## Task 2 — CHANGELOG entry (what AND why)

## Orchestrator-owned

1. agy-print review → merge → push → fleet pull → restart 4 asdk seats.
2. Engine-level live gate (resume_thread nonce recall) + bridge-level loud-miss gate.
