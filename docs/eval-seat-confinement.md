# Eval seat read-confinement — design & decision

**Status:** RESOLVED + BUILT. Human chose **C (container)** over S (Panel 6 was 2–1 for C on
by-construction robustness; the §Recommendation below is the pre-decision analysis, kept for the
record). Built + proven: `ContainerDispatcher` + `tools/eval/confinement/` (Dockerfile/build.sh/
canary.sh/confined-review.sh); canary verified (positive + negative control); codex confined scored
PASS (MILESTONE 2, un-cheatable by construction); agy confined too, and the jail **held against an
escaping seat** (agy tried remount/overlay/sudo — all blocked). Integrity guard fail-closed, gated on
the canary (not a settable var). Remaining: agy output-format wrapper + detection/format split; the
canary's readable-surface seed-grep as a per-run pre-flight; pipeline hygiene. Branch
`feat/p3-fixture-corpus`. Sources: decision-panel log (Panel 6, MILESTONE 2, multi-seat run).

## Problem
The floor suite dispatches a reviewing seat (codex/agy via the bridge) to investigate a fixture repo
and report defects; an off-quorum normalizer + oracle then score caught-vs-noise against a **scenario
file that holds the seed/control answer-key**. The first live class-level run (milestone, both seats
PASS, verified earned) also showed a seat **searching the workspace for that answer-key**. The run was
clean only because the seat's search tool skipped the `.claude/` dot-dir the key sat in — *luck, not a
control*. A looked-up catch produces an NDJSON record **identical** to a real catch, so a gamed verdict
is invisible downstream — the floor-suite analogue of the NDJSON-authoritativeness bug. Any seat with
file tools (all reviewing seats, by design) can do this. **This must be closed by confinement, not
placement, before any further live verdict is trusted.**

## Traced fact: the bridge offers SELECTION + write-confinement, NOT read-confinement
- `--worktree` = a worktree of the **main repo** (still contains scenarios) — selection, not isolation.
- `--allowed-path` = post-turn write/commit validation only.
- codex `-s read-only|workspace-write|danger-full-access` all permit **full-disk reads**
  (`sandbox_permissions=["disk-full-read-access"]`); `-C/--cd` adds **writable** dirs, not read limits.
- ⇒ a seat can read the answer-key in **any** mode or cwd. Read-confinement must be ADDED at the OS
  layer; it is not a bridge flag.

## Shipped (fail-closed enforcement — mechanical, not convention)
`BridgeDispatcher.assert_read_confined()` (`pipeline.py`): `run_floor` refuses a live bridge dispatch
unless `ARB_SEAT_CONFINED_ROOT=<fixture>` is set (only a real confinement wrapper sets it); else
`Parked`. So live floor runs are **disabled** until a jail exists. `ARB_ALLOW_UNCONFINED_FLOOR=1` is an
explicit, integrity-UNGUARANTEED escape (diagnostics only). Test: `test_read_confinement_guard_is_fail_closed`.

## Convergence (all three seats / probed) — true regardless of S vs C
1. **Nesting:** run codex `--dangerously-bypass-approvals-and-sandbox` **inside** the outer jail; do NOT
   nest its own sandbox. Probed: Seatbelt nesting is **monotonic** (a nested jail can only further
   restrict — an un-jail attempt from inside was blocked), so the outer jail is strictly the stronger,
   authoritative boundary; and a real outer `(deny file-read*)` makes codex's inner `sandbox_apply`
   fail exit-71. Bypassing the inner sandbox loses nothing for read-confinement and removes the fragility.
2. **Confinement is necessary but NOT sufficient — in-band leaks survive any OS jail:**
   - **Fixture `.git` history / comments / seed markers** — "read the fixture fully" includes `.git`;
     seed-naming commits/branches/comments put the answer *inside what the seat may read*.
   - **Dispatch/task prose** — readable by construction; must not name the defect class or seed.
   - **Scenario path in env/argv** — a hint, and a lever combined with any copy primitive.
   - **Normalizer/oracle** — keep off-quorum; its inputs/outputs must never touch the seat's tree.
3. **Build-regardless integrity layer (the deepest takeaway):**
   - **Canary self-test (per run, mandatory):** before any scored dispatch, read a known canary token
     in the key tree *through the actual jail* and assert it is DENIED; `Park` if not. Rationale: a
     profile that *looks* correct but uses a non-canonical path **silently fails open** (probed: a
     logical `/tmp/...` deny didn't match `/private/tmp/...` and the key read cleanly — an invisible
     fail-OPEN). Don't trust the profile text; test the boundary each run. Realpath every path.
   - **Readable-surface seed-grep (per run, mandatory):** `grep -r` the entire seat-readable surface
     (fixture worktree + its `.git` + any seat-readable temp/cache) for the seed/canary token before
     scoring; `Park` on any hit. Converts "trust nobody copied the key in" into a mechanical pre-flight.
   - **Pipeline hygiene:** squash fixture history to a single neutral commit; strip seed-naming
     comments; audit the dispatch template; do not pass the scenario path into the seat's env/argv.

