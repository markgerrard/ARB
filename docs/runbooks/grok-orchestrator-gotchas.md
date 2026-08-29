# Grok as ARB orchestrator — gotchas log

Append-only. Dated entries. No secrets, no DSN/password/token values.

**Companion:** an archived build-orchestrator note (not included in this repository) (first-session path). Protocol truth stays in `skills/using-agent-bridge/SKILL.md`.

**This host (2026-08-19):** `scutil --get ComputerName` = `<operator>’s Mac mini`; `LocalHostName` = `Mac-mini`. Use **`Mac-mini`** as `BRIDGE_WORKER_VANTAGE` (no unicode apostrophe, not `project-h-mbp`).

---

## 2026-08-19 — standing `agy-print` for project-h TDD (Gemini 3.7 Flash)

### Seat that ended live

| Knob | Value |
|------|--------|
| agent_id | `agy-project-h-dev-tdd` |
| engine | `agy-print` |
| model | `gemini-3.7-flash-high` |
| workdir | `/Volumes/<workspace>/repos/project-h` |
| python | `/Users/<user>/<workspace>/.venv/bin/python` |
| env-file | `/Users/<user>/<workspace>/envs/agent-redis-bridge-dev.env` (Redis db12 only) |
| vantage | `Mac-mini` (`BRIDGE_WORKER_VANTAGE` in **process env**, not only the env-file) |
| senders | `grok-project-h-orch=trusted`, `human-codexctl=human` |
| notify | `--notify-inbox 0` |
| supervisor | **nohup** (user-requested). pid/log: `/tmp/arb-seats/agy-project-h-dev-tdd.{pid,log}` |
| ping | `heartbeat=alive` + `consumer=alive` after restart |

Stop: `kill $(cat /tmp/arb-seats/agy-project-h-dev-tdd.pid)`

### Lessons

1. **Homebrew `python -m agent_redis_bridge` has no module.** Live launchd agy seats either wrap `agent-redis-bridge-systemd` or set `PYTHONPATH` to a clone `src/` and use that clone’s `.venv/bin/python`. First nohup with Cellar python died immediately: `No module named agent_redis_bridge`. Registry stayed `missing`.

2. **Copy env flags from launchd plists, do not invent them.** Persistent agy seats live in `~/Library/LaunchAgents/com.example.agy-bridge.*.plist` and `com.example.arbseat.agy-*.plist`. Useful non-secret keys seen on this Mac: `BRIDGE_WORKER_VANTAGE`, `BRIDGE_NOTIFY_INBOX=0`, `BRIDGE_TASK_REF_REQUIRED`, `BRIDGE_SUPERVISOR_PYTHON`, `AGENT_TURN_TIMEOUT=600`, `PATH` including `~/.local/bin` (where `agy` is), `PYTHONPATH=<clone>/src`. Redact `ARB_MEMORY_*` / DSN / password when logging.

3. **`BRIDGE_WORKER_VANTAGE` is the computer name, and it must be in the process environment at spawn.** Registry field is copied from env at boot. FABA `arb-memory-harness-publish` fails with `blank/missing worker_vantage` if the seat registered without it. Restart the seat after changing it; do not only HSET the registry. This host: **`Mac-mini`**. `project-h-mbp` was wrong (this is the mini, not the MBP). Existing project seats use strings like `bridge-dev-mac` / `project-g-consult-ldp-mac` — still override to the computer name when standing a *new* grok-owned seat unless the operator says otherwise.

4. **`BRIDGE_WORKER_VANTAGE` in an env-*file* is not enough** if the bridge reads process env. Plist `EnvironmentVariables` is what launchd injects. Nohup must `env BRIDGE_WORKER_VANTAGE=Mac-mini ...`.

5. **Do not use `--no-enforce-completion` on a durable TDD worker.** Current bridge restricts that flag to `--self-test` / `--once` / `--dry-run`. The 2026-07-11 nohup recipe is stale on that point.

6. **Slice 1d-iv killed free-form dispatch.** `dispatch-dev` and `go-client dispatch -adhoc` both refuse a positional prompt: need `--artefact-id --version --receipt --brief` from `arb-memory-harness-publish` first. A ping (`consumer=alive`) is not a nonce consumption proof.

7. **FABA publish needs two different Redis stories at once:** `ARB_MEMORY_REDIS_URL` (memory bus) sourced only for the publish process, plus `--env-file` with `AGENT_REDIS_*` for the *seat* bus (here `127.0.0.1:6379` db 12). Do not export memory URL into the dispatch step (`env -u ARB_MEMORY_REDIS_URL`). Memory env file: `/Users/<user>/<workspace>/envs/arb-memory-do-dev.env` (never cat it).

8. **Assumptions `vantage` in the brief must equal the target seat’s registered `worker_vantage`.** After the Mac-mini fix, assumed items should use `"vantage": "Mac-mini"`. Using the sender id (`grok-project-h-orch`) as vantage is the wrong field.

