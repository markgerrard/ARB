# Bus-Side Gate — Slice 1c: psycopg resolver and bridge wiring

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the real PostgreSQL-backed resolver for Slice 1a's decision core, provision its
dedicated SELECT-only reader credential, and wire the decision into `Bridge.handle_raw` at the
authenticated-request boundary without enabling a fleet-wide lockout before Slice 1d exists.

**Architecture:** `PsycopgClaimResolver` owns one dedicated, autocommit
`arb_gate_reader` connection and maps the three Slice 1b views directly to Slice 1a's resolver
contract. `Bridge` owns that resolver at daemon scope so a later result-delivery caller can reuse it.
When enforcement is enabled, the bridge proves the reader is ready before registration; during
service, any failed read becomes `StoreUnreachable`, the failed connection is discarded, and that
request is refused. The next request reconnects. The gate governs executable dispatches: it runs
only after envelope, recipient, request-kind, timeout, and sender-policy validation, and before
duplicate suppression and usage budget. The two trusted, closed-schema lease lifecycle operations
(`worktree_arm` and `worktree_release`) remain outside the gate's subject because they start no
engine work and produce no diff; `worktree_run` remains gated.

**Tech Stack:** Python 3.11+, `psycopg` 3 with `dict_row`, PostgreSQL 17, pytest. Resolver in
`src/agent_redis_bridge/claim_resolver.py`; request-path wiring in
`src/agent_redis_bridge/bridge.py`; deployment grants through `python -m arb_memory grants`.

**Spec:** `docs/superpowers/specs/2026-07-26-bus-side-gate-design.md` — ARB Memory
`art-8742dfc1ca4b8be8` v6. §5 (admission and refusal codes), §5.3 (store-authoritative lane
resolution), §7 (verification identity), §9.3 (credential residual), and §11 (tests). Also read
`docs/defect-classes/refusal-is-ambient-assert-the-code.md` before writing bridge tests.

**Prerequisite — database-backed evidence must not skip.** Export the supplied owner DSN before
every verification command:

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
```

`tests/arb_memory/` skips without `ARB_MEMORY_DSN`; a skipped run proves nothing. Before Task 1,
confirm the current substrate and Slice 1a/1b baselines:

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
.venv/bin/python - <<'PY'
import os
import psycopg
with psycopg.connect(os.environ["ARB_MEMORY_DSN"]) as conn:
    row = conn.execute(
        "SELECT current_setting('server_version'), current_user, current_database(), "
        "EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'arb_gate_reader')"
    ).fetchone()
print(
    f"server={row[0]} user={row[1]} db={row[2]} "
    f"arb_gate_reader_exists={row[3]}"
)
PY
.venv/bin/python -m pytest tests/arb_memory/test_schema.py -q
.venv/bin/python -m pytest \
    tests/arb_memory/test_gate_schema.py \
    tests/arb_memory/test_gate_grants.py \
    tests/arb_memory/test_gate_store.py \
    tests/arb_memory/test_gate_schema_deny_proof.py -q
.venv/bin/python -m pytest \
    tests/test_claim_gate.py \
    tests/test_claim_gate_deny_proof.py \
    tests/defect_hunts/test_gate_assertions.py -q
```

Re-observed during plan remediation on 2026-07-27 in this worktree, not predicted:

```text
server=17.10 (Debian 17.10-1.pgdg12+1) user=arb_memory db=arb_memory arb_gate_reader_exists=False
4 passed in 0.81s
45 passed in 10.84s
25 passed in 0.04s
```

The bridge regression baseline was also run with the same exported DSN:

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
.venv/bin/python -m pytest \
    tests/test_bridge.py tests/test_bridge_handle_raw.py tests/test_envelope_claim_fields.py -q
