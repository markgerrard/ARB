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
    """F2's NOT NULL half: a re-run reference must be SUPPLIED.

    That is all NOT NULL establishes -- the literal 'x' satisfies it. Existence of the cited
    artefact is enforced by the FK (see test_attestation_cannot_cite_a_nonexistent_rerun_artefact);
    authorship is enforced only at the consumer (F4, gate_store.insert_attestation). The three
    strengths are deliberately different; do not restate any of them as "machinery contact".
    """
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


def _claim(scratch, claim_id, *, status="confirmed", review_by=None,
           author_family="gpt", author_provenance="wire"):
    scratch.execute(
        "INSERT INTO claims (claim_id, finding_ref, status, author_seat, author_family, "
        "author_family_provenance, review_by) VALUES (%s, %s, %s, 'codex-a', %s, %s, %s)",
        (claim_id, f"f-{claim_id}", status, author_family, author_provenance, review_by),
    )


def _seed_rerun_artefact(scratch, artefact_id="art-rerun", version=1, author="harness-runner"):
    """The re-run reference is a FOREIGN KEY into `artefacts` (P2-3), so a cited artefact must
    exist. Idempotent: helpers call it freely."""
    scratch.execute(
        "INSERT INTO artefacts (artefact_id, version, content, content_hash, source, author) "
        "VALUES (%s, %s, 'rerun log', 'h', 'harness', %s) ON CONFLICT DO NOTHING",
        (artefact_id, version, author),
    )


def _attest(scratch, claim_id, *, verifier_family="claude", provenance="wire",
            restatement="the door is unwired", mechanism="audit.py:13-20 never called",
            falsifier="a passing wire test", verifier_seat="claude-b"):
    _seed_rerun_artefact(scratch)
    scratch.execute(
        "INSERT INTO attestations (claim_id, verifier_seat, verifier_family, family_provenance, "
        "restatement, mechanism, falsifier, falsifier_kind, rerun_artefact_id, "
        "rerun_artefact_version) VALUES (%s, %s, %s, %s, %s, %s, %s, 'command', 'art-rerun', 1)",
        (claim_id, verifier_seat, verifier_family, provenance, restatement, mechanism, falsifier),
    )


@pytest.mark.parametrize(
    "column,bad_value",
    [("family_provenance", "vibes"), ("falsifier_kind", "interpretive-dance")],
)
def test_attestation_check_constraints_reject_out_of_domain_values(scratch, column, bad_value):
    """P2-2: the parametrized enum test near the top of this file covers 3 of the 5 gate CHECK
    columns; these are the two it omitted. `attestations.family_provenance` is read directly by the
    provenance CASE (`bool_and(a.family_provenance = 'wire')`), so an unconstrained value there is a
    silent drift channel into `decorrelation_provenance`."""
    _claim(scratch, "c-enum")
    _seed_rerun_artefact(scratch)
    values = {"family_provenance": "wire", "falsifier_kind": "command"}
    values[column] = bad_value
    with pytest.raises(psycopg.errors.CheckViolation):
        scratch.execute(
            "INSERT INTO attestations (claim_id, verifier_seat, verifier_family, "
            "family_provenance, restatement, mechanism, falsifier, falsifier_kind, "
            "rerun_artefact_id, rerun_artefact_version) VALUES "
            "('c-enum', 'claude-b', 'claude', %(family_provenance)s, 'r', 'm', 'f', "
            "%(falsifier_kind)s, 'art-rerun', 1)",
            values,
        )


