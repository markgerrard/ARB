"""Frozen public API for the diagnose shared neutral foundation.

Parallel skill builds may import only these names from `skills._diagnose_common`:

- `RUN_RECORD_SCHEMA_PATH: pathlib.Path`
- `clock.append_event(log_path, event_type, seat, channel, artifact, *, timestamp=None) -> dict`
- `clock.read_dispatch_log(log_path) -> list[dict]`
- `clock.validate_dispatch_log(events) -> list[str]`
- `clock.max_seq(events, event_type) -> int | None`
- `collation.capture_artifact(path) -> dict`
- `collation.collate_artifacts(artifacts) -> dict`
- `canonical.canonical_bytes(obj) -> bytes`
- `canonical.seal(obj) -> str`
- `neutral_validators.resolve_evidence_category(evidence_ref) -> str`
- `neutral_validators.NEUTRAL_BLOCK_REASONS: set[str]`
- `neutral_validators.validate_run_record(run_record) -> list[str]`
- `neutral_validators.assert_run_record_conformant(run_record) -> None`

This package owns neutral artifact capture, collation, run-record schema location,
the monotonic dispatch clock, and neutral-only validation. It owns no per-skill
dispatch and no steer-specific validation.
"""

from __future__ import annotations

from pathlib import Path

from .clock import append_event, max_seq, read_dispatch_log, validate_dispatch_log
from .collation import capture_artifact, collate_artifacts
from .canonical import canonical_bytes, seal
from .neutral_validators import (
    NEUTRAL_BLOCK_REASONS,
    assert_run_record_conformant,
    resolve_evidence_category,
    validate_run_record,
)

RUN_RECORD_SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "run_record.json"

__all__ = [
    "RUN_RECORD_SCHEMA_PATH",
    "append_event",
    "read_dispatch_log",
    "validate_dispatch_log",
    "max_seq",
    "capture_artifact",
    "collate_artifacts",
    "canonical_bytes",
    "seal",
    "validate_run_record",
    "NEUTRAL_BLOCK_REASONS",
    "resolve_evidence_category",
    "assert_run_record_conformant",
]