.venv/bin/python -m pytest tests/arb_memory/test_run_grants.py -q
```

Observed:

```text
86 passed, 76 warnings, 10 subtests passed in 4.75s
1 passed in 0.38s
```

The warnings are the existing Redis `retry_on_timeout` deprecation at `bridge.py:542`, not a gate
failure.

## Global Constraints

- **The executable-dispatch insertion order is fixed by the live request path.**
  `Envelope.from_json` and the recipient, self-message, control, and request-kind checks run at
  `bridge.py:1163-1184`; turn-timeout validation runs at `bridge.py:1186-1191`; sender rejection
  runs at `bridge.py:1193-1199`; lifecycle duplicate handling begins at `bridge.py:1201`,
  duplicate suppression at `bridge.py:1206`, usage budget at `bridge.py:1210`, and
  `handle_worktree_operation` at `bridge.py:1216`. Classify the two exact lifecycle operations
  after the sender-rejection return, then run the claim gate for every other request before
  `durable_duplicate`. This means the gate sees a valid executable request for this seat from a
  non-rejected sender, while invalid/rejected traffic never touches Postgres and duplicate/budget
  logic cannot mask a gate refusal.
- **Lease lifecycle operations are outside the gate's subject, not admitted through an exception.**
  `worktree_arm` and `worktree_release` are trusted-sender-only, closed-schema lifecycle control;
  `handle_worktree_operation` consumes them, starts no engine, and produces no task diff. Use one
  shared exact operation set for classification and handler consumption so a lifecycle-shaped
  payload cannot fall through into engine work. Do not exempt any sender, posture, arbitrary
  operation, or `worktree_run`. Slice 1d must not reply success to arm until both the existing
  filesystem lease and its `lease_lanes` row are durable. If the row write fails after filesystem
  creation, 1d must compensate by reclaiming/tombstoning the lease and return failure. Release must
  tombstone/reclaim the filesystem lease and retire the row before replying success; a partial
  cleanup returns failure and remains reconcileable, while the unavailable filesystem lease
  prevents execution. This ordering is a Slice 1d prerequisite to fleet enablement, not an admit
  path to add in 1c.
- **The envelope layer is already complete for 1c.** `envelope.py:61-84` requires a task and performs
  type/non-blank checks for `claim_ref` and `lane`, while deliberately refusing to decide lane
  semantics. Do not add admissibility logic there.
- **Use Slice 1a; do not copy it.** `claim_gate.check` already owns all six codes and the three
  admission doors (`claim_gate.py:88-172`). `handle_raw` calls it once and translates its
  `GateOutcome` into the existing `send_reply(...); return False` early-refusal shape.
- **Every bridge refusal test asserts the exact code.** `ok=False`, a non-empty error, or a bare
  refusal is ambient and can stay green when the intended mechanism is deleted. The response error
  must begin with the expected Slice 1a code; `store_unreachable` must never be accepted as
  `unconfirmed_claim` or another legitimate claim refusal.
- **No Python copy of admissibility.** The resolver selects `confirmed_now`, `attested`, and
  `decorrelation_provenance` from `claim_admissibility_v` (`schema.sql:404-443`) and constructs
  `ClaimFacts` with those same names (`claim_gate.py:68-78`). It never selects raw `status` /
  `review_by` to recompute the predicate.
- **Missing and malformed rows fail closed.** A missing `seat_posture_v` row returns `True`
  (default-deny: require a claim); only an exact Boolean `False` may express a centrally configured
  open posture. A missing `lease_lane_v` row returns `None`; a missing
  `claim_admissibility_v` row returns `None`. A present row with a missing column, non-Boolean
  posture/claim flag, invalid lane/provenance domain, or any other mapping-contract failure becomes
  `StoreUnreachable` inside the resolver—never a truthiness-based admit.
  `claim_gate.check` then treats an unknown lease as gated traffic (`claim_gate.py:127-139`) and an
  unknown claim as `unknown_claim_ref` (`claim_gate.py:146-153`).
- **Unknown lease is not a store outage.** Slice 1d has not written any real `lease_lanes` rows.
  A successful SELECT with no row is `None`, not `StoreUnreachable`. With no declared exempt lane it
  falls through to `missing_claim_ref`; with `payload.lane == "exempt"` it produces
  `lane_not_armed_exempt`. A failed query/connection or a malformed present row is
  `store_unreachable`; absence alone is not.
- **No cache.** Spec §5.1 says no cache (or seconds only). At this volume, query the views on every
  gate evaluation so retraction/expiry is visible immediately.
- **One separate least-privilege connection.** Use only `ARB_GATE_READER_DSN`, never
  `ARB_MEMORY_DSN`, `ARB_MEMORY_MCP_DSN`, the Redis client, or a DSN derived by username replacement.
  The owner/writer DSN must never be a fallback. Read this secret from the supervisor's process
  environment, not the app-repo `--env-file`: `bridge.py:406-448` proves that file is loaded from
  the seat worktree, the exact local-control residual spec §9.3 warns about.
- **Connection lifecycle is daemon-owned and fail-closed.** The resolver object is created on
  `Bridge`, opens its connection during pre-registration readiness, and closes from
  `Bridge.cleanup`. A `psycopg.Error` during connect/query or an unexpected failure while validating
  or mapping a successfully fetched authority row discards the connection and raises
  `StoreUnreachable` without an in-request retry. The gate refuses that request; the next request
  reconnects. Keep this exception boundary inside the resolver, not as a broad catch in
  `handle_raw`. Use `autocommit=True` so a failed SELECT never leaves a shared aborted transaction.
- **Serialise access to the persistent connection.** Current admission calls occur on the inbox
  thread, but the backlogged result-delivery preview will use the same resolver from delivery code.
  Put connection/query/discard behind a resolver-owned lock now; do not bury a connection local
  inside `handle_raw`.
- **Pre-serve readiness proves identity and capability; a role-name override is not the
  guarantee.** When enforcement is enabled, before `register()` (`bridge.py:799`) require the DSN,
  connect, verify `current_user == ARB_GATE_READER_ROLE` (default `arb_gate_reader`), prove the
  connected role has SELECT on all three views, and prove it has no non-SELECT privilege on
  `claim_admissibility_v`, `seat_posture_v`, or `lease_lane_v`: check relation-level
  INSERT/UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER/MAINTAIN with `has_table_privilege` and
  column-level INSERT/UPDATE/REFERENCES with `has_any_column_privilege`. Independently use
  `has_table_privilege` to prove it has none of INSERT/UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER on
  `claims`, `attestations`, `seat_posture`, or `lease_lanes`. The view proof is load-bearing:
  `seat_posture_v` and `lease_lane_v` are automatically updatable, so clean base-table ACLs alone
  do not prevent the view owner from supplying base-table access for DML.
  Keep the role override for legitimate isolated deployment and test roles, but never let matching
  a configurable name substitute for the privilege proof. An owner DSN therefore fails even if
  `ARB_GATE_READER_ROLE` is configured to the owner's name. Missing role, wrong credential, missing
  view, missing SELECT, any non-SELECT view privilege, base-table write privilege, or unavailable
  database routes through `StoreUnreachable` before registration, so the daemon refuses to
  register or serve. This runtime ACL proof supplements rather than replaces the deployment-time
  membership/ownership isolation check below.
- **The grant helper is necessary but only valid after isolation.**
  `apply_gate_reader_grants` calls `assert_gate_role_isolation` at `grants.py:423-451`. That assertion
  rejects membership and ownership bypasses (`grants.py:364-420`); `NOINHERIT` alone is explicitly
  proven insufficient. Deployment must create a dedicated login with no memberships and no gate
  relation ownership, then run the helper as owner. Do not create the cluster-global role in
  `schema.sql` or in bridge startup.
- **Rollout is default OFF in Slice 1c, explicitly.** `BRIDGE_CLAIM_GATE` defaults to `0`. This is
  temporarily the only safe default: the spec says an enabled gate cannot ship before the exempt
  lane exists (`design.md:652-656`), and repo search finds `lease_lanes` only in schema/grant code,
  with no production INSERT writer. Slice 1d owns the consumer arm-time writer and the fleet
  transition to `BRIDGE_CLAIM_GATE=1`, including the two-record arm compensation above. Once an
  operator explicitly enables 1c on a canary, missing or broken reader readiness fails startup;
  there is no enabled-but-admit mode.
- **Keep disabled seats importable during the rollout.** `psycopg` currently lives only in the
  `arb-memory` extra (`pyproject.toml:20-31`), while `bridge.py` deliberately lazy-imports optional
  engine dependencies (`bridge.py:29-31`). Promote `psycopg[binary]>=3.1` to the core dependency for
  new installs, and still lazy-import `claim_resolver` only when the gate is enabled so already
  installed, gate-off seats do not break before their environments are refreshed.
- **Forward constraint, shaping only.** `docs/BACKLOG.md:1189-1236` will reuse the live reader at
  result delivery. Keep `self.claim_resolver` as a daemon-scoped dependency with public `claim()`
  and `close()` methods. Do not implement the preview, add reply-frame fields, or parse result claim
  refs in this slice.
- **Do not modify** `tests/arb_memory/test_schema.py` or the append-only Slice 1b deny-proof record in
  `tests/arb_memory/test_gate_schema_deny_proof.py`.

## File Structure

- **Create `src/agent_redis_bridge/claim_resolver.py`** — `PsycopgClaimResolver`, connection
  ownership/reconnect, readiness, and straight view-to-`ClaimFacts` mapping.
- **Modify `pyproject.toml`** — make `psycopg[binary]>=3.1` available to bridge installations,
  without duplicating it in the `arb-memory` extra.
- **Create `tests/test_claim_resolver.py`** — fake-connection unit coverage for mapping, defaults,
  connection failure, discard/reconnect, close, and readiness identity/capability probes.
- **Create `tests/arb_memory/test_claim_resolver.py`** — live-Postgres integration against a
  temporary isolated reader role, including empty `lease_lanes`, positive SELECT-only readiness,
  and post-grant view-DML drift refusal.
- **Modify `src/arb_memory/run.py`** — let the existing `grants` command apply
  `apply_gate_reader_grants` to an explicitly configured role; never create the role.
- **Modify `tests/arb_memory/test_run_grants.py`** — prove the out-of-band command applies the
  reader grant and refuses an unisolated role.
- **Create `tests/arb_memory/test_gate_deploy_shape.py`** — keep the documented reader-role,
  reader-DSN, isolation-check, and default-off deployment shape executable.
- **Modify `deploy/.env.example` and `deploy/README.md`** — document role provisioning, isolated
  grant verification, separate DSN handoff to seat supervisors, and the 1c default-off posture.
- **Modify `src/agent_redis_bridge/bridge.py`** — rollout config, daemon-owned resolver, readiness
  before registration, cleanup, and the `handle_raw` gate call.
- **Modify `tests/test_bridge.py`** — startup/default-off/secret-source/readiness/cleanup tests.
- **Modify `tests/conftest.py`** — scrub ambient gate configuration from bridge unit tests.
- **Create `tests/test_bridge_claim_gate.py`** — exact-code request-path integration and ordering.
- **Modify `tests/test_bridge_worktree_lease.py`** — enabled-gate lifecycle scope: arm/release
  remain lifecycle control while gated/exempt `worktree_run` requests are resolved.
- **Modify `tests/defect_hunts/test_gate_assertions.py`** — enrol the new bridge gate test module in
  the exact-code AST guard.

Not in this plan, by design:

- **Slice 1d:** exempt-lane credential, consumer arm-time `lease_lanes` write plus the
  filesystem/row compensation described above, brief-artefact dispatch, and worker hydration.
- **Slice 2:** close-time re-resolution and the sampler consuming
  `decorrelation_provenance` / `falsifier_kind`.
- **Result-delivery admissibility preview:** only preserve a reusable resolver boundary.

---

### Task 1: The real resolver — mapping, defaults, and connection recovery

**Files:**
- Create: `src/agent_redis_bridge/claim_resolver.py`
- Modify: `pyproject.toml`
- Test: `tests/test_claim_resolver.py`
- Test: `tests/arb_memory/test_claim_resolver.py`

**Interfaces:**
- Consumes: `claim_admissibility_v`, `seat_posture_v`, `lease_lane_v`; Slice 1a
  `ClaimFacts` / `StoreUnreachable`.
- Produces:
  `PsycopgClaimResolver(dsn, *, expected_role="arb_gate_reader", schema="public",
  connect=psycopg.connect)`, with `assert_ready()`, `seat_requires_claim_ref(seat_id)`,
  `lease_lane(lease_id)`, `claim(claim_ref)`, and `close()`.

- [ ] **Step 1: Write the failing unit tests**

Create `tests/test_claim_resolver.py`. Use explicit fake connections/cursors; no test may inherit a
real DSN. Cover:

```python
def test_missing_posture_defaults_to_requiring_a_claim():
    resolver = resolver_with_rows([None])
    assert resolver.seat_requires_claim_ref("unconfigured-seat") is True


