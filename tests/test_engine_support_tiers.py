"""ARB-B14b: the support-tier table is asserted against reality, both ways.

A dead adapter must not look live (retired => build_engine refuses), and a new
adapter must not enter unclassified (every registered engine has a tier; every
adapter module is either infra or a registered engine's implementation).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from agent_redis_bridge.bridge import ENGINE_TO_TOOL, EngineError, build_engine
from agent_redis_bridge.engines.support_tiers import (
    RETIRED,
    SUPPORT_TIERS,
    VALID_TIERS,
    tier_for,
)

ENGINES_DIR = Path(__file__).resolve().parents[1] / "src" / "agent_redis_bridge" / "engines"

# Modules that are infrastructure, not engine adapters. A NEW module must land
# here (with a reason in the commit) or as a registered, tiered engine.
_INFRA_MODULES = {
    "__init__",
    "base",          # AgentEngine protocol
    "_acp",          # shared ACP permission layer (_select_allow_option, mixin)
    "_acp_base",     # shared ACP transport + prompt loop the adapters subclass
    "generic_acp",   # the generic ACP client; gemini_acp is its deprecated shim
    "_stdio",        # shared stdio plumbing
    "support_tiers",  # this table
    "agent_sdk_continuation",
    "agent_sdk_loop",
    "agent_sdk_mediation",
    "agent_sdk_models",
    "agent_sdk_session",
    "pi_broker_mcp",  # broker adjunct for scored pi turns, not a routable engine
    "_agy_gate",      # host gate shared by the agy engines, not a routable engine
}

# Pure CLI aliases normalized before ENGINE_TO_TOOL lookup elsewhere; the tier
# table lists canonical names only.
_ALIASES = {"asdk"}


def test_every_registered_engine_has_a_valid_tier():
    canonical = set(ENGINE_TO_TOOL) - _ALIASES
    assert canonical == set(SUPPORT_TIERS), (
        "engine registry and support-tier table disagree; classify the "
        f"difference: {sorted(canonical ^ set(SUPPORT_TIERS))}"
    )
    bad = {e: t for e, t in SUPPORT_TIERS.items() if t not in VALID_TIERS}
    assert not bad, f"invalid tier values: {bad}"


def test_every_adapter_module_is_infra_or_a_registered_engine():
    engine_modules = {name.replace("-", "_") for name in SUPPORT_TIERS}
    unaccounted = []
    for path in ENGINES_DIR.glob("*.py"):
        stem = path.stem
        if stem in _INFRA_MODULES:
            continue
        if stem in engine_modules:
            continue
        unaccounted.append(stem)
    assert not unaccounted, (
        "adapter module(s) neither infra-classified nor a registered+tiered "
        f"engine: {sorted(unaccounted)} — add to ENGINE_TO_TOOL + SUPPORT_TIERS "
        "or to _INFRA_MODULES with a reason"
    )


def test_retired_engines_refuse_to_construct():
    retired = [e for e, t in SUPPORT_TIERS.items() if t == RETIRED]
    assert retired, "no retired engines listed — gemini-acp should be here"
    for engine in retired:
        args = argparse.Namespace(engine=engine, model=None)
        with pytest.raises(EngineError, match="RETIRED"):
            build_engine(args, cwd=".")


def test_live_tiers_do_not_refuse_at_the_tier_guard():
    # The guard must not over-fire: a certifying engine reaching build_engine
    # gets PAST the tier check (later construction may fail for other reasons
    # in a test env; the tier guard itself must not be the refusal).
    assert tier_for("codex") != RETIRED
    assert tier_for("gemini-acp") == RETIRED
    assert tier_for("not-an-engine") is None
