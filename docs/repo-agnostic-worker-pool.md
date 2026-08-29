# Repo-agnostic codex worker pool ("repo as a job parameter")

**Status:** DESIGN / PROPOSED — not built. Intended as a **parallel, greenfield worker variant**;
the existing per-repo bridges and the `--worktree` feature stay untouched. Default-off by
construction (a worker only behaves this way if launched as a pool worker and the dispatch
carries the new fields).

**Filed:** 2026-06-02 by Claude (project-g-laravel-dev), after a source review + a live probe of codex
cwd/thread behaviour (see "Evidence" below).

---

## The idea

Today a bridge instance is pinned to one repo: `AGENT_WORKDIR` is baked in at launch, so you run
one daemon per repo (`codex-project-g-laravel-dev`, `codex-other-dev`, …). The orchestrator must target
a repo-specific `agent_id`.

The goal: a pool of **N warm, repo-agnostic codex workers**. The orchestrator dispatches jobs —
each tagged with its target repo — to **one generic inbox**; any free worker serves it, in the
right repo, with a cold per-job context. This is the **CI-runner-pool model**: generic runners,
the *job* names the repo.

---

## Evidence (what's proven vs. assumed)

**PROVEN by probe** (live codex app-server, 2026-06-02; `scripts/codex_cwd_probe.py` +
`scripts/codex_thread_soak.py`):

- codex's working directory is a **per-turn protocol parameter**, not an OS-level process binding.
  `CodexEngine.start()` (`src/.../engines/codex.py:41`) spawns the app-server with **no `cwd=`**;
  the real working dir is sent in `thread/start` (`codex.py:81`) and re-sent on every `turn/start`
  (`codex.py:107`).
- On **one** warm app-server (stable pid across all three turns):
  - changing only the `turn/start` cwd on an existing thread → codex executed in the new repo
    (`git rev-parse --show-toplevel` reported the new path);
  - a fresh `thread/start` with a new cwd on the same app-server → new `thread_id`, new repo, **no
    respawn**.
- So **warm process + per-job repo + per-job cold context** is achievable simultaneously with no
  app-server spawn cost.
- **Discarded threads don't leak** (`scripts/codex_thread_soak.py`, 30 new-thread→turn→discard
  cycles on one warm app-server): RSS, open-fd count, and OS-thread count were **flat across all 30
  cycles** — `rss=41984 kB`, `fds=17`, `os_threads=11`, `+0` delta on every axis, 0 turn failures.
  So the new-thread-per-job loop is safe at this scale. (Caveat: RSS sampled at rest between turns
  and is page-granular; 30 cycles is a steady-state check, not a thousands-of-jobs endurance run.)

**CONFIRMED in source:**

- An engine can be built at any cwd: `build_engine(args, *, cwd)` (`bridge.py:877`), already called
  with a non-workdir cwd for worktree tasks (`bridge.py:412`).
- A per-job engine distinct from the pool, with steer/cancel routed to it, already exists:
  `task_engines` (`bridge.py:127-130, 420, 506-508`).
- The inbox is keyed on `agent_id` (`derive_agent_id` `bridge.py:903`); Redis `BLPOP` delivers each
  `LPUSH` to exactly one blocked consumer, so N workers sharing one `agent_id` is competing-consumers
  for free.
- Trust is keyed on **sender**, not repo: `sender_policies` (`bridge.py:270, 762`).
- codex runs `--dangerously-bypass-approvals-and-sandbox` → `sandbox="danger-full-access"`
  (`codex.py:80`), so the per-repo workdir is **not** a filesystem containment boundary today — it
  is only a default cwd. Going repo-agnostic exposes no new filesystem reach.

**ASSUMED / TO-VERIFY before build:**

- Endurance at scale: the soak above ran 30 cycles perfectly flat, but a worker handling thousands
  of jobs over days hasn't been exercised. Low risk given the zero-growth result; if paranoid,
  recycle workers periodically (à la `--max-jobs`), which sidesteps the question entirely.
- gemini-acp / agy behaviour under per-job cwd (see Caveat 1) — not in scope for V1.

---

## What ties a daemon to a repo today

`self.workdir` (from `AGENT_WORKDIR`, `bridge.py:78-79`) is woven into four places:

| Use | Site |
|---|---|
| Pool engine cwd | `bridge.py:136` (`build_engine(args, cwd=str(self.workdir))`) |
| Worktree anchor | `bridge.py:357` (`git -C self.workdir worktree add`) |
| Branch detection | `bridge.py:80` (`git_branch(self.workdir)`) |
| Registry `path` | `bridge.py:165` |

Repo-agnosticism = make the *cwd* per-job and stop deriving identity/branch from a single workdir.

---

## Design

### Envelope (additive, default-off)

- `payload.cwd` — absolute repo root for this job. Absent → today's behaviour exactly (pooled
  engine at the daemon's workdir).
- Optional `payload.fresh_thread` (default **true** for a pool worker) — start a new thread for the
  job so the context is cold. (A pool worker should default to fresh-per-job; see "Cold context".)