def test_posture_accepts_only_exact_boolean_values():
    assert resolver_with_rows([{"requires_claim_ref": True}]).seat_requires_claim_ref("s") is True
    assert resolver_with_rows([{"requires_claim_ref": False}]).seat_requires_claim_ref("s") is False


@pytest.mark.parametrize("value", [None, 0, "", []])
def test_malformed_falsey_posture_is_store_unreachable_not_an_admit(value):
    resolver = resolver_with_rows([{"requires_claim_ref": value}])
    with pytest.raises(claim_gate.StoreUnreachable, match="requires_claim_ref"):
        resolver.seat_requires_claim_ref("s")


def test_unknown_lease_is_none_not_store_unreachable():
    assert resolver_with_rows([None]).lease_lane("not-written-by-1d") is None


def test_claim_maps_view_columns_without_recomputing():
    found = resolver_with_rows([{
        "confirmed_now": True,
        "attested": False,
        "decorrelation_provenance": "degraded",
    }]).claim("c-1")
    assert found == claim_gate.ClaimFacts(
        confirmed_now=True,
        attested=False,
        decorrelation_provenance="degraded",
    )


def test_query_failure_is_named_store_unreachable_and_next_call_reconnects():
    first = FakeConnection(error=psycopg.OperationalError("connection dropped"))
    second = FakeConnection(rows=[{"requires_claim_ref": False}])
    resolver = PsycopgClaimResolver("reader-dsn", connect=Factory(first, second))

    with pytest.raises(claim_gate.StoreUnreachable, match="connection dropped"):
        resolver.seat_requires_claim_ref("s")
    assert first.closed
    assert resolver.seat_requires_claim_ref("s") is False
    assert Factory.calls == 2


def test_readiness_checks_identity_and_all_three_views():
    resolver = resolver_with_role("arb_gate_reader")
    resolver.assert_ready()
    assert resolver.queried_views == {
        "claim_admissibility_v", "seat_posture_v", "lease_lane_v"
    }
    assert resolver.proved_select_on_all_views
    assert resolver.proved_no_non_select_on_all_views
    assert resolver.proved_no_write_on_all_base_tables


def test_readiness_refuses_owner_even_when_expected_role_matches_owner():
    resolver = resolver_with_role(
        "arb_memory",
        expected_role="arb_memory",
        base_table_write_privilege=True,
    )
    with pytest.raises(claim_gate.StoreUnreachable, match="write privilege"):
        resolver.assert_ready()
```

Also cover a missing posture key, an invalid present lane, a claim row with a missing key or
non-Boolean flag, and an invalid provenance value. Every present-row contract failure must raise
`StoreUnreachable`, close/discard the connection, and reconnect on the next call. Assert `close()`
closes once and remains idempotent, and assert normal lookup SQL selects only the published view
columns—never raw tables or a Python `status` predicate. The readiness privilege probe is the only
permitted base-table reference.

These tests lock specific mutations: returning posture by truthiness makes the `None`/`0` cases
admit; returning an unchecked mapping lets the missing-key/type/domain cases escape without a named
refusal; checking only `current_user == expected_role` lets the matching-owner case pass. The live
tests below—not fake privilege flags—separately own the mutation-kills for omitting the
relation-level and column-level halves of the non-SELECT view privilege proof.

- [ ] **Step 2: Run the unit tests to verify RED**

Run:

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
.venv/bin/python -m pytest tests/test_claim_resolver.py -q
```

Expected RED (prediction for the future TDD step, not an observed result):
collection fails with `ModuleNotFoundError: No module named
'agent_redis_bridge.claim_resolver'`.

- [ ] **Step 3: Write the live-Postgres tests before implementation**

Create `tests/arb_memory/test_claim_resolver.py`. Reuse `scratch`, create a unique temporary role,
run `apply_gate_reader_grants(scratch, role)`, and inject a connection factory that opens a separate
autocommit connection on the scratch schema then `SET ROLE`s to that role. Do not connect the
resolver as the owner for the positive integration case. First call `resolver.assert_ready()` and
require it to pass for this real SELECT-only role; then run the lookup assertions below.

Seed both posture values, both lane values, and three claim states. Then assert:

```python
assert resolver.seat_requires_claim_ref("missing-seat") is True
assert resolver.seat_requires_claim_ref("required-seat") is True
assert resolver.seat_requires_claim_ref("open-seat") is False

assert resolver.lease_lane("missing-lease") is None
assert resolver.lease_lane("gated-lease") == "gated"
assert resolver.lease_lane("exempt-lease") == "exempt"

assert resolver.claim("missing-claim") is None
assert resolver.claim("confirmed-attested") == ClaimFacts(True, True, "wire")
assert resolver.claim("confirmed-unattested") == ClaimFacts(True, False, "none")
```

Against a separate connection authenticated as the supplied owner, construct the resolver with
`expected_role` set to that connection's actual owner name. `assert_ready()` must still raise
`StoreUnreachable` because `has_table_privilege` observes write authority on the four base tables.
This is the live mutation-kill for deleting the negative-capability check while retaining the
configurable identity equality.

Add a separate live readiness mutation test using another unique, otherwise isolated temporary
reader role, named
`test_readiness_rejects_view_dml_drift_while_base_table_predicate_is_clean`. Apply
`apply_gate_reader_grants(scratch, role)` first, then, as the owner, grant
`INSERT, UPDATE, DELETE` on each of `seat_posture_v` and `lease_lane_v` (parameterize the view or
use independently provisioned roles) without granting any base-table DML. Through the reader
connection, execute the same `has_table_privilege` predicate used by readiness and assert that all
four base tables still report no INSERT/UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER privilege. Then
require `resolver.assert_ready()` to raise `StoreUnreachable` naming a non-SELECT view privilege,
before any bridge registration can occur. This test must kill exactly this mutation: delete only
the new view-DML capability check while leaving identity, SELECT, and base-table checks intact, and
the test must fail because `assert_ready()` now returns successfully. Reapply the real grants or
use a fresh isolated role for the normal positive case so it still proves a genuine SELECT-only
reader passes readiness.

