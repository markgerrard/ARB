import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
from psycopg.types.json import Jsonb

_path = Path(__file__).parents[2] / "scripts" / "arb-memory-migrate-audit-kind"
_spec = importlib.util.spec_from_file_location(
    "migrate_audit_kind", _path, loader=SourceFileLoader("migrate_audit_kind", str(_path))
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
migrate_audit_kind = _mod.migrate_audit_kind


def _seed_legacy_table(conn):
    # simulate the pre-migration shape: NO kind column, rows with kind only in raw_entry
    conn.execute("ALTER TABLE audit_events DROP COLUMN kind")
    conn.execute(
        "INSERT INTO audit_events (run_id, seq, source, payload, raw_entry) VALUES (%s,%s,%s,%s,%s)",
        ("legacy-1", 1, "orchestrator", Jsonb({"actor": "x"}), Jsonb({"kind": "dispatch", "actor": "x"})),
    )
    conn.execute(
        "INSERT INTO audit_events (run_id, seq, source, payload, raw_entry) VALUES (%s,%s,%s,%s,%s)",
        ("legacy-2", 1, "orchestrator", Jsonb({}), Jsonb({})),  # unbackfillable
    )


def test_migration_backfills_and_quarantines_then_sets_not_null(conn_factory):
    conn = conn_factory()
    _seed_legacy_table(conn)
    migrate_audit_kind(conn)
    rows = dict(conn.execute("SELECT run_id, kind FROM audit_events ORDER BY run_id").fetchall())
    assert rows["legacy-1"] == "dispatch"        # backfilled from raw_entry
    assert rows["legacy-2"] == "unknown"         # quarantined, not NULL
    # column is now NOT NULL
    nn = conn.execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_schema=current_schema() AND table_name='audit_events' AND column_name='kind'"
    ).fetchone()[0]
    assert nn == "NO"


def test_migration_is_idempotent(conn_factory):
    conn = conn_factory()
    _seed_legacy_table(conn)
    migrate_audit_kind(conn)
    migrate_audit_kind(conn)  # second run must not error
