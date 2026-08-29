# Worktree-isolated dispatch ("collision-free by construction")

**Status:** SHIPPED 2026-05-31. `--worktree` on `agent-dispatch`; bridge runs the
task on a fresh single-use engine whose cwd is the worktree. Default-off (no
`worktree` payload field → behaviour byte-identical). Tests:
`tests/test_bridge_worktree.py` (incl. the base-checkout-untouched isolation probe).
**Filed:** 2026-05-31 by Claude (project-g-laravel-dev), after an external review flagged worktree-collision as the last discipline-enforced gate.

> **Resolution / divergence from the sketch below:** the worktree is created with
> `git worktree add --detach <path> <base_ref>` (base_ref defaults to `HEAD`, not
> the current branch) so it never hits "branch already checked out" when the main
> workdir holds that branch — the agent gets a detached checkout it can commit on,
> and the caller branches from it afterwards if needed. Cleanup defaults to `keep`
> (don't destroy the agent's work); `auto` removes the worktree after the reply.
> A worktree task still takes a pool slot (respects `max_parallel`) but runs on a
> fresh engine, not the pooled one.

## The gap

Parallel file-mutating dispatches can collide. Today, collision avoidance is the
**caller's responsibility** (see `bridge-parallelism-followup.md` design-consideration #2:
"collisions are the caller's responsibility, same as today"). The skill's mitigation
(`skills/using-agent-bridge/SKILL.md` ~L43-53) is "worktree-everything": the caller
manually `git worktree add`s a subdir and tells the agent, **in the brief prose**,
"Worktree: <path>. Work only here."

That is still discipline, just relocated:

- The orchestrator must remember to create the worktree + inject the instruction.
- The **agent** must honour it — codex runs with `cwd = AGENT_WORKDIR` (the repo root,
  fixed per bridge instance, `bridge.py:110` → `build_engine(..., cwd=str(self.workdir))`),
  so nothing *stops* it writing outside the worktree subdir. Two parallel impl
  dispatches that both disregard the instruction collide on the same checkout.

"By construction" means the agent's **cwd is the worktree** — it physically cannot
touch the base checkout — not "the agent was asked nicely."

## Why it isn't a cheap client flag

The dispatch envelope (`scripts/agent-dispatch`) carries only
`{id, from, branch, to, kind, sent_at, payload:{task}}` — **no per-task cwd**. The
bridge builds ONE engine per type via `EnginePool`, all sharing `self.workdir`
(`bridge.py:110`). The engines (`engines/{codex,generic_acp,grok_acp}.py`) take `cwd`
at construction and hand it to `session/new`. So per-task isolation needs bridge-side
changes, not a client flag.

## Design (bridge-side, default-off)

1. **Envelope:** optional `payload.worktree = { "base_ref": "<ref>", "name": "<slug>" }`
   (or an explicit `payload.cwd`). Absent → today's behaviour exactly.
2. **Bridge, on receiving a worktree task:**
   - `git -C <workdir> worktree add <workdir>/.claude/worktrees/<name> <base_ref>`
   - build a **fresh, single-use** engine with `cwd=<worktree path>` (NOT a pooled
     engine — pooled engines are keyed on the shared workdir; a per-task cwd is by
     definition not poolable). This is the main interaction with the parallelism
     `EnginePool`: worktree tasks bypass the pool and count against `max_parallel`
     separately.
   - run the turn, capture the reply, then (policy below) clean up.
