# Bus-Side Gate — Slice 1a: the claim gate decision core

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `claim_gate.check()` — the pure decision function that decides whether a `request` envelope is admitted, refused, or exempt — with every refusal code from the spec and no database dependency.

**Architecture:** The gate is split into a *pure decision core* (this plan) and *store-backed resolvers* (later plans). `check()` takes an injected resolver object, so every branch is testable with fakes and no Postgres. This is deliberate: the repo's Postgres-backed tests skip without `ARB_MEMORY_DSN`, and a skipped test is a vacuously-green test. Everything in this plan runs and fails for real on a bare checkout.

**Tech Stack:** Python 3, pytest, stdlib only (`dataclasses`, `typing`). No psycopg in this module.

**Spec:** `docs/superpowers/specs/2026-07-26-bus-side-gate-design.md` — ARB Memory `art-8742dfc1ca4b8be8` v6, hash `61b7332a01a1c97f`. §5.1 (refusal codes), §5.3 (lane resolution order). Read those two sections before starting.

## Global Constraints

- **Fail-closed (spec §5.1, MUST):** any resolver failure ⇒ refuse. Never admit on error.
- **No caching (spec §5.1, MUST):** the gate resolves per call. Do not memoise resolver results.
- **No local posture short-circuit (spec §5, MUST):** posture comes from the resolver, never from a
  local flag, env var, or constructor default. There is no "assume not posture-bearing" path.
- **Silence is never exemption (spec §5.3, rule 1):** absent lease reference, unknown lease, or
  unreadable lane ⇒ gated traffic.
- **An envelope-declared lane is routing metadata only (spec §5.3, MUST):** it may be read to
  produce a *better refusal*, and must never be read to *admit*.
- **Refusal shape follows `src/arb_memory/close.py:139,171`:** `{"outcome", "exit_code", "gaps"}`.
  `gaps` must NAME what is missing and which lanes the payload could legitimately take.
- **Distinct codes are mandatory (spec §5.1):** `store_unreachable` must never be emitted where a
  confirmation failure is meant, and vice versa.
- Exit code for every gate refusal: `5` (2/3/4 are taken by `arb_memory.close` outcomes).

## File Structure

- **Create `src/agent_redis_bridge/claim_gate.py`** — the whole decision core: refusal codes,
  `GateOutcome`, `ClaimFacts`, `StoreUnreachable`, the `Resolver` protocol, and `check()`. One file,
  one responsibility (decide admission); it has no I/O, so it stays small and holdable.
- **Create `tests/test_claim_gate.py`** — table-driven tests over fake resolvers.
- **Modify `src/agent_redis_bridge/envelope.py`** — type checks only for two new optional payload
  fields. The envelope layer never decides admissibility.
- **Modify `tests/test_envelope.py`** (or create `tests/test_envelope_claim_fields.py`
  if the former does not exist — check with `ls tests/ | grep envelope` first).

Not in this plan, by design (each is its own plan, each independently shippable):

- **Slice 1b — store schema:** `claims`, `attestations`, `seat_posture`, `lease_lanes`, the three
  views, the `arb_gate_reader` GRANT, and the consumer-side F4 harness-identity check. Needs
  `ARB_MEMORY_DSN`; cannot be red-green verified without a live Postgres.
- **Slice 1c — bridge wiring:** the real psycopg resolver + the `handle_raw` insertion at
  `bridge.py:1193`.
- **Slice 1d — exempt lane + brief-artefact dispatch:** push-less worktree credential, the
  consumer-written `lease_lanes` row at arm time, store-before-send, worker-side hydration.

---

### Task 1: Refusal outcomes in house style

