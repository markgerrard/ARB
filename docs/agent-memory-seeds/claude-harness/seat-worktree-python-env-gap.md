---
name: seat-worktree-python-env-gap
description: FIXED at bridge level (dev 89b152b3, 2026-07-19) — worktrees now mirror a gitignored base .venv; briefs still name .venv/bin/python explicitly
metadata: 
  node_type: memory
  type: project
  originSessionId: ccb5f209-b148-478f-ab2f-c9336ae1633e
  modified: 2026-07-19T21:04:05.437Z
---

Bridge `--worktree` review worktrees used to lack the repo's `.venv`; a codex seat running
plain `pytest` there hit `ModuleNotFoundError` (PyYAML observed) and silently fell back to
source-tracing — losing the panel's execution-verification axis (r27/r28 OI/Pi panels,
2026-07-17).

**FIXED 2026-07-19** (AgentRedisBridge dev `89b152b3`): `create_worktree` mirrors a
**gitignored** base `.venv` into every fresh worktree as a real `.venv/` directory of
top-level symlinks (a plain symlink would not match the directory-only `.venv/` ignore
pattern and would bounce turns off the completion gate). Bridge-project seat daemons
restarted onto this code 2026-07-19; arb-codex seats run the ARB clone (feat/faba-harness)
and pick it up when that branch merges dev.

**F2 CLOSED by copy-on-write (dev `dee2d503`, 2026-07-19, owner-directed):** the mirror now
real-dirs `bin/` + the `lib*/…/site-packages` chain and rewrites editable hooks to the
worktree, so mirrored test runs exercise WORKTREE code and new pip installs land
worktree-side (verified un-mocked: sentinel edit imported; pycowsay install left base
untouched). P2 folds F3/F4/F6/F9/F11/F12/F13 landed in the same commit.
**Fleet restarted onto CoW 2026-07-19 ~22:03** (owner-directed): <workspace> fast-forwarded to
`166c4c12` and all nine running bridge-dev seats kickstarted (codex sol/luna, agy, pi-glm,
pi-m3, devin, grok, asdk-opus48, asdk-sonnet5) — dispatch worktrees on the bridge-dev fleet
now get the CoW mirror. Excluded: the pi orchestrator's arb-codex fleet and other project
fleets (other clones, other owners). Residuals (documented, not defects): upgrading/
uninstalling a symlinked dist through the mirror can reach the base venv; bin entry-point
script shebangs name the base interpreter — briefs must use `.venv/bin/python -m …`.

**How to apply now:** review briefs that must execute tests should still name
`.venv/bin/python -m pytest` explicitly — the venv is *discoverable in the worktree*, not
on `PATH`, so bare `pytest` still misses. Ask seats to report the exact error on any
collection failure rather than skipping silently. If a worktree lacks `.venv`, the seat's
daemon predates the fix or the base repo doesn't gitignore `.venv` — check the bridge log
for `[worktree] linked base .venv` / `worktree-venv-link-failed`.
Related: [[bridge-turn-timeout-backlog]].
