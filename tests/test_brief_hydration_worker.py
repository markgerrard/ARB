"""Bridge worker-boundary tests for brief hydration (Slice 1d-v Task 6 Step 2).

Fake engine mirrors the live worker shape: it parses the pointer prompt, invokes
the hydrate helper (or writes a receipt matching that helper's multi-line JSON
shape), then returns success. Fidelity pin: receipt is multi-field JSON with
artefact_id/version/content_hash/path — same keys the live helper emits.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent_redis_bridge.bridge import Bridge
from agent_redis_bridge.envelope import Envelope
from agent_redis_bridge.engines.base import TurnResult

BODY_SENTINEL = "BODY_SENTINEL_1d_v_v1_NOT_ON_WIRE"


def _request_with_task(task, *, request_id="req-hyd-1"):
    return Envelope(
        id=request_id,
        sender="claude-x",
        branch="dev",
        recipient="codex-x",
        kind="request",
        sent_at="2026-07-28T00:00:00+00:00",
        payload={"task": task},
        run_id="run-hyd-1",
    )


def _parse_paths_from_prompt(prompt: str) -> tuple[Path, Path]:
    """Extract --output and --receipt paths from a pointer prompt."""
    out_m = re.search(r"--output\s+(\S+)", prompt)
    rcpt_m = re.search(r"--receipt\s+(\S+)", prompt)
    assert out_m and rcpt_m, f"pointer prompt missing paths: {prompt[:500]}"
    return Path(out_m.group(1)), Path(rcpt_m.group(1))


class HydratingFakeEngine:
    """Emulates worker: run helper (or write faithful receipt), then succeed.

    Real shape mirrored: multi-field receipt JSON, paths taken from prompt argv
    (not invented), body written only under the staged output path.
    """

    def __init__(
        self,
        *,
        write_receipt: bool = True,
        receipt_override: dict | None = None,
        raise_on_turn: Exception | None = None,
        body: str = BODY_SENTINEL,
        content_hash: str = "a" * 64,
    ):
        self.write_receipt = write_receipt
        self.receipt_override = receipt_override
        self.raise_on_turn = raise_on_turn
        self.body = body
        self.content_hash = content_hash
        self.prompts: list[str] = []
        self.events: list[tuple] = []
        self.transcript_chunks: list[str] = []
        self.seen_body_paths: list[Path] = []

    def run_turn_with_progress(self, prompt, *, timeout, policy, on_event) -> TurnResult:
        self.prompts.append(prompt)
        # Emit prompt onto event + transcript capture surfaces so body-sentinel
        # checks can pin plan claim "prompt, event, and transcript".
        if on_event is not None:
            on_event("prompt", {"text": prompt})
            self.events.append(("prompt", {"text": prompt}))
            self.transcript_chunks.append(prompt)
            on_event("transcript", {"text": prompt})
            self.events.append(("transcript", {"text": prompt}))
        if self.raise_on_turn is not None:
            raise self.raise_on_turn
        out, receipt = _parse_paths_from_prompt(prompt)
        # Worker stages the brief body (helper does this live).
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(self.body, encoding="utf-8")
        os.chmod(out, 0o600)
        self.seen_body_paths.append(out)
        if self.write_receipt:
            # Parse ref from prompt for faithful receipt.
            id_m = re.search(r"--artefact-id\s+(\S+)", prompt)
            ver_m = re.search(r"--version\s+(\d+)", prompt)
            payload = {
                "artefact_id": id_m.group(1) if id_m else "missing",
                "version": int(ver_m.group(1)) if ver_m else 0,
                "content_hash": self.content_hash,
                "path": str(out),
            }
            if self.receipt_override is not None:
                payload = dict(self.receipt_override)
            receipt.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            os.chmod(receipt, 0o600)
        return TurnResult(ok=True, result="engine-ok")


def _bridge_ready(*, ready: bool = True, runtime_root: Path | None = None) -> Bridge:
    bridge = object.__new__(Bridge)
    bridge.args = SimpleNamespace(dry_run=False, turn_timeout=30)
    bridge.brief_hydrate_ready = ready
    bridge.engine_name = "codex"
    bridge.role_profile = None
    if runtime_root is not None:
        bridge.brief_stage_root = Path(runtime_root)
    else:
        bridge.brief_stage_root = Path(tempfile.mkdtemp(prefix="brief-stage-root-"))
        os.chmod(bridge.brief_stage_root, 0o700)
    return bridge


def test_pointer_only_prompt_no_body_sentinel(monkeypatch, tmp_path):
    bridge = _bridge_ready(runtime_root=tmp_path / "root")
    bridge.brief_stage_root.mkdir(parents=True, exist_ok=True)
    os.chmod(bridge.brief_stage_root, 0o700)
    engine = HydratingFakeEngine()
    progress_events: list[tuple] = []

    def capture_progress(self, envelope, event, data, policy=None):
        progress_events.append((event, data))

    monkeypatch.setattr(
        Bridge,
        "role_profile_for_turn",
        lambda self, engine, task_override=None: None,
    )
    monkeypatch.setattr(Bridge, "expects_structured", lambda self, env: False)
    monkeypatch.setattr(Bridge, "effective_task_turn_timeout", lambda self, env: 30)
    monkeypatch.setattr(Bridge, "handle_progress", capture_progress)

    ref = {"artefact_id": "art-pointer-1", "version": 2}
    result = Bridge.run_engine(
        bridge,
        _request_with_task(ref),
        policy="trusted",
        engine=engine,
    )
    assert result.ok is True
    assert len(engine.prompts) == 1
    prompt = engine.prompts[0]
    assert BODY_SENTINEL not in prompt
    assert "art-pointer-1" in prompt
    assert "arb-memory-brief-hydrate" in prompt
    assert "--output" in prompt
    assert "--receipt" in prompt
    assert "--version" in prompt
    # Constant filenames, not id-derived.
    assert "art-pointer-1" not in prompt.split("--output")[1].split()[0]
    assert "/brief" in prompt or "brief" in Path(prompt.split("--output")[1].split()[0]).name
    # Plan claim: body sentinel absent from prompt, event, AND transcript captures.
    event_blob = json.dumps(engine.events) + json.dumps(progress_events)
    transcript_blob = "\n".join(engine.transcript_chunks)
    assert BODY_SENTINEL not in event_blob
    assert BODY_SENTINEL not in transcript_blob
    assert engine.events, "fake must emit event capture for sentinel check"
    assert engine.transcript_chunks, "fake must emit transcript capture for sentinel check"


def test_valid_receipt_permits_turn(monkeypatch, tmp_path):
    bridge = _bridge_ready(runtime_root=tmp_path / "root")
    bridge.brief_stage_root.mkdir(parents=True, exist_ok=True)
    engine = HydratingFakeEngine(content_hash="b" * 64)
    monkeypatch.setattr(Bridge, "role_profile_for_turn", lambda *a, **k: None)
    monkeypatch.setattr(Bridge, "expects_structured", lambda self, env: False)
    monkeypatch.setattr(Bridge, "effective_task_turn_timeout", lambda self, env: 30)

    result = Bridge.run_engine(
        bridge,
        _request_with_task({"artefact_id": "art-ok", "version": 1}),
        policy="trusted",
        engine=engine,
    )
    assert result.ok is True
    assert result.result == "engine-ok"


def test_absent_receipt_converts_success_to_hydration_failure(monkeypatch, tmp_path):
    bridge = _bridge_ready(runtime_root=tmp_path / "root")
    bridge.brief_stage_root.mkdir(parents=True, exist_ok=True)
    engine = HydratingFakeEngine(write_receipt=False)
    monkeypatch.setattr(Bridge, "role_profile_for_turn", lambda *a, **k: None)
    monkeypatch.setattr(Bridge, "expects_structured", lambda self, env: False)
    monkeypatch.setattr(Bridge, "effective_task_turn_timeout", lambda self, env: 30)

    result = Bridge.run_engine(
        bridge,
        _request_with_task({"artefact_id": "art-noreceipt", "version": 1}),
        policy="trusted",
        engine=engine,
    )
    assert result.ok is False
    assert result.error == "brief_hydration_receipt_missing"


def test_wrong_ref_receipt_converts_success(monkeypatch, tmp_path):
    bridge = _bridge_ready(runtime_root=tmp_path / "root")
    bridge.brief_stage_root.mkdir(parents=True, exist_ok=True)
    engine = HydratingFakeEngine(
        receipt_override={
            "artefact_id": "art-OTHER",
            "version": 99,
            "content_hash": "c" * 64,
            "path": "/tmp/x",
        }
    )
    monkeypatch.setattr(Bridge, "role_profile_for_turn", lambda *a, **k: None)
    monkeypatch.setattr(Bridge, "expects_structured", lambda self, env: False)
    monkeypatch.setattr(Bridge, "effective_task_turn_timeout", lambda self, env: 30)

    result = Bridge.run_engine(
        bridge,
        _request_with_task({"artefact_id": "art-expected", "version": 1}),
        policy="trusted",
        engine=engine,
    )
    assert result.ok is False
    assert result.error == "brief_hydration_receipt_mismatch"


def test_malformed_receipt_converts_success(monkeypatch, tmp_path):
    bridge = _bridge_ready(runtime_root=tmp_path / "root")
    bridge.brief_stage_root.mkdir(parents=True, exist_ok=True)

    class BadReceiptEngine(HydratingFakeEngine):
        def run_turn_with_progress(self, prompt, *, timeout, policy, on_event):
            self.prompts.append(prompt)
            out, receipt = _parse_paths_from_prompt(prompt)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(self.body, encoding="utf-8")
            receipt.write_text("not-json{{{\n", encoding="utf-8")
            return TurnResult(ok=True, result="engine-ok")

    engine = BadReceiptEngine()
    monkeypatch.setattr(Bridge, "role_profile_for_turn", lambda *a, **k: None)
    monkeypatch.setattr(Bridge, "expects_structured", lambda self, env: False)
    monkeypatch.setattr(Bridge, "effective_task_turn_timeout", lambda self, env: 30)

    result = Bridge.run_engine(
        bridge,
        _request_with_task({"artefact_id": "art-badjson", "version": 1}),
        policy="trusted",
        engine=engine,
    )
    assert result.ok is False
    assert result.error == "brief_hydration_receipt_invalid"


def test_finally_cleanup_after_engine_exception(monkeypatch, tmp_path):
    bridge = _bridge_ready(runtime_root=tmp_path / "root")
    bridge.brief_stage_root.mkdir(parents=True, exist_ok=True)
    staged_dirs: list[Path] = []

    class ExplodingEngine:
        def run_turn_with_progress(self, prompt, *, timeout, policy, on_event):
            out, _ = _parse_paths_from_prompt(prompt)
            staged_dirs.append(out.parent)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(BODY_SENTINEL, encoding="utf-8")
            raise RuntimeError("engine crashed mid-turn")

    monkeypatch.setattr(Bridge, "role_profile_for_turn", lambda *a, **k: None)
    monkeypatch.setattr(Bridge, "expects_structured", lambda self, env: False)
    monkeypatch.setattr(Bridge, "effective_task_turn_timeout", lambda self, env: 30)

    result = Bridge.run_engine(
        bridge,
        _request_with_task({"artefact_id": "art-crash", "version": 1}),
        policy="trusted",
        engine=ExplodingEngine(),
    )
    assert result.ok is False
    assert staged_dirs, "engine must have seen a stage dir"
    for d in staged_dirs:
        assert not d.exists(), f"stage dir leaked after exception: {d}"


def test_finally_cleanup_after_success(monkeypatch, tmp_path):
    bridge = _bridge_ready(runtime_root=tmp_path / "root")
    bridge.brief_stage_root.mkdir(parents=True, exist_ok=True)
    engine = HydratingFakeEngine()
    monkeypatch.setattr(Bridge, "role_profile_for_turn", lambda *a, **k: None)
    monkeypatch.setattr(Bridge, "expects_structured", lambda self, env: False)
    monkeypatch.setattr(Bridge, "effective_task_turn_timeout", lambda self, env: 30)

    Bridge.run_engine(
        bridge,
        _request_with_task({"artefact_id": "art-clean", "version": 1}),
        policy="trusted",
        engine=engine,
    )
    # Stage directories under runtime root must be gone.
    remaining = list(bridge.brief_stage_root.iterdir()) if bridge.brief_stage_root.exists() else []
    assert remaining == [], f"leaked stage paths: {remaining}"


def test_bridge_never_reads_staged_body(monkeypatch, tmp_path):
    bridge = _bridge_ready(runtime_root=tmp_path / "root")
    bridge.brief_stage_root.mkdir(parents=True, exist_ok=True)
    body_reads: list[str] = []
    real_open = open
    real_read_text = Path.read_text
    real_read_bytes = Path.read_bytes

    def tracking_open(file, *args, **kwargs):
        path = str(file)
        if path.endswith("/brief") or path.endswith("\\brief") or path.rstrip("/").endswith("brief"):
            # Allow the fake engine's write path only when mode is write.
            mode = args[0] if args else kwargs.get("mode", "r")
            if "r" in str(mode) and "w" not in str(mode):
                body_reads.append(f"open:{path}")
        return real_open(file, *args, **kwargs)

    def tracking_read_text(self, *args, **kwargs):
        if self.name == "brief":
            body_reads.append(f"read_text:{self}")
        return real_read_text(self, *args, **kwargs)

    def tracking_read_bytes(self, *args, **kwargs):
        if self.name == "brief":
            body_reads.append(f"read_bytes:{self}")
        return real_read_bytes(self, *args, **kwargs)

    engine = HydratingFakeEngine()
    monkeypatch.setattr(Bridge, "role_profile_for_turn", lambda *a, **k: None)
    monkeypatch.setattr(Bridge, "expects_structured", lambda self, env: False)
    monkeypatch.setattr(Bridge, "effective_task_turn_timeout", lambda self, env: 30)

    with (
        patch("builtins.open", tracking_open),
        patch.object(Path, "read_text", tracking_read_text),
        patch.object(Path, "read_bytes", tracking_read_bytes),
    ):
        result = Bridge.run_engine(
            bridge,
            _request_with_task({"artefact_id": "art-nobody", "version": 1}),
            policy="trusted",
            engine=engine,
        )
    assert result.ok is True
    assert body_reads == [], f"bridge read staged body: {body_reads}"


def test_poisoned_dsn_bridge_never_calls_store_fetch(monkeypatch, tmp_path):
    """Bridge process must not call store.fetch_artefact or psycopg.connect for hydration."""
    bridge = _bridge_ready(runtime_root=tmp_path / "root")
    bridge.brief_stage_root.mkdir(parents=True, exist_ok=True)
    # Poison every DSN the bridge might see.
    monkeypatch.setenv("ARB_MEMORY_LOCAL_DSN", "host=poisoned port=1 dbname=x user=x password=x")
    monkeypatch.setenv("ARB_MEMORY_DSN", "host=poisoned port=1 dbname=x user=x password=x")
    monkeypatch.setenv(
        "ARB_MEMORY_GATE_READER_DSN",
        "host=poisoned port=1 dbname=x user=gate password=x",
    )

    fetch_calls: list = []
    connect_calls: list = []

    def boom_fetch(*a, **k):
        fetch_calls.append((a, k))
        raise AssertionError("bridge must not fetch_artefact for hydration")

    def boom_connect(*a, **k):
        connect_calls.append((a, k))
        raise AssertionError("bridge must not psycopg.connect for hydration")

    monkeypatch.setattr("arb_memory.store.fetch_artefact", boom_fetch)
    monkeypatch.setattr("psycopg.connect", boom_connect)
    engine = HydratingFakeEngine()
    monkeypatch.setattr(Bridge, "role_profile_for_turn", lambda *a, **k: None)
    monkeypatch.setattr(Bridge, "expects_structured", lambda self, env: False)
    monkeypatch.setattr(Bridge, "effective_task_turn_timeout", lambda self, env: 30)

    result = Bridge.run_engine(
        bridge,
        _request_with_task({"artefact_id": "art-poison", "version": 1}),
        policy="trusted",
        engine=engine,
    )
    assert result.ok is True
    assert fetch_calls == []
    assert connect_calls == []


def test_parse_only_control_brief_hydration_unavailable_before_prompt(monkeypatch):
    """1d-iv control: absent readiness → exact code, no prompt, no ref string form."""
    bridge = _bridge_ready(ready=False)
    engine = MagicMock()
    prompts: list[str] = []

    def boom(*a, **k):
        raise AssertionError("build_task_prompt must not run")

    monkeypatch.setattr("agent_redis_bridge.bridge.build_task_prompt", boom)
    result = Bridge.run_engine(
        bridge,
        _request_with_task({"artefact_id": "art-1", "version": 1}),
        policy="trusted",
        engine=engine,
    )
    assert result.ok is False
    assert result.error == "brief_hydration_unavailable"
    engine.run_turn_with_progress.assert_not_called()
    assert prompts == []
    assert "{'artefact_id'" not in (result.result or "")
    assert "art-1" not in (result.result or "")


def test_readiness_false_without_console_script(monkeypatch, tmp_path):
    """H1: absent console script → readiness False (import alone is not enough)."""
    from agent_redis_bridge import brief_hydrate_ready as bhr

    monkeypatch.setattr(bhr, "locate_brief_hydrate_console_script", lambda: None)
    root = tmp_path / "rt"
    root.mkdir()
    ok = bhr.prove_brief_hydrate_readiness(
        env={}, runtime_root=root, allow_missing_dsn=True
    )
    assert ok is False


def test_readiness_true_when_console_script_probe_exits_zero(monkeypatch, tmp_path):
    """H1 present→True: locatable script that exits 0 on --readiness-probe."""
    from agent_redis_bridge import brief_hydrate_ready as bhr

    script = tmp_path / "arb-memory-brief-hydrate"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--readiness-probe" ]; then echo ok; exit 0; fi\n'
        "exit 1\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setattr(bhr, "locate_brief_hydrate_console_script", lambda: str(script))
    root = tmp_path / "rt"
    root.mkdir()
    ok = bhr.prove_brief_hydrate_readiness(
        env={}, runtime_root=root, allow_missing_dsn=True
    )
    assert ok is True


def test_readiness_false_when_console_script_probe_fails(monkeypatch, tmp_path):
    from agent_redis_bridge import brief_hydrate_ready as bhr

    script = tmp_path / "arb-memory-brief-hydrate"
    script.write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setattr(bhr, "locate_brief_hydrate_console_script", lambda: str(script))
    root = tmp_path / "rt"
    root.mkdir()
    ok = bhr.prove_brief_hydrate_readiness(
        env={}, runtime_root=root, allow_missing_dsn=True
    )
    assert ok is False


def test_readiness_true_via_real_path_locator(monkeypatch, tmp_path):
    """H1 binding hygiene: real locate/shutil.which branch (no locator stub)."""
    from agent_redis_bridge import brief_hydrate_ready as bhr

    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / bhr.HELPER_CONSOLE_SCRIPT
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--readiness-probe" ]; then echo ok; exit 0; fi\n'
        "exit 1\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    # Controlled PATH: only our bin/ first so which finds the probe script.
    monkeypatch.setenv("PATH", str(bindir))
    # Do NOT stub locate_brief_hydrate_console_script — exercise shutil.which.
    root = tmp_path / "rt"
    root.mkdir()
    ok = bhr.prove_brief_hydrate_readiness(
        env={}, runtime_root=root, allow_missing_dsn=True
    )
    assert ok is True
    # Locator itself returns the which path.
    located = bhr.locate_brief_hydrate_console_script()
    assert located is not None
    assert Path(located).name == bhr.HELPER_CONSOLE_SCRIPT


def test_readiness_false_when_script_only_in_venv_bin_not_on_path(monkeypatch, tmp_path):
    """G1 corridor: executable in venv bin/ but bare name off PATH → readiness False.

    The pointer prompt instructs the bare name; a venv-bin-only copy must not
    make advertisement True (engine child would fail to resolve it).
    """
    import sys

    from agent_redis_bridge import brief_hydrate_ready as bhr

    fake_bin = tmp_path / "fakevenv" / "bin"
    fake_bin.mkdir(parents=True)
    fake_python = fake_bin / "python"
    fake_python.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_python.chmod(0o755)
    script = fake_bin / bhr.HELPER_CONSOLE_SCRIPT
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--readiness-probe" ]; then echo ok; exit 0; fi\n'
        "exit 1\n",
        encoding="utf-8",
    )
    script.chmod(0o755)

    monkeypatch.setattr(sys, "executable", str(fake_python))
    # PATH without the fake venv bin — bare name does not resolve.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    # Diagnostics helper may still see the venv-bin copy; advertisement must not.
    assert bhr.locate_brief_hydrate_console_script_in_venv_bin() == str(script)
    assert bhr.locate_brief_hydrate_console_script() is None

    root = tmp_path / "rt"
    root.mkdir()
    ok = bhr.prove_brief_hydrate_readiness(
        env={}, runtime_root=root, allow_missing_dsn=True
    )
    assert ok is False


def test_advertisement_false_readiness_registers_empty(monkeypatch, tmp_path):
    """H3: readiness forced False → registered brief_hydrate is \"\"."""
    registered: dict = {}

    class FakeRedis:
        def register(self, **kwargs):
            registered.update(kwargs)

    bridge = object.__new__(Bridge)
    bridge.args = SimpleNamespace(dry_run=False, turn_timeout=30, heartbeat_ttl=60)
    bridge.engine_name = "codex"
    bridge.agent_id = "codex-test"
    bridge.tool = "codex"
    bridge.project = "bridge"
    bridge.workspace = "ws"
    bridge.branch = "dev"
    bridge.workdir = tmp_path
    bridge.registered_at = "2026-07-28T00:00:00+00:00"
    bridge.owner_token = "tok"
    bridge.brief_stage_root = tmp_path / "stage"
    bridge.brief_stage_root.mkdir()
    bridge.redis = FakeRedis()
    monkeypatch.setattr(
        "agent_redis_bridge.engines._stdio.prove_env_scrub_capability",
        lambda **k: "bus-and-gate-daemon-creds-v2",
    )
    monkeypatch.setattr(
        "agent_redis_bridge.brief_hydrate_ready.prove_brief_hydrate_readiness",
        lambda **k: False,
    )
    Bridge.reassert_liveness(bridge)
    assert bridge.brief_hydrate_ready is False
    assert registered.get("brief_hydrate") == ""