## Mechanism options (the open decision)
The boundary: the seat reads the **fixture repo FULLY** (the investigation we measure) + its toolchain;
reads the **scenario / seed-manifest / answer-key NOT AT ALL** (same shape as the normalizer's
role-boundary — each component gets exactly the access its job needs).

### S — `sandbox-exec` / Seatbelt profile (cold-opus, the adversary; probed on this host)
- Profile: `(allow default)` + `(deny file-read* (subpath "<CANONICAL key tree>"))`; codex run with its
  inner sandbox bypassed. **Default-deny allowlist was probed and REJECTED** — it SIGABRT-kills the
  toolchain (`cat` exit 134); a Node/codex CLI touches far more.
- **Held against every probed escape:** absolute path to the key, `/tmp`↔`/private/tmp` alias, planted
  absolute symlink, relative `../` symlink, directory listing — all `Operation not permitted`.
- **Residual escape (S-specific):** a **pre-placed copy** of the key inside the readable fixture subtree
  (or `.git`, a temp/cache, `CODEX_HOME`) reads cleanly. The seat can't make this copy (it can't read
  the key), so the danger is a *trusted* pipeline step placing it → closed by the readable-surface
  seed-grep above.
- **Pros:** deployable on this macOS host today; kernel-enforced; light; no infra. **Cons:** correctness
  depends on canonical paths + no-copies — made mechanical only by the canary self-test; macOS-only.

### C — container (codex, agy; the stronger end-state)
- Seat runs in a container with **only the fixture bind-mounted read-only** + a scratch write dir; codex
  inner-sandbox bypassed, the container is the boundary.
- **Strongest by construction:** the answer-key is **absent from the seat's namespace** — nothing to
  deny, nothing to copy *from*; closes the pre-placed-copy residual that S can't. Won't rot.
- **Cons:** heavy on macOS — containers run in a Linux VM, so codex's macOS-Seatbelt story changes, its
  toolchain must be re-homed in the image, and dispatch is reworked. Disproportionate for the first
  milestone on this host.

## Recommendation
1. **Build the integrity layer first (mechanism-independent, no regret):** canary self-test +
   readable-surface seed-grep + pipeline hygiene. This is the real per-run boundary verification and is
   needed under either jail. Re-confirm the milestone under it.
2. **Jail: lean S + both rails now** — probed to hold, deployable today, and the canary self-test makes
   the boundary *verified per run* (answering "deny-policy is fragile" by testing it, not trusting it).
   **Migrate to C** if copy-leakage ever proves real, if eval moves to Linux/CI, or if hard
   multi-tenant isolation is needed. (Panel was 2–1 toward C on robustness-by-construction; the dissent
   — S — is the adversary that actually probed the escapes. Human decides; this doc is the record.)

## Current confinement state
The jail has shipped (ContainerDispatcher `5864d83`, Option C `00a1ac8`, merged
`c1d38f7`). Live floor runs no longer depend on the old "until a jail ships" hold; they
must run through the jail plus the integrity layer. What remains gated is operational:
confirm the selected seat is a confinable external process, keep the canary/integrity
checks enabled per run, and do not score in-session subagents as floor seats.

## Constraint: a scored floor SEAT must be a confinable EXTERNAL process (cold-Opus / in-session subagents)
cold-Opus has two roles; only one is a confinement question.
- **Meta-reviewer of the instrument** (reviewing ARB's corpus/pipeline/design): it SHOULD read the
  scenarios + answer-key — that is its job (auditing the floor), and it is never scored on catching
  seeds. Confinement does not apply. This is how cold-Opus was used throughout P-3.
- **Scored floor seat** (measured on catching seeded defects): an in-session Agent-tool subagent
  CANNOT be jailed — it runs in the orchestrator's harness with read-everything tools over the whole
  checkout; there is no container/namespace to make the answer-key *absent*, and path tool-scoping is
  the deny-based (by-policy) approach the panel rejected. So **an in-session subagent is disqualified
  as a scored seat** — it has agy's cheat-hole and is structurally un-confinable.

**Handling:** a scored floor seat MUST be a confinable external process. To use *Claude* as a floor
seat, run Claude Code CLI in the container (installed + authed like codex/agy), dispatched via
`ContainerDispatcher` jailed — same absence-by-construction boundary + canary. The in-session
cold-Opus stays a meta-reviewer only, never a scored seat. (Sharpens SC-1: orchestrator-driven is not
sufficient; the scored seat must be containerized. `DEFAULT_SEAT_MAP` is bridge engines only;
`ContainerDispatcher.seats` is the confinable set — add a containerized `claude` here, never the
in-session subagent.)
