# DSP-1 residual — env-tunable engine initialize/start budget (15s → 60s default)

> **rev 1.1 (2026-07-11, post-BLOCKED):** luna falsified the plan claim that
> `CodexEngine` exposes a `popen_factory` seam — it does not (`codex.py:79-88`;
> `start()` calls `subprocess.Popen` inline at `:158`). Codex tests fake by
> SUBCLASSING (`tests/test_codex_io.py:8` `FakeCodexEngine` overrides
> `request`/`_send`/`_get_message`). Task 1's codex RED section rewritten
> accordingly; pi_sdk section unchanged (its `popen_factory` seam is real and
> smoke-verified). Drift note: the fixture smoke exercised only PiSdkEngine, so
> the codex constructor claim traveled unchecked — a smoke block per FAKED CLASS
> is the lesson.

**Spec:** `docs/BACKLOG.md` § DSP-1 "Remaining fix". Root cause on record there: the 15s
initialize budget sits ~2s above codex's NORMAL first-after-idle startup (13.2s probed);
failures are the tail of that distribution under pipeline load. The client-side
retry-once (`b828e5d`) is tail insurance; this change removes the too-tight budget at
the source. Default becomes **60** so seats pick it up at next restart with NO plist
change; `BRIDGE_ENGINE_INIT_TIMEOUT_S` tunes it per seat.

**World (verified 2026-07-11 against dev `a9af21a`):**
- Pool calls `start()` with **no arguments** (`src/agent_redis_bridge/engine_pool.py:92`),
  so the value CANNOT be passed at the call site — read the env var in each engine's
  `__init__` (the established `BRIDGE_CODEX_*` pattern, `engines/codex.py:126-129`) and
  use it inside `start()`.
- In-scope 15s literals (start/initialize handshake ONLY — turn-time timeouts are out of
  scope): `codex.py:178` (initialize); `pi_sdk.py:272` (`start(self, probe_timeout: int = 15)`,
  used at `:313`); `cursor_acp.py:110,113,150,170` (initialize/authenticate/session-new/
  model-select inside `start()`); `grok_acp.py:130,146`; `gemini_acp.py:95,108`.
  Explicitly OUT of scope: `pi_sdk.py:319` `thread/start timeout=30`, `cursor_acp.py:311`
  (post-start set_model path), `pi_rpc.py:233` (`probe_timeout=10`, different engine
  family), all `run_turn` timeouts, `agy_*`/`agent_sdk` (no stdio init handshake budget).
- Error string is composed in two places: each engine `request()` raises
  `EngineError(f"{method} timed out after {timeout}s")`; `bridge.py:941` prefixes
  `engine-start-failed: `. Neither site changes — the DSP-1 retry discriminator
  (`initialize timed out`) keeps matching regardless of the number in the message.
- Fixture style: hand-rolled `FakeStdin`/`FakeStdout`/`FakeProcess` injected via the
  `popen_factory` constructor seam (`tests/test_gemini_acp.py:7-46` exemplar);
  `unittest.TestCase`; env vars via `unittest.mock.patch.dict`.

**Evidence contract (reply MUST contain):** per task — SHA, test file(s) run + counts,
any deviation from this plan named explicitly (undeclared deviations are a BLOCKED-class
event). Run tests as:
`env -u ARB_MEMORY_LOCAL_MCP PYTHONPATH=$PWD/src /Users/<user>/<workspace>/.venv/bin/python -m pytest -q <files>`

```python fixture-smoke
# Prove the fixture semantics the tests below rely on, against the CURRENT tree:
# (1) a never-responding fake process makes the start-path request() time out and
#     raise EngineError whose message carries "timed out after {N}s";
# (2) the pi_sdk start() probe_timeout parameter actually gates that wait (0 → instant).
import sys
sys.path.insert(0, "src")
from agent_redis_bridge.engines.pi_sdk import PiSdkEngine
from agent_redis_bridge.engines.base import EngineError

class FakeStdin:
    def write(self, data): pass
    def flush(self): pass
    def close(self): pass

class FakeStdout:
    def readline(self): import time; time.sleep(0.05); return ""

class FakeProc:
    stdin = FakeStdin(); stdout = FakeStdout(); stderr = None; pid = 4242
    def poll(self): return None
    def terminate(self): pass
    def kill(self): pass
    def wait(self, timeout=None): return 0

eng = PiSdkEngine(cwd=".", model=None, popen_factory=lambda *a, **k: FakeProc())
try:
    eng.start(probe_timeout=0)
    raise AssertionError("start() should have raised EngineError on a silent fake")
except EngineError as e:
    assert "timed out after 0s" in str(e), f"unexpected message: {e}"
print("fixture-smoke OK: silent fake -> EngineError('... timed out after 0s')")
```

*(If `PiSdkEngine`'s constructor signature differs — e.g. requires model/role args — the
implementor adapts the smoke's constructor call, NOT the predicate. The predicate is the
claim under test.)*