**Files:**
- Create: `src/agent_redis_bridge/claim_gate.py`
- Test: `tests/test_claim_gate.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `GateOutcome(code: str, gaps: list[str], exit_code: int = 5)` with
  `.as_dict() -> dict` and `.as_error() -> str`; module constants `MISSING_CLAIM_REF`,
  `UNCONFIRMED_CLAIM`, `UNATTESTED_CLAIM`, `UNKNOWN_CLAIM_REF`, `LANE_NOT_ARMED_EXEMPT`,
  `STORE_UNREACHABLE`, `GATE_EXIT_CODE`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_claim_gate.py
from agent_redis_bridge import claim_gate


def test_outcome_serialises_in_close_house_style():
    outcome = claim_gate.GateOutcome(
        code=claim_gate.MISSING_CLAIM_REF,
        gaps=["no claim_ref on payload", "legitimate lanes: exempt (build the probe first)"],
    )
    assert outcome.as_dict() == {
        "outcome": "missing_claim_ref",
        "exit_code": 5,
        "gaps": [
            "no claim_ref on payload",
            "legitimate lanes: exempt (build the probe first)",
        ],
    }


def test_error_string_names_the_code_and_the_gaps():
    outcome = claim_gate.GateOutcome(code=claim_gate.UNCONFIRMED_CLAIM, gaps=["claim c-1 is unconfirmed"])
    error = outcome.as_error()
    assert "unconfirmed_claim" in error
    assert "claim c-1 is unconfirmed" in error


def test_all_six_spec_codes_exist_and_are_distinct():
    codes = {
        claim_gate.MISSING_CLAIM_REF,
        claim_gate.UNCONFIRMED_CLAIM,
        claim_gate.UNATTESTED_CLAIM,
        claim_gate.UNKNOWN_CLAIM_REF,
        claim_gate.LANE_NOT_ARMED_EXEMPT,
        claim_gate.STORE_UNREACHABLE,
    }
    assert len(codes) == 6, "spec §5.1 requires six DISTINCT refusal codes"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_claim_gate.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent_redis_bridge.claim_gate'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/agent_redis_bridge/claim_gate.py
"""Admission decision for `request` envelopes — the bus-side gate's decision core.

Spec: docs/superpowers/specs/2026-07-26-bus-side-gate-design.md (art-8742dfc1ca4b8be8 v6),
§5.1 refusal codes and §5.3 lane resolution.

This module performs NO I/O. All store facts arrive through an injected resolver, so every
branch below is reachable in a test without a database. That is deliberate: the repo's
Postgres-backed tests skip without ARB_MEMORY_DSN, and a skipped test is a green test.
"""

from __future__ import annotations

from dataclasses import dataclass, field


MISSING_CLAIM_REF = "missing_claim_ref"
UNCONFIRMED_CLAIM = "unconfirmed_claim"
UNATTESTED_CLAIM = "unattested_claim"
UNKNOWN_CLAIM_REF = "unknown_claim_ref"
LANE_NOT_ARMED_EXEMPT = "lane_not_armed_exempt"
STORE_UNREACHABLE = "store_unreachable"

GATE_EXIT_CODE = 5  # 2/3/4 are taken by arb_memory.close outcomes.


@dataclass(frozen=True)
class GateOutcome:
    """A refusal. `gaps` NAMES what is missing so an honest dispatcher is routed, not bounced."""

    code: str
    gaps: list[str] = field(default_factory=list)
    exit_code: int = GATE_EXIT_CODE

    def as_dict(self) -> dict:
        return {"outcome": self.code, "exit_code": self.exit_code, "gaps": list(self.gaps)}

    def as_error(self) -> str:
        return f"{self.code}: " + "; ".join(self.gaps) if self.gaps else self.code
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_claim_gate.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/claim_gate.py tests/test_claim_gate.py
git commit -m "feat(claim-gate): refusal outcomes in close.py house style"
```

---

### Task 2: Fail-closed posture resolution

**Files:**
- Modify: `src/agent_redis_bridge/claim_gate.py`
- Test: `tests/test_claim_gate.py`

**Interfaces:**
- Consumes: `GateOutcome`, `STORE_UNREACHABLE` from Task 1.
- Produces: `StoreUnreachable(Exception)`; `ClaimFacts(confirmed_now: bool, attested: bool,
  decorrelation_provenance: str)`; `check(envelope, *, seat_id: str, resolver) -> GateOutcome | None`
  returning `None` to admit. Resolver duck-type: `seat_requires_claim_ref(seat_id) -> bool`,
  `lease_lane(lease_id) -> str | None`, `claim(claim_ref) -> ClaimFacts | None`; any of them may
  raise `StoreUnreachable`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_claim_gate.py
