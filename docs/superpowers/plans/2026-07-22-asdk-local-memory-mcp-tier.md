# asdk Local-Memory MCP Tier Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Direct-launched (plist) asdk seats mount the read-only `arb-memory-local` MCP by resolving `ARB_MEMORY_LOCAL_MCP=dev|prod` → `~/.arb-memory-local/readers.env` in Python, so Opus/Fable panel seats can fetch pinned subjects instead of abstaining.

**Architecture:** All code changes live in `src/agent_redis_bridge/local_memory_mcp.py` (tier resolution + venv-anchored command); engines are untouched because they already consume `local_memory_mcp_config()`. Ops follow-up adds one env var to six launchd plists and kickstarts the seats; a live probe dispatch is the acceptance gate.

**Tech Stack:** Python 3 (<workspace> `.venv`), pytest, launchd/PlistBuddy, agent-dispatch.

**Spec:** `docs/superpowers/specs/2026-07-22-asdk-local-memory-mcp-tier-design.md`

## Global Constraints

- Repo: `/Users/<user>/<workspace>`, branch `dev`. Run all tests with `/Users/<user>/<workspace>/.venv/bin/python3 -m pytest`.
- Known pre-existing failures NOT ours (do not chase, do not run): `test_bridge_emit_vote`, `test_doc_index` (hangs). Targeted suite for this work: `tests/arb_memory/`.
- Fail-soft rule: a missing/unreadable readers.env or empty tier DSN returns `None` (feature absent) and must never raise at daemon startup. The cross-store mismatch (`ARB_MEMORY_DSN` vs local DSN) must still raise (fail closed) via the existing `local_read_dsn`.
- Legacy behaviour unchanged: `ARB_MEMORY_LOCAL_MCP=1` (or any non-tier value) requires `ARB_MEMORY_LOCAL_DSN` already in the environment and raises `RuntimeError` when it is missing.
- Secrets never on argv; the existing `local_memory_mcp_argv_safe_config` relay-file path must keep working unmodified.
- readers.env line format: optional `export ` prefix, `KEY=VALUE`, values may be single- or double-quoted, split on FIRST `=` only (DSNs contain `=` in query params).

---

### Task 1: Tier resolution in `local_memory_mcp_config()`

**Files:**
- Modify: `src/agent_redis_bridge/local_memory_mcp.py` (function `local_memory_mcp_config`, new helper `_read_readers_env`, new module constant `READERS_ENV_PATH`)
- Test: `tests/arb_memory/test_local_memory_mcp_tier.py` (new file)

**Interfaces:**
- Consumes: `arb_memory.local_read_policy.local_read_dsn(env_mapping)` (existing — raises on missing DSN or cross-store mismatch).
- Produces: `local_memory_mcp_config() -> dict | None` with unchanged return shape `{"command": str, "args": [], "env": {…}}`; `_read_readers_env(path: Path) -> dict[str, str]`. Task 2 modifies the same function's `command` value.

- [ ] **Step 1: Write the failing tests**