def test_attestation_cannot_cite_a_nonexistent_rerun_artefact(scratch):
    """P2-3: `NOT NULL` proves a pointer EXISTS, not that it RESOLVES -- the schema comment claimed
    "a row cannot exist without machinery contact", which a literal 'x' satisfied. The FK makes the
    existence half genuinely column-strength. The AUTHOR half legitimately stays at the consumer
    (F4, `gate_store.insert_attestation`), because it is cross-artefact."""
    _claim(scratch, "c-fk")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        scratch.execute(
            "INSERT INTO attestations (claim_id, verifier_seat, verifier_family, "
            "family_provenance, restatement, mechanism, falsifier, falsifier_kind, "
            "rerun_artefact_id, rerun_artefact_version) VALUES "
            "('c-fk', 'claude-b', 'claude', 'wire', 'r', 'm', 'f', 'command', 'art-nope', 1)"
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
    """The boundary is `review_by > now()`, STRICTLY: a claim expiring exactly now is expired.

    P2-1: this previously pinned '2020-01-01', which made it a duplicate of the past-expiry test
    and insensitive to the operator -- mutating `>` to `>=` left the whole suite green. `now()` is
    `transaction_timestamp()`, so inserting `review_by = now()` and resolving the view INSIDE one
    transaction compares bit-identical timestamps: no clock race, and the strict inequality is
    genuinely locked. Under `>=` this reads True and the test fails, which is the point.
    """
    with scratch.transaction():
        scratch.execute(
            "INSERT INTO claims (claim_id, finding_ref, status, author_seat, author_family, "
            "author_family_provenance, review_by) VALUES "
            "('c-bnd', 'f', 'confirmed', 'codex-a', 'gpt', 'wire', now())"
        )
        confirmed_now = scratch.execute(
            "SELECT confirmed_now FROM claim_admissibility_v WHERE claim_id = 'c-bnd'"
        ).fetchone()[0]
    assert confirmed_now is False, "review_by == now() must be expired: the boundary is strict"


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


def test_seat_posture_v_projects_both_postures(scratch):
    """P1-B: the contract test below inserts one value and asserts that same value back, so it
    cannot distinguish "projects the column" from "returns a constant". Hardwiring
    `false AS requires_claim_ref` is the FAIL-OPEN mutation (spec §3 default-deny: every seat
    un-gated) and left the whole suite green. Note the asymmetry that hid it: the test at the top
    of this file that does exercise the true-side default reads the BASE TABLE, not the view."""
    scratch.execute("INSERT INTO seat_posture (seat_id, requires_claim_ref) VALUES "
                    "('s-gated', true), ('s-open', false)")
    rows = dict(scratch.execute(
        "SELECT seat_id, requires_claim_ref FROM seat_posture_v "
        "WHERE seat_id IN ('s-gated', 's-open')"
    ).fetchall())
    assert rows == {"s-gated": True, "s-open": False}


def test_lease_lane_v_projects_both_lanes(scratch):
    """P1-B: hardwiring `'exempt'::text AS lane` collapses spec §5.3 to always-exempt -- a total
    gate bypass -- and left the whole suite green. Both domain values must be observable."""
    scratch.execute("INSERT INTO lease_lanes (lease_id, lane, armed_by) VALUES "
                    "('l-g', 'gated', 'consumer'), ('l-e', 'exempt', 'consumer')")
    rows = dict(scratch.execute(
        "SELECT lease_id, lane FROM lease_lane_v WHERE lease_id IN ('l-g', 'l-e')"
    ).fetchall())
    assert rows == {"l-g": "gated", "l-e": "exempt"}


def test_claim_admissibility_v_projects_the_raw_claim_columns(scratch):
    """The view's declared shape includes raw `status` and `review_by`, and nothing pinned them:
    hardwiring `'confirmed'::text AS status, NULL::timestamptz AS review_by` left all 41 focused
    tests green (r2 panel). Not a dispatch bypass today -- Slice 1a's `ClaimFacts` consumes only
    confirmed_now/attested/decorrelation_provenance -- but the close gate and any audit surface
    read the view, so an un-pinned passthrough is a drift channel into whatever consumes it next.
    """
    _claim(scratch, "c-raw-conf", status="confirmed", review_by=None)
    scratch.execute(
        "INSERT INTO claims (claim_id, finding_ref, status, author_seat, author_family, "
        "author_family_provenance, review_by) VALUES "
        "('c-raw-ret', 'f', 'retracted', 'codex-a', 'gpt', 'wire', %s)",
        ("2031-01-01 00:00:00+00",),
    )
    rows = dict(
        (r[0], (r[1], r[2]))
        for r in scratch.execute(
            "SELECT claim_id, status, review_by FROM claim_admissibility_v "
            "WHERE claim_id IN ('c-raw-conf', 'c-raw-ret')"
        ).fetchall()
    )
    assert rows["c-raw-conf"][0] == "confirmed"
    assert rows["c-raw-conf"][1] is None, "NULL review_by must project as NULL"
    assert rows["c-raw-ret"][0] == "retracted", "status must project the claim, not a constant"
    assert rows["c-raw-ret"][1] is not None, "a non-null review_by must project as non-null"
    assert rows["c-raw-ret"][1].year == 2031


def test_schema_sql_is_reappliable(scratch):
    """schema.sql is re-applied over itself by test_schema.py:30. Bare CREATE VIEW raises
    DuplicateTable; this is the regression test for that."""
    scratch.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
    scratch.execute(SCHEMA_SQL.read_text(encoding="utf-8"))


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
