# Seat-provisioning pre-flight — `scripts/seat-preflight` (third member of the pre-flight family)

**Why (the scar, 2026-07-11):** a scratch-seat env set `ARB_MEMORY_LOCAL_MCP` without a
derivable `ARB_MEMORY_LOCAL_DSN`; every engine start failed loudly and dispatches queued
(5th world-claim failure of the day, 2nd orchestrator-side — BACKLOG § ENG-1b closure).
The family pattern: falsify a claim BEFORE the expensive step. `plan-fixture-smoke`
guards fixture claims at dispatch; this guards SEAT-WORLD claims at seat standup.

**World (verified 2026-07-11 against dev `a9af21a`):**
- The exact fault seam: `scripts/agent-redis-bridge-systemd:101-122` auto-derives
  `ARB_MEMORY_LOCAL_DSN` ONLY when `ARB_MEMORY_LOCAL_MCP` is literally `dev`|`prod` AND
  `~/.arb-memory-local/readers.env` exists; any other shape reaches
  `src/arb_memory/local_read_policy.py:9-12`, which raises
  `RuntimeError("ARB_MEMORY_LOCAL_MCP is set but ARB_MEMORY_LOCAL_DSN is missing/empty")`
  via `src/agent_redis_bridge/local_memory_mcp.py:24-29` at engine start.
- Required Redis keys (`require()` at `src/agent_redis_bridge/redis_io.py:258-262`, over
  env-file ∪ process-env): `AGENT_REDIS_HOST`, `AGENT_REDIS_PORT`, `AGENT_REDIS_DB`,
  `AGENT_REDIS_PREFIX`. `read_env_file()` (`redis_io.py:239-255`) silently returns `{}`
  for a MISSING file — the pre-flight must assert existence itself.
- Cross-store guard: `local_read_policy.py:19-24` — both `ARB_MEMORY_DSN` and
  `ARB_MEMORY_LOCAL_DSN` set with different store fingerprints raises unless
  `ARB_MEMORY_LOCAL_ALLOW_CROSS_STORE=1`.
- Script conventions to match (`scripts/plan-fixture-smoke`): `#!/usr/bin/env python3`,
  docstring with Usage, simple argv scan (no argparse), exit **0** = all checks hold /
  **1** = violation (fix before standup) / **2** = misuse, `[seat-preflight]` output tag,
  count-bearing OK line, RED banner to stderr on failure.
- Test convention: `tests/test_plan_fixture_smoke.py:6-12` loads the extensionless
  script via `SourceFileLoader`; tests call internal functions directly. Mirror it.

**Interface:**
```
scripts/seat-preflight <plist-or-env-path> [--strict]
```
- `.plist` argument (launchd seat): parse with `plistlib`, take `EnvironmentVariables`
  as the process-env layer, resolve `AGENT_ENV_FILE` from it (default
  `<AGENT_WORKDIR>/.env` per `bridge.py:88-102` order) and layer the env file under it.
- `.env` argument: single-layer check (env-file-only invariants).
- Checks return `(ok: bool, msg: str)` from small pure functions so tests hit them
  directly; `main()` just sequences and formats.

**The checks (each a named function):**
1. `check_env_file(path)` — resolved env file exists and is readable.
2. `check_redis_required(env)` — the four `require()` keys present in the union.
3. `check_local_mcp_dsn(env, home)` — if `ARB_MEMORY_LOCAL_MCP` set: PASS if
   `ARB_MEMORY_LOCAL_DSN` non-empty in the union, else PASS only if the value is
   `dev`|`prod` AND `<home>/.arb-memory-local/readers.env` exists and defines
   `ARB_MEMORY_LOCAL_DSN_<TIER>` or `ARB_MEMORY_LOCAL_DSN` (read the systemd script's
   `case` at `:101-122` and mirror its EXACT derivation — if the derivation differs from
   this description, mirror the script and note the deviation in the reply). Otherwise
   FAIL with the downstream RuntimeError message quoted so the operator recognizes it.
4. `check_cross_store(env)` — both DSNs set → compare host+db (parse URI authority);
   differ → FAIL unless `ARB_MEMORY_LOCAL_ALLOW_CROSS_STORE=1`. If
   `local_read_policy` exposes a pure fingerprint helper, import and reuse it instead
   of re-parsing (PYTHONPATH-safe: fall back to local parsing when import fails).