Create `tests/arb_memory/test_local_memory_mcp_tier.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from agent_redis_bridge import local_memory_mcp
from agent_redis_bridge.local_memory_mcp import local_memory_mcp_config


DEV_DSN = "postgresql://reader:p=a?ss@dev-host:25060/arbmem?sslmode=require"
PROD_DSN = "postgresql://reader:secret@prod-host:25060/arbmem?sslmode=require"


def write_readers(home: Path, body: str) -> None:
    d = home / ".arb-memory-local"
    d.mkdir(parents=True, exist_ok=True)
    (d / "readers.env").write_text(body, encoding="utf-8")


@pytest.fixture()
def clean_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in ("ARB_MEMORY_LOCAL_MCP", "ARB_MEMORY_LOCAL_DSN", "ARB_MEMORY_DSN", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def test_dev_tier_selects_dev_dsn(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_readers(
        clean_env,
        f'export ARB_MEMORY_LOCAL_DSN_DEV="{DEV_DSN}"\n'
        f'export ARB_MEMORY_LOCAL_DSN_PROD="{PROD_DSN}"\n'
        'export OPENAI_API_KEY="sk-readers"\n',
    )
    monkeypatch.setenv("ARB_MEMORY_LOCAL_MCP", "dev")
    config = local_memory_mcp_config()
    assert config is not None
    # value split on FIRST '=' only; quotes stripped; export prefix handled
    assert config["env"]["ARB_MEMORY_LOCAL_DSN"] == DEV_DSN
    assert config["env"]["OPENAI_API_KEY"] == "sk-readers"


def test_prod_tier_selects_prod_dsn(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_readers(
        clean_env,
        f"export ARB_MEMORY_LOCAL_DSN_DEV={DEV_DSN}\n"
        f"export ARB_MEMORY_LOCAL_DSN_PROD={PROD_DSN}\n",
    )
    monkeypatch.setenv("ARB_MEMORY_LOCAL_MCP", "prod")
    config = local_memory_mcp_config()
    assert config is not None
    assert config["env"]["ARB_MEMORY_LOCAL_DSN"] == PROD_DSN


def test_missing_readers_file_is_feature_absent(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARB_MEMORY_LOCAL_MCP", "dev")
    assert local_memory_mcp_config() is None


def test_empty_tier_dsn_is_feature_absent(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_readers(clean_env, "export ARB_MEMORY_LOCAL_DSN_DEV=\n")
    monkeypatch.setenv("ARB_MEMORY_LOCAL_MCP", "dev")
    assert local_memory_mcp_config() is None


def test_readers_key_wins_over_process_env_openai_key(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_readers(
        clean_env,
        f"export ARB_MEMORY_LOCAL_DSN_DEV={DEV_DSN}\n"
        "export OPENAI_API_KEY=sk-from-readers\n",
    )
    monkeypatch.setenv("ARB_MEMORY_LOCAL_MCP", "dev")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-process")
    config = local_memory_mcp_config()
    assert config is not None
    assert config["env"]["OPENAI_API_KEY"] == "sk-from-readers"


def test_tier_cross_store_mismatch_still_raises(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_readers(clean_env, f"export ARB_MEMORY_LOCAL_DSN_DEV={DEV_DSN}\n")
    monkeypatch.setenv("ARB_MEMORY_LOCAL_MCP", "dev")
    monkeypatch.setenv(
        "ARB_MEMORY_DSN", "postgresql://writer:pw@other-host:25060/arbmem?sslmode=require"
    )
    with pytest.raises(RuntimeError, match="does not match"):
        local_memory_mcp_config()


def test_legacy_flag_with_env_dsn_unchanged(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARB_MEMORY_LOCAL_MCP", "1")
    monkeypatch.setenv("ARB_MEMORY_LOCAL_DSN", DEV_DSN)
    config = local_memory_mcp_config()
    assert config is not None
    assert config["env"]["ARB_MEMORY_LOCAL_DSN"] == DEV_DSN


def test_legacy_flag_without_dsn_still_raises(clean_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARB_MEMORY_LOCAL_MCP", "1")
    with pytest.raises(RuntimeError, match="ARB_MEMORY_LOCAL_DSN"):
        local_memory_mcp_config()


def test_flag_absent_is_none(clean_env: Path) -> None:
    assert local_memory_mcp_config() is None
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `/Users/<user>/<workspace>/.venv/bin/python3 -m pytest tests/arb_memory/test_local_memory_mcp_tier.py -v` (cwd `/Users/<user>/<workspace>`)
Expected: `test_dev_tier_selects_dev_dsn`, `test_prod_tier_selects_prod_dsn`, `test_missing_readers_file_is_feature_absent`, `test_empty_tier_dsn_is_feature_absent`, `test_readers_key_wins_over_process_env_openai_key`, `test_tier_cross_store_mismatch_still_raises` FAIL (tier values currently fall through to `local_read_dsn` and raise "ARB_MEMORY_LOCAL_DSN is missing"). The three legacy/absent tests PASS already — that is expected; they pin the invariants.

- [ ] **Step 3: Implement tier resolution**

In `src/agent_redis_bridge/local_memory_mcp.py`, replace `local_memory_mcp_config` and add the helper + constant (keep existing imports; add none beyond what is shown):

```python
READERS_ENV_PATH = "~/.arb-memory-local/readers.env"

