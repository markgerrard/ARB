"""Fail-closed readiness contracts for the Open Interpreter/Pi bakeoff.

This module owns the value-free gate record and aggregate controller contracts.  Live seat,
UID, ACL, provider, and scorer observations are injected by the orchestrator; no default or
seat-reported result can turn an unproven gate green.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


GATE_IDS: tuple[str, ...] = tuple(f"G{i}" for i in range(1, 15))
GATE_STATUSES = frozenset({"PASS", "FAIL", "UNKNOWN"})
KNOWN_CAPABILITY_CLASSES = frozenset({"read", "search", "shell", "edit", "write", "network", "browser", "memory"})
REQUIRED_CAPABILITY_CLASSES = frozenset({"read", "search", "shell", "edit", "write"})
PROHIBITED_CAPABILITY_CLASSES = frozenset({"network", "browser", "memory"})
_UNTRUSTED_ACKS = frozenset({"request", "request-echo", "config", "config-echo", "echo", "prose", "model-prose"})

LIVE_BOUNDARY_GATES = {
    "G9": "cell-runtime-and-secret-boundaries",
    "G10": "matched-capability-and-network-probes",
    "G11": "scorer-isolation-and-confidentiality",
    "G12": "git-importer-hostile-boundaries",
    "G13": "exact-scored-seat-preflight-and-acl",
    "G14": "aggregate-close-validation-and-calibration",
}

GATE_DESCRIPTIONS = {
    "G1": "Open Interpreter protocol, tool interception, and retirement",
    "G2": "declared bench environment from a fresh dependency sync",
    "G3": "concurrency hard-refusal except one",
    "G4": "independently acknowledged model, harness, and generation-control provenance",
    "G5": "cell and attempt identity everywhere",
    "G6": "immutable schedule and manifest",
    "G7": "observed telemetry and bounded provider/engine error evidence",
    "G8": "G0 infrastructure classification and non-delivery close",
    "G9": "per-cell UID, home, config, credential, and runtime cleanup",
    "G10": "matched capability positive/negative and network probes",
    "G11": "G1/G4 scorer isolation and confidentiality attacks",
    "G12": "Git shim, importer, hostile metadata/object/path, and accounting probes",
    "G13": "exact scored-path seat preflight and ACL lifecycle",
    "G14": "controller-owned aggregate close, validation, and known-good calibration",
}


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp must be a non-empty ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class GateRecord:
    gate_id: str
    status: str
    evidence_digest: str
    started_at: str
    ended_at: str

    def __post_init__(self) -> None:
        if self.gate_id not in GATE_IDS:
            raise ValueError(f"unknown gate: {self.gate_id}")
        if self.status not in GATE_STATUSES:
            raise ValueError("gate status must be PASS, FAIL, or UNKNOWN")
        if not isinstance(self.evidence_digest, str) or len(self.evidence_digest) != 64 or any(char not in "0123456789abcdef" for char in self.evidence_digest):
            raise ValueError("evidence_digest must be a lowercase SHA-256 hex digest")
        started = _timestamp(self.started_at)
        ended = _timestamp(self.ended_at)
        if ended < started:
            raise ValueError("ended_at must not precede started_at")

    def to_dict(self) -> dict[str, str]:
        return {
            "gate_id": self.gate_id,
            "status": self.status,
            "evidence_digest": self.evidence_digest,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }


def gate_record(gate_id: str, status: str, evidence: Any, *, started_at: str | None = None, ended_at: str | None = None) -> GateRecord:
    """Create a value-free record from an observation projection."""

    return GateRecord(gate_id, status, _digest(evidence), started_at or _now(), ended_at or _now())


@dataclass(frozen=True)
class AggregateReadiness:
    status: str
    gates: tuple[GateRecord, ...]
    clean_controller: bool

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "gates": [gate.to_dict() for gate in self.gates], "clean_controller": self.clean_controller}


def _coerce_record(value: GateRecord | Mapping[str, Any]) -> GateRecord:
    if isinstance(value, GateRecord):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("gate check did not return a gate record")
    try:
        return GateRecord(
            value["gate_id"], value["status"], value["evidence_digest"], value["started_at"], value["ended_at"]
        )
    except KeyError as exc:
        raise ValueError("gate record is missing a required field") from exc


def aggregate_readiness(records: Iterable[GateRecord | Mapping[str, Any]], *, clean_controller: bool) -> AggregateReadiness:
    """Aggregate exactly one record for every frozen gate; missing is never skipped."""

    coerced = tuple(_coerce_record(record) for record in records)
    seen: set[str] = set()
    for record in coerced:
        if record.gate_id in seen:
            raise ValueError(f"duplicate gate: {record.gate_id}")
        seen.add(record.gate_id)
    if len(coerced) != len(GATE_IDS):
        raise ValueError("aggregate requires exactly fourteen gate records")
    missing = set(GATE_IDS) - seen
    if missing:
        raise ValueError(f"missing gate records: {sorted(missing)}")
    if not clean_controller:
        raise ValueError("aggregate requires a clean controller checkout")
    ordered = tuple(sorted(coerced, key=lambda record: GATE_IDS.index(record.gate_id)))
    status = "PASS" if all(record.status == "PASS" for record in ordered) else ("FAIL" if any(record.status == "FAIL" for record in ordered) else "UNKNOWN")
    return AggregateReadiness(status, ordered, clean_controller)


@dataclass(frozen=True)
class CapabilityMatch:
    classes: frozenset[str]
    positive_probes: frozenset[str]
    negative_probes: frozenset[str]
    left_surface_digest: str
    right_surface_digest: str


def _normalise_surface(surface: Mapping[str, str | Iterable[str]]) -> dict[str, tuple[str, ...]]:
    if not isinstance(surface, Mapping) or not surface:
        raise ValueError("effective tool surface must be a non-empty mapping")
    normalised: dict[str, tuple[str, ...]] = {}
    for tool, raw_classes in surface.items():
        if not isinstance(tool, str) or not tool:
            raise ValueError("tool names must be non-empty strings")
        classes = (raw_classes,) if isinstance(raw_classes, str) else tuple(raw_classes)
        if not classes or any(not isinstance(item, str) or not item for item in classes):
            raise ValueError("tool capability classes must be non-empty strings")
        unknown = set(classes) - KNOWN_CAPABILITY_CLASSES
        if unknown:
            raise ValueError(f"unknown capability class: {sorted(unknown)}")
        normalised[tool] = tuple(sorted(set(classes)))
    return normalised


@dataclass(frozen=True)
class CapabilityContract:
    effective_tool_surface: Mapping[str, tuple[str, ...]]
    classes: frozenset[str]
    surface_digest: str

    @classmethod
    def from_tool_surface(cls, surface: Mapping[str, str | Iterable[str]]) -> "CapabilityContract":
        normalised = _normalise_surface(surface)
        classes = frozenset(item for values in normalised.values() for item in values)
        return cls(normalised, classes, _digest(normalised))

    def to_dict(self) -> dict[str, Any]:
        return {"effective_tool_surface": dict(self.effective_tool_surface), "classes": sorted(self.classes), "surface_digest": self.surface_digest}

    def verify_probes(self, *, positive: Mapping[str, bool], negative: Mapping[str, bool]) -> None:
        for capability in REQUIRED_CAPABILITY_CLASSES:
            if positive.get(capability) is not True:
                raise ValueError(f"positive capability probe failed: {capability}")
        for capability in PROHIBITED_CAPABILITY_CLASSES:
            if negative.get(capability) is not False:
                raise ValueError(f"negative capability probe failed: {capability}")

    def match(self, other: "CapabilityContract") -> CapabilityMatch:
        if not isinstance(other, CapabilityContract):
            raise ValueError("capability contract is missing")
        if self.classes != other.classes:
            raise ValueError("capability classes do not match")
        if not REQUIRED_CAPABILITY_CLASSES <= self.classes:
            raise ValueError("required capability class is missing")
        if self.classes & PROHIBITED_CAPABILITY_CLASSES:
            raise ValueError("prohibited capability class is exposed")
        return CapabilityMatch(self.classes, REQUIRED_CAPABILITY_CLASSES, PROHIBITED_CAPABILITY_CLASSES, self.surface_digest, other.surface_digest)


def match_capability_surfaces(left: Mapping[str, str | Iterable[str]], right: Mapping[str, str | Iterable[str]]) -> CapabilityMatch:
    return CapabilityContract.from_tool_surface(left).match(CapabilityContract.from_tool_surface(right))


@dataclass(frozen=True)
class ControlAcknowledgement:
    name: str
    requested: str
    effective: str
    verified_via: str

    def validate(self) -> None:
        if not all(isinstance(value, str) and value for value in (self.name, self.requested, self.effective, self.verified_via)):
            raise ValueError("control acknowledgement fields must be non-empty strings")
        if self.effective == "UNKNOWN":
            raise ValueError(f"{self.name} effective value is UNKNOWN")
        if self.verified_via in _UNTRUSTED_ACKS:
            raise ValueError(f"{self.name} acknowledgement is request/config echo")
        if self.name == "reasoning" and (self.requested != "medium" or self.effective != "medium"):
            raise ValueError("reasoning requested and effective values must both be medium")


def validate_control_map(controls: Mapping[str, Mapping[str, str]]) -> tuple[ControlAcknowledgement, ...]:
    if not isinstance(controls, Mapping) or not controls:
        raise ValueError("control map must be non-empty")
    acknowledgements = []
    for name, values in controls.items():
        if not isinstance(values, Mapping) or set(values) != {"requested", "effective", "verified_via"}:
            raise ValueError(f"{name} control acknowledgement fields mismatch")
        acknowledgement = ControlAcknowledgement(name, values["requested"], values["effective"], values["verified_via"])
        acknowledgement.validate()
        acknowledgements.append(acknowledgement)
    return tuple(acknowledgements)


def match_control_maps(left: Mapping[str, Mapping[str, str]], right: Mapping[str, Mapping[str, str]]) -> None:
    left_values = {item.name: item for item in validate_control_map(left)}
    right_values = {item.name: item for item in validate_control_map(right)}
    if set(left_values) != set(right_values):
        raise ValueError("exposed control sets do not match")
    for name in left_values:
        if left_values[name].requested != right_values[name].requested or left_values[name].effective != right_values[name].effective:
            raise ValueError(f"control values do not match: {name}")


def run_gate14(
    manifest: Mapping[str, Any],
    cell_factory: Callable[[], Any],
    *,
    validate: Callable[[Mapping[str, Any]], Any],
    known_good_calibration: Callable[[Mapping[str, Any], Callable[[], Any]], Any],
) -> GateRecord:
    """Run Gate 14's injected protocol in normative order: validate, then calibration."""

    started = _now()
    try:
        validate(manifest)
        known_good_calibration(manifest, cell_factory)
    except Exception as exc:  # noqa: BLE001 - readiness is fail-closed and value-free
        return gate_record("G14", "UNKNOWN", {"failure": type(exc).__name__}, started_at=started)
    return gate_record("G14", "PASS", {"validate": "PASS", "known_good_calibration": "PASS"}, started_at=started)


