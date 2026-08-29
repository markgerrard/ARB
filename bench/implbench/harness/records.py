"""Canonical, closed evidence records for the isolated bakeoff.

This module deliberately contains no controller policy.  It validates the bytes that a
controller may persist; authentication and durable append are in :mod:`authlog`.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Any


class RecordError(ValueError):
    """Raised when a record is not an authoritative record-v2 value."""


MAX_RECORD_BYTES = 1_048_576
IDENTITY_FIELDS = frozenset(
    {
        "run_id", "cell_id", "attempt_id", "pair", "arm", "task", "repetition", "schedule_index",
        "fixture_sha", "model_declared", "model_verified_via", "engine_version", "harness_version",
        "corpus_version", "config_digest", "capability_manifest_digest", "reasoning_requested",
        "reasoning_effective", "reasoning_verified_via", "started_at", "ended_at", "wall_time_s",
        "terminal_status", "retry_count", "tool_call_count", "schema_version", "prior_record_digest",
        "controls",
    }
)
ENVELOPE_FIELDS = IDENTITY_FIELDS | {"record_type", "payload"}
AUTH_FIELDS = frozenset({"sequence", "nonce", "mac"})
CONTROL_NAMES = (
    "temperature", "top_p", "top_k", "seed", "penalties", "maximum_output", "stop_behavior",
    "tool_choice", "parallel_tool_behavior", "retry", "backoff", "timeouts",
)
PAIRS = {"GLM", "Kimi"}
ARMS = {"glm-pi", "glm-zcode", "kimi-pi", "kimi-cli"}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
OID = re.compile(r"^[0-9a-f]{40}$")
PATH = re.compile(r"^[^\x00\n\r]+$")
TERMINAL_STATUSES = {"completed", "failed", "timeout", "unknown", "not-delivered", "budget-failed"}
VERIFIED_VIA_FORBIDDEN = {"request", "request-echo", "config", "config-echo", "echo", "prose", "model-prose"}
_BUDGET_DIMENSIONS = {
    "wall_time_s", "cpu_time_s", "memory_bytes", "process_count", "disk_bytes", "tool_command_bytes",
    "ingress_bytes", "max_frame_bytes", "max_path_bytes", "max_components_per_path", "max_component_bytes",
    "max_paths_per_request", "max_in_flight", "status_rate_per_second", "status_burst", "status_calls",
    "hash_bytes", "stage_bytes", "tree_bytes", "commit_bytes", "object_count",
}
_ERROR_ENUMS = {"provider_401", "provider_429", "provider_5xx", "provider_timeout", "engine_failure", "bridge_busy", "daemon_death", "sandbox_failure", "worktree_failure", "scorer_failure"}


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RecordError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_numbers(value: Any) -> None:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RecordError("non-finite JSON number")
        raise RecordError("floating-point JSON numbers are not canonical")
    if isinstance(value, Mapping):
        for item in value.values():
            _reject_numbers(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_numbers(item)


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Return the one accepted JSON representation of a record value."""

    if not isinstance(value, Mapping):
        raise RecordError("canonical JSON root must be an object")
    _reject_numbers(value)
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise RecordError(f"record is not canonical JSON: {exc}") from exc
    if len(encoded) > MAX_RECORD_BYTES:
        raise RecordError("record exceeds maximum frame size")
    return encoded


def parse_canonical_json(raw: bytes | bytearray | memoryview) -> dict[str, Any]:
    """Parse bytes only when they round-trip to canonical JSON exactly."""

    data = bytes(raw)
    if len(data) > MAX_RECORD_BYTES:
        raise RecordError("record exceeds maximum frame size")
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_float=lambda value: (_ for _ in ()).throw(RecordError("floating-point JSON numbers are not canonical")),
            parse_constant=lambda value: (_ for _ in ()).throw(RecordError(f"invalid JSON constant: {value}")),
        )
    except RecordError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecordError(f"invalid canonical JSON: {exc}") from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) != data:
        raise RecordError("JSON is not canonical or has trailing bytes")
    return value


