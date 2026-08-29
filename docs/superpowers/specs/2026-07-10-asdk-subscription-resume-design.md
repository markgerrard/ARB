# Agent-sdk subscription thread-continuation fix — design/spec

Date: 2026-07-10 · Workflow A · Author: warm orchestrator (inline)
Status: spec — closes the latent bug found during the asdk retire triage

## Problem

`--thread-id` continuation on a SUBSCRIPTION agent-sdk seat silently loses context.
`resume_thread()` (agent_sdk.py:696-709) disconnects and rebuilds the client, but
`_build_subscription_options()` hardcodes `resume=None` — and the `explicit_resume=True` flag
added in `375d37a` is dropped at the subscription branch (`_build_options` line 265 returns
`self._build_subscription_options()` without forwarding it). The continuation dispatch replies
`ok=true` with a fresh conversation: the caller believes the seat remembers its round-1
findings when it remembers nothing.

The hardcoded `None` was justified by a comment (agent_sdk.py:322-332) claiming the CLI
"cannot locate the prior conversation" under the randomized per-process `CLAUDE_CONFIG_DIR`.
That claim is STALE: the installed claude-agent-sdk has `materialize_resume_session`
(`_internal/session_resume.py`) — when `resume` is paired with `session_store`, the SDK loads
the transcript FROM THE STORE, writes it into a temp `CLAUDE_CONFIG_DIR` laid out like
`~/.claude/`, copies auth config, and points the CLI at it.

**Live-proven 2026-07-10** (probe script, real haiku-4.5 subscription seat token): phase 1
stored nonce `HERON-4D1B77E2-MOSS` in session `8d46c9be-...`; phase 2, a FRESH process with
the engine's own subscription options plus `resume=<sid>`, replied
`RECALL HERON-4D1B77E2-MOSS` on the same session id. Resume across processes works via the
store — the one constraint is that the store key includes
`project_key_for_directory(options.cwd)`, so the resuming engine's cwd must equal the
original session's cwd.

## Fix (two parts)

### 1. Thread `explicit_resume` through the subscription branch

```python
def _build_options(self, *, explicit_resume: bool = False) -> ClaudeAgentOptions:
    if self.spec.subscription:
        return self._build_subscription_options(explicit_resume=explicit_resume)
    ...

def _build_subscription_options(self, *, explicit_resume: bool = False) -> ClaudeAgentOptions:
    ...
    resume=self._last_session_id if explicit_resume else None,
```

Auto-resume at connect stays OFF for subscription seats (correct, and consistent with
retirement). Only `resume_thread()` — the explicit continuation path — passes the id.

### 2. Fail-loud pre-check in `resume_thread`

When the store cannot serve `(project_key(cwd), thread_id)`, the SDK's materialization
returns None and falls through to raw CLI `--resume` under a randomized config dir — the
historical crash. Pre-check in `resume_thread`, before any disconnect/rebuild:

```python
store = FileSessionStore(self.session_root, self.agent_id)
key = {"project_key": project_key_for_directory(self.cwd), "session_id": thread_id}
entries = <run store.load(key) on the engine's loop thread, bounded timeout>
if entries is None:
    raise EngineError(
        f"thread-resume-unavailable: session {thread_id} not in the session store "
        f"for cwd {self.cwd}"
    )
```

- `project_key_for_directory` is a public claude_agent_sdk export.
- Applies to BOTH subscription and API-key branches (both materialize via the store).
- The bridge already converts the raise into a legible `thread-resume-failed: ...` reply
  (bridge.py:1798-1801). Net behaviour: same-cwd continuation genuinely resumes;
  wrong-cwd/unknown-id continuation fails loud. No path replies success with a fresh context.
- A worktree continuation dispatch (`--worktree` + `--thread-id`) has the worktree's cwd →
  store miss → loud error. That is CORRECT: worktrees are single-use
  (`git worktree add` refuses an existing path), so no worktree continuation can ever share
  the original cwd. Cross-cwd continuation support is out of scope.

## Non-goals

- Oneshot seats keep refusing `resume_thread` (existing loud error — sonnet5/bridge-opus48).
- No new dispatch params; no bridge.py changes.
- No worktree-reuse mechanism.

## Tests (extend the existing FakeClient/captured-options patterns in `tests/test_agent_sdk_engine.py`)

New file `tests/test_agent_sdk_resume.py`:

1. Subscription engine, `_last_session_id` set: `_build_subscription_options()` → `resume is None`;
   `_build_subscription_options(explicit_resume=True)` → `resume == sid`.
2. The flag survives the branch: subscription engine, `_build_options(explicit_resume=True)`
   → `resume == sid` (this is the exact dropped-flag bug).
3. `resume_thread` happy path: seed the store file at
   `<session_root>/<agent_id>/<project_key(cwd)>/<sid>.jsonl` (one JSON line), engine with a
   FakeClient factory capturing options → `resume_thread(sid)` reconnects with
   `captured.resume == sid`.
4. `resume_thread` miss: no store file → raises `EngineError` matching
   `thread-resume-unavailable`, and NO reconnect happened (client factory not re-invoked).
5. API-key branch parity: same happy/miss pair with a non-subscription model (e.g.
   `minimax-m3`), retire default ON (proves explicit resume beats the retire gating from
   `375d37a`).

## Live gates (post-deploy)

- Engine-level (the fixed code path end-to-end, real subscription token): nonce turn on
  engine 1 → `stop()` → fresh engine 2, `start()`, `resume_thread(sid)`, recall turn →
  exact nonce. (Probe already exists; rerun through `resume_thread` instead of hand-built
  options.)
- Bridge-level (live haiku45 seat): `--worktree probe --thread-id <sid>` dispatch → reply is
  `ok=false` with `thread-resume-failed ... thread-resume-unavailable ...` — loud, not
  silently fresh.

## Deployment

Merge dev → fleet clone pull → restart haiku45 + project-e-opus48 (the stateful asdk seats;
the two oneshot seats refuse resume regardless, but restart all 4 for uniform code).
