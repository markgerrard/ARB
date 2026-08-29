# Dispatching implementation briefs through the bridge

> **Status note (2026-07-06):** examples here use `--engine pi-rpc`; the documented
> default worker engine is now `pi-sdk` (`docs/implementor-routing.md`, 2026-06-07).
> The examples remain valid for pi-rpc fallback seats.

Grounded in the 2026-06-06 `qwen3-coder-next` 10-task gate against
dev.project-f. The gate succeeded only after two non-obvious things were
right; this doc captures both so future callers don't relearn them by losing
work.

## TL;DR — the worker-brief preamble

Every implementation brief sent to a full-tools pi-rpc worker must open with
this preamble. Without it, the model uses absolute paths from the brief verbatim
and writes outside the worktree.

```
WORKING CONTEXT:
You are in an isolated git worktree. Your current working directory is the
worktree's root, a fresh checkout of the `<branch>` branch of a <stack> app.

PATH RULES (strict):
- Use ONLY relative paths in all file operations (read, write, edit, ls).
- Do NOT use absolute paths starting with `/home/` or `/`.
- Do NOT navigate above your cwd with `..`.
- All file paths below are relative to your cwd.

EXECUTION RULES:
- Make ONLY the change described below.
- Do not run tests, composer, php artisan, git, or any other command.
- Make the code edit and stop.
```

Then the task-specific body (target file as a relative path, change to make,
explicit prohibitions, stop conditions). End with the exact reply contract:

```
OUTPUT: After making the edit, reply with ONLY the single line `done`
(lowercase, no period). Nothing else.
```

### Why the preamble matters

The bridge's `--worktree` flag sets pi's cwd to the worktree, so cwd-relative
file ops land inside the worktree by construction. But pi has a `write` tool
that accepts absolute paths — and a brief that names target files with absolute
paths invites the model to use those paths verbatim.

In the gate's first attempt, two of three dispatched tasks wrote to the **main
checkout** because their briefs said
`TARGET FILE: /home/<user>/dev.project-f.../app/Models/...`. The model copied
the path. The bridge's worktree was technically created but the writes never
landed in it.

The preamble makes the relative-path constraint explicit. After it was added,
all 10 dispatches landed inside their respective worktrees.

## The completion-gate / `--expected-artifact` contract

After the brief-preamble fix, 8 of 10 dispatches in the 2026-06-06 gate came
back with `ok:false, error:"incomplete: uncommitted changes, no commit
(commit, or mark NO_COMMIT)"`. The model **did make the changes correctly**;
the bridge's orchestrator-commit gate (commit `5d9380d`) flagged them because
nothing authorised the auto-commit.

The bridge's commit gate has three terminal states for `dirty_uncommitted`:

1. Agent committed something → bridge verifies and adopts.
2. Agent didn't commit but every dirty file is inside the dispatch's
   `expected_artifacts` (or matches an `--allowed-path` prefix) → bridge
   commits for the agent.
3. Otherwise → fail with `incomplete: uncommitted changes`.

`agent-dispatch` now (post-gate) surfaces the relevant fields as CLI flags
(matching the long-standing `agent_redis_bridge.ctl send` shape):

```
--expected-artifact <relative-path>   # repeatable; populates envelope's
                                      # expected_artifacts array.
--allowed-path     <relative-prefix>  # repeatable; populates envelope's
                                      # allowed_paths array.
--commit-message   <message>          # optional; populates envelope's
                                      # commit_message override.
```

For a worker dispatch where you want orchestrator-commit to adopt the changes:

```
agent-dispatch \
  --engine pi-rpc \
  --target-id pi-<project>-dev-qcn-worker \
  --worktree gate-task-3 \
  --worktree-base main \
  --expected-artifact app/Models/Account.php \
  --commit-message 'worker: gate task 3 — add casts to Account' \
  --run-id "gate-task-3-$(date -u +%Y%m%dT%H%M%SZ)" \
  "$(cat docs/worker-brief-preamble.txt) Your task: …"
```

For a migration task where the exact filename has a timestamp the dispatcher
can't predict, combine the two:

```
  --allowed-path database/migrations/ \
  --commit-message 'worker: add description column to settings'
```

The bridge's commit gate (`completion_gate.py:90`+) reads these fields; no
bridge-side change is needed.

### Earlier workarounds (now superseded by the CLI flags)

For reference if you find dispatches sent before the flags landed:

1. **Tell the agent to commit** in the brief itself: "After editing, stage the
   file and commit with the literal message `worker: <task-N>`". The brief
   explicitly authorises the agent's git call, the model commits, the gate
   adopts. Bloats the brief but works.
2. **Score from the working-tree diff** (`git diff <base_ref>`) and ignore
   the dispatch reply's `ok` field. This is what the 2026-06-06 gate did.
   Cheap, but a downstream orchestrator that trusts the reply will mark the
   work failed.

Both still work; both are now unnecessary for new dispatches.

## Worker brief checklist

Before sending an implementation brief to a full-tools pi-rpc worker:

- [ ] Preamble at the top (path rules + execution rules).
- [ ] Target file(s) named with **relative** paths, not absolute.
- [ ] Every "do NOT X" prohibition that you genuinely need is listed (the
      model will follow them; ones you don't list, it may add as helpfulness).
- [ ] Stop conditions for assumption failures ("if file already has Y, STOP
      and reply explaining"). Saves a worktree from a bad-assumption dispatch.
- [ ] Reply contract is exact (e.g. "reply with ONLY the single line `done`").
- [ ] If you want a committed result, either tell the agent to commit OR plan
      to score from working-tree diff (see above on the gap).
- [ ] If the brief survey makes claims about the file (column names, method
      signatures, presence of imports), **verify them yourself** before
      dispatch — a survey error becomes a wasted dispatch.

## Pre-flight checklist for new gate targets

If you're standing up a 10-task gate against a fresh codebase:

- [ ] Confirm baseline test suite is green from a fresh worktree (`vendor`
      symlinked, `.env` copied). A red baseline makes "tests pass" meaningless.
- [ ] Verify vendor's autoloader `$baseDir` points at this repo, not a
      sibling worktree (someone may have run `composer install` from another
      worktree). If it points elsewhere, regenerate with `composer
      dump-autoload` from the canonical workdir.
- [ ] Make sure no other orchestrator is actively working in the same workdir
      (worktrees in `.claude/worktrees/` are a signal).
- [ ] If repointing a daemon at a new workdir, set `AGENT_TRUSTED_SENDERS` in
      the per-seat env to include the sender ID you'll use (`claude-<project>-dev`).
