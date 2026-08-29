from __future__ import annotations

import asyncio
from dataclasses import replace
import logging
import threading
from pathlib import Path

import pytest
from claude_agent_sdk import ResultMessage

from agent_redis_bridge.engines import agent_sdk as agent_sdk_mod
from agent_redis_bridge.engines.agent_sdk import AgentSdkEngine
from agent_redis_bridge.engines.agent_sdk_models import MODELS, resolve
from agent_redis_bridge.engines.base import EngineError


class FakeClient:
    def __init__(self, messages=()) -> None:
        self.messages = list(messages)
        self.queries = []
        self.connected = False
        self.disconnected = False

    async def connect(self):
        self.connected = True

    async def query(self, prompt):
        self.queries.append(prompt)

    async def receive_response(self):
        for message in self.messages:
            yield message

    async def disconnect(self):
        self.disconnected = True


def _result(session_id="sid-sub"):
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id=session_id,
        result="done",
        stop_reason="success",
    )


def _engine(tmp_path: Path, **kwargs) -> AgentSdkEngine:
    return AgentSdkEngine(
        cwd=str(tmp_path),
        model=kwargs.pop("model", "sonnet-5"),
        tool_ceiling=kwargs.pop("tool_ceiling", "Read,Grep,Glob,LS"),
        key=kwargs.pop("key", "oauth-token"),
        session_root=tmp_path / "sessions",
        startup_probe=False,
        client_factory=kwargs.pop("client_factory", lambda **factory_kwargs: FakeClient([_result()])),
        **kwargs,
    )


def test_subscription_build_options_neutralizes_shadow_keys_and_sets_per_seat_config(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "vendor-key")
    monkeypatch.setenv("ANTHROPIC_UNLISTED_FUTURE", "new-leak")
    monkeypatch.setenv("AGENT_SDK_MINIMAX_KEY", "minimax-leak")

    engine = _engine(tmp_path, agent_id="sub-seat")
    options = engine._build_options()

    assert options.env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-token"
    assert Path(options.env["CLAUDE_CONFIG_DIR"]).parent == tmp_path / "sessions" / "sub-seat" / "claude-config"
    assert options.env["ANTHROPIC_API_KEY"] == ""
    assert options.env["ANTHROPIC_UNLISTED_FUTURE"] == ""
    assert options.env["AGENT_SDK_MINIMAX_KEY"] == ""


def test_subscription_build_options_fails_if_sdk_merge_would_leave_shadow_key(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "vendor-key")

    def unsafe_env(*, base, config_dir=None):
        return {"CLAUDE_CODE_OAUTH_TOKEN": base["CLAUDE_CODE_OAUTH_TOKEN"]}

    monkeypatch.setattr(agent_sdk_mod, "subscription_env", unsafe_env)

    with pytest.raises(EngineError, match="shadow"):
        _engine(tmp_path)._build_options()