def test_advertisement_true_readiness_registers_v1(monkeypatch, tmp_path):
    """H3: readiness forced True → registered brief_hydrate is \"v1\"."""
    registered: dict = {}

    class FakeRedis:
        def register(self, **kwargs):
            registered.update(kwargs)

    bridge = object.__new__(Bridge)
    bridge.args = SimpleNamespace(dry_run=False, turn_timeout=30, heartbeat_ttl=60)
    bridge.engine_name = "codex"
    bridge.agent_id = "codex-test"
    bridge.tool = "codex"
    bridge.project = "bridge"
    bridge.workspace = "ws"
    bridge.branch = "dev"
    bridge.workdir = tmp_path
    bridge.registered_at = "2026-07-28T00:00:00+00:00"
    bridge.owner_token = "tok"
    bridge.brief_stage_root = tmp_path / "stage"
    bridge.brief_stage_root.mkdir()
    bridge.redis = FakeRedis()
    monkeypatch.setattr(
        "agent_redis_bridge.engines._stdio.prove_env_scrub_capability",
        lambda **k: "bus-and-gate-daemon-creds-v2",
    )
    monkeypatch.setattr(
        "agent_redis_bridge.brief_hydrate_ready.prove_brief_hydrate_readiness",
        lambda **k: True,
    )
    Bridge.reassert_liveness(bridge)
    assert bridge.brief_hydrate_ready is True
    assert registered.get("brief_hydrate") == "v1"


