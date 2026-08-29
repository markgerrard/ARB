"""Every engine the daemon can run must also be dispatchable.

`cline-acp` shipped in bridge.py on 2026-08-01 — ENGINE_TO_TOOL, the engine
registry, and a CLI branch — but never reached `scripts/agent-dispatch`'s engine
case. The seat ran, heartbeated, and answered preflight as a live target; the
dispatch died with `unknown engine: cline-acp`. Nothing was broken enough to be
noticed until someone tried to use that seat, which was a four-seat certification
panel four days after the seat went live.

Two lists that must agree, edited in different files by different changes, with
no check between them: this is the drift, not the typo.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
DISPATCH = ROOT / "scripts" / "agent-dispatch"
BRIDGE = ROOT / "src" / "agent_redis_bridge" / "bridge.py"

# Engines the dispatcher deliberately refuses. Deprecation is a REASON, not an
# exemption: the entry must still exist so the caller gets the explanation
# rather than "unknown engine".
DELIBERATELY_REFUSED = {"gemini-acp"}


def _engine_to_tool() -> dict[str, str]:
    """Parse ENGINE_TO_TOOL out of bridge.py without importing it."""
    text = BRIDGE.read_text()
    start = text.index("ENGINE_TO_TOOL")
    body = text[start : text.index("}", start)]
    return dict(re.findall(r'"([\w.-]+)"\s*:\s*"([\w.-]+)"', body))


def _dispatch_cases() -> dict[str, str]:
    """Engine -> TOOL from agent-dispatch's `case "$ENGINE" in` block."""
    text = DISPATCH.read_text()
    start = text.index('case "$ENGINE" in')
    block = text[start : text.index("esac", start)]
    cases: dict[str, str] = {}
    # `a) TOOL=x ;;` and `a|b) TOOL=x ;;` both occur; shell case patterns may be
    # alternations, so split on | rather than assuming one engine per branch.
    for patterns, tool in re.findall(r"^\s*([\w.|-]+)\)\s*TOOL=([\w.-]+)\s*;;", block, re.M):
        for engine in patterns.split("|"):
            cases[engine] = tool
    for patterns in re.findall(r"^\s*([\w.|-]+)\)\s*echo", block, re.M):
        for engine in patterns.split("|"):
            cases.setdefault(engine, "<refused>")
    return cases


def test_the_parsers_find_both_lists():
    """Guard the guard: a regex that silently matches nothing always passes.

    Honest limit — this one test has NO mutation in the sidecar, and cannot.
    Every mutation that would exercise it works by emptying a parsed list, and
    both lists drive `@pytest.mark.parametrize`, so emptying either changes how
    many tests are COLLECTED. The mutation sweep refuses a mutant that alters
    collection (`SWEEP-REFUSED — collected 3 tests, baseline collected 35`),
    which is correct: a mutant that changes which tests exist cannot show the
    surviving ones bite. So the two parity assertions below are gate-proven by
    M1/M2, and this guard is not. Stated rather than papered over with a mutant
    aimed somewhere easier.
    """
    daemon, dispatcher = _engine_to_tool(), _dispatch_cases()
    assert len(daemon) >= 8, f"parsed only {len(daemon)} engines from bridge.py — parser is broken"
    assert len(dispatcher) >= 8, f"parsed only {len(dispatcher)} cases from agent-dispatch — parser is broken"
    assert "codex" in daemon and "codex" in dispatcher


@pytest.mark.parametrize("engine", sorted(_engine_to_tool()))
def test_every_daemon_engine_is_dispatchable(engine):
    cases = _dispatch_cases()
    assert engine in cases, (
        f"bridge.py can run engine {engine!r} but scripts/agent-dispatch has no case for it, "
        "so a healthy seat on that engine cannot be dispatched to — it fails at the point of "
        f"use with `unknown engine: {engine}`. Add `{engine}) TOOL=<tool> ;;` to the case block, "
        "or add it to DELIBERATELY_REFUSED here with the reason."
    )


@pytest.mark.parametrize("engine", sorted(_engine_to_tool()))
def test_dispatch_tool_prefix_matches_the_daemon(engine):
    """A disagreeing prefix silently derives the WRONG target id when
    --target-id is omitted, so the dispatch goes to a seat nobody is running."""
    daemon, cases = _engine_to_tool(), _dispatch_cases()
    if engine in DELIBERATELY_REFUSED or cases.get(engine) == "<refused>":
        pytest.skip(f"{engine} is deliberately refused by the dispatcher")
    assert cases[engine] == daemon[engine], (
        f"tool prefix disagrees for {engine!r}: bridge.py says {daemon[engine]!r}, "
        f"agent-dispatch says {cases[engine]!r}. With --target-id omitted the dispatcher "
        f"derives {cases[engine]}-<project>-<workspace>, which is not the seat the daemon registers."
    )