Add another isolated live readiness mutation test, parameterized over exactly these view/column
pairs:

```python
@pytest.mark.parametrize(
    ("view_name", "column_name"),
    [
        ("seat_posture_v", "requires_claim_ref"),
        ("lease_lane_v", "lane"),
    ],
)
def test_readiness_rejects_column_only_view_update_drift(
    scratch, view_name, column_name
):
    ...
```

Provision a fresh unique temporary reader role for each parameter case and first apply
`apply_gate_reader_grants(scratch, role)`. As the owner, grant **only**
`UPDATE (<column_name>)` on `<view_name>` to that role—no relation-level view DML and no base-table
privilege. Through the reader connection, prove the mutation setup is specific to the
column-privilege half: `has_any_column_privilege(view_name, 'UPDATE')` is true, the exact
relation-level view non-SELECT predicate used by readiness reports false for all three gate views,
and the exact base-table write predicate reports false for all four base tables. Do not combine
this grant with the relation-level `INSERT, UPDATE, DELETE` case above. Then require
`resolver.assert_ready()` to raise the named non-SELECT-view `StoreUnreachable`. The positive
real-reader case remains unchanged and continues to use a clean, independently provisioned role.

This parameterized test owns one exact mutation: delete only the
`has_any_column_privilege` probe from `assert_ready()` while leaving identity, SELECT,
relation-level view, and base-table predicates intact. Both parameter cases must then fail because
`assert_ready()` returns successfully. Restore the column probe and require both cases to pass.
This mutation check is separate from the relation-level test's whole-view-DML mutation check; one
must not stand in for the other.

Use the real resolver with Slice 1a for the no-writer state:

```python
outcome = claim_gate.check(
    envelope({"task": "t", "worktree_lease": "not-written-by-1d"}),
    seat_id="required-seat",
    resolver=resolver,
)
assert outcome.code == claim_gate.MISSING_CLAIM_REF

presented = claim_gate.check(
    envelope({
        "task": "t",
        "worktree_lease": "not-written-by-1d",
        "lane": "exempt",
    }),
    seat_id="required-seat",
    resolver=resolver,
)
assert presented.code == claim_gate.LANE_NOT_ARMED_EXEMPT
```

The second assertion is load-bearing: a missing lease row is a normal `None`; the *gate*, not the
resolver, decides which exact refusal applies.

- [ ] **Step 4: Implement the resolver**

Promote `psycopg[binary]>=3.1` from the `arb-memory` extra into `[project].dependencies`. Keep
`pgvector` and the rest of ARB Memory optional.

Implement `claim_resolver.py` with this shape:

```python
from __future__ import annotations

import threading
from collections.abc import Callable

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from agent_redis_bridge import claim_gate
from arb_memory.mcp.grants import GATE_READER_ROLE


class PsycopgClaimResolver:
    def __init__(
        self,
        dsn: str,
        *,
        expected_role: str = GATE_READER_ROLE,
        schema: str = "public",
        connect: Callable = psycopg.connect,
    ) -> None:
        self._dsn = dsn
        self.expected_role = expected_role
        self.schema = schema
        self._connect_fn = connect
        self._conn = None
        self._lock = threading.Lock()

    def _connect(self):
        if self._conn is None or self._conn.closed:
            self._conn = self._connect_fn(
                self._dsn, autocommit=True, row_factory=dict_row
            )
        return self._conn

    def _read(self, statement, params=(), *, project):
        with self._lock:
            try:
                row = self._connect().execute(statement, params).fetchone()
            except psycopg.Error as exc:
                self._discard()
                raise claim_gate.StoreUnreachable(str(exc)) from exc
            if row is None:
                return None
            try:
                return project(row)
            except (KeyError, TypeError, ValueError) as exc:
                self._discard()
                raise claim_gate.StoreUnreachable(
                    f"authority row violates its published contract: {exc}"
                ) from exc

    def seat_requires_claim_ref(self, seat_id: str) -> bool:
        value = self._read(
            sql.SQL("SELECT requires_claim_ref FROM {} WHERE seat_id = %s").format(
                sql.Identifier(self.schema, "seat_posture_v")
            ),
            (seat_id,),
            project=self._validated_posture,
        )
        return True if value is None else value

    def lease_lane(self, lease_id: str) -> str | None:
        return self._read(
            sql.SQL("SELECT lane FROM {} WHERE lease_id = %s").format(
                sql.Identifier(self.schema, "lease_lane_v")
            ),
            (lease_id,),
            project=self._validated_lane,
        )

    def claim(self, claim_ref: str) -> claim_gate.ClaimFacts | None:
        return self._read(
            sql.SQL(
                "SELECT confirmed_now, attested, decorrelation_provenance "
                "FROM {} WHERE claim_id = %s"
            ).format(sql.Identifier(self.schema, "claim_admissibility_v")),
            (claim_ref,),
            project=self._validated_claim_facts,
        )
```

The three `_validated_*` projectors run inside `_read`'s resolver-owned lock. A mapping
`KeyError`/`TypeError`/`ValueError` is converted to `StoreUnreachable` after discarding the
connection. `_validated_posture` requires an exact Boolean; `_validated_lane` requires exactly
`gated` or `exempt`; and `_validated_claim_facts` requires exactly the three published keys, exact
Booleans for `confirmed_now` / `attested`, and provenance in
`{"none", "degraded", "wire"}` before constructing `ClaimFacts`. Do not add a broad catch to
`handle_raw`; unrelated programming errors outside this row boundary remain visible.

`assert_ready()` must check `current_user`, execute `SELECT <published columns> FROM <view> LIMIT 0`
for all three views and prove SELECT with `has_table_privilege`. For each view, prove no
relation-level INSERT/UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER/MAINTAIN with
`has_table_privilege`, and no column-level INSERT/UPDATE/REFERENCES with
`has_any_column_privilege`; together these are the no-non-SELECT-view predicate. Independently prove
no INSERT/UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER privilege on the four base tables. Treat view
and base-table probes as independent capability predicates; a clean base-table result must not
stand in for the view result. Route connect, permission, missing-view, wrong-role, and either
capability failure through `StoreUnreachable`, which startup propagates before registration.
`_discard()` closes best-effort and clears `_conn`. Do not retry inside `_read`; `close()` locks,
closes best-effort, and clears.

- [ ] **Step 5: Run unit and live tests to verify GREEN**

Run:

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
.venv/bin/python -m pytest \
    tests/test_claim_resolver.py \
    tests/arb_memory/test_claim_resolver.py -q
```

Expected GREEN (prediction): all resolver tests pass and **zero skip**. If the live test skips,
stop; the resolver-to-view mapping is not verified.

- [ ] **Step 5a: Verify the column-probe mutation is killed**

After the green run, temporarily delete only the `has_any_column_privilege` branch from
`assert_ready()`; leave the identity, SELECT, relation-level view, and base-table checks unchanged.
Run:

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
.venv/bin/python -m pytest \
    tests/arb_memory/test_claim_resolver.py \
    -k column_only_view_update_drift -q
```

Required RED: both parameter cases fail because `resolver.assert_ready()` returns instead of
raising the named non-SELECT-view `StoreUnreachable`. Restore the column probe, rerun the same
command, and require both cases to pass with zero skips. Review the final diff to confirm the
temporary mutation is absent before committing.

- [ ] **Step 6: Commit**

```bash
git add \
    pyproject.toml \
    src/agent_redis_bridge/claim_resolver.py \
    tests/test_claim_resolver.py \
    tests/arb_memory/test_claim_resolver.py
git commit -m "feat(claim-gate): add psycopg resolver"
```

---

### Task 2: Provision the isolated reader through the real deployment path

