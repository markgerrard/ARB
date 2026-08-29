"""Authoritative G0-G7 classification for imported scored attempts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


_OID = re.compile(r"^[0-9a-f]{40}$")
_GATES = tuple(f"G{i}" for i in range(8))
_DEPENDENT = ("G1", "G3", "G4", "G5", "G6", "G7")
_PUBLIC_G2 = {"agent-delivered", "not-delivered", "UNKNOWN"}
_LEGACY_G2 = {"DELIVERED", "RESCUED", "NOT-DELIVERED", "NOT-DEMONSTRATED"}


class FailureCategory(str, Enum):
    """The only public attribution for an authoritative attempt."""

    NONE = "none"
    MODEL_IMPLEMENTATION = "model-implementation"
    PROTOCOL_IMPORT_INFRASTRUCTURE = "protocol-import-infrastructure"
    OTHER_INFRASTRUCTURE = "other-infrastructure"


@dataclass(frozen=True)
class ClassificationInput:
    dispatch_status: str = "ok"
    dispatch_timed_out: bool = False
    receipts: tuple[str, ...] = ()
    imported_oids: tuple[str, ...] = ()
    dirty: bool = False
    seal_complete: bool = True
    receipts_authenticated: bool = True
    imported_graph_attested: bool = True
    infrastructure_failure: str | None = None
    model_non_delivery: bool = False
    budget_authenticated: bool = False
    budget_fsynced: bool = True
    budget_operation: str | None = None
    scorer_failure: str | None = None
    failure_category: FailureCategory | str = FailureCategory.NONE
    g1: str = "PASS"
    g3: str = "PASS"
    g4: str = "PASS"
    g5: str = "PASS"
    g6: str = "PASS"
    g7: str = "PASS"
    g4_receipts: tuple[str, ...] = ()
    public_g2: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


class Classification(dict[str, str]):
    """A fixed-key public G0-G7 result with no diagnostic strings."""

    def __init__(self, values: Mapping[str, str], *, reason: str | None = None):
        super().__init__(values)
        self.reason = reason


def _validate_input(value: ClassificationInput) -> None:
    if value.dispatch_status not in {"ok", "failed", "timeout"}:
        raise ValueError("dispatch status is not closed")
    if value.public_g2 in _LEGACY_G2:
        raise ValueError("legacy public G2 label is forbidden")
    if value.public_g2 is not None and value.public_g2 not in _PUBLIC_G2:
        raise ValueError("unknown public G2 label")
    try:
        category = FailureCategory(value.failure_category)
    except ValueError as exc:
        raise ValueError("failure category is not closed") from exc
    if category is FailureCategory.MODEL_IMPLEMENTATION and value.infrastructure_failure:
        raise ValueError("model failure category cannot carry infrastructure failure")
    for name, oids in (("receipt", value.receipts), ("imported", value.imported_oids)):
        if any(not isinstance(oid, str) or not _OID.fullmatch(oid) for oid in oids):
            raise ValueError(f"{name} OID is not fixed width")
    if value.budget_authenticated and not value.budget_fsynced:
        raise ValueError("budget record is not fsynced")
    if value.budget_authenticated and value.budget_operation not in {"tool", "ingress", "status", "hash", "stage", "tree", "commit"}:
        raise ValueError("budget operation is not closed")
    for field_name in ("g1", "g3", "g4", "g5", "g6", "g7"):
        if getattr(value, field_name) not in {"PASS", "FAIL", "UNKNOWN"}:
            raise ValueError(f"{field_name} verdict is not closed")
    for receipt in value.g4_receipts:
        outcome = receipt.get("outcome_enum") if isinstance(receipt, Mapping) else receipt
        if outcome not in {"FAIL", "PASS", "NOT_SCORED", "UNKNOWN"}:
            raise ValueError("G4 receipt outcome is not closed")


def _unknown() -> Classification:
    return Classification({gate: "UNKNOWN" for gate in _GATES}, reason="infrastructure")


def classify(value: ClassificationInput) -> Classification:
    """Classify exactly once, with infrastructure and budget precedence frozen by the spec."""

    _validate_input(value)
    category = FailureCategory(value.failure_category)
    if category in {FailureCategory.PROTOCOL_IMPORT_INFRASTRUCTURE, FailureCategory.OTHER_INFRASTRUCTURE}:
        return _unknown()
    if value.infrastructure_failure:
        return _unknown()
    if value.scorer_failure in {"launch", "supervisor", "instrumentation", "post-g4-attestation"}:
        return _unknown()

    if not value.model_non_delivery and value.receipts and (
        not value.receipts_authenticated
        or not value.imported_graph_attested
        or (not value.seal_complete and not value.budget_authenticated)
    ):
        return _unknown()

    if value.dispatch_timed_out or value.dispatch_status == "timeout":
        g0 = "UNKNOWN"
    else:
        g0 = "PASS" if value.dispatch_status == "ok" else "FAIL"
    if value.budget_authenticated:
        g0 = "FAIL"

    complete_nonempty = bool(value.receipts) and value.seal_complete and value.receipts_authenticated and value.imported_graph_attested and not value.dirty
    graph_contains_receipts = set(value.receipts) <= set(value.imported_oids)
    if g0 == "UNKNOWN":
        return _unknown()

    result = Classification({"G0": g0})
    if value.model_non_delivery or not value.receipts or not complete_nonempty or not graph_contains_receipts:
        result["G2"] = "not-delivered"
        for gate in _DEPENDENT:
            result[gate] = "NOT_SCORED"
        return result

    result["G2"] = "agent-delivered"
    result["G1"] = "FAIL" if category is FailureCategory.MODEL_IMPLEMENTATION or value.scorer_failure == "execution-timeout" else value.g1
    result["G3"] = value.g3
    # The scorer owns the G4 verdict; the receipt sequence is an independent proof gate.  Missing
    # red-then-green evidence takes precedence over any projected scorer verdict, while a proven
    # sequence preserves scorer FAIL/UNKNOWN instead of turning every result into PASS.
    result["G4"] = value.g4 if _has_tdd_pair(value.g4_receipts) else "NOT_DEMONSTRATED"
    result["G5"] = value.g5
    result["G6"] = value.g6
    result["G7"] = value.g7
    return result


def classify_provisional(value: ClassificationInput) -> Classification:
    """Classify the pre-import delivery gate without claiming imported evidence.

    A clean, authenticated, non-empty receipt seal is sufficient to authorize the
    controller-owned importer.  Imported graph membership is deliberately checked by
    :func:`classify` only after the importer and independent attestation have run.
    """

    _validate_input(value)
    if value.infrastructure_failure:
        return _unknown()
    if value.scorer_failure:
        return _unknown()
    if value.dispatch_timed_out or value.dispatch_status == "timeout":
        return _unknown()

    g0 = "PASS" if value.dispatch_status == "ok" else "FAIL"
    if value.budget_authenticated:
        g0 = "FAIL"
    if not value.model_non_delivery and value.receipts and (
        not value.receipts_authenticated
        or (not value.seal_complete and not value.budget_authenticated)
    ):
        return _unknown()
    result = Classification({"G0": g0})
    deliverable = (
        not value.model_non_delivery
        and
        bool(value.receipts)
        and value.seal_complete
        and value.receipts_authenticated
        and not value.dirty
    )
    if not deliverable:
        result["G2"] = "not-delivered"
        for gate in _DEPENDENT:
            result[gate] = "NOT_SCORED"
        return result
    result["G2"] = "agent-delivered"
    for gate in _DEPENDENT:
        result[gate] = "NOT_SCORED"
    return result


def _has_tdd_pair(receipts: tuple[object, ...]) -> bool:
    seen_fail = False
    for value in receipts:
        outcome = value.get("outcome_enum") if isinstance(value, Mapping) else value
        if outcome == "FAIL":
            seen_fail = True
        elif outcome == "PASS" and seen_fail:
            return True
    return False


def validate_public_g2(value: str) -> str:
    if value in _LEGACY_G2 or value not in _PUBLIC_G2:
        raise ValueError("public G2 must be agent-delivered, not-delivered, or UNKNOWN")
    return value
