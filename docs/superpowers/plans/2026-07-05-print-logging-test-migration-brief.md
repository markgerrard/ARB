# Implementation brief — migrate print-based test asserts to assertLogs

**Branch:** `feat/daemon-logging-and-acp-tests` (you are in a worktree already checked out at/near its tip — run `git log --oneline -3` to confirm you see commits `bf84e45` + `04e6246`; if detached, `git switch -c feat/daemon-logging-and-acp-tests-codex` and note the branch name in your report).

**Context.** Commit `bf84e45` converted the bridge daemon's `print(..., flush=True)` diagnostics to leveled `logging` calls (`[bridge-error]` → `logger.error`, `[bridge-warning]`/`WARNING` → `logger.warning`, everything else → `logger.info`; message text including bracket prefixes is UNCHANGED). The logger is `agent_redis_bridge.bridge`'s module logger (`logging.getLogger("agent_redis_bridge.bridge")`). That commit is deliberately RED on 5 test files that still assert via `mock.patch("builtins.print")`. Your job: migrate those asserts to logging-based asserts. **Do not modify anything under `src/` unless you find a conversion bug (wrong level, mangled message) — fix minimally and call it out in your report.**

## Files and exact assertions to migrate

All are unittest-style classes. Use `self.assertLogs("agent_redis_bridge.bridge", level="INFO")` (or the exact level you expect) around the code that logs, and assert on `cm.output` / `cm.records`. For NEGATIVE assertions (message must NOT appear) use `self.assertNoLogs(...)` where the code emits nothing at all, or capture with `assertLogs` at a broad level and assert absence — beware `assertLogs` FAILS if zero records fire inside the context.

1. **tests/test_reliable_inbox.py** — 5 tests currently `mock.patch("builtins.print")`:
   - `[bridge-error] inbox-handle-failed boom` (level ERROR)
   - `[bridge] recovered in-flight envelope id=req-recovered` AND `id=unknown` (INFO)
   - `[bridge] shutdown with parked envelope id=req-shutdown (will recover on restart)` (INFO)
   - two tests asserting `[bridge-warning] blmove-unsupported falling back to blpop (at-most-once delivery)` appears **exactly once** — preserve the count assertion (count matching records, not just `any()`).
2. **tests/test_bridge_capacity_gate.py** — 2 sites: `[bridge-error] control-fail redis blip`, `[bridge-error] control-lane-non-control dropped` (ERROR).
3. **tests/test_bridge_handle_raw.py** — sites asserting: `[bridge-warning] structured-reply-parse-failed task_id=req-9 ...` (positive) and a NEGATIVE assert on the same string in another test; negative assert on `worktree-setup-failed`; `[bridge-warning] fresh-context-unsupported engine=codex task_id=req-15`; `[bridge-warning] fresh-context-reset-failed engine=codex ... error=boom`; `[bridge-warning] role-profile-unavailable path=` (all WARNING).
4. **tests/test_bridge.py** — two tests asserting the multi-line startup warnings (now `logger.warning` fired during `Bridge.__init__` inside `make_env_bridge`): "no sender policies configured" and "max_parallel>1 with notify_inbox=1". Note the message is a single logging call whose text spans concatenated string literals — assert on substrings.
5. Sanity-check no OTHER test asserts on converted prints: `grep -rn 'builtins.print' tests/` and inspect each hit; migrate any I missed that target `src/agent_redis_bridge/bridge.py`/`engines/{agent_sdk,pi_rpc,grok_acp}.py` daemon prints. (`tests/test_agent_sdk_subscription.py`'s capsys assert targets the stderr passthrough that was deliberately KEPT as print — leave it.)

## Gotchas
- Tests run without any logging config → only `assertLogs`' own handler sees records; that's fine. Do NOT add global logging config to tests.
- Engines' new loggers are `agent_redis_bridge.engines.pi_rpc` / `.grok_acp` — irrelevant to these 5 files, don't touch their tests.
- `mock.patch("builtins.print")` context managers also SUPPRESSED output; after migration remove the mock entirely (don't leave a dead patch).

## Verify (must all pass before you commit)
```sh
python3 -m pytest tests/test_reliable_inbox.py tests/test_bridge_capacity_gate.py tests/test_bridge_handle_raw.py tests/test_bridge.py -q
python3 -m pytest -m "not e2e" -q   # DB-gated tests will skip without ARB_MEMORY_DSN — that's expected; everything else must pass
```
If `python3 -m pytest` can't import the package in your worktree, prefix `PYTHONPATH=$(pwd)/src` AND verify `python3 -c "import agent_redis_bridge, sys; print(agent_redis_bridge.__file__)"` points INSIDE your worktree (editable-install shadowing is a known footgun).

## Deliverables
1. The test migration, committed on the branch with a conventional message explaining what AND why (reference commit `bf84e45` as the conversion it completes).
2. A short report at `docs/superpowers/reviews/2026-07-05-print-logging-test-migration-report-codex.md` (committed): what you changed per file, test results (paste the two pytest tails), anything you found wrong in the src conversion, any deviations from this brief.
3. `git push origin HEAD` (push the branch with your commits).

End your reply with a one-paragraph summary + the commit SHA(s) you pushed.
