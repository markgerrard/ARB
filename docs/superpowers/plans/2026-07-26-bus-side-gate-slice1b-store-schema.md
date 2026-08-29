# Bus-Side Gate — Slice 1b: the store schema behind the gate

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create the four gate tables, the three views the gate reads, the `arb_gate_reader` role and its SELECT-only grant, and the consumer-side F4 harness-identity check — so that Slice 1a's `check()` has something real to resolve against.

**Architecture:** Slice 1a built a pure decision core with an injected resolver. This slice builds what the resolver will read. Admissibility is expressed **once**, in `claim_admissibility_v`, so the dispatch gate and the close gate cannot drift about what "confirmed" means. The trust asymmetry is the point: the bridge holds `arb_gate_reader` (three views, SELECT only, no table access) and structurally cannot mint confirmation state; only the consumer writes.

**Tech Stack:** PostgreSQL 17, `psycopg` 3, pytest. DDL in `src/arb_memory/schema.sql`; privilege statements in `src/arb_memory/mcp/grants.py`.

**Spec:** `docs/superpowers/specs/2026-07-26-bus-side-gate-design.md` — ARB Memory `art-8742dfc1ca4b8be8` v6. §4 (data model), §7.1 (F2/F4 provenance), §11 (testing). Read those three before starting.

**Prerequisite — this slice cannot be verified without a live database.** Every task here runs against Postgres. Export the DSN before starting:

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
```

Verified working 2026-07-26: PostgreSQL 17.10, user `arb_memory`, db `arb_memory`. Without it, `tests/arb_memory/` **skips**, and a skipped test is a vacuously-green test. Confirm it is live before Task 1:

```bash
.venv/bin/python -m pytest tests/arb_memory/test_schema.py -q   # must report 4 passed, NOT 4 skipped
```

## Global Constraints

- **Admissibility is expressed once (spec §4, MUST):** only `claim_admissibility_v` decides what
  "confirmed" means. No second copy of the predicate in Python, in the resolver, or in `close.py`.
- **`confirmed_now` and `attested` stay separately exposed (spec §4):** the gate must emit
  `unconfirmed_claim` and `unattested_claim` as **distinct** refusals, because the dispatcher's next
  action differs — build a probe versus route to verification.
- **Decorrelation folds into `attested`, not beside it (spec §4, F1):** a same-family attestation is
  *not an attestation*. It must read not-attested, never merely flagged.
- **The two subqueries share one WHERE clause (spec §4, MUST):** `attested` and
  `decorrelation_provenance` must filter an identical population. Drift here yields a claim that is
  `attested = false` but `provenance = 'wire'`.
- **`count(*) = 0` is tested FIRST in the provenance CASE (spec §4):** `bool_and` over zero rows is
  NULL, which would otherwise fall through to `'degraded'` and report a claim with **no** attestation
  as merely weakly-decorrelated.
- **The re-run reference is `NOT NULL` at the column, not in the view (spec §7.1, F2):** in the view
  it would be one view-rewrite from disappearing.
- **Expiry folds in at resolution (spec §4, MUST):** admissibility is "confirmed *as of now*"; the
  boundary is `review_by > now()`, so a claim expiring exactly now is **not** confirmed.
- **Everything in `schema.sql` must be re-appliable.** It is applied fresh per test schema
  (`tests/arb_memory/conftest.py:98`) *and* re-applied over itself
  (`tests/arb_memory/test_schema.py:30`).

### The two spec/repo conflicts this plan resolves — read before writing any SQL

The spec's §4 code block puts `CREATE ROLE arb_gate_reader LOGIN;` and a `GRANT` **inside**
`src/arb_memory/schema.sql`. **Do not do that.** Both were verified against this repo on 2026-07-26
and both break it:

1. **`schema.sql` is DDL-only, and a test enforces it.**
   `tests/arb_memory/test_schema.py:60-71` (`test_schema_does_not_manage_mcp_role`) asserts the
   literal substrings `"CREATE ROLE"`, `"ALTER ROLE"`, `"DROP ROLE"`, `"GRANT"` and `"REVOKE"` do
   **not** appear in the file. Its stated reason (lines 68-69): grants are applied out-of-band so
   that schema-apply stays decoupled from the role model. Roles are also cluster-global, so a
   `CREATE ROLE` inside a file applied into ~hundreds of throwaway per-test schemas is wrong twice.
   **The role and grant therefore go in `src/arb_memory/mcp/grants.py`** alongside
   `apply_mcp_grants` (`grants.py:57`), which is exactly the out-of-band mechanism referred to.

   > **Trap:** that assertion is a plain substring check over the whole file, **including comments**.
   > A helpful comment such as `-- the GRANT for this view lives in grants.py` will turn the suite
   > red. Task 1 gives comment wording that is safe.

2. **Bare `CREATE VIEW` is not re-appliable.** Verified directly against the dev database:
   a second `CREATE VIEW v AS ...` raises `DuplicateTable: relation "v" already exists`, while
   `CREATE OR REPLACE VIEW` run twice succeeds. Since `schema.sql` is re-applied over itself by
   `tests/arb_memory/test_schema.py:30`, **all three views MUST use `CREATE OR REPLACE VIEW`.**
   Note the standing limitation for later slices: `CREATE OR REPLACE VIEW` cannot change an existing
   view's column names, types, or order. These three views are new, so this is free today; a future
   slice that reshapes one must add an explicit `DROP VIEW`.

`schema.sql` currently contains **zero** views — these are the first, so there is no local precedent
to copy. Follow the constraint above rather than looking for one.

## File Structure

- **Modify `src/arb_memory/schema.sql`** — append the four tables and three views. DDL only: no
  role, no privilege statement, and none of the five forbidden substrings anywhere in the file.
- **Modify `src/arb_memory/mcp/grants.py`** — add `apply_gate_reader_grants(conn, role)` following
  the established `apply_*_grants(conn, role)` shape (`grants.py:6, 57, 124, 166, 246, 325`):
  resolve `current_schema()`, compose with `psycopg.sql.Identifier`, never f-string a role name.
- **Create `src/arb_memory/gate_store.py`** — the consumer's write path for attestations, holding
  the F4 harness-identity check and `HarnessIdentityRefused`. Separate module because F4 is a
  *cross-artefact* property: it must read `artefacts` to validate a row destined for `attestations`,
  which is exactly why it cannot be a column constraint (spec §7.1).
- **Create `tests/arb_memory/test_gate_schema.py`** — admissibility truth table, enum drift,
  column-level NOT NULL, population alignment.
- **Create `tests/arb_memory/test_gate_store.py`** — F4 refusal and admission.
- **Create `tests/arb_memory/test_gate_schema_deny_proof.py`** — inject-revert proofs.

Do **not** modify `tests/arb_memory/test_schema.py`. It is the guard that keeps `schema.sql`
DDL-only; a task that "fixes" it by relaxing the assertion has removed the mechanism instead of
satisfying it.

Not in this plan, by design:

- **Slice 1c — bridge wiring:** the real psycopg resolver implementing Slice 1a's `Resolver`
  protocol, plus the `handle_raw` insertion at `bridge.py:1193`.
- **Slice 1d — exempt lane + brief-artefact dispatch:** push-less worktree credential, the
  consumer-written `lease_lanes` row at arm time, store-before-send, worker-side hydration.
- **Slice 2 — the sampler** that consumes `decorrelation_provenance` and `falsifier_kind`. This
  slice only records those columns; nothing acts on them yet.

---

### Task 1: The four gate tables

**Files:**
- Modify: `src/arb_memory/schema.sql` (append at end)
- Test: `tests/arb_memory/test_gate_schema.py`

**Interfaces:**
- Consumes: nothing.
- Produces: tables `claims`, `attestations`, `seat_posture`, `lease_lanes`. Column names are load-
  bearing — `ClaimFacts` in Slice 1a already matches `confirmed_now`, `attested`,
  `decorrelation_provenance`, so the Slice 1c resolver is a straight row-to-dataclass mapping.

- [ ] **Step 1: Write the failing test**

```python
# tests/arb_memory/test_gate_schema.py
import os
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("pgvector")
pytestmark = pytest.mark.skipif(not os.environ.get("ARB_MEMORY_DSN"), reason="no ARB_MEMORY_DSN")

