"""Shared degenerate-tool-allowlist guard (backlog: pi_sdk fail-open).

The bug this pins: the original guard was
``if pi_tools and len(pi_tools.strip()) > 0 and not parsed`` — for ``"   "`` the
value is truthy but ``.strip()`` is falsy, so the guard was SKIPPED and the engine
spawned with the FULL toolset. It failed OPEN on exactly the typo it existed to
catch. pi-rpc had no guard at all and additionally mis-read ``"   "`` as a
restricted surface in its policy check.

Every case below is asserted against ALL THREE engines that share the surface, so
the three call sites cannot drift apart again.
"""

import pytest

from agent_redis_bridge.engines.base import EngineError, parse_tool_allowlist
from agent_redis_bridge.engines.omp_acp import OmpAcpEngine
from agent_redis_bridge.engines.pi_rpc import PiRpcEngine
from agent_redis_bridge.engines.pi_sdk import PiSdkEngine

DEGENERATE = [",", "   ", "\t", " , , ", "\n", ",,,"]
UNSET = [None, ""]


def _build(engine_cls, pi_tools):
    return engine_cls(cwd="/tmp/project", model=None, pi_tools=pi_tools)


ENGINES = [PiSdkEngine, PiRpcEngine, OmpAcpEngine]


@pytest.mark.parametrize("engine_cls", ENGINES)
@pytest.mark.parametrize("value", DEGENERATE)
def test_degenerate_allowlist_refuses_to_construct(engine_cls, value):
    with pytest.raises(EngineError) as ctx:
        _build(engine_cls, value)
    assert "empty tool list" in str(ctx.value)


@pytest.mark.parametrize("engine_cls", ENGINES)
@pytest.mark.parametrize("value", UNSET)
def test_unset_allowlist_is_allowed(engine_cls, value):
    # Unset legitimately means "use the engine's default toolset"; only a
    # SUPPLIED-but-degenerate value is an operator typo.
    engine = _build(engine_cls, value)
    assert engine._tools_list == ()


@pytest.mark.parametrize("engine_cls", ENGINES)
def test_real_allowlist_parses_and_normalizes(engine_cls):
    engine = _build(engine_cls, " read , grep ,glob ")
    assert engine._tools_list == ("read", "grep", "glob")


class TestSharedHelperDirectly:
    def test_whitespace_only_is_the_regression(self):
        # The exact value the replaced condition let through.
        with pytest.raises(EngineError):
            parse_tool_allowlist("   ", fallback_hint="the full toolset")

    def test_old_condition_would_have_passed_it(self):
        # Demonstrates the bug is real, not hypothetical: the superseded
        # expression evaluates False for "   ", i.e. it skipped the raise.
        value = "   "
        parsed = tuple(t.strip() for t in value.split(",") if t.strip())
        old_condition_fires = bool(value and len(value.strip()) > 0 and not parsed)
        new_condition_fires = bool(value not in (None, "") and not parsed)
        assert old_condition_fires is False
        assert new_condition_fires is True

    def test_fallback_hint_reaches_the_operator(self):
        with pytest.raises(EngineError) as ctx:
            parse_tool_allowlist(",", fallback_hint="omp's full 29-tool surface")
        assert "29-tool" in str(ctx.value)


def test_pi_rpc_policy_guard_uses_the_parsed_list():
    """A degenerate surface must not read as 'restricted' to the policy guard.

    Before the fix `if not self.pi_tools` was False for "   ", so a non-trusted
    turn was accepted by a seat pi would run with FULL tools. Construction now
    refuses first, which is the stronger closure.
    """
    with pytest.raises(EngineError):
        _build(PiRpcEngine, "   ")
    restricted = _build(PiRpcEngine, "read,grep")
    assert restricted._tools_list == ("read", "grep")
    # and the spawn args carry the normalized list, not the raw string
    assert "read,grep" in restricted.command_args()