5. `check_workdir(env)` — `AGENT_WORKDIR` (when set) exists and is a directory.
6. `check_program(plist)` — plist mode only: `ProgramArguments[0]` exists + executable;
   `WorkingDirectory` (when set) exists. Catches stale-clone-path seats.
7. `check_trusted_senders(env)` — `AGENT_TRUSTED_SENDERS` (when set) parses as CSV of
   `id=role` pairs (the format `agent-redis-bridge-systemd:38-45` forwards).

`--strict` upgrades warnings (cross-store with override set; empty trusted-senders on a
non-scratch seat) to failures. Default keeps them warnings with the `[seat-preflight]`
tag, mirroring plan-fixture-smoke's anti-vacuous-gate WARNING style.

```python fixture-smoke
# World-claims of THIS plan, executed against the current tree:
# (1) local_read_policy raises the exact RuntimeError the pre-flight quotes;
# (2) read_env_file returns {} for a missing file (why check 1 must exist);
# (3) the four require() keys are what redis_io actually requires.
import sys
sys.path.insert(0, "src")
from arb_memory.local_read_policy import local_read_dsn
try:
    local_read_dsn({"ARB_MEMORY_LOCAL_MCP": "1"})
    raise AssertionError("expected RuntimeError for MCP-without-DSN")
except RuntimeError as e:
    assert "ARB_MEMORY_LOCAL_DSN is missing/empty" in str(e), str(e)
from agent_redis_bridge.redis_io import read_env_file
import pathlib
assert read_env_file(pathlib.Path("/nonexistent/definitely-not-here.env")) == {}
import inspect
from agent_redis_bridge import redis_io
src_text = inspect.getsource(redis_io)
for key in ("AGENT_REDIS_HOST", "AGENT_REDIS_PORT", "AGENT_REDIS_DB", "AGENT_REDIS_PREFIX"):
    assert key in src_text, f"{key} not found in redis_io — required-key claim is stale"
print("fixture-smoke OK: fault seam + silent-missing-env-file + required keys confirmed")
```

---

## Task 1 — checks module + tests (RED first)

`tests/test_seat_preflight.py`: load `scripts/seat-preflight` via `SourceFileLoader`
(mirror `tests/test_plan_fixture_smoke.py:6-12`). Tests against REAL tmp files
(`tmp_path`): missing env file fails check 1; env file with all four Redis keys passes
check 2, missing `AGENT_REDIS_PREFIX` fails naming the key; `ARB_MEMORY_LOCAL_MCP=1`
without DSN fails check 3 with the RuntimeError text; `=dev` + a tmp readers.env
passes (point the home arg at tmp_path); both DSNs same host/db passes check 4,
different fails, override downgrades; workdir/program/trusted-senders happy+sad paths.
GREEN: implement the script. Commit
`feat(scripts): seat-preflight — world-claims gate for seat standup (T1: checks + tests)`.

## Task 2 — plist mode + end-to-end + docs

RED: tests writing a real minimal `.plist` (plistlib.dump) with EnvironmentVariables →
full `main()` run via `run_main(argv)` returns 0 on a healthy synthetic seat, 1 when the
MCP/DSN fault is injected, 2 on bad usage. GREEN: wire `main()`. Docs: register as third
family member in `docs/pipeline-operating-manual.md` § "Plan-stage pre-flight" vicinity
(new short subsection "Seat-standup pre-flight"), CHANGELOG entry (what + why: the
ARB_MEMORY_LOCAL_MCP-without-DSN scar), one-line pointer in BACKLOG § ENG-1b closure
("recipe wants pre-flight" → "shipped: scripts/seat-preflight"). Commit
`feat(scripts): seat-preflight T2 — plist mode + manual/CHANGELOG registration`.

**Evidence contract (reply MUST contain):** per task — SHA, test counts, deviations
named. Test command:
`env -u ARB_MEMORY_LOCAL_MCP PYTHONPATH=$PWD/src /Users/<user>/<workspace>/.venv/bin/python -m pytest -q tests/test_seat_preflight.py`
