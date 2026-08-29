"""Admission decision for `request` envelopes — the bus-side gate's decision core.

Spec: `docs/superpowers/specs/2026-07-26-bus-side-gate-design.md`
(ARB Memory `art-8742dfc1ca4b8be8` v6, hash 61b7332a01a1c97f), §5.1 refusal codes and
§5.3 lane resolution.

This module performs NO I/O. Every store fact arrives through an injected resolver, so each
branch below is reachable in a test without a database. That split is deliberate: the repo's
Postgres-backed tests skip when no store DSN is configured, and a skipped test is a green test.

(The env var is deliberately not named here: `tests/defect_hunts/test_negatives.py` tracks which
source files READ it, and a prose mention would enrol this non-reader in that guard's allowlist.)

Two properties this file exists to hold, both MUST in the spec:

* **Fail-closed.** Any resolver failure refuses. There is no path from "the authority could
  not be consulted" to "admitted".
* **No local posture short-circuit.** Whether a seat is gate-bearing is a fact the STORE
  returns. A seat able to answer that from its own config could disable the gate by editing a
  file it owns — the flaw `readonly_gate` already has (see spec §9.3).

Slice 1d-v: ``evaluate`` is the only public admission entry point. ``check`` is removed.
``GateEvaluation.audit`` records facts already read by that evaluation — never body or DSN —
and is never consulted for later admission.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


MISSING_CLAIM_REF = "missing_claim_ref"
UNCONFIRMED_CLAIM = "unconfirmed_claim"
UNATTESTED_CLAIM = "unattested_claim"
UNKNOWN_CLAIM_REF = "unknown_claim_ref"
LANE_NOT_ARMED_EXEMPT = "lane_not_armed_exempt"
STORE_UNREACHABLE = "store_unreachable"

GATE_EXIT_CODE = 5  # 2/3/4 are taken by arb_memory.close outcomes.


@dataclass(frozen=True)
class GateOutcome:
    """A refusal.

    `gaps` NAMES what is missing and which lanes the payload could legitimately take, so a
    confused-but-honest dispatcher is routed rather than bounced. That is half of why
    `refused_reconcile` worked as a template (`src/arb_memory/close.py:139,171`).
    """

    code: str
    gaps: list[str] = field(default_factory=list)
    exit_code: int = GATE_EXIT_CODE

    def as_dict(self) -> dict:
        return {"outcome": self.code, "exit_code": self.exit_code, "gaps": list(self.gaps)}

    def as_error(self) -> str:
        if not self.gaps:
            return self.code
        return f"{self.code}: " + "; ".join(self.gaps)


class StoreUnreachable(Exception):
    """The authority could not be consulted.

    ALWAYS refuses. There is deliberately no downgrade-to-admit path: an outage that ungated
    every seat would be the worst possible failure mode for a default-deny gate.
    """


@dataclass(frozen=True)
class ClaimFacts:
    """One row of `claim_admissibility_v`, as the gate sees it.

    Field names match the view's output columns so the psycopg resolver (slice 1c) is a
    straight row-to-dataclass mapping with nowhere for a rename to hide.
    """

    confirmed_now: bool
    attested: bool
    decorrelation_provenance: str = "none"


@dataclass(frozen=True)
class GateEvaluation:
    """One-pass admission decision plus the facts already read during that evaluation.

    ``outcome`` is None to admit, or a :class:`GateOutcome` to refuse.
    ``audit`` is metadata only — no body, no DSN — and is never an admission credential.
    """

    outcome: GateOutcome | None
    audit: dict[str, Any]


def _store_unreachable(what: str, exc: Exception) -> GateOutcome:
    return GateOutcome(
        code=STORE_UNREACHABLE,
        gaps=[f"{what}: {exc}", "operator action: the authority is unavailable"],
    )


def _brief_ref_from_payload(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    task = payload.get("task")
    if isinstance(task, Mapping) and "artefact_id" in task:
        return {
            "artefact_id": task.get("artefact_id"),
            "version": task.get("version"),
        }
    return None


def _claim_facts_dict(facts: ClaimFacts | None) -> dict[str, Any] | None:
    if facts is None:
        return None
    return {
        "confirmed_now": facts.confirmed_now,
        "attested": facts.attested,
        "decorrelation_provenance": facts.decorrelation_provenance,
    }


def evaluate(envelope, *, seat_id: str, resolver) -> GateEvaluation:
    """Decide admission in one resolver pass and accumulate audit facts.

    `resolver` must provide ``seat_requires_claim_ref(seat_id)``, ``lease_lane(lease_id)`` and
    ``claim(claim_ref)``; any of them may raise :class:`StoreUnreachable`.

    Prior audit records on the payload (e.g. ``gate_audit``) are ignored for admission —
    Slice 2's close contract is fresh resolver calls only.
    """
    payload = envelope.payload if isinstance(envelope.payload, Mapping) else {}
    # Explicitly ignore any injected prior audit — no code path from audit to admit.
    _ = payload.get("gate_audit")

    audit: dict[str, Any] = {
        "brief_ref": _brief_ref_from_payload(payload),
        "posture_required": None,
        "lease_id": None,
        "resolved_lane": None,
        "claim_ref": None,
        "claim_facts": None,
        "decision": STORE_UNREACHABLE,  # overwritten on every exit
    }

    try:
        posture_required = resolver.seat_requires_claim_ref(seat_id)
    except StoreUnreachable as exc:
        outcome = _store_unreachable(f"posture unavailable for seat {seat_id}", exc)
        audit["decision"] = outcome.code
        return GateEvaluation(outcome=outcome, audit=audit)

    audit["posture_required"] = bool(posture_required)

    if not posture_required:
        audit["decision"] = "admit"
        return GateEvaluation(outcome=None, audit=audit)

    # Lane resolution (spec §5.3). The lane is a STORE fact. `payload["lane"]`, if present, is
    # routing metadata: it may be read to produce a BETTER refusal, and never to admit.
    lease_id = payload.get("worktree_lease")
    declared_lane = payload.get("lane")

    lane = None
    if isinstance(lease_id, str) and lease_id:
        audit["lease_id"] = lease_id
        try:
            lane = resolver.lease_lane(lease_id)
        except StoreUnreachable as exc:
            outcome = _store_unreachable(f"lane unavailable for lease {lease_id}", exc)
            audit["decision"] = outcome.code
            return GateEvaluation(outcome=outcome, audit=audit)

    audit["resolved_lane"] = lane

    if lane == "exempt":
        audit["decision"] = "admit"
        return GateEvaluation(outcome=None, audit=audit)

    if declared_lane == "exempt":
        outcome = GateOutcome(
            code=LANE_NOT_ARMED_EXEMPT,
            gaps=[
                f"envelope presents as exempt but the store records lane={lane!r} "
                f"for lease {lease_id!r}",
                "lanes are armed by the consumer at arm time, not asserted by the dispatcher",
            ],
        )
        audit["decision"] = outcome.code
        return GateEvaluation(outcome=outcome, audit=audit)

    # Silence is never exemption: no lease, an unrecorded lease, or lane='gated' all fall
    # through to gated traffic, which requires an admissible claim ref.
    claim_ref = payload.get("claim_ref")
    probe_route = "legitimate lane: exempt — build the probe first, then dispatch the remediation"

    if not isinstance(claim_ref, str) or not claim_ref.strip():
        outcome = GateOutcome(
            code=MISSING_CLAIM_REF,
            gaps=[
                f"seat {seat_id} requires a claim_ref; none present on payload",
                probe_route,
            ],
        )
        audit["decision"] = outcome.code
        return GateEvaluation(outcome=outcome, audit=audit)

    audit["claim_ref"] = claim_ref

    try:
        found = resolver.claim(claim_ref)
    except StoreUnreachable as exc:
        outcome = _store_unreachable(f"claim {claim_ref} unavailable", exc)
        audit["decision"] = outcome.code
        return GateEvaluation(outcome=outcome, audit=audit)

    audit["claim_facts"] = _claim_facts_dict(found)

    if found is None:
        outcome = GateOutcome(
            code=UNKNOWN_CLAIM_REF,
            gaps=[
                f"claim_ref {claim_ref!r} does not resolve",
                "check for a typo or a stale reference",
            ],
        )
        audit["decision"] = outcome.code
        return GateEvaluation(outcome=outcome, audit=audit)

    if not found.confirmed_now:
        outcome = GateOutcome(
            code=UNCONFIRMED_CLAIM,
            gaps=[f"claim {claim_ref} is not admissible as of now", probe_route],
        )
        audit["decision"] = outcome.code
        return GateEvaluation(outcome=outcome, audit=audit)

    if not found.attested:
        # Deliberately NOT routed to the probe lane: this claim already has its probe. The
        # dispatcher's next action is verification, which is why the code is distinct.
        outcome = GateOutcome(
            code=UNATTESTED_CLAIM,
            gaps=[
                f"claim {claim_ref} is confirmed but carries no complete cross-family attestation",
                "next action: verification — this claim already has its probe",
            ],
        )
        audit["decision"] = outcome.code
        return GateEvaluation(outcome=outcome, audit=audit)

    audit["decision"] = "admit"
    return GateEvaluation(outcome=None, audit=audit)