**Files:**
- Modify: `src/arb_memory/run.py`
- Modify: `tests/arb_memory/test_run_grants.py`
- Modify: `deploy/.env.example`
- Modify: `deploy/README.md`

**Interfaces:**
- Consumes: `GATE_READER_ROLE`, `apply_gate_reader_grants`, and
  `assert_gate_role_isolation` from `src/arb_memory/mcp/grants.py`.
- Produces: `ARB_GATE_READER_ROLE` support in `python -m arb_memory grants`; documented
  `ARB_GATE_READER_DSN` handoff to bridge supervisors.

The role does not exist on the live dev cluster as of the observed prerequisite query. This task
must not smuggle role creation into application code merely to make the next task start.

- [ ] **Step 1: Write the failing deployment-path tests**

Extend `tests/arb_memory/test_run_grants.py`:

1. Create a unique temporary gate role with no memberships.
2. Run `python -m arb_memory grants` in a subprocess with the scratch-schema DSN and
   `ARB_GATE_READER_ROLE=<temporary role>`.
3. Assert return code 0, SELECT on all three views, and no SELECT/INSERT/UPDATE/DELETE on the four
   base tables.
4. Give a second temporary role membership in a privilege-bearing parent, run the command, and
   assert nonzero with `GateRoleNotIsolated`; do not accept "the command failed" without checking
   the isolation reason.
5. Scrub ambient `ARB_GATE_READER_ROLE` / `ARB_GATE_READER_DSN` from the existing grants test's
   subprocess environment so an operator shell cannot change its subject.

Add a small static deploy-shape assertion (in the same test module or a focused
`tests/arb_memory/test_gate_deploy_shape.py`) that requires:

```python
assert "ARB_GATE_READER_ROLE" in deploy_env
assert "ARB_GATE_READER_DSN" in deploy_env
assert "assert_gate_role_isolation" in deploy_readme
assert "BRIDGE_CLAIM_GATE=0" in deploy_readme
```

- [ ] **Step 2: Run the tests to verify RED**

Run:

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
.venv/bin/python -m pytest \
    tests/arb_memory/test_run_grants.py \
    tests/arb_memory/test_gate_deploy_shape.py -q
```

Expected RED (prediction): the new privilege assertion fails because `run_grants()` currently
imports/calls no gate-reader helper (`run.py:313-360`), and the deploy files contain neither gate
variable.

- [ ] **Step 3: Wire the existing grants command**

In `run_grants()`:

```python
from arb_memory.mcp.grants import apply_gate_reader_grants

gate_reader_role = os.environ.get("ARB_GATE_READER_ROLE")
...
if gate_reader_role:
    apply_gate_reader_grants(conn, gate_reader_role)
```

Include the role in the final `grants applied:` line. Keep it conditional so today's unrelated
grant runs do not fail merely because the cluster-global role has not yet been provisioned. If
`ARB_GATE_READER_ROLE` is explicitly set and the role is missing, owns a gate relation, has any
membership, or cannot receive the grants, let the exception abort the command and prevent
`conn.commit()`.

Do not call `CREATE ROLE`. `apply_gate_reader_grants` already calls the isolation assertion; do not
weaken or duplicate that logic.

- [ ] **Step 4: Document the operator-owned provisioning order**

Add to `deploy/.env.example`:

```dotenv
# Gate role used only by bridge admission reads. Provision separately; never use owner DSN here.
ARB_GATE_READER_ROLE=arb_gate_reader
ARB_GATE_READER_DSN=postgresql://arb_gate_reader:<pw>@<host>:25060/<db>?sslmode=require
```

Update `deploy/README.md` with this exact order:

1. As cluster admin, create a dedicated LOGIN role with no memberships, no superuser/create-role/
   create-db/bypass-RLS capability, and ownership of no gate relation.
2. As the ARB Memory owner, set `ARB_GATE_READER_ROLE` and run
   `python -m arb_memory grants`. The command must abort if
   `assert_gate_role_isolation` fails.
3. Connect using the reader DSN itself. Prove SELECT on all three views and prove write failure on
   both `seat_posture_v` / `lease_lane_v` (automatically updatable views) and a base table.
4. Store `ARB_GATE_READER_DSN` in the seat supervisor's secret/process environment, not the
   app-repo `.env`, and install the refreshed package containing psycopg.
5. Leave `BRIDGE_CLAIM_GATE=0` for the fleet in Slice 1c. A canary may use `1` only after the
   readiness checks pass and test data gives it a viable claim path. Slice 1d owns fleet enablement
   after the exempt-lane writer lands.

State explicitly: do not restart a seat with enforcement on if any provisioning/negative check is
missing; Task 3 will make such a start fail before registration anyway.

- [ ] **Step 5: Run the deployment tests to verify GREEN**

Run:

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
.venv/bin/python -m pytest \
    tests/arb_memory/test_gate_grants.py \
    tests/arb_memory/test_run_grants.py \
    tests/arb_memory/test_gate_deploy_shape.py -q
```

Expected GREEN (prediction): all pass with zero skips. In particular, the unisolated-role case must
fail its inner grants subprocess for `GateRoleNotIsolated` while the pytest case passes.

- [ ] **Step 6: Commit**

```bash
git add \
    src/arb_memory/run.py \
    tests/arb_memory/test_run_grants.py \
    tests/arb_memory/test_gate_deploy_shape.py \
    deploy/.env.example \
    deploy/README.md
git commit -m "feat(claim-gate): provision isolated reader role"
```

---

### Task 3: Bridge-owned resolver, startup readiness, cleanup, and rollout default

**Files:**
- Modify: `src/agent_redis_bridge/bridge.py`
- Modify: `tests/test_bridge.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Consumes: `PsycopgClaimResolver`, process-env `ARB_GATE_READER_DSN`,
  `ARB_GATE_READER_ROLE`, and `BRIDGE_CLAIM_GATE`.
- Produces: `Bridge.claim_resolver`, `Bridge.claim_gate_enabled`, and
  `Bridge.enforce_claim_gate_ready()`.

- [ ] **Step 1: Write the failing startup/lifecycle tests**

Add focused tests to `tests/test_bridge.py`:

```python
def test_claim_gate_defaults_off(monkeypatch):
    monkeypatch.delenv("BRIDGE_CLAIM_GATE", raising=False)
    assert build_parser().parse_args([]).claim_gate is False


def test_claim_gate_can_be_enabled_only_explicitly(monkeypatch):
    monkeypatch.setenv("BRIDGE_CLAIM_GATE", "1")
    assert build_parser().parse_args([]).claim_gate is True


def test_missing_reader_dsn_never_falls_back_to_process_owner_dsns(monkeypatch):
    monkeypatch.setenv("BRIDGE_CLAIM_GATE", "1")
    monkeypatch.delenv("ARB_GATE_READER_DSN", raising=False)
    monkeypatch.setenv("ARB_MEMORY_DSN", "postgresql://owner@db/arb_memory")
    monkeypatch.setenv("ARB_MEMORY_MCP_DSN", "postgresql://writer@db/arb_memory")
    with mock.patch(
        "agent_redis_bridge.claim_resolver.PsycopgClaimResolver"
    ) as resolver_type:
        with pytest.raises(RuntimeError, match="ARB_GATE_READER_DSN"):
            make_env_bridge("")
    resolver_type.assert_not_called()


def test_owner_dsn_in_app_env_is_never_a_gate_reader_fallback(monkeypatch):
    monkeypatch.setenv("BRIDGE_CLAIM_GATE", "1")
    monkeypatch.delenv("ARB_GATE_READER_DSN", raising=False)
    with pytest.raises(RuntimeError, match="ARB_GATE_READER_DSN"):
        make_env_bridge("ARB_MEMORY_DSN=postgresql://owner@db/arb_memory\n")