SCHEMA_SQL = Path(__file__).parents[2] / "src" / "arb_memory" / "schema.sql"


def test_gate_tables_exist(scratch):
    for table in ("claims", "attestations", "seat_posture", "lease_lanes"):
        oid = scratch.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0]
        assert oid is not None, f"table {table} missing"


def test_seat_posture_defaults_to_requiring_a_claim_ref(scratch):
    """Default-deny (spec §3): a seat nobody configured still requires a ref."""
    scratch.execute("INSERT INTO seat_posture (seat_id) VALUES ('codex-unconfigured')")
    row = scratch.execute(
        "SELECT requires_claim_ref FROM seat_posture WHERE seat_id = 'codex-unconfigured'"
    ).fetchone()
    assert row[0] is True


def test_claims_default_to_unconfirmed(scratch):
    scratch.execute(
        "INSERT INTO claims (claim_id, finding_ref, author_seat, author_family, "
        "author_family_provenance) VALUES ('c-1', 'f-1', 'codex-a', 'gpt', 'configured')"
    )
    assert scratch.execute("SELECT status FROM claims WHERE claim_id = 'c-1'").fetchone()[0] == (
        "unconfirmed"
    )


@pytest.mark.parametrize(
    "table,columns,values",
    [
        (
            "claims",
            "(claim_id, finding_ref, author_seat, author_family, author_family_provenance, status)",
            ("c-bad", "f-1", "codex-a", "gpt", "configured", "probably"),
        ),
        (
            "claims",
            "(claim_id, finding_ref, author_seat, author_family, author_family_provenance)",
            ("c-bad2", "f-1", "codex-a", "gpt", "vibes"),
        ),
        ("lease_lanes", "(lease_id, lane, armed_by)", ("l-bad", "semi-exempt", "consumer")),
    ],
)
def test_check_constraints_reject_out_of_domain_values(scratch, table, columns, values):
    """Enum drift: the views trust these strings, so an unconstrained column is a silent
    drift channel into `attested`."""
    placeholders = ", ".join(["%s"] * len(values))
    with pytest.raises(psycopg.errors.CheckViolation):
        scratch.execute(f"INSERT INTO {table} {columns} VALUES ({placeholders})", values)


def test_attestation_cannot_be_written_without_a_rerun_artefact(scratch):
    """F2 at the COLUMN, not the view: a row cannot exist without machinery contact."""
    scratch.execute(
        "INSERT INTO claims (claim_id, finding_ref, author_seat, author_family, "
        "author_family_provenance) VALUES ('c-2', 'f-2', 'codex-a', 'gpt', 'wire')"
    )
    with pytest.raises(psycopg.errors.NotNullViolation):
        scratch.execute(
            "INSERT INTO attestations (claim_id, verifier_seat, verifier_family, "
            "family_provenance, restatement, mechanism, falsifier, falsifier_kind) "
            "VALUES ('c-2', 'claude-b', 'claude', 'wire', 'r', 'm', 'f', 'command')"
        )


