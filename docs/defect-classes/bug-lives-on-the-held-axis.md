# The bug lives on the held axis

**The meta-pattern under most of the others.** A unit suite holds environmental and configuration axes
*constant* (the default prefix, autocommit on, the test fixture's env, a single process). A property is only
ever exercised at one point on each such axis — so a value that is **wrong off-default but identical
on-default** is indistinguishable from the correct one until something varies the axis. The bug lives on the
held axis, precisely where no test looked.

This is the generalization of [`fake-cheaper-than-real`](fake-cheaper-than-real.md) /
`cheap-fake-hidden-by-wrong-axis`: the test isn't just "cheaper than real," it's **constant on the one axis
the bug varies along.**

## Two distinct detection moves (this is why it earns its own entry)

The instances split into two sub-shapes with *different* hunts:

1. **Run-the-path** (environmental assumption never violated): *for each environmental assumption the code
   makes, is there a run that violates it?* — a lean process without the dep, real process timing, a real
   workdir, a second concurrent process. (The "did we run the real path" discipline,
   [`primary-path-was-the-unreviewed-path`](primary-path-was-the-unreviewed-path.md).)
2. **Vary-the-config / grep-the-constant** (configuration drift across call sites): *when you make X
   configurable, did **every** site that reads X move together?* and *for each configurable property, is there
   a test that **varies** it off its default?* This is a static, cheap check — grep for the constant, confirm
   one test sets the env to a non-default and asserts the dependent behaviour. It is NOT a run-the-path check.

## Canonical instances — all four found in ONE session (2026-06-21), each by a real run on a varied axis

| Finding | Held axis the test never varied | Sub-shape |
|---|---|---|
| seat can't `import arb_memory` | the env (always the repo venv, never a lean seat) | run-the-path |
| consumer-group boot-race (write before group at `id="$"`) | process timing (always slept before writing) | run-the-path |
| `redis` not in seat env | the workdir (always the repo, never a real seat workdir) | run-the-path |
| **`audit.py PREFIX = ""` (missed when `bus.py` became env-configurable)** | the prefix (always the default; `""` ≡ `os.environ.get(...,"")` on-default) | vary-config / grep-the-constant |

The audit-prefix one is the sharpest of the config-drift shape: `PREFIX = ""` and
`PREFIX = os.environ.get("ARB_MEMORY_PREFIX", "")` are **behaviourally identical** until something runs at a
non-default prefix — so every default-prefix test passed against the un-isolatable value. It was also a real
production hazard, not a test nicety: a hardcoded-unprefixed audit consumer means a second audit consumer
(test/canary/staging) **steals from the live `arbmem-audit` group** — the same message-stealing hazard caught
for the *memory* consumer at design time, sitting latent in audit. The fix pins the **symmetry** with a
regression test (`test_audit_prefix` mirrors `test_bus_prefix`): the property that matters is
"`audit.PREFIX` tracks `bus.PREFIX`," and the test guards them against drifting apart again.

## Why this belongs in the diagnosis meta-skill

These four are a convergent, fresh **validation set**: a defect-detection skill that encodes "vary every
configurable property; violate every environmental assumption" should catch all four. Promote the move into
the skill, then test the skill against this session's findings as the eval (see the skill-promotion task).
Related: [`deny-proofs-need-adversarial-verification`](deny-proofs-need-adversarial-verification.md) (the same
"is the axis actually varied?" question, applied to the proof itself).