3. **Cleanup policy (pick one, make it explicit):**
   - `auto` — `git worktree remove --force` after the reply (good for read-only
     reviews; loses the agent's uncommitted work).
   - `keep` — leave the worktree for the caller to inspect / push / remove (matches
     the skill's current "review at leisure, push from inside, then remove" flow).
   Default should be `keep` for impl dispatches (don't destroy work), `auto` only on
   explicit request.
4. **Client:** `agent-dispatch --worktree <name> [--base <ref>] [--worktree-cleanup auto|keep]`
   sets the envelope field. Default off.
5. **Per-engine safety:** confirm each engine tolerates a fresh per-task session in a
   new cwd (codex spawns a node app-server per engine — cost is real; worktree tasks
   are heavier than pooled ones). Document the cost.

## Python venv mirror (landed 2026-07-18; copy-on-write 2026-07-19)

A worktree checkout omits the base repo's untracked `.venv`, so a seat running plain
`pytest` there hits `ModuleNotFoundError` and silently degrades to source-tracing —
losing the panel's execution-verification axis (observed on the r27/r28 OI/Pi boundary
panels; memory `seat-worktree-python-env-gap`). `create_worktree` therefore mirrors a
**gitignored** base `.venv` into every fresh worktree as a real `.venv/` directory.

Shape (post-CoW, panel `panel-faba-econ-20260719T070835Z-b6871d` finding F2):

- **`bin/` and the `lib*/…/site-packages` chain are real directories** with symlinked
  entries; everything else (`pyvenv.cfg`, `include`, …) is a top-level symlink. `bin`
  must be a real dir because CPython finds `pyvenv.cfg` relative to the *unresolved*
  executable path only when the executable file itself is the symlink — a symlinked
  `bin` directory resolves the prefix back to the BASE venv.
- **Copy-on-write editable hooks:** any small `.pth`/`.py` in site-packages whose text
  names the base workdir (setuptools/uv `__editable__*` files) is copied with the path
  rewritten to the worktree (single-pass alternation over the macOS `/private` alias
  forms), so the mirrored interpreter imports the **worktree's** checkout of the base
  repo's own package — a worktree-local edit is what a mirrored test run exercises.
- **Pip writes land worktree-side:** new installs go into the worktree's real
  site-packages, not the shared base venv. Residual: upgrading/uninstalling a
  *symlinked* dist can still reach the base — the mirror is for running tests, not
  managing packages. Use `.venv/bin/python -m …`, not the bin entry-point scripts
  (their shebangs name the base interpreter).
- **Ignore check follows the worktree's own ref:** the gitignore decision is asked of
  the worktree's checked-out rules (its `base_ref`), not the base tip; `check-ignore`
  rc 1 (not ignored) is distinguished from rc >1 (git error, logged as
  `worktree-venv-ignore-check-failed`).
- **Best-effort with rollback:** any OSError mid-mirror logs
  `[bridge-error] worktree-venv-link-failed` and removes the partial `.venv` entirely —
  a worktree without a venv is degraded, not broken. POSIX symlink semantics assumed.
- Seats should invoke `.venv/bin/python -m pytest` — the mirror makes the venv
  *discoverable in the worktree*; it does not put it on `PATH`.

## Acceptance

- A dispatch with no worktree field behaves exactly as today (pooled engine, fixed workdir).
- `--worktree foo --base main` runs the agent with `cwd` = a fresh worktree off `main`;
  a probe (`agent writes a file; git -C base status` shows base untouched) proves
  isolation **by construction** — the base checkout is unmodified regardless of what
  the agent does.
- Two concurrent `--worktree` dispatches to the same bridge do not collide (distinct
  worktrees) and both replies route correctly (reply routing is already per-`id`).
- Cleanup policy honoured; `keep` leaves a usable worktree, `auto` removes it.

## Cheaper interim (does NOT meet the "by construction" bar)

A caller-side `--worktree` flag on `agent-dispatch` that only (a) creates the worktree
subdir and (b) prepends "operate only within <path>" to the task — no bridge change.
This automates the 3 manual steps but leaves isolation as the *agent's* discipline
(cwd is still the repo root). Worth shipping as a stopgap **only if** labelled as soft
isolation; it is not the hazard-removed-by-construction the review asked for.

## Caveat: file-based test fixtures are still shared mutable state

`--worktree` isolates the *code* — but a test harness that writes a **file-based test
database** (e.g. `database/test.sqlite`) inside the worktree reintroduces a shared-mutable
hazard one layer down. The bridge can't fix a consuming project's test setup, but the
worktree pattern *exposes* this, so it's worth knowing before you parallelise. Two failure
shapes seen in practice:

- **Sibling collision.** Distinct worktrees each get their *own* `database/test.sqlite` (it's
  a relative path inside each checkout), so on a plain filesystem siblings don't collide. The
  collision appears when the path resolves to **one shared location** — a fixed absolute path
  (`/tmp/test.sqlite`), or (the common case) worktrees run in containers that **bind-mount** the
  workspace/DB to the same host path — and the two suites clobber each other's fixtures mid-run.
  Give the harness a **per-worktree or per-process** DB path (a `$TMPDIR`/PID-qualified file, or
  an in-memory DB where supported) so even a shared mount can't collide.
- **Cross-container / cross-UID ownership.** When the *implementer* runs tests in a
  container as `root` and the *orchestrator* re-runs them as its own UID, a `root`-owned
  `test.sqlite` left in the worktree is unwritable by the orchestrator's runner — surfacing
  as a flood of `attempt to write a readonly database` / phantom failures that look like a
  code regression but are an environment artifact. Remove-and-recreate the fixture between
  cross-UID runs, or make the harness write to an ephemeral per-run path it always owns.
  (This is the generic shape of the shared-checkout hazard, one layer down in the test
  fixtures rather than the git tree — `--worktree` closes the tree face but not this one.)

## Out of scope for V1

- Nested/recursive worktrees, worktree reuse across tasks, auto-merge of agent commits.
