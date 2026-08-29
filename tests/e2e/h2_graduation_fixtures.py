from __future__ import annotations


def green_records() -> list[dict]:
    return _records(10, answered=20, nlb=1)


def n_minus_one_records(guard_id: str) -> list[dict]:
    fixtures = {
        "min_runs": _records(9, answered=20, nlb=1),
        "min_disposed": _records(10, answered=18, nlb=1),
        "discrimination": _records(10, answered=20, nlb=0),
        "fp_threshold": _records(10, answered=18, nlb=2),
        "complete_only": [_record(True, ["answered", "answered"]) for _ in range(9)]
        + [_record(False, ["answered", "not_load_bearing"])],
    }
    return fixtures[guard_id]


def _records(count: int, *, answered: int, nlb: int) -> list[dict]:
    records = [_record(True, []) for _ in range(count)]
    for index, disposition in enumerate(["answered"] * answered + ["not_load_bearing"] * nlb):
        records[index % count]["dispositions"].append({"disposition": disposition})
    return records


def _record(complete: bool, dispositions: list[str]) -> dict:
    return {
        "complete": complete,
        "dispositions": [{"disposition": disposition} for disposition in dispositions],
    }