```python fixture-smoke
# rev 1.1: prove the CODEX test recipe (subclass-capture + patched Popen) against the
# CURRENT tree — start() must complete under the patch and capture ("initialize", 15).
import sys
sys.path.insert(0, "src")
from unittest.mock import patch, MagicMock
from agent_redis_bridge.engines.codex import CodexEngine

captured = []

class CaptureStartEngine(CodexEngine):
    def __init__(self):
        super().__init__(cwd="/tmp", model="gpt-5.5", approval_policy="never",
                         sandbox="workspace-write")
    def request(self, method, params, *, timeout):
        captured.append((method, timeout))
        if method == "thread/start":
            return {"thread": {"id": "t"}}
        return {}
    def _read_stdout(self):
        return None

with patch("agent_redis_bridge.engines.codex.subprocess.Popen", return_value=MagicMock()), \
     patch("agent_redis_bridge.engines.codex.start_stderr_drain", return_value=None):
    eng = CaptureStartEngine()
    eng.start()

init = [t for m, t in captured if m == "initialize"]
assert init == [15], f"expected captured initialize timeout [15] on current tree, got {captured}"
print("fixture-smoke OK: codex subclass-capture recipe works; initialize timeout captured =", init[0])
```

---

## Task 1 — shared helper + codex + pi_sdk

**RED:** in `tests/test_codex_io.py` add `InitTimeoutEnvTests` using the file's OWN
subclass-fake convention (`FakeCodexEngine` at `tests/test_codex_io.py:8` is the
exemplar — do NOT invent a popen seam):
- `test_default_init_timeout_is_60`: construct `FakeCodexEngine()`, assert
  `eng._init_timeout == 60`.
- `test_env_overrides_init_timeout`: `patch.dict(os.environ, {"BRIDGE_ENGINE_INIT_TIMEOUT_S": "33"})`
  around construction → `eng._init_timeout == 33`.
- `test_initialize_uses_init_timeout`: subclass `CaptureStartEngine(CodexEngine)` whose
  `request(method, params, *, timeout)` appends `(method, timeout)` to a list and
  returns canned responses (`thread/start` → `{"thread": {"id": "t"}}`, else `{}`),
  and whose `_read_stdout` is a no-op; run `start()` under
  `unittest.mock.patch("agent_redis_bridge.engines.codex.subprocess.Popen")` (and, if
  needed, `...codex.start_stderr_drain`) so no real process spawns; with env `"7"`,
  assert the captured `initialize` entry is `("initialize", 7)` (proves the literal is
  gone and the env value reaches the initialize request).

In `tests/test_pi_sdk.py` add the same three, plus
`test_explicit_probe_timeout_still_wins`: `eng.start(probe_timeout=0)` raises the 0s
timeout even with env unset (backwards-compat for `tests/test_pi_rpc.py:302`-style
callers).

**GREEN:**
- New module-level helper in `src/agent_redis_bridge/engines/base.py`:
  ```python
  def engine_init_timeout(default: int = 60) -> int:
      """Start/initialize handshake budget (seconds). DSP-1: 15s sat ~2s above codex's
      normal first-after-idle startup; 60 costs nothing unless a start is genuinely wedged."""
      try:
          return int(os.environ.get("BRIDGE_ENGINE_INIT_TIMEOUT_S", str(default)))
      except ValueError:
          return default
  ```
- `codex.py` `__init__`: `self._init_timeout = engine_init_timeout()`; `:178` becomes
  `timeout=self._init_timeout`.
- `pi_sdk.py` `__init__`: same; signature becomes
  `def start(self, probe_timeout: int | None = None) -> None:` with first line
  `probe_timeout = self._init_timeout if probe_timeout is None else probe_timeout`;
  `:313` unchanged (already uses `probe_timeout`). `:319` (`thread/start`, 30s) stays.

Run: the two test files. Commit `feat(engines): DSP-1 — env-tunable init budget, codex + pi-sdk (15s→60s default)`.

## Task 2 — cursor_acp, grok_acp, gemini_acp

**RED:** in each of `tests/test_cursor_acp.py`, `tests/test_grok_acp.py`,
`tests/test_gemini_acp.py`: the same `test_default_init_timeout_is_60` +
`test_initialize_uses_init_timeout` (env `"0"`, silent fake → `EngineError` "timed out
after 0s"). Follow each file's existing fake conventions exactly.

**GREEN:** each engine's `__init__` gains `self._init_timeout = engine_init_timeout()`;
replace ONLY the listed start-path literals (`cursor_acp.py:110,113,150,170`;
`grok_acp.py:130,146`; `gemini_acp.py:95,108`) with `timeout=self._init_timeout`.
`cursor_acp.py:311` is deliberately untouched.

Run: the three test files + re-run Task 1's two (regression). Commit
`feat(engines): DSP-1 — init budget env-tunable on cursor/grok/gemini ACP starts`.

## Task 3 — docs

CHANGELOG.md entry (what + why: the 13.2s first-after-idle probe vs 15s budget, retry
stays as tail insurance, `BRIDGE_ENGINE_INIT_TIMEOUT_S`, default 60, takes effect at
seat restart). BACKLOG § DSP-1: flip "timeout raise OPEN" → SHIPPED with SHA. Commit
`docs(dsp1): init-budget raise shipped`.

**Full-sweep gate (orchestrator runs post-merge, not the implementor):** targeted files
above + `tests/test_bridge_handle_raw.py` (engine-start-failed prefix path untouched).