def test_enabled_gate_builds_daemon_scoped_resolver_from_process_secret(...):
    monkeypatch.setenv("BRIDGE_CLAIM_GATE", "1")
    monkeypatch.setenv("ARB_GATE_READER_DSN", "postgresql://reader@db/arb_memory")
    monkeypatch.setenv("ARB_GATE_READER_ROLE", "arb_gate_reader")
    with mock.patch(
        "agent_redis_bridge.claim_resolver.PsycopgClaimResolver"
    ) as resolver_type:
        bridge = make_env_bridge("")
    resolver_type.assert_called_once_with(
        "postgresql://reader@db/arb_memory",
        expected_role="arb_gate_reader",
    )
    assert bridge.claim_resolver is resolver_type.return_value
```

The first no-fallback test deliberately populates both owner/writer process variables under the
same environment shape as the mandated DSN export. A mutant using
`get("ARB_GATE_READER_DSN") or get("ARB_MEMORY_DSN")` (or the MCP equivalent) would construct the
resolver and fail both the expected exception and `assert_not_called`; the test cannot pass merely
because the process already has an owner DSN.

Then prove startup order and cleanup:

```python
def test_reader_readiness_precedes_registration_and_engine_start(...):
    order = []
    bridge.claim_resolver.assert_ready.side_effect = lambda: order.append("reader-ready")
    bridge.register = lambda: order.append("register")
    bridge.start_engine = lambda: order.append("engine")
    bridge.inbox_loop = lambda: 0
    bridge.cleanup = lambda: None
    assert bridge.run() == 0
    assert order.index("reader-ready") < order.index("register")
    assert order.index("reader-ready") < order.index("engine")


def test_reader_readiness_failure_cleans_up_and_refuses_to_register(...):
    bridge.claim_resolver.assert_ready.side_effect = claim_gate.StoreUnreachable("denied")
    bridge.register = Mock(side_effect=AssertionError("must not register"))
    with pytest.raises(claim_gate.StoreUnreachable, match="denied"):
        bridge.run()
    bridge.claim_resolver.close.assert_called_once_with()


def test_cleanup_closes_claim_resolver(...):
    bridge.cleanup()
    bridge.claim_resolver.close.assert_called_once_with()
```

The second ordering assertion kills the mutation that starts the engine before reader readiness
while leaving registration after readiness.

Keep these tests hermetic by clearing `BRIDGE_CLAIM_GATE`, `ARB_GATE_READER_DSN`,
`ARB_GATE_READER_ROLE`, `ARB_MEMORY_DSN`, and `ARB_MEMORY_MCP_DSN` in the existing bridge autouse
containment fixture, then setting them only in the individual gate tests. In particular, the
no-fallback test re-injects the two owner/writer variables deliberately. Otherwise an operator's
enabled canary environment will silently turn every bridge unit fixture into a Postgres client or
make a security test depend on ambient credentials.

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
.venv/bin/python -m pytest tests/test_bridge.py -q
```

Expected RED (prediction): parser namespace has no `claim_gate`, enabled construction does not
require a reader DSN, and no readiness call exists.

- [ ] **Step 3: Implement the rollout and lifecycle**

Add a parser option whose default is the explicit process-env switch:

```python
parser.add_argument(
    "--claim-gate",
    action=argparse.BooleanOptionalAction,
    default=os.environ.get("BRIDGE_CLAIM_GATE", "0").lower() in {"1", "true", "yes"},
    help="Enforce the Postgres-backed dispatch claim gate (default off in Slice 1c).",
)
```

In `Bridge.__init__`, after `self.agent_id` is settled:

```python
self.claim_gate_enabled = bool(args.claim_gate)
self.claim_resolver = None
if self.claim_gate_enabled:
    reader_dsn = os.environ.get("ARB_GATE_READER_DSN")
    if not reader_dsn:
        raise RuntimeError(
            "claim gate enabled but ARB_GATE_READER_DSN is missing; refusing to serve"
        )
    from .claim_resolver import PsycopgClaimResolver
    from arb_memory.mcp.grants import GATE_READER_ROLE

    expected_role = os.environ.get("ARB_GATE_READER_ROLE", GATE_READER_ROLE)
    self.claim_resolver = PsycopgClaimResolver(
        reader_dsn, expected_role=expected_role
    )
```

Do not consult the parsed app env dict for either gate credential variable. Do not log the DSN.
The role override selects the identity to verify; it does not weaken Task 1's positive-SELECT,
negative-non-SELECT-view, and negative-base-table-write capability proof.

Add:

```python
def enforce_claim_gate_ready(self) -> None:
    if not self.claim_gate_enabled:
        return
    assert self.claim_resolver is not None
    self.claim_resolver.assert_ready()
    logger.info(f"[claim-gate] {self.agent_id} reader ready; enforcement active")
```

Call it in `run()` after `enforce_readonly_gate()` and before `reconcile_worktree_leases()` /
`register()` (`bridge.py:799`). The current `run()` enters its `try/finally` only after the
pre-serve checks, so widen that existing cleanup boundary to include the read-only check,
reader-readiness check, lease
reconciliation, registration, and self-test branch. Remove the self-test branch's explicit
`cleanup()` call when it becomes covered by `finally`; do not double-close. This is required for
the readiness-failure test above: a resolver that opened a connection and then failed its role/view
check must be closed even though registration never occurred.

In `cleanup()`, close the resolver best-effort without preventing Redis ownership cleanup or engine
shutdown. Keep the resolver reference on `Bridge`; `close()` is idempotent, so later cleanup
attempts are harmless.

Do not add a disabled resolver fake and do not instantiate a connection per request. Keeping the
real resolver on `Bridge` is the forward-compatible boundary the result-delivery preview needs.

- [ ] **Step 4: Run tests to verify GREEN**

Run:

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
.venv/bin/python -m pytest tests/test_bridge.py tests/test_claim_resolver.py -q
```

Expected GREEN (prediction): all pass. Confirm the default-off test executes with
`ARB_GATE_READER_DSN` absent; a hidden DSN fallback is a failure, not convenience.

- [ ] **Step 5: Commit**

```bash
git add src/agent_redis_bridge/bridge.py tests/test_bridge.py tests/conftest.py
git commit -m "feat(claim-gate): own reader lifecycle in bridge"
```

---

### Task 4: Wire `handle_raw` at the authenticated-request boundary

**Files:**
- Modify: `src/agent_redis_bridge/bridge.py`
- Create: `tests/test_bridge_claim_gate.py`
- Modify: `tests/test_bridge_worktree_lease.py`
- Modify: `tests/defect_hunts/test_gate_assertions.py`

**Interfaces:**
- Consumes: `claim_gate.check(envelope, seat_id, resolver)`.
- Produces: one exact lifecycle classification plus an early-refusal branch in `handle_raw`, after
  sender rejection and before duplicate / budget. Only `worktree_arm` / `worktree_release` are
  lifecycle control; every engine-dispatching request remains subject to the gate.

- [ ] **Step 1: Write the failing exact-code integration tests**

Create `tests/test_bridge_claim_gate.py`, reusing `FakeRedis`, `make_bridge`, and `request_json`
from `tests/test_bridge_handle_raw.py`. Enable the gate on the fixture by setting
`bridge.claim_gate_enabled = True` and injecting a fake resolver; do not open Postgres in these
request-order tests.

Parameterize all six refusal codes and their routing gaps through the real `claim_gate.check`:

| payload / resolver state | required response error prefix | required routing text |
|---|---|---|
| no claim ref / posture required | `missing_claim_ref` | `exempt` / `build the probe` |
| unknown claim ref | `unknown_claim_ref` | `typo` / `stale reference` |
| `ClaimFacts(confirmed_now=False, ...)` | `unconfirmed_claim` | `exempt` / `build the probe` |
| `ClaimFacts(confirmed_now=True, attested=False, ...)` | `unattested_claim` | `verification` |
| envelope declares exempt, unknown/gated lease | `lane_not_armed_exempt` | `armed by the consumer` |
| any resolver method raises `StoreUnreachable` | `store_unreachable` | `operator` / `authority is unavailable` |

For every case:

```python
bridge.pool.acquire = Mock(
    side_effect=AssertionError("engine work must not start")
)
bridge.handle_raw(request_json(request_id, payload=payload))

