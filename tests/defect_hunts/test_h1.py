from __future__ import annotations

from collections.abc import Callable

from skills.defect_hunts.h1_config_drift import hunt
from skills.defect_hunts.types import Finding, Scenario, Verdict


def test_flags_literal_sibling_matching_env_default():
    scenario = _scenario(
        files={
            "pkg/bus.py": """
import os

PREFIX = os.environ.get("ARB_MEMORY_PREFIX", "")
""",
            "pkg/audit.py": """
PREFIX = ""
""",
        },
        diff="""
diff --git a/pkg/bus.py b/pkg/bus.py
@@
-PREFIX = ""
+PREFIX = os.environ.get("ARB_MEMORY_PREFIX", "")
""",
    )

    verdict = hunt(scenario)

    assert _decisions(verdict) == ["FLAG"]
    assert "pkg/audit.py:PREFIX" in verdict.findings[0].subject
    assert 'default ""' in verdict.findings[0].evidence


def test_clears_when_siblings_co_move_on_same_env_name():
    scenario = _scenario(
        files={
            "pkg/bus.py": """
import os

PREFIX = os.environ.get("ARB_MEMORY_PREFIX", "")
""",
            "pkg/audit.py": """
import os

PREFIX = os.environ.get("ARB_MEMORY_PREFIX", "")
""",
        },
        diff="""
diff --git a/pkg/bus.py b/pkg/bus.py
@@
-PREFIX = ""
+PREFIX = os.environ.get("ARB_MEMORY_PREFIX", "")
""",
    )

    verdict = hunt(scenario)

    assert _decisions(verdict) == ["CLEAR"]
    assert "co-move" in verdict.findings[0].evidence


def test_clears_when_literal_sibling_diverges_from_env_default():
    scenario = _scenario(
        files={
            "pkg/bus.py": """
import os

MAXLEN = os.environ.get("ARB_MEMORY_MAXLEN", 10_000)
""",
            "pkg/audit.py": """
MAXLEN = 1_000_000
""",
        },
        diff="""
diff --git a/pkg/bus.py b/pkg/bus.py
@@
-MAXLEN = 10_000
+MAXLEN = os.environ.get("ARB_MEMORY_MAXLEN", 10_000)
""",
    )

    verdict = hunt(scenario)

    assert _decisions(verdict) == ["CLEAR"]
    assert "diverges" in verdict.findings[0].evidence


def test_indirected_only_reader_emits_non_silent_known_limitation_flag():
    scenario = _scenario(
        files={
            "pkg/bus.py": """
from pkg.config import env

PREFIX = env("ARB_MEMORY_PREFIX", "")
""",
            "pkg/audit.py": """
PREFIX = ""
""",
        },
        diff="""
diff --git a/pkg/bus.py b/pkg/bus.py
@@
-PREFIX = ""
+PREFIX = env("ARB_MEMORY_PREFIX", "")
""",
    )

    verdict = hunt(scenario)

    assert _decisions(verdict) == ["FLAG"]
    assert verdict.findings[0].evidence.startswith("could-not-analyze:")


def test_inject_revert_always_flag_and_always_clear_fail_contract_cases():
    positive = _scenario(
        files={
            "pkg/bus.py": 'import os\nPREFIX = os.environ.get("ARB_MEMORY_PREFIX", "")\n',
            "pkg/audit.py": 'PREFIX = ""\n',
        },
        diff='diff --git a/pkg/bus.py b/pkg/bus.py\n+PREFIX = os.environ.get("ARB_MEMORY_PREFIX", "")\n',
    )
    divergent_negative = _scenario(
        files={
            "pkg/bus.py": 'import os\nMAXLEN = os.environ.get("ARB_MEMORY_MAXLEN", 10_000)\n',
            "pkg/audit.py": "MAXLEN = 1_000_000\n",
        },
        diff='diff --git a/pkg/bus.py b/pkg/bus.py\n+MAXLEN = os.environ.get("ARB_MEMORY_MAXLEN", 10_000)\n',
    )

    assert _contract_passes(hunt, positive, divergent_negative)
    assert not _contract_passes(_always_flag, positive, divergent_negative)
    assert not _contract_passes(_always_clear, positive, divergent_negative)


def _scenario(*, files: dict[str, str], diff: str) -> Scenario:
    return Scenario(scenario_id="h1-case", files=files, diff=diff)


def _decisions(verdict: Verdict) -> list[str]:
    return [finding.decision for finding in verdict.findings]


def _contract_passes(
    candidate: Callable[[Scenario], Verdict],
    positive: Scenario,
    negative: Scenario,
) -> bool:
    return _decisions(candidate(positive)) == ["FLAG"] and _decisions(candidate(negative)) == [
        "CLEAR"
    ]


def _always_flag(scenario: Scenario) -> Verdict:
    return Verdict(
        [
            Finding(
                subject=scenario.scenario_id,
                kind="H1",
                decision="FLAG",
                evidence="mutant catch-all",
            )
        ]
    )


def _always_clear(scenario: Scenario) -> Verdict:
    return Verdict(
        [
            Finding(
                subject=scenario.scenario_id,
                kind="H1",
                decision="CLEAR",
                evidence="mutant no-op",
            )
        ]
    )
