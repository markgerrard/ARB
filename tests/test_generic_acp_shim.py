"""`gemini_acp.GeminiAcpEngine` must stay a pure alias of `GenericAcpEngine`.

The generic ACP client moved to `generic_acp.py` on 2026-08-29 because it is the
live base of omp/opencode/kimi-code/mini-agent/dsh and was carrying a RETIRED
adapter's name. `gemini_acp.py` stayed behind as a shim so nothing importing the
old path breaks.

A shim earns its keep only while it stays thin. The risk this file exists for is
the obvious one: someone fixes a transport bug in `gemini_acp.py` because that is
the name they remember, and the five live seats never see the fix. So the shim
is allowed to carry ONLY identity (`engine_label`, `display_name`,
`default_command`) and the gemini command line — anything else it defines is a
divergence, and this test names it.
"""

from __future__ import annotations

import inspect

import pytest

from agent_redis_bridge.engines._acp_base import AcpEngineBase
from agent_redis_bridge.engines.gemini_acp import GeminiAcpEngine
from agent_redis_bridge.engines.generic_acp import GenericAcpEngine

# The shim's whole legitimate surface.
_ALLOWED_SHIM_MEMBERS = {
    "engine_label",
    "display_name",
    "default_command",
    "command_args",
    "__doc__",
    "__module__",
    "__qualname__",
    "__dict__",
    "__weakref__",
    "__firstlineno__",
    "__static_attributes__",
    "__annotations__",
}


def test_shim_subclasses_the_generic_engine() -> None:
    assert issubclass(GeminiAcpEngine, GenericAcpEngine)
    assert GeminiAcpEngine is not GenericAcpEngine


def test_shim_mro_tail_is_the_generic_engine_then_the_shared_base() -> None:
    """The tail is the load-bearing part: the shim adds exactly one link."""
    assert GeminiAcpEngine.__mro__ == (GeminiAcpEngine, GenericAcpEngine, AcpEngineBase, object)


def test_shim_defines_nothing_beyond_identity_and_its_command_line() -> None:
    extra = sorted(set(vars(GeminiAcpEngine)) - _ALLOWED_SHIM_MEMBERS)
    assert not extra, (
        f"GeminiAcpEngine defines {extra}. The shim may carry only its identity "
        "and command line — put anything else on GenericAcpEngine, or the five "
        "live seats (omp/opencode/kimi-code/mini-agent/dsh) will not get it."
    )


def test_shim_overrides_no_transport_or_turn_loop_method() -> None:
    """Named explicitly rather than inferred, so the check reads as a contract."""
    shared = [
        "start", "stop", "steer", "interrupt", "request", "send_request_no_wait",
        "notify", "_send", "_next_request_id", "_read_stdout", "_get_message",
        "_next_progress_seq", "_with_progress_schema", "_handle_client_message",
        "_respond_to_client_request", "run_turn_with_progress", "start_session",
        "reset_context", "set_session_mode_for_policy", "_await_or_detect_death",
        "_dead_child_error", "_normalize_session_update", "_start_handshake",
    ]
    inherited = {}
    for name in shared:
        # Present at all (guard the guard: a typo'd name would pass vacuously).
        assert hasattr(GeminiAcpEngine, name), f"{name} is not on the shim's MRO at all"
        assert name not in vars(GeminiAcpEngine), f"the shim overrides {name}"
        inherited[name] = getattr(GeminiAcpEngine, name)
    # Every one of them is the SAME function object the generic engine exposes.
    for name, member in inherited.items():
        assert member is getattr(GenericAcpEngine, name), (
            f"{name} resolves to a different object on the shim than on "
            "GenericAcpEngine"
        )


def test_shim_keeps_gemini_identity_and_command_line() -> None:
    engine = GeminiAcpEngine(cwd="/tmp/g", model=None, popen_factory=lambda *a, **k: None)
    assert engine.command_args() == ["gemini", "--acp"]
    assert engine.engine_label == "gemini"
    assert engine.display_name == "Gemini"