_TIER_VALUES = ("dev", "prod")


def _read_readers_env(path: Path) -> dict[str, str]:
    """Parse a readers.env-style file: optional `export ` prefix, KEY=VALUE,
    first-`=` split, optional single/double quotes. Unreadable → empty (the
    feature is optional; absence must never break seat startup)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        values[key.strip()] = value
    return values


def local_memory_mcp_config() -> dict[str, Any] | None:
    flag = os.environ.get("ARB_MEMORY_LOCAL_MCP")
    if flag is None:
        return None
    overlay: dict[str, str] = {}
    if flag in _TIER_VALUES:
        readers = _read_readers_env(Path(os.path.expanduser(READERS_ENV_PATH)))
        tier_dsn = readers.get(f"ARB_MEMORY_LOCAL_DSN_{flag.upper()}", "")
        if not tier_dsn:
            return None
        overlay["ARB_MEMORY_LOCAL_DSN"] = tier_dsn
        if readers.get("OPENAI_API_KEY"):
            overlay["OPENAI_API_KEY"] = readers["OPENAI_API_KEY"]
    merged: dict[str, str] = {**os.environ, **overlay}
    dsn = local_read_dsn(merged)
    env = {key: merged[key] for key in LOCAL_MEMORY_MCP_ENV_KEYS if key in merged}
    env["ARB_MEMORY_LOCAL_DSN"] = dsn
    return {
        "command": "arb-memory-local-mcp",
        "args": [],
        "env": env,
    }
```

- [ ] **Step 4: Run the targeted suite**

Run: `/Users/<user>/<workspace>/.venv/bin/python3 -m pytest tests/arb_memory/ -v`
Expected: all PASS (new tier tests plus every pre-existing injection/env-file test — the injection tests prove the engines still see the same config shape).

- [ ] **Step 5: Commit**

```bash
cd /Users/<user>/<workspace>
git add src/agent_redis_bridge/local_memory_mcp.py tests/arb_memory/test_local_memory_mcp_tier.py
git commit -m "feat: resolve ARB_MEMORY_LOCAL_MCP dev|prod tiers in Python

Direct-launched (plist) seats bypass agent-redis-bridge-systemd, whose
shell tier→readers.env resolution was the only activation path for the
arb-memory-local read MCP. local_memory_mcp_config() now performs the
same resolution itself; wrapper-launched seats keep working via the
legacy flag=1 branch."
```

### Task 2: Venv-anchored MCP command resolution

**Files:**
- Modify: `src/agent_redis_bridge/local_memory_mcp.py` (new helper `_server_command`; `local_memory_mcp_config` uses it)
- Test: `tests/arb_memory/test_local_memory_mcp_tier.py` (append two tests)

**Interfaces:**
- Consumes: `local_memory_mcp_config()` as completed in Task 1.
- Produces: `_server_command() -> str` — absolute `<dir-of-sys.executable>/arb-memory-local-mcp` when that file exists, else the bare name `"arb-memory-local-mcp"`. Config `"command"` value carries it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/arb_memory/test_local_memory_mcp_tier.py`:

```python
def test_command_is_venv_anchored_when_sibling_binary_exists(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bin_dir = tmp_path / "venv-bin"
    bin_dir.mkdir()
    (bin_dir / "arb-memory-local-mcp").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(local_memory_mcp.sys, "executable", str(bin_dir / "python3"))
    monkeypatch.setenv("ARB_MEMORY_LOCAL_MCP", "1")
    monkeypatch.setenv("ARB_MEMORY_LOCAL_DSN", DEV_DSN)
    config = local_memory_mcp_config()
    assert config is not None
    assert config["command"] == str(bin_dir / "arb-memory-local-mcp")


def test_command_falls_back_to_bare_name(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    empty_bin = tmp_path / "no-binary-here"
    empty_bin.mkdir()
    monkeypatch.setattr(local_memory_mcp.sys, "executable", str(empty_bin / "python3"))
    monkeypatch.setenv("ARB_MEMORY_LOCAL_MCP", "1")
    monkeypatch.setenv("ARB_MEMORY_LOCAL_DSN", DEV_DSN)
    config = local_memory_mcp_config()
    assert config is not None
    assert config["command"] == "arb-memory-local-mcp"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/<user>/<workspace>/.venv/bin/python3 -m pytest tests/arb_memory/test_local_memory_mcp_tier.py -v`
Expected: the first new test FAILS (`config["command"] == "arb-memory-local-mcp"` bare name; also `local_memory_mcp` has no `sys` attribute yet → AttributeError on monkeypatch). Second may pass by accident; both must be present.

- [ ] **Step 3: Implement**

In `src/agent_redis_bridge/local_memory_mcp.py`: add `import sys` to the module imports, add the helper, and change the config dict's `"command"`:

```python
def _server_command() -> str:
    candidate = Path(sys.executable).parent / "arb-memory-local-mcp"
    if candidate.is_file():
        return str(candidate)
    return "arb-memory-local-mcp"
```

and in `local_memory_mcp_config()`:

```python
    return {
        "command": _server_command(),
        "args": [],
        "env": env,
    }
```

- [ ] **Step 4: Run the targeted suite**

Run: `/Users/<user>/<workspace>/.venv/bin/python3 -m pytest tests/arb_memory/ -v`
Expected: all PASS. Note: injection tests that assert `command == "arb-memory-local-mcp"` will now see the real venv's absolute path (the binary exists at `/Users/<user>/<workspace>/.venv/bin/arb-memory-local-mcp`). If any assert the bare name, update them to accept `str(Path(sys.executable).parent / "arb-memory-local-mcp")` — that changed expectation is the feature, not a regression.

- [ ] **Step 5: Commit and push**

```bash
cd /Users/<user>/<workspace>
git add src/agent_redis_bridge/local_memory_mcp.py tests/arb_memory/test_local_memory_mcp_tier.py
git commit -m "feat: venv-anchor the arb-memory-local-mcp server command

Plist-launched seats have no venv bin on PATH; resolve the console
script next to sys.executable, bare-name fallback for exotic layouts."
git push origin dev
git log origin/dev..dev --oneline   # must print nothing (remote verified — milestone rule)
```

### Task 3: Plist env + seat restart (ops, no repo commit)

**Files:**
- Modify (launchd, outside git): `~/Library/LaunchAgents/com.example.arbseat.asdk-bridge-dev-opus48.plist`, `…asdk-piext-dev-fable5.plist`, `…asdk-piext-dev-opus48.plist`, `…asdk-bridge-dev-haiku45.plist`, `…asdk-bridge-dev-sonnet5.plist`, `…asdk-project-e-dev-opus48.plist`

**Interfaces:**
- Consumes: Task 1/2 code deployed by restart (seats run `PYTHONPATH=/Users/<user>/<workspace>/src`, so the working tree IS the deployment; no install step).
- Produces: six seats whose daemon env contains `ARB_MEMORY_LOCAL_MCP=dev`, alive on the bus.

- [ ] **Step 1: Add the env var to each plist**

```bash
for label in asdk-bridge-dev-opus48 asdk-piext-dev-fable5 asdk-piext-dev-opus48 \
             asdk-bridge-dev-haiku45 asdk-bridge-dev-sonnet5 asdk-project-e-dev-opus48; do
  p=~/Library/LaunchAgents/com.example.arbseat.$label.plist
  /usr/libexec/PlistBuddy -c 'Print :EnvironmentVariables:ARB_MEMORY_LOCAL_MCP' "$p" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c 'Add :EnvironmentVariables:ARB_MEMORY_LOCAL_MCP string dev' "$p"
  /usr/libexec/PlistBuddy -c 'Print :EnvironmentVariables:ARB_MEMORY_LOCAL_MCP' "$p"
done
```

Expected: prints `dev` six times.

- [ ] **Step 2: Kickstart the seats (sanctioned restart path — never manual kill+nohup)**

```bash
for label in asdk-bridge-dev-opus48 asdk-piext-dev-fable5 asdk-piext-dev-opus48 \
             asdk-bridge-dev-haiku45 asdk-bridge-dev-sonnet5 asdk-project-e-dev-opus48; do
  launchctl kickstart -k "gui/$(id -u)/com.example.arbseat.$label"
done
```

- [ ] **Step 3: Verify all six are back on the bus (python, not shell-var redis-cli)**

```bash
/Volumes/<workspace>/repos/ARB/.venv/bin/python3 - <<'PY'
import redis, time
time.sleep(20)
r = redis.Redis(host='127.0.0.1', port=6379, db=12)
for agent in ("asdk-bridge-dev-opus48", "asdk-piext-dev-fable5", "asdk-piext-dev-opus48",
              "asdk-bridge-dev-haiku45", "asdk-bridge-dev-sonnet5", "asdk-project-e-dev-opus48"):
    ttl = r.ttl(f"agent_scratch:agent:{agent}:consumer")
    print(f"{agent:35s} consumer_ttl={ttl}")
    assert ttl and ttl > 0, f"{agent} not consuming"
print("all seats alive")
PY
```

Expected: six positive TTLs and `all seats alive`. If a seat is missing, check `~/Library/Logs/agent-bridge/` (launchd logs must NOT be pointed at `/Volumes/*` — silent exit 78).

### Task 4: Live gate — the exact operation the panel abstains failed on

**Files:** none (dispatch probe).

**Interfaces:**
- Consumes: live seats from Task 3; `scripts/dispatch-dev`; seat trusts `claude-bridge-dev` (per plist `--sender-policy`).
- Produces: evidence that `mcp__arb-memory-local__memory_get` works on an asdk seat.

- [ ] **Step 1: Dispatch the probe to the Opus seat**

```bash
cd /Users/<user>/<workspace>
FROM_AGENT_ID=claude-bridge-dev BRANCH=dev AGENT_ENV_FILE=envs/agent-redis-bridge-dev.env \
scripts/dispatch-dev --workspace dev --engine agent-sdk --target-id asdk-bridge-dev-opus48 \
  --timeout 600 --run-id "probe-asdk-memtool-$(date -u +%Y%m%dT%H%M%SZ)" \
  "Using your arb-memory-local MCP tool, fetch artefact arb-role-polisher-workflow version 13 via memory_get. Reply with ONLY: the tool name you called, whether it succeeded, and the SHA-256 of the exact body text you received. Do not review the content." \
  > /tmp/probe-memtool.out 2> /tmp/probe-memtool.err
cat /tmp/probe-memtool.out
```

Expected: `ok: true` reply naming `mcp__arb-memory-local__memory_get`, success, and body SHA-256 `004abb71e01b7aa28451d6e190dbb720c515125be6aec5fba3e841a1112c5d7d` (the v13 raw pin verified during the r1b round). A seat replying "tool unavailable" = gate FAILED — check the seat log in `~/Library/Logs/agent-bridge/` for MCP mount errors before touching code.

- [ ] **Step 2: Repeat for the Fable seat**

Same command with `--target-id asdk-piext-dev-fable5` and output files `/tmp/probe-memtool-fable.{out,err}`.
Expected: same success shape. (Fable seat brief stays minimal — no reasoning-echo asks.)

- [ ] **Step 3: Record the gate**

Append a dated note to `docs/superpowers/specs/2026-07-22-asdk-local-memory-mcp-tier-design.md` under a new `## Live gate result` heading: probe run-ids, both seats' tool-call success, and the matched SHA. Commit:

```bash
cd /Users/<user>/<workspace>
git add docs/superpowers/specs/2026-07-22-asdk-local-memory-mcp-tier-design.md
git commit -m "docs: asdk local-memory MCP live gate results"
git push origin dev
```
