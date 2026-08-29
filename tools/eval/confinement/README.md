# Confined eval-seat (decision panel Option C — absence-based confinement)

A reviewing seat can read the whole disk (codex/agy sandboxes restrict writes, not reads), so an
un-confined seat can read the scenario / seed-manifest / **answer-key** and game the floor — a
looked-up catch is invisible in the verdict. This confines the seat so the answer-key is **absent from
its namespace by construction** (not denied by a policy that can have a gap — the panel chose absence
over denial deliberately; see `../../docs/eval-seat-confinement.md`).

## The boundary
The seat runs in a container whose **entire read-surface** is: this image (Node + codex + git) + the
**fixture repo bind-mounted read-only** + engine auth read-only. The scenario/answer-key is **never
mounted** → there is no path to it, enumerated or not. The container *is* the boundary, so codex runs
`--dangerously-bypass-approvals-and-sandbox` (its own sandbox would only further-restrict and adds
fragility; panel nesting answer).

## Self-setup (portable: macOS+OrbStack or Linux/CI, plain `docker`)
- `bash build.sh` — builds `arb-eval-seat:latest` from `Dockerfile` and runs the canary as a smoke.
- `canary.sh <fixture-dir> <scenario.json>` — **proves the boundary per run**: fixture readable AND
  the answer-key (seed/control IDs) absent from the entire jail read-surface (image layers + fixture
  incl `.git`). Run this before any scored dispatch; `Park` on failure. Verified against a negative
  control (planting the key into the fixture makes it fail).

## Status
- **PROVEN:** the absence boundary + canary (positive pass + negative control). The image + fixture
  mount provably exclude the answer-key.
- **VERIFIED (codex):** `confined-review.sh` runs `codex exec` in the jail and codex authenticates
  (auth.json+config.toml mounted RO -> writable CODEX_HOME), investigates the fixture, and emits
  matcher-compatible findings (all 5 secrets-in-logs mechanisms on floor-secrets-full) — a catch earned
  by construction (no answer-key in the namespace to look up).
- **WIRED:** `confined-review.sh` is the eval `ContainerDispatcher` path for `arb-eval run
  --confined`; it owns timeout/canary classification and nonce-fenced provenance capture. The live
  scored class-level run remains a manual gate because it needs docker + real engines.

## Read-surface scope of the absence proof
The canary + `_assert_no_answer_key_in_fixture` cover the **eval-authored** read surface — the image
and the fixture's **working tree (tracked AND untracked) + full git history** (`git log -p`, so a
token scrubbed from the worktree but live in compressed objects is still caught). The other mounts the
review container exposes are the seat's **own credentials/state** — `~/.codex` auth.json/config.toml,
the `agy-home` OAuth volume — which live outside the eval tree and are never where a scenario/answer-key
is written; they are trusted, non-eval mounts, deliberately not part of the answer-key-absence check.
(Hardening follow-on if the threat model ever includes a compromised credential store: grep every
mounted path inside the review container before the engine runs.)

## Not closed by confinement (pipeline hygiene — see the design doc)
The seat reads the fixture fully, so seed identity must not leak *in-band*: scrub fixture git history
to a neutral commit, keep source marker-free (enforced for IDs incl. untracked files + history by
`_assert_no_answer_key_in_fixture`), don't name seeds in the dispatch prose, keep the scenario path out
of the seat's env.
