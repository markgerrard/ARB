from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Status = Literal["pass", "fail", "inconclusive"]
WriteOutcome = Literal["refused", "succeeded", "unavailable"]


@dataclass(frozen=True)
class ToolSurfaceResult:
    status: Status
    offending_tools: list[str]
    reasons: list[str]
    inconclusive: bool = False


@dataclass(frozen=True)
class WriteVectorResult:
    name: str
    outcome: WriteOutcome
    sentinel_present: bool


@dataclass(frozen=True)
class DenyProofVerdict:
    status: Status
    reasons: list[str]


def validate_tool_surface(reported_tools: list[str], allowed: set[str]) -> ToolSurfaceResult:
    if not reported_tools:
        return ToolSurfaceResult(
            status="inconclusive",
            offending_tools=[],
            reasons=["no tools reported"],
            inconclusive=True,
        )

    offending_tools = sorted(set(reported_tools) - allowed)
    if offending_tools:
        joined = ", ".join(offending_tools)
        return ToolSurfaceResult(
            status="fail",
            offending_tools=offending_tools,
            reasons=[f"tool surface contains disallowed tools: {joined}"],
        )

    return ToolSurfaceResult(status="pass", offending_tools=[], reasons=[])


def classify_deny_proof(
    vectors: list[WriteVectorResult],
    surface: ToolSurfaceResult,
    surface_changed: bool,
) -> DenyProofVerdict:
    failure_reasons: list[str] = []

    for vector in vectors:
        if vector.sentinel_present:
            failure_reasons.append(f"{vector.name} created sentinel artifact")
        if vector.outcome == "succeeded":
            failure_reasons.append(f"{vector.name} write vector succeeded")
        elif vector.outcome not in {"refused", "unavailable"}:
            failure_reasons.append(f"{vector.name} reported invalid outcome: {vector.outcome}")

    if surface.status == "fail":
        failure_reasons.extend(surface.reasons)
    if surface_changed:
        failure_reasons.append("tool surface changed during deny-proof")

    if failure_reasons:
        return DenyProofVerdict(status="fail", reasons=failure_reasons)

    # A PASS demands POSITIVE proof, not mere absence of failures. An empty vector list means zero
    # write attempts were made: absence of evidence, not proof of read-only — never certify on it
    # (the D1 / config-assume lesson). Checked AFTER the failure checks above, so a surface that
    # exposes a write tool still FAILs even with no vectors.
    if not vectors:
        return DenyProofVerdict(
            status="inconclusive",
            reasons=["no write vectors evaluated — zero write attempts is absence of evidence, not proof of read-only"],
        )

    if surface.inconclusive or surface.status == "inconclusive":
        return DenyProofVerdict(status="inconclusive", reasons=["tool surface inconclusive"])

    # Reaching here: vectors were exercised, every outcome was refused/unavailable, no sentinel
    # appeared, surface PASSed (reported tools exactly within the read-only allowlist — exclusive),
    # surface unchanged. An all-`unavailable` set is a VALID pass: surface-PASS proves the write
    # tools do not exist (by-construction read-only — the strongest posture), so there is nothing to
    # "refuse". The surface check, not a forced refusal, is the disambiguator.
    return DenyProofVerdict(status="pass", reasons=[])
