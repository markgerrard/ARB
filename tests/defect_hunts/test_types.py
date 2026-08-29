from __future__ import annotations

import pytest

from skills.defect_hunts.types import Finding, Scenario, Verdict


def test_verdict_round_trips_findings():
    scenario = Scenario(
        scenario_id="scenario-1",
        files={"pkg/mod.py": "VALUE = ''\n"},
        diff="diff --git a/pkg/mod.py b/pkg/mod.py\n",
    )
    findings = [
        Finding(
            subject=scenario.scenario_id,
            kind="H1",
            decision="FLAG",
            evidence="hardcoded sibling remains on the default value",
        ),
        Finding(
            subject="pkg/other.py",
            kind="H2",
            decision="CLEAR",
            evidence="assumption has a spot-checkable violating run",
        ),
    ]

    verdict = Verdict(findings=findings)

    assert verdict.findings == findings
    assert verdict.findings[0].subject == "scenario-1"
    assert verdict.findings[0].kind == "H1"
    assert verdict.findings[0].decision == "FLAG"
    assert verdict.findings[0].evidence == "hardcoded sibling remains on the default value"
    assert verdict.findings[1].kind == "H2"
    assert verdict.findings[1].decision == "CLEAR"


def test_finding_rejects_missing_required_field():
    with pytest.raises(TypeError):
        Finding(subject="pkg/mod.py", kind="H1", decision="FLAG")


def test_finding_rejects_bogus_decision():
    with pytest.raises(ValueError):
        Finding(
            subject="pkg/mod.py",
            kind="H1",
            decision="BOGUS",
            evidence="not a valid wire decision",
        )


@pytest.mark.parametrize("field", ["subject", "kind", "decision", "evidence"])
def test_finding_rejects_empty_required_field(field):
    values = {
        "subject": "pkg/mod.py",
        "kind": "H1",
        "decision": "FLAG",
        "evidence": "direct evidence",
    }
    values[field] = ""

    with pytest.raises(ValueError):
        Finding(**values)