def test_shim_module_still_re_exports_the_old_names() -> None:
    from agent_redis_bridge.engines import gemini_acp

    assert gemini_acp.GeminiAcpEngine is GeminiAcpEngine
    assert gemini_acp.normalize_session_update is GenericAcpEngine._normalize_session_update.__globals__[
        "normalize_session_update"
    ]
    # The deprecation notice is what tells an operator not to stand the seat up.
    assert "DEPRECATED (2026-07-03)" in inspect.getdoc(gemini_acp)


@pytest.mark.parametrize(
    "module_name,class_name,label",
    [
        ("omp_acp", "OmpAcpEngine", "omp"),
        ("opencode_acp", "OpencodeAcpEngine", "opencode"),
        ("kimi_code_acp", "KimiCodeAcpEngine", "kimi-code"),
        ("mini_agent_acp", "MiniAgentAcpEngine", "mini-agent"),
        ("dsh_acp", "DshAcpEngine", "dsh"),
    ],
)
def test_live_family_sits_on_the_generic_engine_under_its_own_name(
    module_name: str, class_name: str, label: str
) -> None:
    """No live seat may inherit the deprecated adapter's identity.

    Before the rename every one of these announced itself as "Gemini" in its
    error text and drained its child's stderr under a `[gemini-stderr]` prefix,
    because they all inherited GeminiAcpEngine's class attributes.
    """
    import importlib

    cls = getattr(importlib.import_module(f"agent_redis_bridge.engines.{module_name}"), class_name)
    assert issubclass(cls, GenericAcpEngine)
    assert not issubclass(cls, GeminiAcpEngine), (
        f"{class_name} still descends from the deprecated gemini shim"
    )
    assert cls.engine_label == label
    assert cls.display_name == label


# --------------------------------------------------------------------------
# Adapter identity, asserted where it is RENDERED — not on the attribute
#
# Codex P2-2: the tests above (and test_stdio_child_env, and the per-adapter
# refusal tests) check `cls.engine_label == "omp"` or the substring
# `stopReason=refusal`. None of them observe the two places the value actually
# reaches an operator: `_acp_base.start_stderr_drain(process, self.engine_label)`
# — which every existing test mocks and then discards the args of — and
# `generic_acp`'s completion/error text built from `self.display_name`. A
# regression that hardcoded "Gemini" back into either surface stayed green.
#
# So: drive the real surfaces, and assert the exact rendered string carries each
# adapter's own name. The deprecated gemini shim is parameterised in alongside
# the five live adapters deliberately — it is the control that makes a
# hardcoded "Gemini" discriminating rather than vacuous.
# --------------------------------------------------------------------------

import contextlib
import os
import tempfile
from unittest import mock

from agent_redis_bridge.engines.base import EngineError

from test_dsh_acp import DshLayout
from test_gemini_acp import FakeProcess
from test_omp_acp import ok_preflight

# (adapter id, expected engine_label, expected display_name)
ADAPTER_IDENTITY = [
    ("omp", "omp", "omp"),
    ("opencode", "opencode", "opencode"),
    ("kimi-code", "kimi-code", "kimi-code"),
    ("mini-agent", "mini-agent", "mini-agent"),
    ("dsh", "dsh", "dsh"),
    ("gemini-shim", "gemini", "Gemini"),
]


@contextlib.contextmanager
def _adapter(adapter: str, fake: FakeProcess):
    """Construct one live generic-family adapter, ready to drive."""
    factory = lambda *a, **k: fake  # noqa: E731
    if adapter == "omp":
        from agent_redis_bridge.engines.omp_acp import OmpAcpEngine

        yield OmpAcpEngine(
            cwd="/tmp/p", model=None, popen_factory=factory, preflight_runner=ok_preflight()
        )
    elif adapter == "opencode":
        from agent_redis_bridge.engines.opencode_acp import OpencodeAcpEngine

        yield OpencodeAcpEngine(cwd="/tmp/p", model=None, popen_factory=factory)
    elif adapter == "kimi-code":
        from agent_redis_bridge.engines.kimi_code_acp import KimiCodeAcpEngine

        yield KimiCodeAcpEngine(cwd="/tmp/p", model=None, popen_factory=factory)
    elif adapter == "mini-agent":
        from agent_redis_bridge.engines.mini_agent_acp import MiniAgentAcpEngine

        yield MiniAgentAcpEngine(cwd="/tmp/p", model=None, popen_factory=factory)
    elif adapter == "dsh":
        from agent_redis_bridge.engines.dsh_acp import DshAcpEngine

        # dsh's construction guards need a real on-disk layout and a session cwd
        # equal to the process cwd; reuse the fixture that owns those rules.
        with tempfile.TemporaryDirectory() as tmp:
            layout = DshLayout(tmp)
            with mock.patch.dict(os.environ, layout.env(), clear=True):
                yield DshAcpEngine(cwd=os.getcwd(), model=None, popen_factory=factory)
    elif adapter == "gemini-shim":
        yield GeminiAcpEngine(cwd="/tmp/p", model=None, popen_factory=factory)
    else:  # pragma: no cover - guarded by the parametrize list
        raise AssertionError(f"unknown adapter {adapter!r}")