def test_declarative_registry_v1_without_readiness_overwritten_to_empty(
    monkeypatch, tmp_path
):
    """H3: injected registry v1 without executed readiness is overwritten to \"\"."""
    registered: dict = {"brief_hydrate": "v1"}  # declarative pretence

    class FakeRedis:
        def register(self, **kwargs):
            registered.clear()
            registered.update(kwargs)

    bridge = object.__new__(Bridge)
    bridge.args = SimpleNamespace(dry_run=False, turn_timeout=30, heartbeat_ttl=60)
    bridge.engine_name = "codex"
    bridge.agent_id = "codex-test"
    bridge.tool = "codex"
    bridge.project = "bridge"
    bridge.workspace = "ws"
    bridge.branch = "dev"
    bridge.workdir = tmp_path
    bridge.registered_at = "2026-07-28T00:00:00+00:00"
    bridge.owner_token = "tok"
    bridge.brief_stage_root = tmp_path / "stage"
    bridge.brief_stage_root.mkdir()
    bridge.redis = FakeRedis()
    # Pretend prior declarative value; reassert with readiness False must clear it.
    monkeypatch.setattr(
        "agent_redis_bridge.engines._stdio.prove_env_scrub_capability",
        lambda **k: "bus-and-gate-daemon-creds-v2",
    )
    monkeypatch.setattr(
        "agent_redis_bridge.brief_hydrate_ready.prove_brief_hydrate_readiness",
        lambda **k: False,
    )
    Bridge.reassert_liveness(bridge)
    assert registered.get("brief_hydrate") == ""


