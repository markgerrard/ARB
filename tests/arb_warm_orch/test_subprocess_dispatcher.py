"""Warm-orch live-dispatch slice: SubprocessSeatDispatcher.

The live path is store-before-send (Slice 1d-iv): compose a brief (title +
`## Assumptions` fence + body — NOTHING but the fence in the assumptions
section, per env-trap B4(i)), publish it via arb-memory-harness-publish for a
receipt, then enqueue via agent-dispatch with the pre-minted quartet. The
subprocess runner is injected so these tests exercise composition, env
hygiene, and result parsing without a bus. One test per named behavior,
red-first.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from arb_warm_orch.subprocess_dispatch import (
    DispatchError,
    LiveDispatchConfig,
    SubprocessSeatDispatcher,
    engine_for_seat,
)

RECEIPT = {"artefact_id": "art-abc123", "version": 3}


@dataclass
class FakeRunner:
    """Scripted subprocess.run stand-in recording every invocation."""

    publish_result: CompletedProcess = field(
        default_factory=lambda: CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(RECEIPT), stderr=""
        )
    )
    dispatch_result: CompletedProcess = field(
        default_factory=lambda: CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"ok": True, "result": "seat says hi"}),
            stderr="",
        )
    )
    calls: list = field(default_factory=list)

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if "harness-publish" in str(argv[0]):
            return self.publish_result
        return self.dispatch_result


def _config(tmp_path: Path) -> LiveDispatchConfig:
    return LiveDispatchConfig(
        bridge_clone=tmp_path / "bridge",
        from_agent_id="claude-bridge-dev",
        branch="dev",
        brief_dir=tmp_path / "briefs",
        env_file=tmp_path / "bridge" / "envs" / "bus.env",
        timeout=1800,
    )


def _dispatcher(tmp_path, runner=None):
    runner = runner or FakeRunner()
    return SubprocessSeatDispatcher(_config(tmp_path), runner=runner), runner


# --------------------------------------------------------------- engine map


def test_codex_seat_maps_to_codex_engine():
    assert engine_for_seat("codex-bridge-dev-example") == "codex"


def test_agy_seat_maps_to_agy_print():
    assert engine_for_seat("agy-bridge-dev") == "agy-print"


def test_grok_seat_maps_to_grok_acp():
    assert engine_for_seat("grok-bridge-dev") == "grok-acp"


def test_unknown_seat_prefix_raises_specific_code():
    with pytest.raises(DispatchError, match="dispatch-unknown-seat-engine"):
        engine_for_seat("mystery-seat-1")


# ------------------------------------------------------------------- brief


def test_brief_has_title_assumptions_fence_and_lens_body(tmp_path):
    dispatcher, runner = _dispatcher(tmp_path)
    dispatcher.dispatch("codex-bridge-dev-example", "correctness", "review the diff")
    brief_path = Path(runner.calls[0][0][runner.calls[0][0].index("--brief") + 1])
    text = brief_path.read_text()
    lines = [l for l in text.splitlines() if l.strip()]
    assert lines[0].startswith("# ")
    assumptions = text.split("## Assumptions", 1)[1].split("## ", 1)[0]
    fence_body = assumptions.split("```json", 1)[1].split("```", 1)[0]
    assert json.loads(fence_body) == {"items": []}
    assert "correctness" in text
    assert "review the diff" in text


def test_lens_appears_before_task_in_brief_body(tmp_path):
    dispatcher, runner = _dispatcher(tmp_path)
    dispatcher.dispatch("codex-bridge-dev-example", "security", "audit X")
    text = Path(runner.calls[0][0][runner.calls[0][0].index("--brief") + 1]).read_text()
    assert text.index("security") < text.index("audit X")


# ----------------------------------------------------------------- publish


def test_publish_invoked_with_target_and_brief(tmp_path):
    dispatcher, runner = _dispatcher(tmp_path)
    dispatcher.dispatch("codex-bridge-dev-example", "l", "t")
    argv = runner.calls[0][0]
    assert str(argv[0]).endswith("scripts/arb-memory-harness-publish")
    assert argv[argv.index("--target-agent-id") + 1] == "codex-bridge-dev-example"


def test_publish_failure_raises_specific_code(tmp_path):
    runner = FakeRunner(
        publish_result=CompletedProcess([], 1, stdout="", stderr="no credential")
    )
    dispatcher, _ = _dispatcher(tmp_path, runner)
    with pytest.raises(DispatchError, match="dispatch-publish-failed"):
        dispatcher.dispatch("codex-bridge-dev-example", "l", "t")


# ---------------------------------------------------------------- dispatch


def test_dispatch_carries_quartet_from_receipt(tmp_path):
    dispatcher, runner = _dispatcher(tmp_path)
    dispatcher.dispatch("codex-bridge-dev-example", "l", "t")
    argv = runner.calls[1][0]
    assert str(argv[0]).endswith("scripts/agent-dispatch")
    assert argv[argv.index("--artefact-id") + 1] == "art-abc123"
    assert argv[argv.index("--version") + 1] == "3"
    assert "--receipt" in argv and "--run-id" in argv
    assert argv[argv.index("--engine") + 1] == "codex"
    assert argv[argv.index("--target-id") + 1] == "codex-bridge-dev-example"


def test_dispatch_env_sets_sender_and_branch_and_strips_memory_url(tmp_path, monkeypatch):
    monkeypatch.setenv("ARB_MEMORY_REDIS_URL", "redis://secret")
    dispatcher, runner = _dispatcher(tmp_path)
    dispatcher.dispatch("codex-bridge-dev-example", "l", "t")
    env = runner.calls[1][1]["env"]
    assert env["FROM_AGENT_ID"] == "claude-bridge-dev"
    assert env["BRANCH"] == "dev"
    assert "ARB_MEMORY_REDIS_URL" not in env
    assert str(_config(tmp_path).env_file) == env["AGENT_ENV_FILE"]


def test_publish_env_keeps_memory_url(tmp_path, monkeypatch):
    monkeypatch.setenv("ARB_MEMORY_REDIS_URL", "redis://secret")
    dispatcher, runner = _dispatcher(tmp_path)
    dispatcher.dispatch("codex-bridge-dev-example", "l", "t")
    publish_env = runner.calls[0][1]["env"]
    assert publish_env.get("ARB_MEMORY_REDIS_URL") == "redis://secret"


# ------------------------------------------------------------------ result


def test_success_returns_completed_with_payload_result(tmp_path):
    dispatcher, _ = _dispatcher(tmp_path)
    result = dispatcher.dispatch("codex-bridge-dev-example", "l", "t")
    assert result.status == "completed"
    assert result.seat == "codex-bridge-dev-example"
    assert result.reply == "seat says hi"


def test_timeout_returns_timeout_status(tmp_path):
    runner = FakeRunner(
        dispatch_result=CompletedProcess([], 124, stdout="", stderr="timed out waiting")
    )
    dispatcher, _ = _dispatcher(tmp_path, runner)
    result = dispatcher.dispatch("codex-bridge-dev-example", "l", "t")
    assert result.status == "timeout"


def test_failure_returns_failed_with_stderr_tail(tmp_path):
    runner = FakeRunner(
        dispatch_result=CompletedProcess(
            [], 1, stdout="", stderr="[bridge-error] sender-rejected claude-x"
        )
    )
    dispatcher, _ = _dispatcher(tmp_path, runner)
    result = dispatcher.dispatch("codex-bridge-dev-example", "l", "t")
    assert result.status == "failed"
    assert "sender-rejected" in result.reply


def test_publish_env_puts_bridge_venv_on_path(tmp_path):
    dispatcher, runner = _dispatcher(tmp_path)
    dispatcher.dispatch("codex-bridge-dev-example", "l", "t")
    publish_env = runner.calls[0][1]["env"]
    assert str(_config(tmp_path).bridge_clone / ".venv" / "bin") in publish_env["PATH"]