@pytest.mark.parametrize("adapter,label,display", ADAPTER_IDENTITY)
def test_stderr_drain_is_labelled_with_the_adapters_own_name(
    adapter: str, label: str, display: str
) -> None:
    """`[<label>-stderr]` is how an operator attributes a child's output.

    Captured from the real call site (`_acp_base.start()`), not asserted on the
    class attribute — the existing mocks all throw the argument away.
    """
    fake = FakeProcess()
    captured: dict[str, object] = {}

    def capturing_drain(process, drain_label, **kwargs):
        captured["process"] = process
        captured["label"] = drain_label
        return None

    with _adapter(adapter, fake) as engine:
        engine._init_timeout = 0  # the handshake must fail fast; the drain runs first
        with mock.patch(
            "agent_redis_bridge.engines._acp_base.start_stderr_drain", capturing_drain
        ):
            with pytest.raises(EngineError):
                engine.start()

    assert captured.get("process") is fake, "start_stderr_drain never ran"
    assert captured["label"] == label, (
        f"{adapter} drains its child's stderr under {captured['label']!r}; an "
        f"operator reading `[{captured['label']}-stderr]` in the bridge log would "
        f"attribute it to the wrong engine (expected {label!r})"
    )


def _drive_turn(engine, prompt_result: dict):
    """One turn with the mode call stubbed, so the prompt always takes id 1."""
    fake = FakeProcess()
    engine.process = fake
    engine.session_id = "sess-1"
    engine.set_session_mode_for_policy = lambda policy: None
    engine.messages.put({"jsonrpc": "2.0", "id": 1, "result": prompt_result})
    return engine.run_turn_with_progress("t", timeout=5, policy="trusted", on_event=None)


@pytest.mark.parametrize("adapter,label,display", ADAPTER_IDENTITY)
def test_stop_reason_error_text_names_the_adapter(
    adapter: str, label: str, display: str
) -> None:
    with _adapter(adapter, FakeProcess()) as engine:
        result = _drive_turn(engine, {"stopReason": "refusal"})
    assert result.ok is False
    # The EXACT string, not the `stopReason=refusal` substring the per-adapter
    # refusal tests settle for — that substring survives a hardcoded "Gemini".
    assert result.error == f"{display} ACP stopReason=refusal"


@pytest.mark.parametrize("adapter,label,display", ADAPTER_IDENTITY)
def test_empty_success_result_text_names_the_adapter(
    adapter: str, label: str, display: str
) -> None:
    with _adapter(adapter, FakeProcess()) as engine:
        result = _drive_turn(engine, {"stopReason": "end_turn"})
    assert result.ok is True
    assert result.result == f"{display} ACP prompt 1 completed."


@pytest.mark.parametrize("adapter,label,display", ADAPTER_IDENTITY)
def test_dead_process_and_steer_errors_name_the_adapter(
    adapter: str, label: str, display: str
) -> None:
    with _adapter(adapter, FakeProcess()) as engine:
        engine.process = None
        with pytest.raises(EngineError) as send_err:
            engine._send({"jsonrpc": "2.0", "method": "ping", "params": {}})
        with pytest.raises(EngineError) as steer_err:
            engine.steer("hello")
    assert str(send_err.value) == f"{display} ACP process is not running"
    assert str(steer_err.value) == f"{display} ACP does not support mid-prompt steer"
