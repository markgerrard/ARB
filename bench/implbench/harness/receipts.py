"""Controller-authenticated Git, budget, and post-import receipt producers."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any, Mapping

from .authlog import AuthLog, AuthLogError
from .records import RecordError, canonical_json_bytes, make_identity, validate_record


class ReceiptError(ValueError):
    """Raised when a receipt cannot be admitted to the authenticated chain."""


def make_git_receipt(
    *,
    cell_id: str,
    attempt_id: str,
    fixture_root_oid: str,
    ordered_parent_oids: list[str],
    commit_oid: str,
    tree_oid: str,
    changed_paths: list[str],
    tree_digest: str,
    head_oid: str,
    dirty: bool,
    controller_sequence: int,
    nonce: str | None = None,
) -> dict[str, Any]:
    return {
        "cell_id": cell_id,
        "attempt_id": attempt_id,
        "fixture_root_oid": fixture_root_oid,
        "ordered_parent_oids": list(ordered_parent_oids),
        "commit_oid": commit_oid,
        "tree_oid": tree_oid,
        "changed_paths": list(changed_paths),
        "tree_digest": tree_digest,
        "tree_digest_version": "final-tree-v1",
        "head_oid": head_oid,
        "dirty": dirty,
        "controller_sequence": controller_sequence,
        "nonce": nonce or os.urandom(32).hex(),
    }


class ReceiptChain:
    """An AuthLog wrapper that enforces fixture-root first-parent and path policy."""

    def __init__(
        self,
        path: str | Path,
        key: bytes,
        *,
        identity: Mapping[str, Any],
        fixture_root_oid: str,
        allowed_paths: tuple[str, ...] | list[str],
    ):
        self.identity = dict(identity)
        self.fixture_root_oid = fixture_root_oid
        self.allowed_paths = tuple(allowed_paths)
        try:
            self.log = AuthLog(path, key, run_id=self.identity.get("run_id"), cell_id=self.identity.get("cell_id"), attempt_id=self.identity.get("attempt_id"))
        except AuthLogError as exc:
            raise ReceiptError(str(exc)) from exc

    def _rows(self) -> list[dict[str, Any]]:
        try:
            self.log.verify()
            return self.log._read_rows()[0]  # AuthLog owns the descriptor-safe verification.
        except (AuthLogError, OSError) as exc:
            raise ReceiptError(str(exc)) from exc

    def verify(self) -> int:
        return len(self._rows())

    def append(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(payload)
        if value.get("cell_id") != self.identity.get("cell_id") or value.get("attempt_id") != self.identity.get("attempt_id"):
            raise ReceiptError("receipt identity mismatch")
        if value.get("fixture_root_oid") != self.fixture_root_oid:
            raise ReceiptError("fixture root pin mismatch")
        rows = self._rows()
        git_rows = [row for row in rows if row.get("record_type") == "git-receipt"]
        expected_parent = self.fixture_root_oid if not git_rows else git_rows[-1]["payload"]["commit_oid"]
        if value.get("ordered_parent_oids") != [expected_parent]:
            raise ReceiptError("first-parent receipt chain mismatch")
        if not isinstance(value.get("changed_paths"), list) or not all(any(fnmatch.fnmatchcase(path, pattern) for pattern in self.allowed_paths) for path in value["changed_paths"]):
            raise ReceiptError("receipt contains a path outside the task allowlist")
        expected_sequence = len(rows) + 1
        if value.get("controller_sequence") not in {None, expected_sequence}:
            raise ReceiptError("controller sequence is not controller-assigned")
        value["controller_sequence"] = expected_sequence
        value["nonce"] = os.urandom(32).hex()
        record = make_identity(self.identity, record_type="git-receipt", payload=value)
        try:
            validate_record(record)
            return self.log.append(record)
        except (AuthLogError, RecordError) as exc:
            raise ReceiptError(str(exc)) from exc

    def append_budget_candidate(self, candidate: Mapping[str, Any]) -> dict[str, Any]:
        candidate = dict(candidate)
        if candidate.get("operation") not in {"status", "hash", "stage", "tree", "commit"}:
            raise ReceiptError("budget candidate is not service-owned")
        required = {"operation", "reason", "budget_dimension", "limit", "observed"}
        if set(candidate) != required or candidate["reason"] != "MODEL_BUDGET_EXCEEDED":
            raise ReceiptError("budget candidate is not closed")
        record = make_identity(self.identity, record_type="budget", payload=candidate)
        try:
            return self.log.append(record)
        except (AuthLogError, RecordError) as exc:
            raise ReceiptError(str(exc)) from exc

    def append_infrastructure_failure(
        self, *, operation: str, reason: str, parent_oid: str, commit_oid: str
    ) -> dict[str, Any]:
        payload = {
            "cell_id": self.identity["cell_id"],
            "attempt_id": self.identity["attempt_id"],
            "operation": operation,
            "reason": reason,
            "parent_oid": parent_oid,
            "commit_oid": commit_oid,
        }
        record = make_identity(self.identity, record_type="infrastructure-failure", payload=payload)
        try:
            validate_record(record)
            return self.log.append(record)
        except (AuthLogError, RecordError) as exc:
            raise ReceiptError(str(exc)) from exc

    def append_post_g4_attestation(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Durably bind the verified pre-scorer record to ordered G4 output."""

        record = make_identity(self.identity, record_type="post-g4-attestation", payload=dict(payload))
        try:
            validate_record(record)
            for row in self._rows():
                if row.get("record_type") != "post-g4-attestation":
                    continue
                if canonical_json_bytes(row.get("payload", {})) == canonical_json_bytes(record["payload"]):
                    return row
                raise ReceiptError("post-G4 attestation replay mismatch")
            self.log.append(record)
            durable = [
                row for row in self._rows()
                if row.get("record_type") == "post-g4-attestation"
                and canonical_json_bytes(row.get("payload", {})) == canonical_json_bytes(record["payload"])
            ]
            if len(durable) != 1:
                raise ReceiptError("post-G4 attestation durable reread is ambiguous")
            return durable[0]
        except (AuthLogError, RecordError) as exc:
            raise ReceiptError(str(exc)) from exc

    def append_pre_scorer_attestation(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Durably bind imported input before either scorer topology can launch."""

        record = make_identity(self.identity, record_type="pre-scorer-attestation", payload=dict(payload))
        try:
            validate_record(record)
            for row in self._rows():
                if row.get("record_type") != "pre-scorer-attestation":
                    continue
                if canonical_json_bytes(row.get("payload", {})) == canonical_json_bytes(record["payload"]):
                    return row
                raise ReceiptError("pre-scorer attestation replay mismatch")
            self.log.append(record)
            # Release only a row re-read through the authenticated chain after
            # fsync; never the controller's pre-append object or append return.
            durable = [
                row for row in self._rows()
                if row.get("record_type") == "pre-scorer-attestation"
                and canonical_json_bytes(row.get("payload", {})) == canonical_json_bytes(record["payload"])
            ]
            if len(durable) != 1:
                raise ReceiptError("pre-scorer attestation durable reread is ambiguous")
            return durable[0]
        except (AuthLogError, RecordError) as exc:
            raise ReceiptError(str(exc)) from exc

    def append_g4_receipt(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Append one controller-validated G4 result without accepting role-supplied pins."""

        value = dict(payload)
        required = {
            "cell_id", "attempt_id", "commit_oid", "public_suite_oid", "public_suite_digest",
            "public_suite_digest_version", "outcome_enum", "controller_sequence", "nonce",
        }
        if set(value) != required or any(value[field] != self.identity.get(field) for field in ("cell_id", "attempt_id")):
            raise ReceiptError("G4 receipt identity or schema mismatch")
        rows = self._rows()
        git_rows = [row for row in rows if row.get("record_type") == "git-receipt"]
        matching = [row["payload"] for row in git_rows if row.get("payload", {}).get("commit_oid") == value["commit_oid"]]
        if len(matching) != 1 or matching[0].get("controller_sequence") != value["controller_sequence"]:
            raise ReceiptError("G4 receipt is not bound to one authenticated import commit")
        record = make_identity(self.identity, record_type="g4-receipt", payload=value)
        try:
            validate_record(record)
            for row in rows:
                if row.get("record_type") != "g4-receipt" or row.get("payload", {}).get("commit_oid") != value["commit_oid"]:
                    continue
                # Recovery may only replay the exact authenticated row.  This admits
                # a crash after fsync while rejecting every altered pin/sequence/nonce
                # or outcome as a replay attack.
                if canonical_json_bytes(row.get("payload", {})) == canonical_json_bytes(record["payload"]):
                    return row
                raise ReceiptError("G4 receipt replay mismatch")
            return self.log.append(record)
        except (AuthLogError, RecordError) as exc:
            raise ReceiptError(str(exc)) from exc

    def validate_g4_binding(self, binding: Any) -> None:
        """Reject a durable G4 row unless it exactly matches controller evidence.

        The runtime calls this before a retry constructs the scorer graph.  This is
        deliberately stricter than append idempotency: it prevents a tampered row
        from being hidden by a newly generated recovery nonce.
        """
        required = (
            "cell_id", "attempt_id", "commit_oid", "public_suite_oid", "public_suite_digest",
            "public_suite_digest_version", "controller_sequence", "nonce",
        )
        expected = {field: getattr(binding, field, None) for field in required}
        rows = self._rows()
        for row in rows:
            if row.get("record_type") != "g4-receipt":
                continue
            value = row.get("payload", {})
            if value.get("commit_oid") != expected["commit_oid"]:
                continue
            if any(value.get(field) != expected[field] for field in required):
                raise ReceiptError("G4 receipt durable binding mismatch")


AuthenticatedReceiptLog = ReceiptChain