def test_subscription_build_options_requires_oauth_token(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    with pytest.raises(EngineError, match="CLAUDE_CODE_OAUTH_TOKEN"):
        _engine(tmp_path)._build_options()


def test_subscription_build_options_rejects_bare_launch(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token")

    with pytest.raises(EngineError, match="bare"):
        _engine(tmp_path, bare=True)._build_options()


def test_reviewer_opus_subscription_never_resumes_existing_session(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token")
    session_file = tmp_path / "sessions" / "reviewer" / "last-session-id"
    session_file.parent.mkdir(parents=True)
    session_file.write_text("sid-old\n", encoding="utf-8")

    engine = _engine(tmp_path, model="opus-4.8", agent_id="reviewer")
    options = engine._build_options()

    assert engine.session_id is None
    assert not engine.supports_continuation
    assert options.resume is None


def test_subscription_reviewer_role_stays_cold_if_model_name_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token")
    MODELS["renamed-opus-reviewer"] = replace(
        resolve("opus-4.8"),
        name="renamed-opus-reviewer",
        slug="opus-new",
    )
    try:
        session_file = tmp_path / "sessions" / "reviewer" / "last-session-id"
        session_file.parent.mkdir(parents=True)
        session_file.write_text("sid-old\n", encoding="utf-8")

        engine = _engine(tmp_path, model="renamed-opus-reviewer", agent_id="reviewer")
        options = engine._build_options()

        assert engine.session_id is None
        assert not engine.supports_continuation
        assert options.resume is None
    finally:
        MODELS.pop("renamed-opus-reviewer", None)


def test_implementor_subscription_seat_does_not_autoresume_persisted_session(tmp_path, monkeypatch):
    # Regression: a persisted last-session-id is NEVER a safe resume source for a
    # subscription seat. The per-seat CLAUDE_CONFIG_DIR is randomised per process
    # (config_dir uses uuid4) and trusted implementor dispatches run in a fresh
    # per-dispatch worktree (a new cwd), so the claude CLI cannot find the prior
    # conversation and `--resume <id>` crashes connect with "No conversation found
    # with session ID". Within-dispatch continuation reuses the LIVE client (no
    # reconnect, see drive_to_completion), so it never needs resume. So: keep the
    # id for observability, keep continuation available, but connect resume=None.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token")
    session_file = tmp_path / "sessions" / "implementor" / "last-session-id"
    session_file.parent.mkdir(parents=True)
    session_file.write_text("sid-old\n", encoding="utf-8")

    engine = _engine(tmp_path, model="sonnet-5", agent_id="implementor")
    options = engine._build_options()

    assert engine.session_id == "sid-old"  # still tracked for observability / labels
    assert engine.supports_continuation  # completion loop still drives it via the live client
    assert options.resume is None  # but never auto-resume a stale/missing session at connect


def test_startup_probe_is_read_only_and_does_not_write(tmp_path, monkeypatch):
    # Regression: the live startup smoke-test must NOT ask the seat to write a file.
    # A Write-capable implementor's gate correctly ALLOWS the write, so the old
    # "try to write ARB_AGENT_SDK_DENY_PROBE.txt" probe littered the base checkout
    # (the pooled engine's cwd) on every boot — made reliable by the cwd-anchor fix.
    # The live probe only needs to round-trip and hit the gate; a read-only listing
    # does that without touching the filesystem.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token")
    engine = _engine(tmp_path, model="sonnet-5", agent_id="impl")
    prompt = engine._startup_probe_prompt().lower()
    assert "write" not in prompt, "probe must not instruct a write (litters the base checkout)"
    assert "create" not in prompt, "probe must not instruct file creation"
    assert "list" in prompt, "probe should drive a harmless read-only listing to hit the gate"


def test_subscription_system_prompt_announces_cwd(tmp_path, monkeypatch):
    # Regression (found live via a no-Bash haiku implementor that wrote its file
    # OUTSIDE the worktree): passing a raw-string system_prompt REPLACES Claude
    # Code's default prompt, which is what normally injects the working-directory
    # line. Without it, a seat whose Write tool needs absolute paths and has no
    # shell to run `pwd` cannot discover its cwd and guesses wrong. The engine must
    # announce its cwd in the system prompt, while preserving the role profile.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token")
    engine = _engine(tmp_path, model="sonnet-5", agent_id="impl", role_profile="ROLE-X")
    options = engine._build_options()
    assert str(tmp_path) in (options.system_prompt or ""), "cwd must be announced so the seat knows where to write"
    assert "ROLE-X" in (options.system_prompt or ""), "role profile must be preserved"


def test_subscription_scrubs_oauth_var_name_and_literal_in_stderr_and_payload(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-secret")
    engine = _engine(tmp_path)

    engine._handle_stderr("CLAUDE_CODE_OAUTH_TOKEN=oauth-secret")
    payload = engine._scrub_payload({"env": "CLAUDE_CODE_OAUTH_TOKEN", "token": "oauth-secret"})

    assert "oauth-secret" not in capsys.readouterr().out
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in payload["env"]
    assert "oauth-secret" not in payload["token"]


def test_scrubbed_session_store_removes_subscription_secret_and_var_name(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-secret")
    engine = _engine(tmp_path)
    store = engine._build_options().session_store

    asyncio.run(
        store.append(
            {"session_id": "sid", "project_key": "proj"},
            [{"message": "CLAUDE_CODE_OAUTH_TOKEN oauth-secret"}],
        )
    )
    loaded = asyncio.run(store.load({"session_id": "sid", "project_key": "proj"}))

    assert loaded == [{"message": "[REDACTED] [REDACTED]"}]


def test_subscription_config_dirs_are_distinct_per_seat(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token")

    first = _engine(tmp_path, agent_id="seat-one")._build_options().env["CLAUDE_CONFIG_DIR"]
    second = _engine(tmp_path, agent_id="seat-two")._build_options().env["CLAUDE_CONFIG_DIR"]

    assert first != second
    assert Path(first).parent == tmp_path / "sessions" / "seat-one" / "claude-config"
    assert Path(second).parent == tmp_path / "sessions" / "seat-two" / "claude-config"


def test_subscription_opus_concurrency_allows_only_one_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token")
    monkeypatch.setattr(agent_sdk_mod, "_SUBSCRIPTION_OPUS_SEMAPHORE", threading.BoundedSemaphore(1))

    first = _engine(tmp_path, model="opus-4.8")
    second = _engine(tmp_path, model="opus-4.8")
    slot = first._acquire_subscription_slot()
    try:
        with pytest.raises(EngineError, match="concurrency"):
            second._acquire_subscription_slot()
    finally:
        slot.release()


def test_fable_implementor_can_run_while_opus_reviewer_slot_is_occupied(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token")
    monkeypatch.setattr(agent_sdk_mod, "_SUBSCRIPTION_OPUS_SEMAPHORE", threading.BoundedSemaphore(1))
    monkeypatch.setattr(agent_sdk_mod, "_SUBSCRIPTION_IMPLEMENTOR_SEMAPHORE", threading.BoundedSemaphore(2))

    opus_slot = _engine(tmp_path, model="opus-4.8")._acquire_subscription_slot()
    fable_slot = _engine(tmp_path, model="fable-5")._acquire_subscription_slot()
    try:
        assert opus_slot is not None
        assert fable_slot is not None
    finally:
        fable_slot.release()
        opus_slot.release()


def test_fable_build_options_use_subscription_model_and_isolate_shadow_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "vendor-key")
    monkeypatch.setenv("AGENT_SDK_UNLISTED", "shadow-key")

    options = _engine(tmp_path, model="fable-5", agent_id="fable-author")._build_options()

    assert options.model == "claude-fable-5"
    assert options.resume is None
    assert options.env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-token"
    assert options.env["ANTHROPIC_API_KEY"] == ""
    assert options.env["AGENT_SDK_UNLISTED"] == ""


def test_subscription_renamed_reviewer_uses_single_slot_concurrency(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token")
    monkeypatch.setattr(agent_sdk_mod, "_SUBSCRIPTION_OPUS_SEMAPHORE", threading.BoundedSemaphore(1))
    monkeypatch.setattr(agent_sdk_mod, "_SUBSCRIPTION_IMPLEMENTOR_SEMAPHORE", threading.BoundedSemaphore(2))
    MODELS["renamed-opus-reviewer"] = replace(
        resolve("opus-4.8"),
        name="renamed-opus-reviewer",
        slug="opus-new",
    )
    try:
        first = _engine(tmp_path, model="renamed-opus-reviewer")
        second = _engine(tmp_path, model="renamed-opus-reviewer")
        slot = first._acquire_subscription_slot()
        try:
            with pytest.raises(EngineError, match="concurrency"):
                second._acquire_subscription_slot()
        finally:
            slot.release()
    finally:
        MODELS.pop("renamed-opus-reviewer", None)


def test_subscription_implementor_concurrency_allows_two_turns(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token")
    monkeypatch.setattr(agent_sdk_mod, "_SUBSCRIPTION_IMPLEMENTOR_SEMAPHORE", threading.BoundedSemaphore(2))

    first = _engine(tmp_path, model="sonnet-5")
    second = _engine(tmp_path, model="haiku-4.5")
    third = _engine(tmp_path, model="sonnet-5")
    slots = [first._acquire_subscription_slot(), second._acquire_subscription_slot()]
    try:
        with pytest.raises(EngineError, match="concurrency"):
            third._acquire_subscription_slot()
    finally:
        for slot in slots:
            slot.release()


def test_subscription_seat_enabled_kill_switch_refuses_turn_before_client_start(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token")
    monkeypatch.setenv("SEAT_ENABLED", "0")

    result = _engine(tmp_path).run_turn_with_progress("task", timeout=1, policy="trusted", on_event=None)

    assert not result.ok
    assert "disabled" in (result.error or "")


def test_subscription_seat_enabled_kill_switch_is_read_at_turn_start(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token")
    monkeypatch.setenv("SEAT_ENABLED", "1")
    engine = _engine(tmp_path)
    monkeypatch.setenv("SEAT_ENABLED", "0")

    result = engine.run_turn_with_progress("task", timeout=1, policy="trusted", on_event=None)

    assert not result.ok
    assert "disabled" in (result.error or "")


def test_subscription_audit_event_is_emitted_on_opus_turn_path(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token")
    events = []
    client = FakeClient([_result()])

    engine = _engine(
        tmp_path,
        model="opus-4.8",
        client_factory=lambda **factory_kwargs: client,
    )
    engine.start()
    try:
        engine.set_turn_audit_context(
            orchestrator_identity="claude-code-reviewer",
            orchestrator_model="claude-opus-4-8",
        )
        result = engine.run_turn_with_progress(
            "review",
            timeout=1,
            policy="trusted",
            on_event=lambda event, data: events.append((event, data)),
        )
    finally:
        engine.stop()

    assert result.ok
    audit_events = [data for event, data in events if event == "agent_sdk_subscription_audit"]
    assert audit_events
    assert audit_events[0]["bridge_opus_inside_claude_code_opus"] is True
    assert audit_events[0]["orchestrator_identity"] == "claude-code-reviewer"


def test_subscription_audit_event_does_not_flag_codex_orchestrator(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token")
    events = []
    client = FakeClient([_result()])

    engine = _engine(
        tmp_path,
        model="opus-4.8",
        client_factory=lambda **factory_kwargs: client,
    )
    engine.start()
    try:
        engine.set_turn_audit_context(orchestrator_identity="codex-project-c-dev", orchestrator_model="gpt-5")
        result = engine.run_turn_with_progress(
            "review",
            timeout=1,
            policy="trusted",
            on_event=lambda event, data: events.append((event, data)),
        )
    finally:
        engine.stop()

    assert result.ok
    audit_events = [data for event, data in events if event == "agent_sdk_subscription_audit"]
    assert audit_events
    assert audit_events[0]["bridge_opus_inside_claude_code_opus"] is False


def test_subscription_audit_event_does_not_flag_claude_sonnet_or_missing_model(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token")
    events = []
    client = FakeClient([_result()])

    engine = _engine(
        tmp_path,
        model="opus-4.8",
        client_factory=lambda **factory_kwargs: client,
    )
    engine.start()
    try:
        engine.set_turn_audit_context(orchestrator_identity="claude-sonnet-reviewer", orchestrator_model=None)
        result = engine.run_turn_with_progress(
            "review",
            timeout=1,
            policy="trusted",
            on_event=lambda event, data: events.append((event, data)),
        )
    finally:
        engine.stop()

    assert result.ok
    audit_events = [data for event, data in events if event == "agent_sdk_subscription_audit"]
    assert audit_events
    assert audit_events[0]["bridge_opus_inside_claude_code_opus"] is False


def test_subscription_audit_event_logs_without_on_event(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token")
    client = FakeClient([_result()])
    caplog.set_level(logging.INFO, logger="agent_redis_bridge.engines.agent_sdk")

    engine = _engine(
        tmp_path,
        model="opus-4.8",
        client_factory=lambda **factory_kwargs: client,
    )
    engine.start()
    try:
        engine.set_turn_audit_context(
            orchestrator_identity="claude-code-reviewer",
            orchestrator_model="claude-opus-4-8",
        )
        result = engine.run_turn_with_progress("review", timeout=1, policy="trusted", on_event=None)
    finally:
        engine.stop()

    assert result.ok
    assert "agent_sdk_subscription_audit" in caplog.text
    assert "bridge_opus_inside_claude_code_opus" in caplog.text


def test_subscription_certifier_audit_flags_bridge_opus_inside_claude_code_opus():
    event = agent_sdk_mod.subscription_certifier_audit_event(
        orchestrator_identity="claude-code",
        orchestrator_model="claude-opus-4-8",
        seat_model="opus-4.8",
    )

    assert event["bridge_opus_inside_claude_code_opus"] is True


def test_subscription_certifier_audit_allows_decorrelated_orchestrator():
    event = agent_sdk_mod.subscription_certifier_audit_event(
        orchestrator_identity="codex",
        orchestrator_model="gpt-5",
        seat_model="opus-4.8",
    )

    assert event["bridge_opus_inside_claude_code_opus"] is False


def test_subscription_build_options_neutralizes_bus_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token")
    monkeypatch.setenv("AGENT_REDIS_PASSWORD", "hunter2")
    monkeypatch.setenv("ARB_MEMORY_REDIS_URL", "rediss://:secret@bus:6379/9")

    options = _engine(tmp_path)._build_options()

    assert options.env["AGENT_REDIS_PASSWORD"] == ""
    assert options.env["ARB_MEMORY_REDIS_URL"] == ""
    assert options.env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-token"


def test_subscription_build_options_fails_if_bus_credential_would_survive(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token")
    monkeypatch.setenv("AGENT_REDIS_PASSWORD", "hunter2")

    def unsafe_env(*, base, config_dir=None):
        return {"CLAUDE_CODE_OAUTH_TOKEN": base["CLAUDE_CODE_OAUTH_TOKEN"]}

    monkeypatch.setattr(agent_sdk_mod, "subscription_env", unsafe_env)

    with pytest.raises(
        EngineError,
        match=r"bus/gate-daemon credentials not neutralized: AGENT_REDIS_PASSWORD",
    ):
        _engine(tmp_path)._build_options()