def _exact(value: Any, fields: set[str] | frozenset[str], where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(fields):
        raise RecordError(f"{where} fields mismatch")
    return value


def _string(value: Any, where: str, *, nonempty: bool = True) -> None:
    if not isinstance(value, str) or (nonempty and not value):
        raise RecordError(f"{where} must be a string")


def _integer(value: Any, where: str, *, minimum: int = 0) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise RecordError(f"{where} must be a bounded integer")


def _digest(value: Any, where: str) -> None:
    if not isinstance(value, str) or not HEX64.fullmatch(value):
        raise RecordError(f"{where} must be a lowercase SHA-256 digest")


def _oid(value: Any, where: str) -> None:
    if not isinstance(value, str) or not OID.fullmatch(value):
        raise RecordError(f"{where} must be a lowercase Git object ID")


def _fixture_sha(value: Any, where: str) -> None:
    """Accept the frozen fixture's Git object ID or a SHA-256 fixture digest."""

    if not isinstance(value, str) or not (OID.fullmatch(value) or HEX64.fullmatch(value)):
        raise RecordError(f"{where} must be a lowercase Git object ID or SHA-256 digest")


def _controls(value: Any) -> None:
    if not isinstance(value, Mapping) or set(value) != set(CONTROL_NAMES):
        raise RecordError("controls must be exhaustive")
    for name in CONTROL_NAMES:
        item = _exact(value[name], {"requested", "effective", "verified_via"}, f"controls.{name}")
        for field in ("requested", "effective", "verified_via"):
            _string(item[field], f"controls.{name}.{field}")
        if item["verified_via"] in VERIFIED_VIA_FORBIDDEN:
            raise RecordError(f"controls.{name} is not independently verified")


def _validate_identity(record: Mapping[str, Any]) -> None:
    if not IDENTITY_FIELDS <= set(record):
        raise RecordError("mandatory identity envelope is incomplete")
    _string(record["run_id"], "run_id")
    if not record["run_id"].startswith("oi-pi-bakeoff-"):
        raise RecordError("authoritative records require a bakeoff run ID")
    for field in ("cell_id", "attempt_id", "fixture_sha", "config_digest", "capability_manifest_digest"):
        _string(record[field], field)
    if field := record["cell_id"]:
        if not re.fullmatch(r"cell-[0-9a-f]{64}", field):
            raise RecordError("cell_id is not immutable hex identity")
    if not re.fullmatch(r"attempt-[0-9a-f]{32,64}", record["attempt_id"]):
        raise RecordError("attempt_id is not immutable hex identity")
    _fixture_sha(record["fixture_sha"], "fixture_sha")
    _digest(record["config_digest"], "config_digest")
    _digest(record["capability_manifest_digest"], "capability_manifest_digest")
    if record["pair"] not in PAIRS or record["arm"] not in ARMS:
        raise RecordError("unknown pair or arm")
    _string(record["task"], "task")
    for field in ("repetition", "schedule_index", "wall_time_s", "retry_count", "tool_call_count"):
        _integer(record[field], field)
    for field in ("model_declared", "model_verified_via", "engine_version", "harness_version", "corpus_version", "reasoning_requested", "reasoning_effective", "reasoning_verified_via", "started_at", "ended_at"):
        _string(record[field], field)
    if record["reasoning_requested"] != "medium" or record["reasoning_effective"] != "medium" or record["reasoning_verified_via"] in VERIFIED_VIA_FORBIDDEN:
        raise RecordError("reasoning provenance is not an independent medium acknowledgement")
    if record["terminal_status"] not in TERMINAL_STATUSES:
        raise RecordError("unknown terminal status")
    if record["schema_version"] != "record-v2":
        raise RecordError("unknown record schema version")
    prior = record["prior_record_digest"]
    if prior is not None:
        _digest(prior, "prior_record_digest")
    _controls(record["controls"])


def _validate_payload(kind: str, payload: Any) -> None:
    if kind == "git-receipt":
        value = _exact(payload, {"cell_id", "attempt_id", "fixture_root_oid", "ordered_parent_oids", "commit_oid", "tree_oid", "changed_paths", "tree_digest", "tree_digest_version", "head_oid", "dirty", "controller_sequence", "nonce"}, kind)
        for field in ("fixture_root_oid", "commit_oid", "tree_oid", "head_oid"):
            _oid(value[field], field)
        if not isinstance(value["ordered_parent_oids"], list) or len(value["ordered_parent_oids"]) != 1:
            raise RecordError("receipt must have one ordered parent")
        _oid(value["ordered_parent_oids"][0], "ordered_parent_oids[0]")
        if not isinstance(value["changed_paths"], list) or any(not isinstance(path, str) or not PATH.fullmatch(path) or path.startswith("/") or ".." in path.split("/") for path in value["changed_paths"]):
            raise RecordError("receipt paths are invalid")
        _digest(value["tree_digest"], "tree_digest")
        if value["tree_digest_version"] != "final-tree-v1" or not isinstance(value["dirty"], bool):
            raise RecordError("receipt tree digest pin or dirty flag is invalid")
        _integer(value["controller_sequence"], "controller_sequence", minimum=1)
        if not HEX64.fullmatch(value["nonce"]):
            raise RecordError("receipt nonce is invalid")
    elif kind == "budget":
        value = _exact(payload, {"operation", "reason", "budget_dimension", "limit", "observed"}, kind)
        if value["operation"] not in {"tool", "ingress", "status", "hash", "stage", "tree", "commit"}:
            raise RecordError("unknown budget operation")
        if value["reason"] != "MODEL_BUDGET_EXCEEDED":
            raise RecordError("unknown budget reason")
        if value["budget_dimension"] not in _BUDGET_DIMENSIONS:
            raise RecordError("unknown budget dimension")
        _integer(value["limit"], "limit", minimum=1); _integer(value["observed"], "observed", minimum=0)
    elif kind == "infrastructure-failure":
        value = _exact(payload, {"cell_id", "attempt_id", "operation", "reason", "parent_oid", "commit_oid"}, kind)
        if value["operation"] != "update-ref" or value["reason"] != "UPDATE_REF_FAILED":
            raise RecordError("unknown infrastructure failure")
        _oid(value["parent_oid"], "parent_oid")
        _oid(value["commit_oid"], "commit_oid")
    elif kind == "g4-receipt":
        value = _exact(payload, {"cell_id", "attempt_id", "commit_oid", "public_suite_oid", "public_suite_digest", "public_suite_digest_version", "outcome_enum", "controller_sequence", "nonce"}, kind)
        for field in ("commit_oid", "public_suite_oid"):
            _oid(value[field], field)
        _digest(value["public_suite_digest"], "public_suite_digest")
        _string(value["public_suite_digest_version"], "public_suite_digest_version")
        if value["outcome_enum"] not in {"PASS", "FAIL", "NOT_SCORED", "UNKNOWN"}:
            raise RecordError("unknown G4 outcome")
        _integer(value["controller_sequence"], "controller_sequence", minimum=1)
        if not HEX64.fullmatch(value["nonce"]):
            raise RecordError("G4 nonce is invalid")
    elif kind == "completion":
        value = _exact(payload, {"cell_id", "attempt_id", "fixture_root", "receipts", "head", "dirty", "final_tree_digest", "final_tree_digest_version"}, kind)
        _oid(value["fixture_root"], "fixture_root")
        if not isinstance(value["receipts"], list):
            raise RecordError("completion receipts must be a list")
        for receipt in value["receipts"]:
            _validate_payload("git-receipt", receipt)
        if value["head"] is not None:
            _oid(value["head"], "head")
        if not isinstance(value["dirty"], bool):
            raise RecordError("completion dirty must be boolean")
        _digest(value["final_tree_digest"], "final_tree_digest")
        if value["final_tree_digest_version"] != "final-tree-v1":
            raise RecordError("unknown final tree digest version")
    elif kind == "pre-scorer-attestation":
        value = _exact(payload, {"environment_manifest_digest", "completion_digest", "imported_graph_digest"}, kind)
        for field in value:
            _digest(value[field], field)
    elif kind == "post-g4-attestation":
        value = _exact(payload, {"pre_scorer_attestation_digest", "g4_receipts_digest"}, kind)
        for field in value:
            _digest(value[field], field)
    elif kind == "census-private":
        value = _exact(payload, {"phase", "gate_id", "expected_ref_digest", "observed_ref_digest", "expected_object_digest", "observed_object_digest", "expected_ref_count", "observed_ref_count", "expected_object_count", "observed_object_count", "violation"}, kind)
        if value["phase"] not in {"export", "cell"} or not re.fullmatch(r"G(?:[0-9]|1[0-4])", value["gate_id"]):
            raise RecordError("invalid census phase or gate")
        for field in ("expected_ref_digest", "observed_ref_digest", "expected_object_digest", "observed_object_digest"):
            _digest(value[field], field)
        for field in ("expected_ref_count", "observed_ref_count", "expected_object_count", "observed_object_count"):
            _integer(value[field], field)
        if value["violation"] not in {"EXTRA_REF", "MISSING_REF", "EXTRA_OBJECT", "MISSING_OBJECT", "OBJECT_SET_MISMATCH", "INVALID_OBJECT_TYPE"}:
            raise RecordError("unknown census violation")
    elif kind == "gate":
        value = _exact(payload, {"gate_id", "status", "evidence_digest", "started_at", "ended_at"}, kind)
        if not re.fullmatch(r"G(?:[0-9]|1[0-4])", value["gate_id"]):
            raise RecordError("invalid gate ID")
        if value["status"] not in {"PASS", "FAIL", "UNKNOWN", "NOT_SCORED"}:
            raise RecordError("unknown gate status")
        _digest(value["evidence_digest"], "evidence_digest")
        _string(value["started_at"], "started_at"); _string(value["ended_at"], "ended_at")
    elif kind == "telemetry":
        if not isinstance(payload, Mapping) or not {"event", "value"} <= set(payload) or not set(payload) <= {"event", "value", "token_count", "cost", "error_enum", "diagnostic_digest"}:
            raise RecordError("telemetry fields mismatch")
        value = payload
        if value["event"] not in {"turn-start", "turn-end", "provider-error", "tool-call", "retry", "unavailable"}:
            raise RecordError("unknown telemetry event")
        if not (isinstance(value["value"], int) and not isinstance(value["value"], bool) and 0 <= value["value"] <= 1_000_000):
            raise RecordError("telemetry value is unbounded")
        if "token_count" in value and value["token_count"] != "UNKNOWN":
            _integer(value["token_count"], "token_count")
        if "cost" in value and value["cost"] != "UNKNOWN":
            _integer(value["cost"], "cost")
        if "error_enum" in value and value["error_enum"] not in _ERROR_ENUMS:
            raise RecordError("unknown telemetry error")
        if "diagnostic_digest" in value:
            _digest(value["diagnostic_digest"], "diagnostic_digest")
    elif kind == "provenance":
        value = _exact(payload, {"model_declared", "model_verified_via", "engine_version", "harness_version", "corpus_version"}, kind)
        for field in value:
            _string(value[field], field)
        if value["model_verified_via"] in VERIFIED_VIA_FORBIDDEN:
            raise RecordError("model provenance is not independently verified")
    elif kind == "unavailable":
        value = _exact(payload, {"status", "reason", "diagnostic_digest"}, kind)
        if value["status"] != "UNAVAILABLE" or value["reason"] not in {"provider-timeout", "provider-error", "daemon-death", "sandbox-launch", "scorer-launch", "worktree-setup", "bridge-busy"}:
            raise RecordError("unknown unavailable status")
        _digest(value["diagnostic_digest"], "diagnostic_digest")
    else:
        raise RecordError(f"unknown record type: {kind}")


def make_identity(identity: Mapping[str, Any], *, record_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Construct an envelope without silently filling or changing identity."""

    return {**dict(identity), "record_type": record_type, "payload": dict(payload)}


def make_record(identity: Mapping[str, Any], record_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    return make_identity(identity, record_type=record_type, payload=payload)


def validate_record(record: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(record, Mapping) or not IDENTITY_FIELDS <= set(record) or not set(record) <= ENVELOPE_FIELDS | AUTH_FIELDS:
        raise RecordError("record envelope fields mismatch")
    _validate_identity(record)
    _string(record.get("record_type"), "record_type")
    if not isinstance(record.get("payload"), Mapping):
        raise RecordError("record payload must be an object")
    _validate_payload(record["record_type"], record["payload"])
    payload = record["payload"]
    if record["record_type"] in {"git-receipt", "g4-receipt", "completion", "infrastructure-failure"}:
        for field in ("cell_id", "attempt_id"):
            if payload[field] != record[field]:
                raise RecordError(f"{record['record_type']} identity does not match envelope")
    if "sequence" in record:
        _integer(record["sequence"], "sequence", minimum=1)
    for field in ("nonce", "mac"):
        if field in record and (not isinstance(record[field], str) or not HEX64.fullmatch(record[field])):
            raise RecordError(f"invalid authenticated {field}")
    return dict(record)


def record_digest(record: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(validate_record(record))).hexdigest()


_PUBLIC_IDENTITY = {"record_type", "pair", "arm", "task", "repetition", "schedule_index", "fixture_sha", "terminal_status", "retry_count", "tool_call_count"}
_PUBLIC_PAYLOAD = {
    "telemetry": {"event", "value"},
    "gate": {"gate_id", "status", "evidence_digest"},
    "g4-receipt": {"commit_oid", "public_suite_oid", "public_suite_digest", "public_suite_digest_version", "outcome_enum", "controller_sequence"},
    "unavailable": {"status"},
}


def public_projection(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return the allowlisted public projection; raw diagnostics never cross this boundary."""

    value = validate_record(record)
    result = {key: value[key] for key in _PUBLIC_IDENTITY if key in value}
    fields = _PUBLIC_PAYLOAD.get(value["record_type"], set())
    result.update({key: value["payload"][key] for key in fields if key in value["payload"]})
    return result


def census_evidence_digest(private_payload: Mapping[str, Any]) -> str:
    _validate_payload("census-private", private_payload)
    return hashlib.sha256(canonical_json_bytes(dict(private_payload))).hexdigest()


# Small compatibility spellings for callers that treat the record module as a schema API.
RecordSchemaError = RecordError
canonical_dumps = canonical_json_bytes
parse_record = parse_canonical_json
validate = validate_record