reply = json.loads(fake.replies[0][1])
assert reply["payload"]["ok"] is False
error = reply["payload"]["error"]
assert error.split(":", 1)[0] == expected_code
assert ":" in error
assert any(fragment in error for fragment in expected_route_fragments)
assert fake.events == []
```

For the store-outage case, additionally assert the prefix is *not*
`unconfirmed_claim`, `unattested_claim`, or `unknown_claim_ref`. Returning only the code from
`GateOutcome.as_error()`, or constructing the outcome with empty `gaps`, must make these bridge
tests fail.

Add one focused integration case that injects the real `PsycopgClaimResolver` with a fake
connection returning `{"requires_claim_ref": 0}`. `handle_raw` must reply with the exact
`store_unreachable` prefix and operator/authority routing, and engine acquisition must not start.
This kills the truthiness mutation at the actual request boundary; a unit-only exception assertion
is not enough to prove the dispatcher receives its named refusal.

Write explicit ordering tests:

```python
def test_rejected_sender_never_calls_the_store():
    resolver = ResolverThatRaises(AssertionError("gate must not run"))
    # unknown sender policy reject
    ...
    assert reply["payload"]["error"] == "sender rejected: unknown-agent"


def test_invalid_turn_timeout_never_calls_the_store():
    ...
    assert "turn_timeout" in reply["payload"]


def test_gate_refusal_precedes_duplicate_and_budget():
    bridge.is_duplicate = Mock(side_effect=AssertionError("duplicate check must be later"))
    bridge.check_usage_budget = Mock(side_effect=AssertionError("budget check must be later"))
    ...
    assert reply["payload"]["error"].startswith("missing_claim_ref")


def test_gate_admission_reaches_duplicate_then_budget():
    order = []
    bridge.claim_resolver = NonPostureBearingResolver(order)
    bridge.is_duplicate = lambda _id: order.append("duplicate") or False
    bridge.check_usage_budget = lambda: order.append("budget") or "budget-stop"
    ...
    assert order == ["posture", "duplicate", "budget"]
    assert reply["payload"]["error"] == "budget-stop"


def test_gate_off_request_path_never_calls_the_resolver():
    bridge.claim_gate_enabled = False
    bridge.claim_resolver = ResolverThatRaises(AssertionError("disabled gate touched resolver"))
    bridge.check_usage_budget = lambda: "budget-stop"
    ...
    assert reply["payload"]["error"] == "budget-stop"
```

The disabled-path test kills an accidental unconditional `claim_gate.check` call even though the
parser default remains off.

Extend `tests/test_bridge_worktree_lease.py` with enabled-gate integration coverage using its real
arm/run/release lifecycle:

1. A trusted `worktree_arm` succeeds without consulting a resolver that raises if called.
2. A `worktree_run` whose fake store resolves the lease as `exempt` reaches engine work without a
   claim.
3. A `worktree_run` whose fake store resolves the lease as `gated` is refused
   `missing_claim_ref` before engine acquisition.
4. `worktree_release` succeeds without consulting the resolver for leases represented as both
   `gated` and `exempt`.
5. A lifecycle-shaped payload with an extra task/claim field is rejected by the existing closed
   schema and never reaches engine work; classification is not a general bypass.

These tests kill the mutation that gates arm/release, the opposite mutation that exempts
`worktree_run`, and a drift where the classifier recognizes an operation the lifecycle handler
does not consume. They do not write the Slice 1d Postgres row.

Finally add `"tests/test_bridge_claim_gate.py"` to `GATE_TEST_FILES` in
`tests/defect_hunts/test_gate_assertions.py`. Extend the guard so `ok is False` (and equivalent
bridge-reply refusal styles) counts as a bare refusal unless the same test pins an error prefix via
`.split(":", 1)[0] == ...` or `.startswith(...)`. Add a second synthetic offender to
`test_the_guard_itself_is_not_vacuous` that contains only
`assert reply["payload"]["ok"] is False`, plus a compliant synthetic bridge assertion that pins the
prefix. The synthetic offender must be detected. Enrolment without matching markers is not
completion.

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
.venv/bin/python -m pytest \
    tests/test_bridge_claim_gate.py \
    tests/test_bridge_worktree_lease.py \
    tests/defect_hunts/test_gate_assertions.py -q
```

Expected RED (prediction): requests pass directly from sender policy to duplicate/budget because
`handle_raw` has no claim-gate call; exact-code assertions receive a later response or no response,
and the gated `worktree_run` case is not refused.

- [ ] **Step 3: Insert the gate call once**

Import the pure decision module (`from . import claim_gate`) at bridge module scope; it has no
database dependency. At the live `bridge.py:1193-1201` seam, after the `policy == "reject"` branch
returns and before `durable_duplicate`:

```python
operation = envelope.payload.get("operation")
is_worktree_lifecycle = operation in WORKTREE_LIFECYCLE_OPERATIONS

if self.claim_gate_enabled and not is_worktree_lifecycle:
    if self.claim_resolver is None:
        # Construction/readiness should make this impossible. Refuse rather than make a
        # missing dependency an admit path if a partial fixture or future refactor violates it.
        outcome = claim_gate.GateOutcome(
            code=claim_gate.STORE_UNREACHABLE,
            gaps=["claim resolver is unavailable", "operator action: restart the seat"],
        )
    else:
        outcome = claim_gate.check(
            envelope,
            seat_id=self.agent_id,
            resolver=self.claim_resolver,
        )
    if outcome is not None:
        logger.error(f"[bridge-error] {outcome.code} {outcome.gaps}")
        self.send_reply(
            envelope,
            TurnResult(ok=False, result="", error=outcome.as_error()),
        )
        return False
```

Define `WORKTREE_LIFECYCLE_OPERATIONS = frozenset({"worktree_arm", "worktree_release"})` once and
make `handle_worktree_operation` use the same constant. Its exact membership plus the handler's
trusted-policy check, closed schemas, and unconditional consumption are the boundary. Do not add a
general "admit" exception and do not include `worktree_run`.

Do not catch broad exceptions here. The resolver's public methods translate psycopg failures and
authority-row contract failures to `StoreUnreachable`, which `claim_gate.check` converts to the
named outcome. A programming error outside that resolver boundary must remain visible rather than
being mislabeled as an infrastructure outage.

Do not mark the request duplicate before the gate. `is_duplicate` mutates
`seen_request_ids` (`bridge.py:2934-2941`); moving it earlier would let an ungated first attempt
consume the ID and make a corrected retry disappear without re-evaluation.

- [ ] **Step 4: Run integration tests to verify GREEN**

Run:

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
.venv/bin/python -m pytest \
    tests/test_bridge_claim_gate.py \
    tests/test_bridge_worktree_lease.py \
    tests/test_bridge_handle_raw.py \
    tests/test_claim_gate.py \
    tests/test_claim_gate_deny_proof.py \
    tests/defect_hunts/test_gate_assertions.py -q
