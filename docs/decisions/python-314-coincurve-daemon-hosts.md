# Python 3.14 on the deployed clones: accepted, because they are daemon hosts

**Decision, 2026-08-19 (Mark).** The two deployed clones run Python **3.14.5**, and coincurve
publishes **no cp314 wheel on any version**. This is **accepted as a standing constraint**, not
scheduled as a fix. The interpreters stay where they are.

## Why it is not a defect

`/Users/<user>/<workspace>` and `/Users/<user>/AgentRedisBridge` are **daemon hosts only** — they run
`bridge.py` and the seats, nothing else. The test suite runs on the 3.12 venvs (the repo `.venv`
and `/private/tmp/<dev>/.venv`), which do have coincurve 21.0.0. A dependency the daemon never
imports costs a daemon host nothing.

That is the whole argument, and it rests on one factual claim: **`bridge.py` cannot reach
`arb_registration.nip_oa` or `buzz_ops.nip_oa`**, the only two module-level
`from coincurve import ...` sites. Verified empirically on the affected host rather than asserted:

```
/Users/<user>/<workspace>/.venv/bin/python -c "import agent_redis_bridge.bridge; ..."
→ 309 modules loaded, neither `coincurve` nor `nip_oa` among them
```

## Rejected alternatives

| Option | Why not |
|---|---|
| Rebuild the clone venvs on <=3.13 | Buys nothing a host that never imports `nip_oa` can use, and costs a fleet-wide venv rebuild plus seat restarts. |
| Source-build coincurve there against `libsecp256k1` headers | Same — pays a toolchain and maintenance cost for an import that never happens. |
| Wait for an upstream wheel | There is none to wait for. Every published coincurve version was checked: no abi3 wheel, no cp314 wheel. |

## What makes the acceptance safe

The argument is only as good as the import graph, so the import graph is now **enforced**:
**`tests/test_daemon_import_graph.py`**. It imports `agent_redis_bridge.bridge` in a clean
subprocess and asserts `coincurve` and both `nip_oa` modules are **absent from `sys.modules`**,
naming the `file:line` of any import that reaches them.

Two details are load-bearing and should survive any future edit to that file:

- **It asserts absence, not that the import raises.** Every venv that runs the suite has coincurve
  installed, so a does-it-blow-up check passes vacuously and forever.
- **It runs in a subprocess.** In-process, other test modules have already imported `nip_oa`, so
  the check would measure pytest's import graph and go red for the wrong reason.
- **A permanent positive control sits beside it**, importing a module that *does* reach coincurve
  and asserting the probe sees it. That is what stops the guard degrading into decoration, and it
  discharges "a check must be able to fail" without mutating the tree.

If the guard goes red, the deployed clones are broken. It will present as a `ModuleNotFoundError`
inside a seat, not as a dependency problem — the same fail-at-point-of-use shape as the cline-acp
dispatcher gap (`tests/test_dispatch_engine_parity.py`).

## What this does NOT change

The **production image** still imports `nip_oa`. `deploy/Dockerfile` remains pinned to
**3.13-slim**, and the pin and the dependency must still move together. Before bumping either:

```bash
uv pip install --dry-run --only-binary :all: --python-version 3.X \
  --python-platform x86_64-unknown-linux-gnu coincurve
```

`--only-binary :all:` is **not optional** — without it a missing wheel resolves "successfully" by
silently falling back to an sdist, and the check reports the opposite of the truth.

The full constraint, with the deployed-host readings, lives beside the dependency itself in
`pyproject.toml` (the `coincurve>=21` comment block).
