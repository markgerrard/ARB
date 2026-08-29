"""ARB eval suite — Instrument 1 (seeded floor-capability).

Status: v0 core. Deterministic pieces (power budget, viability oracle, the structural-wall
reporter, scenario schema) are real and tested. The dispatch->segment->normalize->match
pipeline is the next increment (interfaces stubbed in `pipeline.py`).

Design: docs/eval-instrument1-v0-schema.md   Principles: docs/measurement-principles.md
This instrument emits NO decorrelation / seat-keep-drop / ranking verdict — ever (v3 §3).
"""
from . import power, viability, report, schema, stats  # noqa: F401

__all__ = ["power", "viability", "report", "schema", "stats"]