def bind_live_boundaries() -> dict[str, str]:
    """Return the fixed Task 4/5 live-boundary ownership map without executing it."""

    return dict(LIVE_BOUNDARY_GATES)


def _unavailable_gate(gate_id: str, _manifest: Mapping[str, Any]) -> GateRecord:
    return gate_record(gate_id, "UNKNOWN", {"reason": "live-observation-unavailable"})


def production_gate_checks(manifest: Mapping[str, Any], runtime: Any | None = None) -> dict[str, Callable[[Mapping[str, Any]], GateRecord]]:
    """Bind every production gate slot to an observation callback.

    A serialized manifest is configuration, not evidence.  Unless an orchestrator supplies
    controller-owned live callbacks through the in-process test/runtime hook, every callback
    remains UNKNOWN and the production preflight fails closed.
    """

    supplied = getattr(runtime, "gate_checks", None) if runtime is not None else None
    if supplied is None:
        supplied = manifest.get("_production_gate_checks") if isinstance(manifest, Mapping) else None
    checks: dict[str, Callable[[Mapping[str, Any]], GateRecord]] = {}
    for gate_id in GATE_IDS:
        callback = supplied.get(gate_id) if isinstance(supplied, Mapping) else None
        if callable(callback):
            checks[gate_id] = callback
        else:
            checks[gate_id] = lambda current, gate_id=gate_id: _unavailable_gate(gate_id, current)
    return checks


