# Defect-Hunt Eval Manifest

This manifest pins the Task 6 precision set for the defect-hunt slice. H1 is
operative as a same-directory config-drift standing check. H2's schema gate is
built and tested, but H2 is dormant on real reviews until a producer emits
`h2_section`; absent H2 input is reported as `dormant-no-producer`, not treated
as enforcement. Each suspect is closed as either an executable clean row or a
logged finding.

## Clean Precision Rows

| Case | Closure | Evidence |
| --- | --- | --- |
| `ARB_MEMORY_PREFIX` post-fix | Clean row: `bus.py` and `audit.py` both derive `PREFIX` from `ARB_MEMORY_PREFIX`, so H1 must clear. | `tests/defect_hunts/eval/negatives.json` case `arb-memory-prefix-post-fix`; source shape observed in `src/arb_memory/bus.py` and `src/arb_memory/audit.py`. |
| `BRIDGE_NOTIFY_INBOX` | Clean row: one parser path controls notify routing; no module-level same-symbol literal sibling exists for H1 to flag. | `tests/defect_hunts/eval/negatives.json` case `bridge-notify-inbox`; source shape observed in `src/agent_redis_bridge/bridge.py`. |
| `BRIDGE_PI_THINKING_LEVEL` | Clean row: `pi_rpc.py` and `pi_sdk.py` both use `os.environ.get("BRIDGE_PI_THINKING_LEVEL") or None`. | `tests/defect_hunts/eval/negatives.json` case `bridge-pi-thinking-level`; source shape observed in `src/agent_redis_bridge/engines/pi_rpc.py` and `src/agent_redis_bridge/engines/pi_sdk.py`. |
| `ARB_MEMORY_DSN` | Clean row: executable closeout varies the env value and asserts all `src/` readers observe the change. | `tests/defect_hunts/test_negatives.py::test_arb_memory_dsn_and_redis_url_readers_co_move_with_env`. |
| `ARB_MEMORY_REDIS_URL` | Clean row: executable closeout varies the env value and asserts all `src/` readers observe the change. | `tests/defect_hunts/test_negatives.py::test_arb_memory_dsn_and_redis_url_readers_co_move_with_env`. |

## Baited Traps

| Case | Expected | Baits |
| --- | --- | --- |
| `h1-alias-trap` | `CLEAR` | `two literal sites or flag` |
| `h1-divergence-trap` | `FLAG` | `literal value equals env default so it looks intentional` |
| `h1-defaultless-env-trap` | `CLEAR` | `defaultless env read plus nearby literal` |
| `h2-safe-pinned-assumption` | `CLEAR` | `environmental assumption looks unverified but is pinned by a concrete test` |

## BRIDGE_MAX_PARALLEL Finding

`BRIDGE_MAX_PARALLEL` is logged as a finding. Concurrent engine tasks in the same
git worktree can race on the shared `.git` index; that is the held-axis shape
itself, because parser-level configuration is valid while workspace isolation is
left to the operator.

Evidence anchors: `src/agent_redis_bridge/bridge.py`, `docs/orchestrator-patterns.md`,
and `docs/bridge-parallelism.md`.

## Deferred-Class Roadmap

The current eval slice makes H1 config drift operative and keeps H2 limited to
schema validation for externally authored `h2_section` input. A follow slice
must add the review-brief composer or phase-input authoring convention before
H2 becomes operative doctrine. Later slices should add separate eval classes
for fixture-supplies, framework-face, environment-leaker, deny-proof
calibration, and primary-path coverage. Those classes are intentionally
deferred so Task 6 does not expand beyond the H1/H2 gate.