```

Expected GREEN (prediction): all pass. The six bridge cases must name six exact codes and routing
gaps; lifecycle tests must prove arm/release remain outside the subject while `worktree_run`
remains gated.

- [ ] **Step 5: Prove the integration checks can fail for the stated reason**

Temporarily replace the enabled `claim_gate.check(...)` call in `handle_raw` with
`outcome = None`, run only `tests/test_bridge_claim_gate.py`, record the observed exact-code
failures, then revert the source edit. This injection must show the bridge tests fail because the
expected Slice 1a code is absent, not merely because another early refusal happens.

Then temporarily move the gate block above `policy == "reject"` and run the two order tests. The
rejected-sender test must fail because the resolver was contacted. Revert.

Then temporarily remove `and not is_worktree_lifecycle`; the enabled arm/release tests must fail
because their raising resolver was contacted. Revert. Temporarily change the lifecycle constant to
include `worktree_run`; the gated-run test must fail because engine work is reached instead of the
exact `missing_claim_ref` refusal. Revert.

Temporarily reduce `GateOutcome.as_error()` to `return self.code`; the six routing assertions must
fail even though all exact prefix checks still pass. Revert. The Task 3 ordering test already kills
starting the engine before readiness, and its no-fallback test kills either owner-DSN fallback.

These are future implementation steps. Do not pre-fill an "observed" result in source or this plan;
paste the real pytest output into the implementation commit/report only after running it, per
`docs/defect-classes/prediction-written-as-result.md`.

Confirm every injection is gone by inspecting the intended Task 4 diff and rerunning the green
test. Relative to the Task 3 commit, the source diff must contain only the shared lifecycle
constant, its use by the existing handler, and the real gate block—no `outcome = None`, no moved
block above sender rejection, and no other temporary mutation:

```bash
git diff --check
git diff -- src/agent_redis_bridge/bridge.py
! rg -n "outcome = None" src/agent_redis_bridge/bridge.py
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
.venv/bin/python -m pytest tests/test_bridge_claim_gate.py -q
```

If the diff contains anything beyond the intended Task 4 wiring, or the focused test does not pass
with zero skips, stop before the commit. Also inspect the two test-file diffs; no temporary
membership or `as_error()` mutation may remain.

- [ ] **Step 6: Run the full affected suite**

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
.venv/bin/python -m pytest \
    tests/arb_memory/test_schema.py \
    tests/arb_memory/test_gate_schema.py \
    tests/arb_memory/test_gate_grants.py \
    tests/arb_memory/test_gate_store.py \
    tests/arb_memory/test_claim_resolver.py \
    tests/arb_memory/test_run_grants.py \
    tests/arb_memory/test_gate_deploy_shape.py \
    tests/test_envelope_claim_fields.py \
    tests/test_claim_resolver.py \
    tests/test_claim_gate.py \
    tests/test_claim_gate_deny_proof.py \
    tests/test_bridge.py \
    tests/test_bridge_handle_raw.py \
    tests/test_bridge_claim_gate.py \
    tests/test_bridge_worktree_lease.py \
    tests/defect_hunts/test_gate_assertions.py -q
```

Expected GREEN (prediction): all pass, zero skips. A summary containing `skipped` is not completion.

- [ ] **Step 7: Commit**

```bash
git add \
    src/agent_redis_bridge/bridge.py \
    tests/test_bridge_claim_gate.py \
    tests/test_bridge_worktree_lease.py \
    tests/defect_hunts/test_gate_assertions.py
git commit -m "feat(claim-gate): enforce admission in bridge request path"
```

---

## Self-Review

**Spec and brief coverage:**

| Requirement | Task |
|---|---|
| Real psycopg resolver | 1 |
| Straight `ClaimFacts` mapping; no predicate copy | 1 |
| Missing posture defaults gated | 1 |
| Malformed/falsey authority rows cannot admit or escape unnamed | 1 |
| Unknown lease returns `None`, then gates | 1 |
| Separate reader connection / DSN | 1, 3 |
| Mid-run failure becomes `StoreUnreachable` | 1 |
| Drop failed connection; reconnect next request | 1 |
| Reader identity cannot be configured to bless owner capability | 1, 3 |
| Readiness rejects non-SELECT privilege drift on all gate views | 1 |
| Live view-DML mutation kill leaves base-table predicate clean | 1 |
| Real SELECT-only reader still passes readiness | 1 |
| Reader reusable by future result delivery | 1, 3 |
| Real isolated role provisioning | 2 |
| `assert_gate_role_isolation` exercised in deployment path | 2 |
| Refuse startup without credential/readiness | 3 |
| Owner/writer DSNs are never reader fallbacks under the real test env | 3 |
| Default-off rollout before Slice 1d | 3 |
| Lifecycle arm/release outside gate subject; every engine dispatch gated | 4 |
| Exact executable `handle_raw` insertion order | 4 |
| Six specific refusal codes at bridge boundary | 4 |
| Refusal routing gaps survive the bridge translation | 4 |
| Store outage distinguishable from legitimate refusal | 4 |
| Gate-off request path never touches the resolver | 4 |
| Exact-code assertion style enrolled and mutation-proven in guard | 4 |
| Tests do not skip-green | Prerequisite, 1, 2, 4 |

**The five decisions the brief required:**

1. **Exact call location and subject:** after sender rejection, classify only
   `worktree_arm`/`worktree_release` as lifecycle control; gate every other request before
   duplicate/budget. Live `bridge.py:1163-1216` establishes valid request + seat + sender identity
   before that seam and begins mutable duplicate/budget behavior after it. The lifecycle handler is
   trusted-only, closed-schema, starts no engine, and shares the exact operation constant; this is
   a semantic boundary, not a general admit exception.
2. **Connection ownership:** daemon-scoped `PsycopgClaimResolver` on `Bridge`; supervisor-only
   `ARB_GATE_READER_DSN`; readiness before register; autocommit persistent connection; discard and
   named refusal on query or row-contract failure; reconnect on the next request; cleanup on daemon
   exit. Readiness proves the configured identity, SELECT on the published views, no non-SELECT
   privilege on those views, and no base-table write privilege, so neither a matching owner-role
   override nor DML drift on an automatically updatable view can pass.
3. **No Slice 1d lane writer:** resolver returns `None` for an unknown lease. Slice 1a treats silence
   as gated, producing `missing_claim_ref` or, when the envelope presents as exempt,
   `lane_not_armed_exempt`. It never defaults to exempt and never converts "no row" to outage.
   Lifecycle arm/release remain possible; 1d must make filesystem lease + row success atomic from
   the caller's perspective and compensate partial arm failure before fleet enablement.
4. **No deployed reader role:** Task 2 creates no role in code. Operator provisions an isolated
   cluster login, runs the existing grants command as owner, passes isolation plus positive/negative
   privilege checks, and only then supplies its DSN. Enforcement refuses startup without all of it.
5. **Rollout posture:** off by default in 1c because the spec forbids enabled rollout before the
   exempt lane and repo evidence shows no writer. Explicit enablement is fail-closed. Slice 1d owns
   the fleet switch after it supplies the missing lane path and two-record compensation.

**Deliberately out of scope:**

- Slice 1d's exempt credential, lease-lane write/compensation implementation, brief artefact, and
  hydration.
- Slice 2 close reconcile and sampling.
- The backlogged result-delivery preview. The resolver ownership boundary supports it; no preview
  behavior is added.

**Files explicitly preserved:** `tests/arb_memory/test_schema.py` and
`tests/arb_memory/test_gate_schema_deny_proof.py`.