def test_stage_allocation_failure_returns_named_turn_result(monkeypatch, tmp_path):
    """H6: allocate_stage_dir OSError → TurnResult brief_hydration_stage_failure."""
    bridge = _bridge_ready(runtime_root=tmp_path / "root")
    bridge.brief_stage_root.mkdir(parents=True, exist_ok=True)
    engine = MagicMock()
    monkeypatch.setattr(Bridge, "role_profile_for_turn", lambda *a, **k: None)
    monkeypatch.setattr(Bridge, "expects_structured", lambda self, env: False)
    monkeypatch.setattr(Bridge, "effective_task_turn_timeout", lambda self, env: 30)
    monkeypatch.setattr(
        "agent_redis_bridge.brief_hydrate_ready.allocate_stage_dir",
        lambda root: (_ for _ in ()).throw(OSError("ENOSPC simulated")),
    )
    result = Bridge.run_engine(
        bridge,
        _request_with_task({"artefact_id": "art-stage-fail", "version": 1}),
        policy="trusted",
        engine=engine,
    )
    assert isinstance(result, TurnResult)
    assert result.ok is False
    assert result.error == "brief_hydration_stage_failure"
    engine.run_turn_with_progress.assert_not_called()


def test_cleanup_stage_dir_reports_failure_when_rmtree_fails(monkeypatch, tmp_path):
    """P2 cleanup-failure leg: failed rmtree is observable (returns False)."""
    from agent_redis_bridge import brief_hydrate_ready as bhr

    stage = tmp_path / "stage-keep"
    stage.mkdir()
    (stage / "brief").write_text("x", encoding="utf-8")

    def boom(path, *a, **k):
        raise OSError("simulated rmtree failure")

    monkeypatch.setattr(bhr.shutil, "rmtree", boom)
    ok = bhr.cleanup_stage_dir(stage)
    assert ok is False
    assert stage.exists()


