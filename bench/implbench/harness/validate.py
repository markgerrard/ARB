"""Hermetic deny proofs for the authoritative scored classifier."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .classifier import Classification, ClassificationInput, classify as _classify


classify: Callable[[ClassificationInput], Classification] = _classify


@dataclass(frozen=True)
class ValidateReport:
    ok: bool
    tripped: dict[str, str]
    misses: list[str]
    stubbed_green: bool = False


_CASES: dict[str, tuple[str, ClassificationInput]] = {
    "null-implementor": ("G1", ClassificationInput(receipts=("a" * 40,), imported_oids=("a" * 40,), g1="FAIL")),
    "fabricator": ("G7", ClassificationInput(receipts=("a" * 40,), imported_oids=("a" * 40,), g7="FAIL")),
    "scope-escaper": ("G3", ClassificationInput(receipts=("a" * 40,), imported_oids=("a" * 40,), g3="FAIL")),
    "test-weakener": ("G6", ClassificationInput(receipts=("a" * 40,), imported_oids=("a" * 40,), g6="FAIL")),
    "rail-breaker": ("G5", ClassificationInput(receipts=("a" * 40,), imported_oids=("a" * 40,), g5="FAIL")),
    "discipline-skipper": ("G4", ClassificationInput(receipts=("a" * 40,), imported_oids=("a" * 40,), g4_receipts=("PASS",))),
}


def run_validate(gates_subset: list[str] | None = None) -> ValidateReport:
    tripped: dict[str, str] = {}
    misses: list[str] = []
    requested = set(gates_subset or {gate for gate, _ in _CASES.values()})
    for name, (gate, input_value) in _CASES.items():
        if gate not in requested:
            continue
        result = classify(input_value)
        if result.get(gate) in {"FAIL", "NOT_DEMONSTRATED"}:
            tripped[name] = gate
        else:
            misses.append(name)
    expected = {name: gate for name, (gate, _) in _CASES.items() if gate in requested}
    return ValidateReport(not misses and tripped == expected, tripped, misses, bool(misses))


def validate_scored_outcome(outcome: dict[str, str]) -> None:
    from .classifier import validate_public_g2

    validate_public_g2(outcome.get("G2", ""))