import pytest

from agent_redis_bridge.envelope import Envelope


def make_envelope(payload):
    return Envelope(
        id="e-1", sender="claude-x", branch="dev", recipient="codex-x",
        kind="request", sent_at="2026-07-26T00:00:00+00:00", payload=payload,
    )


class FakeResolver:
    """Explicit fake — every method must be configured, so a test cannot pass by accident."""

    def __init__(self, *, posture=True, lanes=None, claims=None, raises=None):
        self.posture = posture
        self.lanes = lanes or {}
        self.claims = claims or {}
        self.raises = raises

    def seat_requires_claim_ref(self, seat_id):
        if self.raises == "posture":
            raise claim_gate.StoreUnreachable("posture read failed")
        return self.posture

    def lease_lane(self, lease_id):
        if self.raises == "lane":
            raise claim_gate.StoreUnreachable("lane read failed")
        return self.lanes.get(lease_id)

    def claim(self, claim_ref):
        if self.raises == "claim":
            raise claim_gate.StoreUnreachable("claim read failed")
        return self.claims.get(claim_ref)


def test_seat_not_posture_bearing_is_admitted():
    outcome = claim_gate.check(
        make_envelope({"task": "do a thing"}), seat_id="codex-x",
        resolver=FakeResolver(posture=False),
    )
    assert outcome is None


@pytest.mark.parametrize("failing_call", ["posture", "lane", "claim"])
def test_any_store_failure_refuses_and_never_admits(failing_call):
    outcome = claim_gate.check(
        make_envelope({"task": "t", "worktree_lease": "l-1", "claim_ref": "c-1"}),
        seat_id="codex-x", resolver=FakeResolver(raises=failing_call),
    )
    assert outcome is not None, "fail-closed: a store failure must never admit"
    assert outcome.code == claim_gate.STORE_UNREACHABLE