def test_turn_finally_logs_when_cleanup_returns_false(monkeypatch, tmp_path, caplog):
    """Turn finally consumes cleanup_stage_dir bool; False → error log with envelope id."""
    import logging

    bridge = _bridge_ready(runtime_root=tmp_path / "root")
    bridge.brief_stage_root.mkdir(parents=True, exist_ok=True)
    engine = HydratingFakeEngine()
    monkeypatch.setattr(Bridge, "role_profile_for_turn", lambda *a, **k: None)
    monkeypatch.setattr(Bridge, "expects_structured", lambda self, env: False)
    monkeypatch.setattr(Bridge, "effective_task_turn_timeout", lambda self, env: 30)
    monkeypatch.setattr(
        "agent_redis_bridge.brief_hydrate_ready.cleanup_stage_dir",
        lambda stage: False,
    )
    req = _request_with_task({"artefact_id": "art-cleanup-log", "version": 1})
    with caplog.at_level(logging.ERROR, logger="agent_redis_bridge.bridge"):
        result = Bridge.run_engine(
            bridge,
            req,
            policy="trusted",
            engine=engine,
        )
    assert result.ok is True
    records = [r for r in caplog.records if "brief-hydrate stage cleanup failed" in r.getMessage()]
    assert records, "turn finally must log when cleanup_stage_dir returns False"
    assert req.id in records[0].getMessage()


def test_object_task_never_str_into_prompt(monkeypatch, tmp_path):
    bridge = _bridge_ready(ready=True, runtime_root=tmp_path / "root")
    bridge.brief_stage_root.mkdir(parents=True, exist_ok=True)
    seen: list[str] = []

    def capture(task, **kwargs):
        seen.append(task if isinstance(task, str) else repr(task))
        raise AssertionError("legacy build_task_prompt must not receive object task")

    monkeypatch.setattr("agent_redis_bridge.bridge.build_task_prompt", capture)
    engine = HydratingFakeEngine()
    monkeypatch.setattr(Bridge, "role_profile_for_turn", lambda *a, **k: None)
    monkeypatch.setattr(Bridge, "expects_structured", lambda self, env: False)
    monkeypatch.setattr(Bridge, "effective_task_turn_timeout", lambda self, env: 30)

    # Ready path builds pointer prompt itself — must not call build_task_prompt with dict.
    result = Bridge.run_engine(
        bridge,
        _request_with_task({"artefact_id": "art-nostr", "version": 1}),
        policy="trusted",
        engine=engine,
    )
    assert result.ok is True
    assert seen == []
