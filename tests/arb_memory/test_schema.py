import os
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("pgvector")
pytestmark = pytest.mark.skipif(not os.environ.get("ARB_MEMORY_DSN"), reason="no ARB_MEMORY_DSN")


def test_schema_applies_and_constraints(scratch):
    assert scratch.execute("SELECT extname FROM pg_extension WHERE extname='vector'").fetchone(), (
        "vector extension missing - DDL must CREATE EXTENSION first"
    )
    for t in ("artefacts", "hints", "audit_events", "idempotency_keys"):
        oid = scratch.execute("SELECT to_regclass(%s)", (t,)).fetchone()[0]
        assert oid is not None, f"table {t} missing"
    with pytest.raises(psycopg.errors.CheckViolation):
        scratch.execute(
            "INSERT INTO hints (text, embedding, artefact_id, content_hash) "
            "VALUES ('x', %s, 'a', 'h')",
            ([0.0] * 1536,),
        )


def test_idempotency_receipt_migration_is_present_and_idempotent(scratch):
    schema_sql = (Path(__file__).parents[2] / "src" / "arb_memory" / "schema.sql").read_text(
        encoding="utf-8"
    )
    scratch.execute(schema_sql)
    columns = scratch.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'idempotency_keys'"
    ).fetchall()
    assert "receipt" in {row[0] for row in columns}


def test_schema_allows_only_one_verdict_per_run(scratch):
    insert = (
        "INSERT INTO audit_events (run_id, seq, source, kind, payload) "
        "VALUES (%s, %s, 'test', %s, '{}'::jsonb)"
    )

    scratch.execute(insert, ("same-run", 1, "verdict"))
    with pytest.raises(psycopg.errors.UniqueViolation) as exc_info:
        scratch.execute(insert, ("same-run", 2, "verdict"))

    assert exc_info.value.diag.constraint_name == "audit_events_one_verdict"
    scratch.execute(insert, ("different-run", 1, "verdict"))
    scratch.execute(insert, ("same-run", 3, "vote"))

    assert scratch.execute(
        "SELECT count(*) FROM audit_events WHERE kind = 'verdict'"
    ).fetchone()[0] == 2
    assert scratch.execute(
        "SELECT count(*) FROM audit_events WHERE run_id = 'same-run'"
    ).fetchone()[0] == 2


def test_schema_does_not_manage_mcp_role():
    schema_sql = (Path(__file__).parents[2] / "src" / "arb_memory" / "schema.sql").read_text(
        encoding="utf-8"
    )
    assert "CREATE ROLE" not in schema_sql
    assert "ALTER ROLE" not in schema_sql
    assert "DROP ROLE" not in schema_sql
    assert "arbmem_mcp" not in schema_sql
    # DDL-only: no privilege statements at all (grants are applied out-of-band via apply_mcp_grants /
    # doadmin). A stray GRANT/REVOKE re-introduced here would re-couple schema-apply to the role model.
    assert "GRANT" not in schema_sql
    assert "REVOKE" not in schema_sql