class ReadinessController:
    """Controller-owned aggregate runner; gate checks are invoked, not seat JSON trusted."""

    def __init__(
        self,
        manifest: Mapping[str, Any],
        gate_checks: Mapping[str, Callable[[Mapping[str, Any]], GateRecord | Mapping[str, Any]]],
        *,
        cell_factory: Callable[[], Any] | None = None,
        validate: Callable[[Mapping[str, Any]], Any] | None = None,
        known_good_calibration: Callable[[Mapping[str, Any], Callable[[], Any]], Any] | None = None,
    ) -> None:
        self.manifest = manifest
        self.gate_checks = gate_checks
        extra = set(gate_checks) - set(GATE_IDS)
        if extra:
            raise ValueError(f"unknown gate check(s): {sorted(extra)}")
        self.cell_factory = cell_factory
        self.validate = validate
        self.known_good_calibration = known_good_calibration

    def run(self, *, clean_controller: bool) -> AggregateReadiness:
        records: list[GateRecord] = []
        gate14_bound = False
        for gate_id in GATE_IDS:
            check = self.gate_checks.get(gate_id)
            if gate_id == "G14" and check is None:
                check = None
            elif check is None:
                records.append(gate_record(gate_id, "UNKNOWN", {"reason": "missing-controller-check"}))
                continue
            if gate_id == "G14" and check is not None:
                gate14_bound = True
                try:
                    records.append(_coerce_record(check(self.manifest)))
                except Exception as exc:  # noqa: BLE001 - no unproven gate may pass
                    records.append(gate_record(gate_id, "UNKNOWN", {"failure": type(exc).__name__}))
                continue
            if gate_id == "G14":
                break
            try:
                records.append(_coerce_record(check(self.manifest)))
            except Exception as exc:  # noqa: BLE001 - no unproven gate may pass
                records.append(gate_record(gate_id, "UNKNOWN", {"failure": type(exc).__name__}))
        if gate14_bound:
            pass
        elif self.cell_factory is None or self.validate is None or self.known_good_calibration is None:
            records.append(gate_record("G14", "UNKNOWN", {"reason": "known-good-calibration-protocol-not-bound"}))
        else:
            records.append(run_gate14(self.manifest, self.cell_factory, validate=self.validate, known_good_calibration=self.known_good_calibration))
        return aggregate_readiness(records, clean_controller=clean_controller)


