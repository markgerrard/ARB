from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import inspect
from pathlib import Path
import unittest.mock


class E2EStatus(Enum):
    PASS = "pass"
    BLOCK_FAIL = "block-fail"
    BLOCK_UNRUN = "block-unrun"


class BoundaryCanaryError(AssertionError):
    pass


@dataclass
class E2EResult:
    status: E2EStatus
    detail: str
    case_count: int
    passed_count: int
    block_fail_count: int
    block_unrun_count: int

    def merges(self) -> bool:
        return self.status is E2EStatus.PASS

    def to_json(self) -> dict:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload

    @classmethod
    def from_counts(
        cls,
        *,
        case_count: int,
        passed: int,
        block_fail: int,
        block_unrun: int,
        detail: str = "",
    ) -> E2EResult:
        if case_count == 0 or block_unrun > 0:
            return cls(
                E2EStatus.BLOCK_UNRUN,
                detail or "vacuous/unrun",
                case_count,
                passed,
                block_fail,
                block_unrun,
            )
        if block_fail > 0:
            return cls(
                E2EStatus.BLOCK_FAIL,
                detail or "feature broken",
                case_count,
                passed,
                block_fail,
                block_unrun,
            )
        return cls(E2EStatus.PASS, detail or "ok", case_count, passed, block_fail, block_unrun)


def assert_real_symbol(func, *, module_name: str, real_path: str | Path) -> None:
    if isinstance(func, unittest.mock.Mock):
        raise BoundaryCanaryError("symbol is a Mock")
    unwrapped = inspect.unwrap(func)
    if getattr(unwrapped, "__module__", None) != module_name:
        raise BoundaryCanaryError(f"symbol module is {getattr(unwrapped, '__module__', None)!r}, not {module_name!r}")
    code = getattr(unwrapped, "__code__", None)
    if code is None:
        raise BoundaryCanaryError("symbol has no code object")
    if Path(code.co_filename).resolve() != Path(real_path).resolve():
        raise BoundaryCanaryError(f"symbol path is {Path(code.co_filename).resolve()}, not {Path(real_path).resolve()}")


def assert_h2_boundary_honest(out: dict) -> None:
    if not out["log_records"]:
        raise BoundaryCanaryError("write boundary did not append a JSONL record")
    if out["record_payload"] != out["log_records"][-1]:
        raise BoundaryCanaryError("record payload does not match written JSONL")
    for derived_id in out["record_payload"].get("derived", []):
        rel = _candidate_relpath(derived_id)
        if rel not in out["read_evidence"]:
            raise BoundaryCanaryError(f"read boundary missing evidence for {rel}")
        if not out["read_evidence"][rel]["sha256"]:
            raise BoundaryCanaryError(f"read boundary missing sha256 for {rel}")
        if derived_id not in out["read_evidence"][rel]["explains"]:
            raise BoundaryCanaryError(f"{derived_id} not explained by seeded bytes")


def _candidate_relpath(candidate_id: str) -> str:
    try:
        _kind, rel, _callee = candidate_id.split(":", 2)
    except ValueError:
        return ""
    return rel