- `payload.worktree` (existing) still composes: a worktree is created under `payload.cwd`'s repo for
  the 2-writer case (see Caveat 2).

### Per-job execution (the core loop)

For each dispatched job, on a **warm pooled app-server**:

1. Validate `payload.cwd` against the allowlist (below); reject before any engine work if it fails.
2. `engine.cwd = payload.cwd`; `thread/start(cwd=payload.cwd)` → fresh `thread_id`.
3. Run the turn (`turn/start` already carries cwd).
4. Discard the thread (don't reuse it); **keep the app-server warm** in the pool for the next job.

**One thread = one job.** That single rule gives warm process + correct repo + cold context at once.

### Identity / inbox

Launch as `--agent-id codex-pool` (or `codex-pool-<workspace>`). The orchestrator targets that one
id and puts the repo in `payload.cwd`. Two topologies (orthogonal to everything above):

- **A — one daemon, `max_parallel=N`, warm engines re-pointed per job.** Minimal change; recommended
  for the MVP. The daemon is a SPOF, but `Restart=always` + `verify-bridge-supervision` already
  cover that.
- **B — N single-slot worker processes sharing one inbox** (the Laravel `queue:work --workers=N`
  competing-consumers model). More crash isolation, more moving parts. Adds **one** net-new
  requirement: dedup is per-process today (`seen_request_ids`, `bridge.py:93`), so B needs a
  Redis-backed dedup set. Defer to a later phase.

### Security — path allowlist

A pool worker will `thread/start` codex at any `payload.cwd`. Validate it against a configured
allowlist of permitted repo roots (e.g. `AGENT_ALLOWED_ROOTS=/srv/project-g-laravel:/srv/...`), reject
traversal, require the dir exists and is a git repo. Given bypass-sandbox, **the real trust
boundary remains the dispatch layer** (`sender_policies` — who may push jobs); the allowlist is
cheap footgun-prevention (typo'd path, wrong repo), not the primary control. Both should be on.

### Cold context

Use **a new thread per job** (`thread/start`), not a "clear" first-turn. A new thread is cold *by
construction* — the prior job's turns aren't in its history. A clear-turn on a reused thread is
*soft* (the model can still see prior turns) and codex exposes no history-clear primitive anyway
(the bridge client uses only `initialize` / `thread/start` / `turn/start` / `turn/steer` /
`turn/interrupt`). This also lets a pool worker serve **cold reviewers** structurally — fresh
thread, reads only what the brief points at.

---

## Caveats / known boundaries

1. **Codex-only.** gemini-acp / agy pin cwd at `session/new` and crash on a missing dir
   (`bridge.py:38`), so they can't be re-pointed per job without per-job session re-init + existence
   checks. This is a **codex implementer pool**; a reviewer pool on agy is separate work.
2. **cwd-per-turn ≠ write isolation.** Re-pointing cwd is safe for read-only jobs and a single
   writer per repo. Two concurrent *writers in the same repo* still collide — that's what
   `--worktree` (collision-free by construction) is for. Orthogonal; layer it on only for that case.
3. **Thread-coldness ≠ read-coldness.** A fresh thread wipes conversation history; what the agent
   *reads* off disk (AGENTS.md at the cwd, the diff, files) is still governed by the brief.
   Brief-cleanliness discipline still applies for genuinely cold reviews.
4. **Registry semantics.** A generic worker's registry `path`/`branch` no longer map to one repo;
   register it as repo-agnostic and report the per-job repo/branch in task status instead.

---

## Acceptance (for when it's built)

- `payload.cwd` absent → byte-identical to today (pooled engine, daemon workdir).
- Job with `payload.cwd=/repoA` runs codex in repoA; a concurrent job at `/repoB` runs in repoB;
  both replies route correctly (routing is already per-`id`).
- Cold context: job 2 cannot see job 1's conversation (e.g. job 1 "remember the word BANANA", job 2
  "what word did I ask you to remember?" → doesn't know).
- `payload.cwd` outside the allowlist (or traversal / non-existent / non-git) → rejected before any
  engine work.
- Warm: app-server pid stable across N sequential jobs to different repos (no respawn).
- (Topology B only) two workers on one inbox each handle a given job exactly once.

---

## Out of scope for V1

- gemini/agy generic pool (Caveat 1).
- Topology B (N-process competing consumers) — designed above, but MVP is A.
- Cross-host dispatch over a managed bus.
- Auto-merge of agent commits across repos.

---

## See also

- [`worktree-isolated-dispatch.md`](worktree-isolated-dispatch.md) — per-task cwd / collision-free
  writes; this doc generalises its `build_engine(cwd=…)` mechanism from "worktree under my repo" to
  "any allowlisted repo in the job".
- [`bridge-parallelism.md`](bridge-parallelism.md) — `--max-parallel` and the EnginePool (Topology A
  rides on this).
- [`pipeline-operating-manual.md`](pipeline-operating-manual.md) — the workflow layer that would
  dispatch to such a pool.