9. **Orchestrator `FROM_AGENT_ID` must not be a live seat’s inbox.** `grok-bridge-dev` already has a grok-acp daemon BLPOP’ing that inbox; replies would be stolen. This session uses **`grok-project-h-orch`** (no daemon). Trust it on the worker via `--sender-policy grok-project-h-orch=trusted` at spawn.

10. **Do not borrow another project’s agy seat.** `agy-bridge-dev` is Gemini 3.7 Flash High but `workdir` is <workspace>. Per-project seat: `agy-project-h-dev-tdd` with `--workdir` the project-h checkout.

11. **Nohup is not a supervisor.** User asked for it this session. Seats die on reboot and on a killed parent session depending on how nohup was started. Production pattern is launchd (`com.example.agy-bridge.*` / `com.example.arbseat.agy-*`). Promote if this seat needs to last.

12. **agy model slug for “Gemini 3.7 Flash” is `gemini-3.7-flash-high`** (`agy models`). Medium/low variants exist. Existing launchd `AGENT_MODEL` on `com.example.agy-bridge.bridge-dev.plist` already pins high.

13. **Local bus vs memory bus.** Seat ping uses `127.0.0.1:6379` db **12** prefix `agent_scratch:`. Prod ARB Memory is a different host/db (see 2026-07-11 notes). `envs/agent-redis-bridge-dev.env` has blank `AGENT_REDIS_*` in some historical copies; `dispatch-dev` hardcodes localhost fallbacks — verify with `rg '^(AGENT_REDIS_|AGENT_PROJECT)'` on the file you actually pass.

14. **agy `--print` auth smoke can ignore your ACK instruction** and lecture about `--model`. Treat non-empty stdout as “CLI is logged in”, not as a protocol probe. Real consumption still needs a FABA dispatch.

### Dispatch identity (this session)

```text
FROM_AGENT_ID=grok-project-h-orch
BRANCH=feat/swift-grokbot
AGENT_ENV_FILE=/Users/<user>/<workspace>/envs/agent-redis-bridge-dev.env
--engine agy-print --target-id agy-project-h-dev-tdd
```

Brief `## Assumptions` JSON items: `"vantage": "Mac-mini"`.

15. **Do not write the seat workdir (or its git-common untracked paths) while a worktree dispatch is in flight.** Task 1's completion gate returned `ok:false` `worktree_escape` / `incomplete: uncommitted changes` even though the worker committed `61ae4c9d` in `.claude/worktrees/task1-protocol`. Causes: (a) orchestrator created `.superpowers/sdd/.../task-2-brief.md` in the parent checkout mid-turn; (b) worker left `task-1-report.md` untracked after commit. The reply `head_before`/`head_after` stayed on the parent SHA. Recover by inspecting the worktree log and fast-forwarding the feature branch. Next dispatches: no parent-tree edits; reports to `/tmp/arb-seats/`.

16. **Treat `ok:false` as a claim, then look at the worktree.** Agy's prose said DONE + SHA; the envelope said incomplete. The SHA was real on the worktree detached HEAD. Always `git -C <worktree> log` before retrying the task.

17. **`scripts/agent-bridge-ping --engine` enum does not include `agy-print`.** Help lists `codex|grok-acp|devin-acp|cline-acp|pi-sdk`. Passing `--engine agy-print` mints a bogus `agent_id` (`agy-project-h---workspace` / `agy-project-h-tdd`) with `registry=missing` even while the real seat `agy-project-h-dev-tdd` is alive. Prove liveness from pid + Redis `agent_scratch:agent:agy-project-h-dev-tdd:status` / `:consumer`, or from a real FABA dispatch. Do not treat a ping-helper miss as a dead seat.

18. **agy-print reply payloads are narration-heavy.** Task 6's `result` string was a dozen "I will wait for coverage/typecheck" lines before the summary. The load-bearing evidence is `completion.state` (`committed_clean`) plus `git -C <worktree> log` / orchestrator re-run of the named vitest. Review the worktree diff after each task; do not treat the prose as the review.

19. **2026-08-19 evening: worktree TDD dispatch returned empty `agy --print` stdout.** Seat `agy-project-h-dev-tdd` was heartbeat+consumer alive; `agy models` and `agy --print 'Reply with exactly PONG'` worked. Two `--worktree` TDD briefs (`task14-omit-effort`, `task14b-omit-effort`) both ended `agy --print pid N produced no output` with log line `agy-print granular transcript disabled: tool call decode failed at idx=1`. Earlier same-seat worktree tasks today succeeded. Do not wait on a retry loop for a morning MVP — implement the load-bearing Swift path in the orch session and re-probe agy later. Worktrees were still created; leftover dirs under `.claude/worktrees/task14*`.
