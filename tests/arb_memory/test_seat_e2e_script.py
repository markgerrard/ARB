import runpy
import os
from pathlib import Path

import pytest


def test_seat_e2e_dispatch_command_and_task_shape():
    script = Path(__file__).parents[2] / "scripts" / "arb-memory-seat-e2e"
    module = runpy.run_path(str(script))

    # Slice 1d-iv: ordinary path is pre-minted-only (quartet, no free-form task).
    command = module["build_dispatch_command"](
        target_id="codex-bridge-dev",
        engine="codex",
        timeout_s=123,
        env_file="/tmp/agent.env",
        from_agent_id="claude-bridge-dev",
        run_id="run-tag-1",
        artefact_id="art-1",
        version=1,
        receipt_path="/tmp/receipt.json",
        brief_path="/tmp/brief.md",
    )

    assert command == [
        "scripts/agent-dispatch",
        "--engine",
        "codex",
        "--target-id",
        "codex-bridge-dev",
        "--timeout",
        "123",
        "--run-id",
        "run-tag-1",
        "--artefact-id",
        "art-1",
        "--version",
        "1",
        "--receipt",
        "/tmp/receipt.json",
        "--brief",
        "/tmp/brief.md",
    ]

    env = module["dispatch_env"]("/tmp/agent.env", "claude-bridge-dev")
    assert env["AGENT_ENV_FILE"] == "/tmp/agent.env"
    assert env["FROM_AGENT_ID"] == "claude-bridge-dev"

    task = module["seat_write_task"](
        repo_root="/repo",
        python="python3",
        redis_url="redis://127.0.0.1:6379/15",
        prefix="e2e:",
        marker="marker-1",
        artefact_id="artefact-1",
        run_tag="run-tag",
    )

    assert "unset OPENAI_API_KEY" in task
    assert "PYTHONPATH=src" in task
    assert "ARB_MEMORY_PREFIX='e2e:'" in task
    assert "ARB_MEMORY_SEAT_WRITE_ULID=" in task
    assert "simulate" not in task.lower()


def test_seat_e2e_query_task_is_transport_only():
    script = Path(__file__).parents[2] / "scripts" / "arb-memory-seat-e2e"
    module = runpy.run_path(str(script))

    task = module["seat_query_task"](
        repo_root="/repo",
        python="python3",
        redis_url="redis://127.0.0.1:6379/15",
        prefix="e2e:",
        marker="marker-2",
        run_tag="run-tag",
    )

    assert "transport_only=True" in task
    assert "ARB_MEMORY_SEAT_QUERY_OK=marker-2" in task
    assert "grep" not in task.lower()


def test_seat_e2e_cleanup_commits_for_other_connections(scratch):
    psycopg = pytest.importorskip("psycopg")
    dsn = os.environ.get("ARB_MEMORY_DSN")
    if not dsn:
        pytest.skip("no ARB_MEMORY_DSN")

    script = Path(__file__).parents[2] / "scripts" / "arb-memory-seat-e2e"
    module = runpy.run_path(str(script))
    run_tag = "arb-memory-seat-e2e-cleanup-test"
    schema = scratch.execute("SELECT current_schema()").fetchone()[0]

    scratch.execute(
        """
        INSERT INTO artefacts (artefact_id, version, content, content_mime, content_hash)
        VALUES (%s, 1, 'content', 'text/plain', 'artefact-hash')
        """,
        (f"{run_tag}-writer",),
    )
    scratch.execute(
        """
        INSERT INTO hints (text, embedding, source, author, artefact_id, artefact_version, content_hash)
        VALUES (%s, %s, %s, %s, %s, 1, 'hint-hash')
        """,
        ("marker", [0.0] * 1536, run_tag, run_tag, f"{run_tag}-writer"),
    )

    conn = module["_connect"](dsn)
    observer = psycopg.connect(dsn, autocommit=True)
    try:
        conn.execute(f'SET search_path TO "{schema}", public')
        observer.execute(f'SET search_path TO "{schema}", public')

        module["cleanup_rows"](conn, run_tag, set())

        assert module["_count_rows"](conn, run_tag) == 0
        assert module["_count_rows"](observer, run_tag) == 0
    finally:
        conn.close()
        observer.close()
