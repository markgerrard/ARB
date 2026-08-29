---
name: worktree-flag-cannot-resume
description: agent-dispatch --worktree ALWAYS creates and hard-fails if the directory exists; a second dispatch into the same worktree must use the prose pattern
metadata: 
  node_type: memory
  type: reference
  originSessionId: 5bb925d0-80ee-483f-96bb-e04e8c412b41
  modified: 2026-07-28T06:04:49.341Z
---

Learned 2026-07-28 (ARB Slice 1d-iv, dispatching Task 5 into the worktree Task 4 built).

**`--worktree <name>` is create-only.** The bridge runs `git worktree add --detach <base>`
unconditionally, so dispatching a second time with the same name fails closed:

```
worktree setup failed: git worktree add failed: Preparing worktree (detached HEAD …)
fatal: '/Users/<user>/<workspace>/.claude/worktrees/<name>' already exists
```

It fails cleanly — no partial work, no side effects — but the dispatch does not run. There is no
`--worktree-resume`/`--worktree-reuse` flag as of this date.

**Two more traps in the same manoeuvre:**

1. **Omitting `--worktree-base` does NOT default to `origin/dev`** — it bases on the seat
   workdir's current HEAD (observed: `detached HEAD 27af2b75`, <workspace>'s tip, not dev's).
2. **A brief pushed to `dev` after the worktree was created is NOT in that worktree.** The worker
   resolves the brief path relative to its own tree and simply cannot see the file. Push the brief
   BEFORE creating the worktree, or materialise it into the worktree afterwards.

**How to run a multi-dispatch stage in ONE worktree:** first dispatch uses
`--worktree <name> --worktree-base origin/dev --worktree-cleanup keep`. For each later dispatch,
prepare the tree yourself (`git -C <wt> fetch origin && git cherry-pick <brief-commit>` so the
brief exists and the tree is clean), then dispatch with **no** `--worktree` flag and put the
worktree path in the task prose — the soft Pattern-A form: *"Worktree: <abs path> — cd into it as
your FIRST action and do ALL work there; do not modify <seat workdir>."* That form is soft
isolation (the engine's cwd is still the seat workdir), so verify the base checkout is clean
afterwards. Related: [[benchmark-harness-destroys-its-own-data]].