def test_store_unreachable_is_not_reported_as_a_confirmation_failure():
    outcome = claim_gate.check(
        make_envelope({"task": "t", "claim_ref": "c-1"}),
        seat_id="codex-x", resolver=FakeResolver(raises="claim"),
    )
    assert outcome.code == claim_gate.STORE_UNREACHABLE
    assert outcome.code not in (claim_gate.UNCONFIRMED_CLAIM, claim_gate.UNKNOWN_CLAIM_REF)
    assert any("operator" in g or "unavailable" in g for g in outcome.gaps), (
        "a store outage must read as an operator page, not an accusation against the dispatcher"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_claim_gate.py -q`
Expected: FAIL — `AttributeError: module 'agent_redis_bridge.claim_gate' has no attribute 'StoreUnreachable'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to src/agent_redis_bridge/claim_gate.py

class StoreUnreachable(Exception):
    """The authority could not be consulted. ALWAYS refuses; never downgrades to admit."""


@dataclass(frozen=True)
class ClaimFacts:
    """One row of claim_admissibility_v, as seen by the gate."""

    confirmed_now: bool
    attested: bool
    decorrelation_provenance: str = "none"


def check(envelope, *, seat_id: str, resolver) -> GateOutcome | None:
    """Return None to admit, or a GateOutcome to refuse.

    Posture is a fact the STORE returns (spec §5, MUST). There is deliberately no local
    short-circuit: a seat able to answer "I am not posture-bearing" from its own config could
    disable the gate by editing a file it owns.
    """
    try:
        posture_required = resolver.seat_requires_claim_ref(seat_id)
    except StoreUnreachable as exc:
        return GateOutcome(
            code=STORE_UNREACHABLE,
            gaps=[f"posture unavailable for seat {seat_id}: {exc}", "operator action: the store is unavailable"],
        )
    if not posture_required:
        return None
    return None  # lane + claim resolution arrive in Tasks 3 and 4.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_claim_gate.py -q`
Expected: `test_seat_not_posture_bearing_is_admitted` PASSES; the three fail-closed tests still FAIL for `lane`/`claim` (only `posture` passes). This is expected — Tasks 3 and 4 close them. Do not weaken the tests to go green early.

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/claim_gate.py tests/test_claim_gate.py
git commit -m "feat(claim-gate): fail-closed posture resolution from the store"
```

---

### Task 3: Lane resolution — silence is never exemption

**Files:**
- Modify: `src/agent_redis_bridge/claim_gate.py`
- Test: `tests/test_claim_gate.py`

**Interfaces:**
- Consumes: `check()`, `LANE_NOT_ARMED_EXEMPT`, `StoreUnreachable` from Tasks 1–2.
- Produces: no new public names. `check()` now consults `resolver.lease_lane()` and returns `None`
  for store-confirmed exempt traffic.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_claim_gate.py

def test_store_confirmed_exempt_lease_admits_without_a_claim_ref():
    outcome = claim_gate.check(
        make_envelope({"task": "build the probe", "worktree_lease": "l-1"}),
        seat_id="codex-x", resolver=FakeResolver(lanes={"l-1": "exempt"}),
    )
    assert outcome is None


def test_no_lease_reference_is_gated_traffic_not_exempt():
    outcome = claim_gate.check(
        make_envelope({"task": "t"}), seat_id="codex-x", resolver=FakeResolver(),
    )
    assert outcome is not None, "spec §5.3 rule 1: silence is never exemption"
    assert outcome.code == claim_gate.MISSING_CLAIM_REF


def test_lease_the_store_does_not_record_is_gated_traffic():
    outcome = claim_gate.check(
        make_envelope({"task": "t", "worktree_lease": "l-unknown"}),
        seat_id="codex-x", resolver=FakeResolver(lanes={}),
    )
    assert outcome.code == claim_gate.MISSING_CLAIM_REF


def test_envelope_declaring_exempt_against_a_gated_lease_is_refused_by_name():
    outcome = claim_gate.check(
        make_envelope({"task": "t", "worktree_lease": "l-1", "lane": "exempt"}),
        seat_id="codex-x", resolver=FakeResolver(lanes={"l-1": "gated"}),
    )
    assert outcome.code == claim_gate.LANE_NOT_ARMED_EXEMPT
    assert any("consumer" in g for g in outcome.gaps), "gaps must route: lanes are armed, not asserted"


def test_envelope_declaring_exempt_cannot_admit_by_itself():
    """The self-attestation hole: a declared lane must never be sufficient."""
    outcome = claim_gate.check(
        make_envelope({"task": "t", "lane": "exempt"}), seat_id="codex-x",
        resolver=FakeResolver(lanes={}),
    )
    assert outcome is not None
    assert outcome.code == claim_gate.LANE_NOT_ARMED_EXEMPT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_claim_gate.py -q`
Expected: FAIL — the lane tests get `None` (admit) because `check()` still returns `None` unconditionally after the posture branch.

- [ ] **Step 3: Write minimal implementation**

Replace the trailing `return None  # lane + claim resolution ...` line with:

```python
    lease_id = envelope.payload.get("worktree_lease")
    declared_lane = envelope.payload.get("lane")  # routing metadata ONLY — never admits.

    lane = None
    if isinstance(lease_id, str) and lease_id:
        try:
            lane = resolver.lease_lane(lease_id)
        except StoreUnreachable as exc:
            return GateOutcome(
                code=STORE_UNREACHABLE,
                gaps=[f"lane unavailable for lease {lease_id}: {exc}", "operator action: the store is unavailable"],
            )

    if lane == "exempt":
        return None

    if declared_lane == "exempt":
        return GateOutcome(
            code=LANE_NOT_ARMED_EXEMPT,
            gaps=[
                f"envelope presents as exempt but the store records lane={lane!r} for lease {lease_id!r}",
                "lanes are armed by the consumer, not asserted by the dispatcher",
            ],
        )

    return None  # gated traffic; claim resolution arrives in Task 4.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_claim_gate.py -q -k lane`
Expected: the two `lane_not_armed_exempt` tests and the exempt-admit test PASS. The two `MISSING_CLAIM_REF` tests still FAIL — Task 4 closes them.

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/claim_gate.py tests/test_claim_gate.py
git commit -m "feat(claim-gate): lane resolves from the store; silence is never exemption"
```

---

### Task 4: Claim resolution — four distinct refusals

**Files:**
- Modify: `src/agent_redis_bridge/claim_gate.py`
- Test: `tests/test_claim_gate.py`

**Interfaces:**
- Consumes: `ClaimFacts`, all codes from Tasks 1–3.
- Produces: `check()` complete. No new public names.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_claim_gate.py

def facts(confirmed=True, attested=True, provenance="wire"):
    return claim_gate.ClaimFacts(
        confirmed_now=confirmed, attested=attested, decorrelation_provenance=provenance
    )


def test_admissible_claim_is_admitted():
    outcome = claim_gate.check(
        make_envelope({"task": "fix it", "claim_ref": "c-1"}), seat_id="codex-x",
        resolver=FakeResolver(claims={"c-1": facts()}),
    )
    assert outcome is None


def test_missing_claim_ref_routes_to_the_probe_lane():
    outcome = claim_gate.check(
        make_envelope({"task": "fix it"}), seat_id="codex-x", resolver=FakeResolver(),
    )
    assert outcome.code == claim_gate.MISSING_CLAIM_REF
    assert any("exempt" in g for g in outcome.gaps), "gaps must name the lane that WOULD be legitimate"


def test_unresolvable_ref_is_distinct_from_unconfirmed():
    outcome = claim_gate.check(
        make_envelope({"task": "t", "claim_ref": "c-typo"}), seat_id="codex-x",
        resolver=FakeResolver(claims={"c-1": facts()}),
    )
    assert outcome.code == claim_gate.UNKNOWN_CLAIM_REF


def test_unconfirmed_claim_routes_to_the_probe_lane():
    outcome = claim_gate.check(
        make_envelope({"task": "t", "claim_ref": "c-1"}), seat_id="codex-x",
        resolver=FakeResolver(claims={"c-1": facts(confirmed=False)}),
    )
    assert outcome.code == claim_gate.UNCONFIRMED_CLAIM
    assert any("exempt" in g for g in outcome.gaps)


def test_unattested_claim_routes_to_VERIFICATION_not_the_probe_lane():
    """Spec §5.1: the dispatcher's next action differs — this is why the codes are distinct."""
    outcome = claim_gate.check(
        make_envelope({"task": "t", "claim_ref": "c-1"}), seat_id="codex-x",
        resolver=FakeResolver(claims={"c-1": facts(attested=False)}),
    )
    assert outcome.code == claim_gate.UNATTESTED_CLAIM
    assert any("verification" in g.lower() for g in outcome.gaps)
    assert not any("build the probe" in g for g in outcome.gaps), (
        "an unattested claim already HAS its probe; routing it to the probe lane is the wrong instruction"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_claim_gate.py -q`
Expected: FAIL — claim tests return `None` (admit) because claim resolution is not implemented.

- [ ] **Step 3: Write minimal implementation**

Replace the trailing `return None  # gated traffic; claim resolution arrives in Task 4.` with:

```python
    claim_ref = envelope.payload.get("claim_ref")
    probe_route = "legitimate lane: exempt — build the probe first, then dispatch the remediation"

    if not isinstance(claim_ref, str) or not claim_ref.strip():
        return GateOutcome(
            code=MISSING_CLAIM_REF,
            gaps=[f"seat {seat_id} requires a claim_ref; none present on payload", probe_route],
        )

    try:
        found = resolver.claim(claim_ref)
    except StoreUnreachable as exc:
        return GateOutcome(
            code=STORE_UNREACHABLE,
            gaps=[f"claim {claim_ref} unavailable: {exc}", "operator action: the store is unavailable"],
        )

    if found is None:
        return GateOutcome(
            code=UNKNOWN_CLAIM_REF,
            gaps=[f"claim_ref {claim_ref!r} does not resolve", "check for a typo or a stale reference"],
        )

    if not found.confirmed_now:
        return GateOutcome(
            code=UNCONFIRMED_CLAIM,
            gaps=[f"claim {claim_ref} is not admissible as of now", probe_route],
        )

    if not found.attested:
        return GateOutcome(
            code=UNATTESTED_CLAIM,
            gaps=[
                f"claim {claim_ref} is confirmed but carries no complete cross-family attestation",
                "next action: verification — this claim already has its probe",
            ],
        )

    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_claim_gate.py -q`
Expected: PASS — all tests in the file, including the three fail-closed cases from Task 2.

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/claim_gate.py tests/test_claim_gate.py
git commit -m "feat(claim-gate): claim resolution with four distinct refusal codes"
```

---

### Task 5: Envelope type checks for the two new payload fields

**Files:**
- Modify: `src/agent_redis_bridge/envelope.py:61-81` (inside the `kind == "request"` branch)
- Test: `tests/test_envelope_claim_fields.py`

**Interfaces:**
- Consumes: `Envelope.from_json`, `EnvelopeError` (existing).
- Produces: `EnvelopeError("invalid-payload-claim_ref")` and `EnvelopeError("invalid-payload-lane")`.
  No semantics — the envelope layer never decides admissibility.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_envelope_claim_fields.py
import json

import pytest

from agent_redis_bridge.envelope import Envelope, EnvelopeError


def raw(payload):
    return json.dumps({
        "id": "e-1", "from": "claude-x", "branch": "dev", "to": "codex-x",
        "kind": "request", "sent_at": "2026-07-26T00:00:00+00:00", "payload": payload,
    })


def test_absent_claim_ref_is_still_a_valid_envelope():
    """The gate decides admission, not the envelope. Absence is a gate concern."""
    assert Envelope.from_json(raw({"task": "t"})).payload.get("claim_ref") is None


def test_valid_claim_ref_is_carried_through():
    env = Envelope.from_json(raw({"task": "t", "claim_ref": "c-1"}))
    assert env.payload["claim_ref"] == "c-1"


@pytest.mark.parametrize("bad", [42, "", "   ", None, [], {}])
def test_non_string_or_blank_claim_ref_is_rejected(bad):
    with pytest.raises(EnvelopeError, match="invalid-payload-claim_ref"):
        Envelope.from_json(raw({"task": "t", "claim_ref": bad}))


@pytest.mark.parametrize("bad", [42, "", "   ", None])
def test_non_string_or_blank_lane_is_rejected(bad):
    with pytest.raises(EnvelopeError, match="invalid-payload-lane"):
        Envelope.from_json(raw({"task": "t", "lane": bad}))


def test_envelope_does_not_validate_lane_VALUES():
    """'lane' is routing metadata; the store decides what a lane means (spec §5.3)."""
    env = Envelope.from_json(raw({"task": "t", "lane": "not-a-real-lane"}))
    assert env.payload["lane"] == "not-a-real-lane"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_envelope_claim_fields.py -q`
Expected: FAIL — the two `raises` tests fail because no validation exists yet.

- [ ] **Step 3: Write minimal implementation**

In `src/agent_redis_bridge/envelope.py`, inside the `if value["kind"] == "request":` block, after
the existing `fork_from_thread_id` checks and before the `has_thread_id` computation, add:

```python
            for optional_field in ("claim_ref", "lane"):
                if optional_field in payload:
                    supplied = payload[optional_field]
                    if not isinstance(supplied, str) or not supplied.strip():
                        raise EnvelopeError(f"invalid-payload-{optional_field}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_envelope_claim_fields.py -q && .venv/bin/python -m pytest tests/ -q -k envelope`
Expected: PASS, and no existing envelope test regresses.

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/envelope.py tests/test_envelope_claim_fields.py
git commit -m "feat(envelope): type-check optional claim_ref and lane payload fields"
```

---

### Task 6: Deny-proof the gate

**Files:**
- Test: `tests/test_claim_gate_deny_proof.py`

**Interfaces:**
- Consumes: everything from Tasks 1–4.
- Produces: nothing. This task adds only tests.

Per `docs/defect-classes/deny-proofs-need-adversarial-verification.md`, a green gate test is just
another green test. These assert the gate goes **red** when its mechanism is removed.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_claim_gate_deny_proof.py
"""Inject-revert proofs: remove the mechanism, the refusal MUST disappear.

If any of these pass with the check deleted, the corresponding test above is vacuous.
"""

from agent_redis_bridge import claim_gate
from tests.test_claim_gate import FakeResolver, facts, make_envelope


def test_gate_admits_nothing_by_default_across_the_whole_refusal_matrix():
    """Every non-admissible state must produce SOME refusal. A gap here is an open door."""
    non_admissible = [
        ({"task": "t"}, {}),                                                    # no ref
        ({"task": "t", "claim_ref": "c-x"}, {}),                                # unknown ref
        ({"task": "t", "claim_ref": "c-1"}, {"c-1": facts(confirmed=False)}),   # unconfirmed
        ({"task": "t", "claim_ref": "c-1"}, {"c-1": facts(attested=False)}),    # unattested
        ({"task": "t", "lane": "exempt"}, {}),                                  # self-declared lane
    ]
    for payload, claims in non_admissible:
        outcome = claim_gate.check(
            make_envelope(payload), seat_id="codex-x",
            resolver=FakeResolver(claims=claims),
        )
        assert outcome is not None, f"OPEN DOOR: {payload} was admitted"


def test_the_only_admitting_states_are_the_three_the_spec_names():
    """Admission has exactly three doors: no posture, store-armed exempt, admissible claim."""
    assert claim_gate.check(
        make_envelope({"task": "t"}), seat_id="s", resolver=FakeResolver(posture=False)
    ) is None
    assert claim_gate.check(
        make_envelope({"task": "t", "worktree_lease": "l-1"}), seat_id="s",
        resolver=FakeResolver(lanes={"l-1": "exempt"}),
    ) is None
    assert claim_gate.check(
        make_envelope({"task": "t", "claim_ref": "c-1"}), seat_id="s",
        resolver=FakeResolver(claims={"c-1": facts()}),
    ) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_claim_gate_deny_proof.py -q`
Expected: PASS immediately (Tasks 1–4 already implement this). **Then prove it is not vacuous:**
temporarily change the `if not found.attested:` branch in `claim_gate.py` to `if False:`, re-run,
and confirm `test_gate_admits_nothing_by_default_across_the_whole_refusal_matrix` FAILS with
"OPEN DOOR". Revert the change.

- [ ] **Step 3: Record the inject-revert result**

Add the observed failure line as a comment at the top of the deny-proof file, e.g.:

```python
# Inject-revert 2026-07-26: `if not found.attested:` -> `if False:` produced
#   AssertionError: OPEN DOOR: {'task': 't', 'claim_ref': 'c-1'} was admitted
# Proof is not vacuous.
```

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q -k "claim_gate or envelope"`
Expected: PASS, all green.

- [ ] **Step 5: Commit**

```bash
git add tests/test_claim_gate_deny_proof.py
git commit -m "test(claim-gate): inject-revert deny-proof for the refusal matrix"
```

---

## Self-Review

**Spec coverage (§5.1 and §5.3 only — the rest is slices 1b–1d):**

| Spec requirement | Task |
|---|---|
| Six distinct refusal codes | 1, 4 |
| `{outcome, exit_code, gaps}` house style | 1 |
| `gaps` names the legitimate lanes | 3, 4 |
| Fail-closed on store failure | 2 |
| No local posture short-circuit | 2 |
| Lane resolves from the store | 3 |
| Silence is never exemption | 3 |
| Declared lane never admits | 3, 6 |
| `unattested_claim` routes to verification, not the probe lane | 4 |
| `claim_ref` type check only in envelope | 5 |
| No caching | Global Constraints; `check()` holds no state |

**Known gap, deliberate:** `decorrelation_provenance` is carried on `ClaimFacts` but not acted on
here — it feeds the slice-2 sampler, not the gate. It is threaded now so the resolver contract does
not change later.

**Type consistency:** `check(envelope, *, seat_id, resolver)` and the three resolver methods
`seat_requires_claim_ref` / `lease_lane` / `claim` are named identically in Tasks 2, 3, 4, 6 and in
the slice-1c resolver contract. `ClaimFacts` field names match `claim_admissibility_v`'s output
columns (`confirmed_now`, `attested`, `decorrelation_provenance`) so the psycopg resolver in 1c is a
straight row-to-dataclass mapping.
