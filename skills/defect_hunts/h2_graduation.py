"""H2 shadow-mode graduation query.

Graduation is computed over disposed candidates from complete H2 records. The
false-positive denominator is the disposition count, never the derived count.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, is_dataclass


GUARDS = {"min_runs", "min_disposed", "discrimination", "fp_threshold", "complete_only"}


def fp_rate(records: Iterable[object]) -> float | None:
    complete_records = [record for record in records if _field(record, "complete") is True]
    counts = _disposition_counts(complete_records)
    disposed = _disposed(counts)
    if disposed == 0:
        return None
    return counts["not_load_bearing"] / disposed


def is_graduation_ready(
    records: Iterable[object],
    *,
    _disabled_guards: frozenset[str] = frozenset(),
) -> bool:
    disabled_guards = set(_disabled_guards)
    unknown_guards = disabled_guards - GUARDS
    if unknown_guards:
        raise ValueError(f"unknown H2 graduation guard(s): {', '.join(sorted(unknown_guards))}")

    records_list = list(records)
    window = records_list if "complete_only" in disabled_guards else _complete_records(records_list)
    counts = _disposition_counts(window)
    disposed = _disposed(counts)

    guard_results = {
        "min_runs": len(window) >= 10,
        "min_disposed": disposed >= 20,
        "discrimination": counts["not_load_bearing"] >= 1 and (counts["answered"] + counts["flag"]) >= 1,
        "fp_threshold": disposed > 0 and (counts["not_load_bearing"] / disposed) < 0.10,
    }

    return all(result for guard, result in guard_results.items() if guard not in disabled_guards)


def _complete_records(records: list[object]) -> list[object]:
    return [record for record in records if _field(record, "complete") is True]


def _disposition_counts(records: Iterable[object]) -> dict[str, int]:
    counts = {"answered": 0, "not_load_bearing": 0, "flag": 0}
    for record in records:
        dispositions = _field(record, "dispositions")
        if not isinstance(dispositions, list):
            continue
        for row in dispositions:
            if not isinstance(row, Mapping):
                continue
            disposition = row.get("disposition")
            if disposition in counts:
                counts[disposition] += 1
    return counts


def _disposed(counts: Mapping[str, int]) -> int:
    return counts["answered"] + counts["not_load_bearing"] + counts["flag"]


def _field(record: object, name: str) -> object:
    if isinstance(record, Mapping):
        return record.get(name)
    if is_dataclass(record):
        return asdict(record).get(name)
    return getattr(record, name, None)


__all__ = ["GUARDS", "fp_rate", "is_graduation_ready"]