def run_production_preflight(
    manifest: Mapping[str, Any],
    *,
    gate_checks: Mapping[str, Callable[[Mapping[str, Any]], GateRecord | Mapping[str, Any]]] | None = None,
    runtime: Any | None = None,
    clean_controller: bool = True,
) -> AggregateReadiness:
    """Run the real production binding; static manifest fields cannot make it PASS."""

    checks = gate_checks or production_gate_checks(manifest, runtime)
    if runtime is None:
        return ReadinessController(manifest, checks).run(clean_controller=clean_controller)
    return ReadinessController(
        manifest,
        checks,
        cell_factory=getattr(runtime, "cell_factory", None),
        validate=getattr(runtime, "validate", None),
        known_good_calibration=getattr(runtime, "known_good_calibration", None),
    ).run(clean_controller=clean_controller)


def run_aggregate_readiness(*args: Any, clean_controller: bool = True, **kwargs: Any) -> AggregateReadiness:
    return ReadinessController(*args, **kwargs).run(clean_controller=clean_controller)


def close_representative_cell(controller: Any, *, terminal: str = "completed") -> Any:
    """Use the existing production close dispatcher; readiness defines no teardown variant."""

    close = getattr(controller, "close", None)
    if not callable(close):
        raise ValueError("production close controller is required")
    return close(terminal=terminal)