def test_schema_stays_ddl_only_after_the_gate_tables_land():
    """Regression guard for this plan specifically: the spec's §4 block contains a CREATE ROLE
    and a privilege statement. Neither may reach this file. Kept here as well as in
    test_schema.py so the failure names *this* slice as the cause."""
    text = SCHEMA_SQL.read_text(encoding="utf-8")
    for forbidden in ("CREATE ROLE", "ALTER ROLE", "DROP ROLE", "GRANT", "REVOKE"):
        assert forbidden not in text, (
            f"{forbidden} reached schema.sql - privilege statements belong in "
            f"src/arb_memory/mcp/grants.py (see test_schema.py:60-71)"
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/arb_memory/test_gate_schema.py -q`
Expected: FAIL. `test_gate_tables_exist` fails with `table claims missing`.

**If it reports `skipped` instead of `failed`, stop** — the DSN is not exported and nothing below
this line is being verified. Re-read the Prerequisite block.

- [ ] **Step 3: Write the DDL**

Append to `src/arb_memory/schema.sql`. Note the comment wording: it says "privilege statements"
and "the reader role", never the forbidden substrings.

```sql
-- Bus-side gate (spec art-8742dfc1ca4b8be8 v6 §4). DDL only: privilege statements and the
-- reader role are applied out-of-band by src/arb_memory/mcp/grants.py, per test_schema.py.
CREATE TABLE IF NOT EXISTS claims (
    claim_id     text PRIMARY KEY,
    finding_ref  text NOT NULL,
    status       text NOT NULL DEFAULT 'unconfirmed'
                 CHECK (status IN ('unconfirmed','confirmed','retracted')),
    severity     text,                                  -- ORDERING ONLY (see spec §8)
    -- Author identity (F1): the cross-family MUST in spec §7.3 is uncheckable without it.
    author_seat   text NOT NULL,
    author_family text NOT NULL,
    author_family_provenance text NOT NULL
                 CHECK (author_family_provenance IN ('wire','configured')),
    probe_artefact_id text,
    probe_artefact_version int,
    review_by    timestamptz,                           -- accepted-risk expiry; NULL = none
    confirmed_at timestamptz,
    confirmed_by text,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS attestations (
    claim_id        text NOT NULL REFERENCES claims(claim_id),
    verifier_seat   text NOT NULL,
    verifier_family text NOT NULL,
    family_provenance text NOT NULL
                    CHECK (family_provenance IN ('wire','configured')),
    restatement     text NOT NULL,          -- the claim in the verifier's own words
    mechanism       text NOT NULL,          -- which lines, which behaviour, why output entails defect
    falsifier       text NOT NULL,          -- what result would have falsified it
    falsifier_kind  text NOT NULL
                    CHECK (falsifier_kind IN ('command','prose')),
    -- Harness-produced record of the verifier's re-run (F2). NOT NULL at the COLUMN,
    -- not merely required by the view: a row cannot exist without machinery contact.
    -- F4: the CITED artefact's author is checked by the consumer at write time.
    rerun_artefact_id      text NOT NULL,
    rerun_artefact_version int  NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (claim_id, verifier_seat)
);

CREATE TABLE IF NOT EXISTS seat_posture (
    seat_id            text PRIMARY KEY,
    requires_claim_ref boolean NOT NULL DEFAULT true,   -- default-deny (spec §3)
    updated_at         timestamptz NOT NULL DEFAULT now()
);

-- Lane membership is a STORE fact, written by the consumer at arm time (F3, spec §5.3).
CREATE TABLE IF NOT EXISTS lease_lanes (
    lease_id  text PRIMARY KEY,
    lane      text NOT NULL CHECK (lane IN ('gated','exempt')),
    armed_by  text NOT NULL,                            -- consumer identity that armed it
    armed_at  timestamptz NOT NULL DEFAULT now()
);
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/arb_memory/test_gate_schema.py tests/arb_memory/test_schema.py -q`
Expected: PASS, all green. `test_schema.py` must still be **4 passed** — if
`test_schema_does_not_manage_mcp_role` went red, a forbidden substring reached the file.

- [ ] **Step 5: Commit**

```bash
git add src/arb_memory/schema.sql tests/arb_memory/test_gate_schema.py
git commit -m "feat(gate-store): claims, attestations, seat_posture, lease_lanes"
```

---

### Task 2: The three views and the admissibility truth table

**Files:**
- Modify: `src/arb_memory/schema.sql` (append after Task 1's tables)
- Test: `tests/arb_memory/test_gate_schema.py` (append)

**Interfaces:**
- Consumes: the four tables from Task 1.
- Produces: `claim_admissibility_v(claim_id, confirmed_now, attested, decorrelation_provenance,
  admissible, status, review_by)`, `seat_posture_v(seat_id, requires_claim_ref)`,
  `lease_lane_v(lease_id, lane)`. The Slice 1c resolver reads exactly these three and nothing else.

- [ ] **Step 1: Write the failing test**

Append to `tests/arb_memory/test_gate_schema.py`:

```python
def _claim(scratch, claim_id, *, status="confirmed", review_by=None,
           author_family="gpt", author_provenance="wire"):
    scratch.execute(
        "INSERT INTO claims (claim_id, finding_ref, status, author_seat, author_family, "
        "author_family_provenance, review_by) VALUES (%s, %s, %s, 'codex-a', %s, %s, %s)",
        (claim_id, f"f-{claim_id}", status, author_family, author_provenance, review_by),
    )


def _attest(scratch, claim_id, *, verifier_family="claude", provenance="wire",
            restatement="the door is unwired", mechanism="audit.py:13-20 never called",
            falsifier="a passing wire test", verifier_seat="claude-b"):
    scratch.execute(
        "INSERT INTO attestations (claim_id, verifier_seat, verifier_family, family_provenance, "
        "restatement, mechanism, falsifier, falsifier_kind, rerun_artefact_id, "
        "rerun_artefact_version) VALUES (%s, %s, %s, %s, %s, %s, %s, 'command', 'art-rerun', 1)",
        (claim_id, verifier_seat, verifier_family, provenance, restatement, mechanism, falsifier),
    )


def _row(scratch, claim_id):
    return scratch.execute(
        "SELECT confirmed_now, attested, decorrelation_provenance, admissible "
        "FROM claim_admissibility_v WHERE claim_id = %s",
        (claim_id,),
    ).fetchone()


def test_confirmed_and_cross_family_attested_is_admissible(scratch):
    _claim(scratch, "c-ok")
    _attest(scratch, "c-ok")
    assert _row(scratch, "c-ok") == (True, True, "wire", True)


@pytest.mark.parametrize("status", ["unconfirmed", "retracted"])
def test_non_confirmed_status_is_not_confirmed_now(scratch, status):
    _claim(scratch, "c-s", status=status)
    _attest(scratch, "c-s")
    confirmed_now, attested, _, admissible = _row(scratch, "c-s")
    assert (confirmed_now, attested, admissible) == (False, True, False)


def test_review_by_in_the_future_still_confirms(scratch):
    scratch.execute("INSERT INTO claims (claim_id, finding_ref, status, author_seat, "
                    "author_family, author_family_provenance, review_by) VALUES "
                    "('c-fut', 'f', 'confirmed', 'codex-a', 'gpt', 'wire', now() + interval '1 day')")
    _attest(scratch, "c-fut")
    assert _row(scratch, "c-fut")[0] is True


def test_review_by_in_the_past_expires_the_confirmation(scratch):
    scratch.execute("INSERT INTO claims (claim_id, finding_ref, status, author_seat, "
                    "author_family, author_family_provenance, review_by) VALUES "
                    "('c-exp', 'f', 'confirmed', 'codex-a', 'gpt', 'wire', now() - interval '1 day')")
    _attest(scratch, "c-exp")
    confirmed_now, _, _, admissible = _row(scratch, "c-exp")
    assert (confirmed_now, admissible) == (False, False)


def test_review_by_exactly_at_now_is_expired_not_confirmed(scratch):
    """The boundary is `review_by > now()`, strictly. A claim expiring exactly now is expired.
    Pinned with a fixed timestamp rather than now() so the assertion cannot race the clock."""
    scratch.execute(
        "INSERT INTO claims (claim_id, finding_ref, status, author_seat, author_family, "
        "author_family_provenance, review_by) VALUES "
        "('c-bnd', 'f', 'confirmed', 'codex-a', 'gpt', 'wire', %s)",
        ("2020-01-01 00:00:00+00",),
    )
    _attest(scratch, "c-bnd")
    assert _row(scratch, "c-bnd")[0] is False


def test_no_attestation_reads_not_attested_and_provenance_none(scratch):
    _claim(scratch, "c-bare")
    assert _row(scratch, "c-bare") == (True, False, "none", False)


def test_same_family_attestation_is_not_an_attestation_at_all(scratch):
    """F1: decorrelation folds INTO attested. Must read not-attested, not merely flagged."""
    _claim(scratch, "c-same", author_family="gpt")
    _attest(scratch, "c-same", verifier_family="gpt")
    confirmed_now, attested, provenance, admissible = _row(scratch, "c-same")
    assert (attested, provenance, admissible) == (False, "none", False)


@pytest.mark.parametrize("blank_field", ["restatement", "mechanism", "falsifier"])
def test_incomplete_attestation_does_not_count(scratch, blank_field):
    _claim(scratch, "c-inc")
    _attest(scratch, "c-inc", **{blank_field: ""})
    assert _row(scratch, "c-inc")[1] is False


def test_seat_posture_v_and_lease_lane_v_expose_only_their_two_columns(scratch):
    scratch.execute("INSERT INTO seat_posture (seat_id, requires_claim_ref) VALUES ('s-1', false)")
    scratch.execute("INSERT INTO lease_lanes (lease_id, lane, armed_by) "
                    "VALUES ('l-1', 'exempt', 'consumer')")
    assert scratch.execute(
        "SELECT requires_claim_ref FROM seat_posture_v WHERE seat_id = 's-1'"
    ).fetchone()[0] is False
    assert scratch.execute(
        "SELECT lane FROM lease_lane_v WHERE lease_id = 'l-1'"
    ).fetchone()[0] == "exempt"
    for view, expected in (("seat_posture_v", {"seat_id", "requires_claim_ref"}),
                           ("lease_lane_v", {"lease_id", "lane"})):
        cols = {r[0] for r in scratch.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s", (view,)).fetchall()}
        assert cols == expected, f"{view} exposes {cols - expected} beyond its contract"


def test_schema_sql_is_reappliable(scratch):
    """schema.sql is re-applied over itself by test_schema.py:30. Bare CREATE VIEW raises
    DuplicateTable; this is the regression test for that."""
    scratch.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
    scratch.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/arb_memory/test_gate_schema.py -q -k "admissib or attest or review_by or posture_v or reappliable"`
Expected: FAIL with `psycopg.errors.UndefinedTable: relation "claim_admissibility_v" does not exist`.

- [ ] **Step 3: Write the views**

Append to `src/arb_memory/schema.sql`. **`CREATE OR REPLACE`, not `CREATE`** — see the conflict
note in Global Constraints.

```sql
-- Admissibility expressed ONCE, here, so the dispatch gate and the close gate
-- cannot drift about what "confirmed" means.
CREATE OR REPLACE VIEW claim_admissibility_v AS
SELECT c.claim_id,
       ok.confirmed_now,
       ok.attested,
       ok.decorrelation_provenance,
       (ok.confirmed_now AND ok.attested) AS admissible,
       c.status, c.review_by
FROM claims c
CROSS JOIN LATERAL (
    SELECT
        (c.status = 'confirmed'
         AND (c.review_by IS NULL OR c.review_by > now()))            AS confirmed_now,
        -- Completeness AND decorrelation, in one predicate: an attestation from the
        -- author's own family does not count as an attestation at all (F1).
        EXISTS (SELECT 1 FROM attestations a
                WHERE a.claim_id = c.claim_id
                  AND a.restatement <> '' AND a.mechanism <> ''
                  AND a.falsifier <> ''
                  AND a.verifier_family <> c.author_family)           AS attested,
        -- 'wire' only when BOTH sides are machinery-attested; otherwise the
        -- decorrelation claim is degraded and the sampler weights it up (spec §7.4).
        -- count(*) = 0 FIRST: bool_and over zero rows is NULL, which would otherwise
        -- fall through to 'degraded' and report a claim with NO attestation as merely
        -- weakly-decorrelated. An aggregate subquery always returns a row, so COALESCE
        -- would never have fired.
        -- The WHERE clause MUST stay identical to `attested`'s: two subqueries measuring
        -- subtly different populations is drift waiting to be resolved in whichever
        -- direction is convenient. Without the completeness predicates an INCOMPLETE
        -- cross-family attestation yields attested=false alongside provenance='wire'.
        (SELECT CASE
                    WHEN count(*) = 0 THEN 'none'
                    WHEN bool_and(a.family_provenance = 'wire')
                         AND c.author_family_provenance = 'wire'
                    THEN 'wire' ELSE 'degraded' END
         FROM attestations a
         WHERE a.claim_id = c.claim_id
           AND a.restatement <> '' AND a.mechanism <> ''
           AND a.falsifier <> ''
           AND a.verifier_family <> c.author_family)                  AS decorrelation_provenance
) AS ok;

-- Seat posture lives behind the DSN, NOT in the seat's env file (spec §9.3).
CREATE OR REPLACE VIEW seat_posture_v AS
SELECT seat_id, requires_claim_ref FROM seat_posture;

CREATE OR REPLACE VIEW lease_lane_v AS
SELECT lease_id, lane FROM lease_lanes;
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/arb_memory/test_gate_schema.py tests/arb_memory/test_schema.py -q`
Expected: PASS, all green.

- [ ] **Step 5: Commit**

```bash
git add src/arb_memory/schema.sql tests/arb_memory/test_gate_schema.py
git commit -m "feat(gate-store): admissibility, posture and lane views"
```

---

### Task 3: Population alignment — the two subqueries must not drift

**Files:**
- Test: `tests/arb_memory/test_gate_schema.py` (append)

**Interfaces:**
- Consumes: `claim_admissibility_v` from Task 2.
- Produces: nothing. Tests only.

This is its own task because it is the regression test for a defect the spec says *already happened
once* — the two subqueries measuring different populations. A reviewer could reasonably accept
Task 2 and reject this, or vice versa.

- [ ] **Step 1: Write the test**

```python
def test_incomplete_cross_family_attestation_is_never_wire(scratch):
    """Population alignment. If the provenance subquery loses the completeness predicates,
    this reads (False, 'wire') - attested=false alongside a full-strength provenance claim."""
    _claim(scratch, "c-align", author_provenance="wire")
    _attest(scratch, "c-align", verifier_family="claude", provenance="wire", mechanism="")
    _, attested, provenance, _ = _row(scratch, "c-align")
    assert attested is False
    assert provenance != "wire", (
        "provenance subquery counted a row `attested` rejected - the two WHERE clauses drifted"
    )
    assert provenance == "none"


@pytest.mark.parametrize(
    "author_provenance,verifier_provenance,expected",
    [
        ("wire", "wire", "wire"),
        ("configured", "wire", "degraded"),
        ("wire", "configured", "degraded"),
        ("configured", "configured", "degraded"),
    ],
)
def test_decorrelation_provenance_degrades_when_either_side_is_configured(
    scratch, author_provenance, verifier_provenance, expected
):
    _claim(scratch, "c-prov", author_provenance=author_provenance)
    _attest(scratch, "c-prov", provenance=verifier_provenance)
    assert _row(scratch, "c-prov")[2] == expected


def test_multiple_attestations_degrade_if_any_is_configured(scratch):
    """bool_and over the counting population: one configured attestation degrades the set."""
    _claim(scratch, "c-multi", author_provenance="wire")
    _attest(scratch, "c-multi", verifier_seat="claude-b", provenance="wire")
    _attest(scratch, "c-multi", verifier_seat="grok-c", verifier_family="grok",
            provenance="configured")
    _, attested, provenance, _ = _row(scratch, "c-multi")
    assert (attested, provenance) == (True, "degraded")
```

- [ ] **Step 2: Run tests**

Run: `.venv/bin/python -m pytest tests/arb_memory/test_gate_schema.py -q -k "align or provenance or multi"`
Expected: PASS — Task 2's view already satisfies these. **They are not proven non-vacuous until
Task 6.** Do not treat this green as evidence yet.

- [ ] **Step 3: Commit**

```bash
git add tests/arb_memory/test_gate_schema.py
git commit -m "test(gate-store): population alignment between attested and provenance"
```

---

### Task 4: The reader role and its SELECT-only grant

**Files:**
- Modify: `src/arb_memory/mcp/grants.py` (append)
- Test: `tests/arb_memory/test_gate_grants.py` (create)

**Interfaces:**
- Consumes: the three views from Task 2.
- Produces: `apply_gate_reader_grants(conn, role: str) -> None` and
  `GATE_READER_ROLE = "arb_gate_reader"`. Slice 1c's deployment path calls these.

The GRANT is the trust story in one statement: three views, SELECT only, no table access. A leaked
bridge credential can read confirmation state and structurally cannot mint any.

- [ ] **Step 1: Write the failing test**

```python
# tests/arb_memory/test_gate_grants.py
import os

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("pgvector")
pytestmark = pytest.mark.skipif(not os.environ.get("ARB_MEMORY_DSN"), reason="no ARB_MEMORY_DSN")

from arb_memory.mcp.grants import GATE_READER_ROLE, apply_gate_reader_grants  # noqa: E402


def _ensure_role(conn, role):
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
    if exists:
        return True
    try:
        conn.execute(f'CREATE ROLE "{role}" LOGIN')
        return True
    except psycopg.errors.InsufficientPrivilege:
        conn.rollback()
        return False


def test_gate_reader_can_select_the_three_views_and_nothing_else(scratch):
    role = f"{GATE_READER_ROLE}_test"
    if not _ensure_role(scratch, role):
        pytest.skip("cannot create roles on this credential")
    try:
        apply_gate_reader_grants(scratch, role)
        schema = scratch.execute("SELECT current_schema()").fetchone()[0]
        for view in ("claim_admissibility_v", "seat_posture_v", "lease_lane_v"):
            assert scratch.execute(
                "SELECT has_table_privilege(%s, %s, 'SELECT')",
                (role, f'"{schema}".{view}'),
            ).fetchone()[0] is True, f"{view} not readable by the gate reader"

        for table in ("claims", "attestations", "seat_posture", "lease_lanes"):
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert scratch.execute(
                    "SELECT has_table_privilege(%s, %s, %s)",
                    (role, f'"{schema}".{table}', priv),
                ).fetchone()[0] is False, (
                    f"gate reader holds {priv} on base table {table} - only the consumer writes"
                )

        for view in ("claim_admissibility_v", "seat_posture_v", "lease_lane_v"):
            for priv in ("INSERT", "UPDATE", "DELETE"):
                assert scratch.execute(
                    "SELECT has_table_privilege(%s, %s, %s)",
                    (role, f'"{schema}".{view}', priv),
                ).fetchone()[0] is False, f"gate reader holds {priv} on {view}"
    finally:
        scratch.execute(f'DROP OWNED BY "{role}" CASCADE')
        scratch.execute(f'DROP ROLE IF EXISTS "{role}"')


def test_gate_reader_cannot_read_artefacts_or_hints(scratch):
    """Scope check: the gate credential is not a general store reader."""
    role = f"{GATE_READER_ROLE}_scope_test"
    if not _ensure_role(scratch, role):
        pytest.skip("cannot create roles on this credential")
    try:
        apply_gate_reader_grants(scratch, role)
        schema = scratch.execute("SELECT current_schema()").fetchone()[0]
        for table in ("artefacts", "hints"):
            assert scratch.execute(
                "SELECT has_table_privilege(%s, %s, 'SELECT')",
                (role, f'"{schema}".{table}'),
            ).fetchone()[0] is False, f"gate reader can read {table}"
    finally:
        scratch.execute(f'DROP OWNED BY "{role}" CASCADE')
        scratch.execute(f'DROP ROLE IF EXISTS "{role}"')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/arb_memory/test_gate_grants.py -q`
Expected: FAIL at import — `ImportError: cannot import name 'GATE_READER_ROLE'`.

- [ ] **Step 3: Write the implementation**

Append to `src/arb_memory/mcp/grants.py`:

```python
GATE_READER_ROLE = "arb_gate_reader"


def apply_gate_reader_grants(conn, role: str) -> None:
    """SELECT on the three gate views and nothing else.

    The bridge holds this role. It can read confirmation state, posture and lane, and
    structurally cannot mint any of them - only the consumer writes the base tables.
    Role creation is deliberately NOT here: roles are cluster-global and schema.sql is
    applied per-schema, so creation belongs to the deployment step that owns the cluster.
    """
    schema = conn.execute("SELECT current_schema()").fetchone()[0]
    role_ident = sql.Identifier(role)
    schema_ident = sql.Identifier(schema)
    views = [
        sql.Identifier(schema, "claim_admissibility_v"),
        sql.Identifier(schema, "seat_posture_v"),
        sql.Identifier(schema, "lease_lane_v"),
    ]
    tables = [
        sql.Identifier(schema, name)
        for name in ("claims", "attestations", "seat_posture", "lease_lanes")
    ]

    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema_ident, role_ident))
    conn.execute(
        sql.SQL("GRANT SELECT ON {} TO {}").format(sql.SQL(", ").join(views), role_ident)
    )
    conn.execute(
        sql.SQL("REVOKE INSERT, UPDATE, DELETE ON {} FROM {}").format(
            sql.SQL(", ").join(views), role_ident
        )
    )
    conn.execute(
        sql.SQL("REVOKE ALL ON {} FROM {}").format(sql.SQL(", ").join(tables), role_ident)
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/arb_memory/test_gate_grants.py tests/arb_memory/test_schema.py -q`
Expected: PASS. If both grant tests report `skipped`, the DSN credential cannot create roles —
note it and move on; the grant is then unverified on this host and must be recorded as such.

- [ ] **Step 5: Commit**

```bash
git add src/arb_memory/mcp/grants.py tests/arb_memory/test_gate_grants.py
git commit -m "feat(gate-store): arb_gate_reader select-only grant on the three views"
```

---

### Task 5: F4 — the cited re-run must be harness-authored

**Files:**
- Create: `src/arb_memory/gate_store.py`
- Test: `tests/arb_memory/test_gate_store.py`

**Interfaces:**
- Consumes: `attestations` (Task 1); `arb_memory.store.fetch_artefact(conn, artefact_id, version)`
  (`src/arb_memory/store.py:248`), which returns a dict or `None`.
- Produces: `HarnessIdentityRefused(Exception)` with `.author` and `.artefact_ref`;
  `HARNESS_AUTHORS: frozenset[str]`; `insert_attestation(conn, *, claim_id, verifier_seat,
  verifier_family, family_provenance, restatement, mechanism, falsifier, falsifier_kind,
  rerun_artefact_id, rerun_artefact_version, harness_authors=HARNESS_AUTHORS) -> None`.

`rerun_artefact_id NOT NULL` proves a *pointer exists*, not that what it points at is
machinery-produced. A verifier could store its own transcript and cite that, reinstating self-report
one pointer deeper. The store already records authorship (`schema.sql:11-12`: `source`, `author`),
so the consumer resolves the artefact and refuses unless its author is on the harness allowlist.

This is a genuine weakening versus F2's column-level `NOT NULL`, and the docstring must say so
rather than gloss it.

- [ ] **Step 1: Write the failing test**

```python
# tests/arb_memory/test_gate_store.py
import os

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("pgvector")
pytestmark = pytest.mark.skipif(not os.environ.get("ARB_MEMORY_DSN"), reason="no ARB_MEMORY_DSN")

from arb_memory import gate_store  # noqa: E402

HARNESS = "harness-runner"
VERIFIER = "claude-b"


def _seed_claim(scratch):
    scratch.execute(
        "INSERT INTO claims (claim_id, finding_ref, status, author_seat, author_family, "
        "author_family_provenance) VALUES ('c-1', 'f-1', 'confirmed', 'codex-a', 'gpt', 'wire')"
    )


def _seed_artefact(scratch, artefact_id, author):
    scratch.execute(
        "INSERT INTO artefacts (artefact_id, version, content, content_hash, source, author) "
        "VALUES (%s, 1, 'rerun log', 'h', 'harness', %s)",
        (artefact_id, author),
    )


def _attest(scratch, artefact_id):
    gate_store.insert_attestation(
        scratch,
        claim_id="c-1",
        verifier_seat=VERIFIER,
        verifier_family="claude",
        family_provenance="wire",
        restatement="the door is unwired",
        mechanism="audit.py:13-20 is never called from run.py",
        falsifier="a passing production-wiring test",
        falsifier_kind="command",
        rerun_artefact_id=artefact_id,
        rerun_artefact_version=1,
        harness_authors=frozenset({HARNESS}),
    )


def test_harness_authored_rerun_is_admitted(scratch):
    _seed_claim(scratch)
    _seed_artefact(scratch, "art-rerun", HARNESS)
    _attest(scratch, "art-rerun")
    assert scratch.execute(
        "SELECT count(*) FROM attestations WHERE claim_id = 'c-1'"
    ).fetchone()[0] == 1


def test_verifier_authored_rerun_is_refused_and_names_the_author(scratch):
    """F4: citing your own transcript is self-report one pointer deeper."""
    _seed_claim(scratch)
    _seed_artefact(scratch, "art-self", VERIFIER)
    with pytest.raises(gate_store.HarnessIdentityRefused) as exc:
        _attest(scratch, "art-self")
    assert exc.value.author == VERIFIER
    assert VERIFIER in str(exc.value)
    assert "art-self" in str(exc.value)
    assert scratch.execute(
        "SELECT count(*) FROM attestations WHERE claim_id = 'c-1'"
    ).fetchone()[0] == 0, "refused attestation was written anyway"


def test_missing_rerun_artefact_is_refused_not_admitted(scratch):
    """Fail-closed: an unresolvable pointer must not pass as 'no evidence of a problem'."""
    _seed_claim(scratch)
    with pytest.raises(gate_store.HarnessIdentityRefused) as exc:
        _attest(scratch, "art-does-not-exist")
    assert "art-does-not-exist" in str(exc.value)
    assert scratch.execute(
        "SELECT count(*) FROM attestations WHERE claim_id = 'c-1'"
    ).fetchone()[0] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/arb_memory/test_gate_store.py -q`
Expected: FAIL at import — `ImportError: cannot import name 'gate_store'`.

- [ ] **Step 3: Write the implementation**

```python
# src/arb_memory/gate_store.py
"""Consumer-side write path for gate attestations.

F4 (spec art-8742dfc1ca4b8be8 v6 §7.1) lives here rather than in a column constraint because it is a
CROSS-ARTEFACT property: validating a row destined for `attestations` requires reading `artefacts`,
which that table cannot see. That placement is a genuine weakening versus F2's column-level
NOT NULL - the check can be bypassed by anything that INSERTs directly rather than calling this
function. Recorded as a known weakening rather than glossed.
"""

from __future__ import annotations

from arb_memory.store import fetch_artefact

# Artefact authors that count as machinery rather than a participant. Slice 1c widens this
# from configuration; the frozenset is the injection point so tests never mutate a global.
HARNESS_AUTHORS: frozenset[str] = frozenset({"harness-runner"})


class HarnessIdentityRefused(Exception):
    """The cited re-run artefact is not harness-authored, or does not resolve."""

    def __init__(self, artefact_ref: str, author: str | None):
        self.artefact_ref = artefact_ref
        self.author = author
        if author is None:
            super().__init__(
                f"re-run artefact {artefact_ref} does not resolve; "
                f"cannot confirm machinery authorship, refusing"
            )
        else:
            super().__init__(
                f"re-run artefact {artefact_ref} is authored by {author!r}, "
                f"which is not a harness identity; citing your own output is self-report"
            )


def insert_attestation(
    conn,
    *,
    claim_id: str,
    verifier_seat: str,
    verifier_family: str,
    family_provenance: str,
    restatement: str,
    mechanism: str,
    falsifier: str,
    falsifier_kind: str,
    rerun_artefact_id: str,
    rerun_artefact_version: int,
    harness_authors: frozenset[str] = HARNESS_AUTHORS,
) -> None:
    """Write an attestation, refusing unless the cited re-run is harness-authored (F4)."""
    ref = f"{rerun_artefact_id} v{rerun_artefact_version}"
    artefact = fetch_artefact(conn, rerun_artefact_id, rerun_artefact_version)
    if artefact is None:
        raise HarnessIdentityRefused(ref, None)
    author = artefact.get("author")
    if author not in harness_authors:
        raise HarnessIdentityRefused(ref, author)

    conn.execute(
        "INSERT INTO attestations (claim_id, verifier_seat, verifier_family, family_provenance, "
        "restatement, mechanism, falsifier, falsifier_kind, rerun_artefact_id, "
        "rerun_artefact_version) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (claim_id, verifier_seat, verifier_family, family_provenance, restatement, mechanism,
         falsifier, falsifier_kind, rerun_artefact_id, rerun_artefact_version),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/arb_memory/test_gate_store.py -q`
Expected: PASS, 3 passed.

`fetch_artefact` is declared `-> dict | None` and builds its dict at `store.py:261-273`, so
`.get("author")` is correct as written. Do not change that function's signature — it has other
callers.

- [ ] **Step 5: Commit**

```bash
git add src/arb_memory/gate_store.py tests/arb_memory/test_gate_store.py
git commit -m "feat(gate-store): F4 harness-identity check on cited re-run artefacts"
```

---

### Task 6: Deny-proof the view predicates

**Files:**
- Create: `tests/arb_memory/test_gate_schema_deny_proof.py`

**Interfaces:**
- Consumes: everything from Tasks 1-3.
- Produces: nothing. Tests only.

Per `docs/defect-classes/refusal-is-ambient-assert-the-code.md`, a green test on a default-deny path
is weak evidence: deleting the mechanism usually just lets another layer refuse. `admissible` is a
conjunction, so *any* broken half still reads `false` and the truth-table tests stay green. These
tests assert the halves independently, which is what makes Tasks 2-3 non-vacuous.

- [ ] **Step 1: Write the test**

```python
# tests/arb_memory/test_gate_schema_deny_proof.py
"""Inject-revert proofs for claim_admissibility_v.

`admissible` = confirmed_now AND attested. A conjunction hides which half failed, so a
truth-table test asserting only `admissible is False` would stay green with either half
permanently false. These assert each half moves independently.
"""

import os

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("pgvector")
pytestmark = pytest.mark.skipif(not os.environ.get("ARB_MEMORY_DSN"), reason="no ARB_MEMORY_DSN")

from tests.arb_memory.test_gate_schema import _attest, _claim, _row  # noqa: E402


def test_each_half_of_admissible_moves_independently(scratch):
    """If either column is hardwired, one of these four rows is wrong."""
    _claim(scratch, "c-tt", status="confirmed")
    _attest(scratch, "c-tt")
    assert _row(scratch, "c-tt")[:2] == (True, True)

    _claim(scratch, "c-tf", status="confirmed")
    assert _row(scratch, "c-tf")[:2] == (True, False)

    _claim(scratch, "c-ft", status="unconfirmed")
    _attest(scratch, "c-ft")
    assert _row(scratch, "c-ft")[:2] == (False, True)

    _claim(scratch, "c-ff", status="unconfirmed")
    assert _row(scratch, "c-ff")[:2] == (False, False)


def test_provenance_is_not_hardwired_to_a_single_value(scratch):
    """Guards the count(*)=0-first ordering: if the CASE collapsed, all three read alike."""
    _claim(scratch, "c-none", author_provenance="wire")
    _claim(scratch, "c-wire", author_provenance="wire")
    _attest(scratch, "c-wire", provenance="wire")
    _claim(scratch, "c-deg", author_provenance="configured")
    _attest(scratch, "c-deg", provenance="wire")

    observed = {_row(scratch, cid)[2] for cid in ("c-none", "c-wire", "c-deg")}
    assert observed == {"none", "wire", "degraded"}, (
        f"provenance CASE collapsed - observed only {observed}"
    )


def test_no_attestation_reads_none_never_degraded(scratch):
    """bool_and over zero rows is NULL. If count(*)=0 stopped being tested first, a claim with
    NO attestation would report as merely weakly-decorrelated."""
    _claim(scratch, "c-empty")
    assert _row(scratch, "c-empty")[2] == "none"
```

- [ ] **Step 2: Run the test**

Run: `.venv/bin/python -m pytest tests/arb_memory/test_gate_schema_deny_proof.py -q`
Expected: PASS.

- [ ] **Step 3: Prove the proofs are not vacuous — this step is the point of the task**

Do all three injections. Each is a temporary edit to `src/arb_memory/schema.sql`, re-run, then
`git checkout -- src/arb_memory/schema.sql`. Record the **observed** failure line for each.

| # | Injection | Must fail |
|---|---|---|
| 1 | in `attested`, change `a.verifier_family <> c.author_family` to `true` | `test_same_family_attestation_is_not_an_attestation_at_all` (F1 collapses) |
| 2 | in `decorrelation_provenance`, delete `WHEN count(*) = 0 THEN 'none'` | `test_no_attestation_reads_none_never_degraded` |
| 3 | in `decorrelation_provenance`, delete the three `<> ''` completeness predicates | `test_incomplete_cross_family_attestation_is_never_wire` |

Injection 3 is the one that matters: it is the exact drift the spec says already happened once.

- [ ] **Step 4: Record the inject-revert results**

Add the observed failure lines as a comment block at the top of the deny-proof file. Paste the
**actual** pytest output, not a prediction of it — writing the prediction as the result is
`docs/defect-classes/prediction-written-as-result.md`.

- [ ] **Step 5: Confirm schema.sql is back to its committed state**

```bash
git diff --stat -- src/arb_memory/schema.sql   # must print nothing
```

A non-empty diff means an injection is still live. Do not proceed until this is clean.

- [ ] **Step 6: Run the full affected suite**

```bash
set -a; . /Users/<user>/<workspace>/envs/arb-memory-dev.env; set +a
.venv/bin/python -m pytest tests/arb_memory/ -q
.venv/bin/python -m pytest tests/test_claim_gate.py tests/test_claim_gate_deny_proof.py \
    tests/defect_hunts/test_gate_assertions.py -q
```

Expected: all green. The second command re-checks Slice 1a (50 tests) — this slice must not have
disturbed it. Note that `tests/test_doc_index.py` takes ~123s; it is unaffected by this slice, but
if you run the whole suite, that is not a hang.

- [ ] **Step 7: Commit**

```bash
git add tests/arb_memory/test_gate_schema_deny_proof.py
git commit -m "test(gate-store): inject-revert deny-proof for the admissibility view"
```

---

## Self-Review

**Spec coverage (§4, §7.1 and the §11 items in this slice's scope):**

| Spec requirement | Task |
|---|---|
| `claims` incl. author identity (F1) | 1 |
| `attestations` incl. re-run refs NOT NULL (F2) | 1 |
| `seat_posture`, default-deny | 1 |
| `lease_lanes`, consumer-written | 1 |
| `claim_admissibility_v` | 2 |
| `seat_posture_v`, `lease_lane_v` | 2 |
| `confirmed_now` / `attested` separately exposed | 2 |
| Expiry at resolution, `> now()` boundary | 2 |
| Decorrelation folded into `attested` (F1) | 2, 6 |
| `count(*) = 0` first in the provenance CASE | 2, 6 |
| Population alignment between the two subqueries | 3, 6 |
| `decorrelation_provenance` degrades on `configured` | 3 |
| `arb_gate_reader` SELECT-only on three views | 4 |
| F4 consumer-side harness-identity check | 5 |
| Enum drift on every CHECK column | 1 |
| Attestation unwritable without a re-run ref | 1 |

**Deliberately out of scope, with reason:**

- **`falsifier_kind = 'prose'` weighting** — the column is recorded here; the sampler that reads it
  is Slice 2 (spec §10).
- **`decorrelation_provenance` acting on anything** — same; it is a slice-2 sampler input.
- **Refusal-code tests over `handle_raw`** — those need the bridge wiring, which is Slice 1c. Slice
  1a already covers the codes against fake resolvers.
- **Lane deny-proof (push from an exempt worktree) and probe rehydration** — both need the exempt
  lane's credential machinery, which is Slice 1d.

**Type consistency:** `claim_admissibility_v`'s output columns (`confirmed_now`, `attested`,
`decorrelation_provenance`) match Slice 1a's `ClaimFacts` field names, so the Slice 1c resolver is a
row-to-dataclass mapping with no renaming. `apply_gate_reader_grants(conn, role)` matches the
`apply_*_grants(conn, role)` signature used at `grants.py:6, 57, 124, 166, 246, 325`.
`insert_attestation`'s keyword names match the `attestations` column names exactly.

**Known weakening, stated rather than glossed:** F4 is enforced at the consumer's write path, so it
binds only callers of `insert_attestation`. A direct `INSERT INTO attestations` bypasses it. The
spec accepts this (§7.1) because the property is cross-artefact; Task 5's module docstring records
it so a later reader does not mistake it for column-strength enforcement.
