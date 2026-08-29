"""Live local-reader hydration tests (Slice 1d-v Task 6 Step 1).

Uses the scratch-schema fixture (throwaway arb_test_<uuid>) and an isolated
local-reader role with apply_local_reader_grants. Never prod.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from arb_memory.hash import artefact_hash
from arb_memory.mcp.grants import (
    GATE_READER_ROLE,
    apply_gate_reader_grants,
    apply_local_reader_grants,
)
from arb_memory.store import upsert_artefact

# Body sentinel: must appear only in store/hydrated-file assertions, never envelope.
BODY_SENTINEL_V1 = "BODY_SENTINEL_1d_v_v1_NOT_ON_WIRE"
BODY_SENTINEL_V2 = "BODY_SENTINEL_1d_v_v2_NEWER_NOT_PINNED"

LOCAL_READER_ROLE_PREFIX = "arbmem_brief_hydrate_test_"


@pytest.fixture
def local_reader_role(scratch):
    role = f"{LOCAL_READER_ROLE_PREFIX}{secrets.token_hex(4)}"
    password = f"brief-hydrate-test-{secrets.token_hex(16)}"
    try:
        scratch.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role),
                sql.Literal(password),
            )
        )
    except psycopg.errors.InsufficientPrivilege:
        scratch.rollback()
        pytest.skip(
            "substrate disallows CREATE ROLE; local-reader hydration needs role creation"
        )
    try:
        yield role, password
    finally:
        scratch.execute("RESET ROLE")
        scratch.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        scratch.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def _schema(scratch) -> str:
    return scratch.execute("SELECT current_schema()").fetchone()[0]


def _local_reader_dsn(schema: str, role: str, password: str) -> str:
    params = conninfo_to_dict(os.environ["ARB_MEMORY_DSN"])
    params["user"] = role
    params["password"] = password
    params["options"] = f"-csearch_path={schema},public"
    return make_conninfo(**params)


def _stage_dir(tmp_path: Path) -> Path:
    d = tmp_path / "stage"
    d.mkdir(mode=0o700)
    os.chmod(d, 0o700)
    return d


def _run_hydrate(
    *,
    artefact_id: str,
    version: int,
    output: Path,
    receipt: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "arb_memory.brief_hydrate",
            "--artefact-id",
            artefact_id,
            "--version",
            str(version),
            "--output",
            str(output),
            "--receipt",
            str(receipt),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_exact_version_staged_0600_in_0700_dir_with_domain_hash(
    scratch, local_reader_role, tmp_path, monkeypatch
):
    from arb_memory import brief_hydrate

    schema = _schema(scratch)
    role, password = local_reader_role
    apply_local_reader_grants(scratch, role)

    art_id = f"art-hydrate-{secrets.token_hex(6)}"
    _, v1, _ = upsert_artefact(
        scratch,
        art_id,
        content=BODY_SENTINEL_V1,
        mime="text/markdown",
        author="test",
    )
    _, v2, _ = upsert_artefact(
        scratch,
        art_id,
        content=BODY_SENTINEL_V2,
        mime="text/markdown",
        author="test",
    )
    assert v2 == v1 + 1

    stage = _stage_dir(tmp_path)
    out = stage / "brief"
    receipt = stage / "receipt"
    dsn = _local_reader_dsn(schema, role, password)
    env = {
        **os.environ,
        "ARB_MEMORY_LOCAL_DSN": dsn,
        "ARB_MEMORY_DSN": os.environ["ARB_MEMORY_DSN"],
    }
    # Fingerprint may differ on user; allow same store host/port/db.
    monkeypatch.setenv("ARB_MEMORY_LOCAL_DSN", dsn)
    monkeypatch.setenv("ARB_MEMORY_DSN", os.environ["ARB_MEMORY_DSN"])
    # Cross-store policy compares host/port/dbname only — user differs but store matches.
    result = brief_hydrate.hydrate(
        artefact_id=art_id,
        version=v1,
        output_path=out,
        receipt_path=receipt,
        env=env,
    )
    assert out.is_file()
    assert receipt.is_file()
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert stat.S_IMODE(stage.stat().st_mode) == 0o700
    body = out.read_text(encoding="utf-8")
    assert body == BODY_SENTINEL_V1
    assert BODY_SENTINEL_V2 not in body
    expected_hash = artefact_hash(BODY_SENTINEL_V1, None, "text/markdown")
    assert result["content_hash"] == expected_hash
    receipt_obj = json.loads(receipt.read_text(encoding="utf-8"))
    assert set(receipt_obj.keys()) <= {
        "artefact_id",
        "version",
        "content_hash",
        "path",
    }
    assert "artefact_id" in receipt_obj
    assert receipt_obj["artefact_id"] == art_id
    assert receipt_obj["version"] == v1
    assert receipt_obj["content_hash"] == expected_hash
    assert BODY_SENTINEL_V1 not in receipt.read_text(encoding="utf-8")
    assert BODY_SENTINEL_V1 not in json.dumps(result)


def test_receipt_metadata_only_never_body(scratch, local_reader_role, tmp_path, monkeypatch):
    from arb_memory import brief_hydrate

    schema = _schema(scratch)
    role, password = local_reader_role
    apply_local_reader_grants(scratch, role)
    art_id = f"art-rcpt-{secrets.token_hex(6)}"
    _, ver, _ = upsert_artefact(
        scratch, art_id, content=BODY_SENTINEL_V1, mime="text/markdown", author="t"
    )
    stage = _stage_dir(tmp_path)
    out, receipt = stage / "brief", stage / "receipt"
    dsn = _local_reader_dsn(schema, role, password)
    env = {**os.environ, "ARB_MEMORY_LOCAL_DSN": dsn, "ARB_MEMORY_DSN": os.environ["ARB_MEMORY_DSN"]}
    brief_hydrate.hydrate(
        artefact_id=art_id, version=ver, output_path=out, receipt_path=receipt, env=env
    )
    raw = receipt.read_text(encoding="utf-8")
    assert BODY_SENTINEL_V1 not in raw
    obj = json.loads(raw)
    for forbidden in ("content", "body", "text", "content_bytes"):
        assert forbidden not in obj


def test_pinned_older_version_ignores_latest(
    scratch, local_reader_role, tmp_path, monkeypatch
):
    from arb_memory import brief_hydrate

    schema = _schema(scratch)
    role, password = local_reader_role
    apply_local_reader_grants(scratch, role)
    art_id = f"art-pin-{secrets.token_hex(6)}"
    _, v1, _ = upsert_artefact(
        scratch, art_id, content=BODY_SENTINEL_V1, mime="text/markdown", author="t"
    )
    upsert_artefact(
        scratch, art_id, content=BODY_SENTINEL_V2, mime="text/markdown", author="t"
    )
    stage = _stage_dir(tmp_path)
    out, receipt = stage / "brief", stage / "receipt"
    dsn = _local_reader_dsn(schema, role, password)
    env = {**os.environ, "ARB_MEMORY_LOCAL_DSN": dsn, "ARB_MEMORY_DSN": os.environ["ARB_MEMORY_DSN"]}
    brief_hydrate.hydrate(
        artefact_id=art_id, version=v1, output_path=out, receipt_path=receipt, env=env
    )
    assert out.read_text(encoding="utf-8") == BODY_SENTINEL_V1


def test_missing_version_exact_code_no_final_files(
    scratch, local_reader_role, tmp_path, monkeypatch
):
    from arb_memory import brief_hydrate

    schema = _schema(scratch)
    role, password = local_reader_role
    apply_local_reader_grants(scratch, role)
    art_id = f"art-miss-{secrets.token_hex(6)}"
    upsert_artefact(
        scratch, art_id, content=BODY_SENTINEL_V1, mime="text/markdown", author="t"
    )
    stage = _stage_dir(tmp_path)
    out, receipt = stage / "brief", stage / "receipt"
    dsn = _local_reader_dsn(schema, role, password)
    env = {**os.environ, "ARB_MEMORY_LOCAL_DSN": dsn, "ARB_MEMORY_DSN": os.environ["ARB_MEMORY_DSN"]}
    with pytest.raises(brief_hydrate.BriefHydrationError) as exc:
        brief_hydrate.hydrate(
            artefact_id=art_id,
            version=999,
            output_path=out,
            receipt_path=receipt,
            env=env,
        )
    assert exc.value.code == "brief_hydration_missing"
    assert not out.exists()
    assert not receipt.exists()


def test_missing_id_exact_code_no_final_files(
    scratch, local_reader_role, tmp_path, monkeypatch
):
    from arb_memory import brief_hydrate

    schema = _schema(scratch)
    role, password = local_reader_role
    apply_local_reader_grants(scratch, role)
    stage = _stage_dir(tmp_path)
    out, receipt = stage / "brief", stage / "receipt"
    dsn = _local_reader_dsn(schema, role, password)
    env = {**os.environ, "ARB_MEMORY_LOCAL_DSN": dsn, "ARB_MEMORY_DSN": os.environ["ARB_MEMORY_DSN"]}
    with pytest.raises(brief_hydrate.BriefHydrationError) as exc:
        brief_hydrate.hydrate(
            artefact_id="art-does-not-exist-zzzz",
            version=1,
            output_path=out,
            receipt_path=receipt,
            env=env,
        )
    assert exc.value.code == "brief_hydration_missing"
    assert not out.exists()
    assert not receipt.exists()


def test_hash_mismatch_exact_code(scratch, local_reader_role, tmp_path, monkeypatch):
    from arb_memory import brief_hydrate

    schema = _schema(scratch)
    role, password = local_reader_role
    apply_local_reader_grants(scratch, role)
    art_id = f"art-hm-{secrets.token_hex(6)}"
    _, ver, _ = upsert_artefact(
        scratch, art_id, content=BODY_SENTINEL_V1, mime="text/markdown", author="t"
    )
    # Corrupt the stored content_hash without changing body.
    scratch.execute(
        "UPDATE artefacts SET content_hash = %s WHERE artefact_id = %s AND version = %s",
        ("0" * 64, art_id, ver),
    )
    stage = _stage_dir(tmp_path)
    out, receipt = stage / "brief", stage / "receipt"
    dsn = _local_reader_dsn(schema, role, password)
    env = {**os.environ, "ARB_MEMORY_LOCAL_DSN": dsn, "ARB_MEMORY_DSN": os.environ["ARB_MEMORY_DSN"]}
    with pytest.raises(brief_hydrate.BriefHydrationError) as exc:
        brief_hydrate.hydrate(
            artefact_id=art_id, version=ver, output_path=out, receipt_path=receipt, env=env
        )
    assert exc.value.code == "brief_hydration_hash_mismatch"
    assert not out.exists()
    assert not receipt.exists()


def test_cross_store_policy_failure(scratch, local_reader_role, tmp_path, monkeypatch):
    from arb_memory import brief_hydrate

    schema = _schema(scratch)
    role, password = local_reader_role
    apply_local_reader_grants(scratch, role)
    art_id = f"art-pol-{secrets.token_hex(6)}"
    _, ver, _ = upsert_artefact(
        scratch, art_id, content=BODY_SENTINEL_V1, mime="text/markdown", author="t"
    )
    stage = _stage_dir(tmp_path)
    out, receipt = stage / "brief", stage / "receipt"
    dsn = _local_reader_dsn(schema, role, password)
    # Poison writer fingerprint so local_read_dsn refuses.
    env = {
        **os.environ,
        "ARB_MEMORY_LOCAL_DSN": dsn,
        "ARB_MEMORY_DSN": "host=other-host-not-this port=1 dbname=otherdb user=x password=y",
    }
    env.pop("ARB_MEMORY_LOCAL_ALLOW_CROSS_STORE", None)
    with pytest.raises(brief_hydrate.BriefHydrationError) as exc:
        brief_hydrate.hydrate(
            artefact_id=art_id, version=ver, output_path=out, receipt_path=receipt, env=env
        )
    assert exc.value.code == "brief_hydration_policy"
    assert not out.exists()
    assert not receipt.exists()


def test_db_outage_exact_code(scratch, local_reader_role, tmp_path, monkeypatch):
    from arb_memory import brief_hydrate

    stage = _stage_dir(tmp_path)
    out, receipt = stage / "brief", stage / "receipt"
    env = {
        **os.environ,
        "ARB_MEMORY_LOCAL_DSN": "host=127.0.0.1 port=1 dbname=nope user=nope password=nope connect_timeout=1",
        "ARB_MEMORY_DSN": "host=127.0.0.1 port=1 dbname=nope user=nope password=nope",
    }
    with pytest.raises(brief_hydrate.BriefHydrationError) as exc:
        brief_hydrate.hydrate(
            artefact_id="art-x",
            version=1,
            output_path=out,
            receipt_path=receipt,
            env=env,
        )
    assert exc.value.code == "brief_hydration_store"
    assert not out.exists()
    assert not receipt.exists()


def test_credential_without_select_fails(
    scratch, local_reader_role, tmp_path, monkeypatch
):
    from arb_memory import brief_hydrate

    schema = _schema(scratch)
    role, password = local_reader_role
    # Do NOT apply local reader grants — role has no SELECT on artefacts.
    art_id = f"art-nosel-{secrets.token_hex(6)}"
    _, ver, _ = upsert_artefact(
        scratch, art_id, content=BODY_SENTINEL_V1, mime="text/markdown", author="t"
    )
    stage = _stage_dir(tmp_path)
    out, receipt = stage / "brief", stage / "receipt"
    dsn = _local_reader_dsn(schema, role, password)
    env = {**os.environ, "ARB_MEMORY_LOCAL_DSN": dsn, "ARB_MEMORY_DSN": os.environ["ARB_MEMORY_DSN"]}
    with pytest.raises(brief_hydrate.BriefHydrationError) as exc:
        brief_hydrate.hydrate(
            artefact_id=art_id, version=ver, output_path=out, receipt_path=receipt, env=env
        )
    # Missing SELECT surfaces as store failure or missing (permission denied).
    assert exc.value.code in ("brief_hydration_store", "brief_hydration_missing")
    assert not out.exists()
    assert not receipt.exists()


def test_arb_gate_reader_not_used_and_no_artefact_select(scratch):
    """After real apply_gate_reader_grants, role has no artefacts SELECT.

    Applies the production grant path on a scratch-isolated role, then asserts
    has_table_privilege(... SELECT on artefacts) is False. A bare role without
    grants would make the assert true-by-construction; this exercises the real
    grant statements. Also pins the hydrator never names arb_gate_reader.
    """
    # Hydrator source must reference local DSN policy, not gate reader role.
    src = Path(__file__).resolve().parents[2] / "src" / "arb_memory" / "brief_hydrate.py"
    text = src.read_text(encoding="utf-8")
    assert "arb_gate_reader" not in text
    assert "local_read_dsn" in text or "local_read_policy" in text

    # Unique role: applying grants to the fleet GATE_READER_ROLE name would race
    # concurrent gate tests that DROP OWNED BY that role.
    role = f"{GATE_READER_ROLE}_bh_{secrets.token_hex(4)}"
    try:
        scratch.execute(
            sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role))
        )
    except psycopg.errors.InsufficientPrivilege:
        scratch.rollback()
        pytest.skip("CREATE ROLE denied; cannot apply gate-reader grants for SELECT assert")
    except Exception:
        scratch.rollback()
        pytest.skip(f"could not create role {role} for privilege assert")
    try:
        apply_gate_reader_grants(scratch, role)
        schema = scratch.execute("SELECT current_schema()").fetchone()[0]
        can_select = scratch.execute(
            "SELECT has_table_privilege(%s, %s, 'SELECT')",
            (role, f'"{schema}".artefacts'),
        ).fetchone()[0]
        assert can_select is False, (
            f"after apply_gate_reader_grants, {role} must not have SELECT on "
            f"artefacts; got {can_select!r}"
        )
    finally:
        try:
            scratch.execute("RESET ROLE")
            scratch.execute(
                sql.SQL("DROP OWNED BY {} CASCADE").format(sql.Identifier(role))
            )
            scratch.execute(
                sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role))
            )
        except Exception:
            scratch.rollback()


def test_apply_gate_reader_grants_does_not_select_grant_artefacts():
    """Parse apply_gate_reader_grants: no SELECT grant targets artefacts.

    Real check on the function body (not identifier-presence alone). A planted
    ``GRANT SELECT ... artefacts`` or an ``"artefacts"`` relation literal in the
    grant function REDs this test. Live privilege after apply is G2's guard.
    """
    import inspect
    import re

    from arb_memory.mcp.grants import apply_gate_reader_grants as _apply

    src = inspect.getsource(_apply)
    assert "def apply_gate_reader_grants" in src
    # Direct GRANT SELECT that names artefacts in its target span.
    if re.search(r"GRANT\s+SELECT\b[\s\S]{0,240}?\bartefacts\b", src, re.I):
        pytest.fail(
            "apply_gate_reader_grants contains a GRANT SELECT involving artefacts"
        )
    # Relation-name literals that feed GRANT/REVOKE lists must not include artefacts.
    if re.search(r"""['\"]artefacts['\"]""", src):
        pytest.fail(
            "apply_gate_reader_grants must not name artefacts as a grant/revoke relation"
        )


def test_domain_separated_hash_vs_raw_sha256_red(
    scratch, local_reader_role, tmp_path, monkeypatch
):
    """Store-written text where raw sha256(body) differs from content_hash.

    Replacing artefact_hash with raw SHA-256 must make the comparison RED
    (mutation kill target). Live path uses domain-separated hash.
    """
    from arb_memory import brief_hydrate

    schema = _schema(scratch)
    role, password = local_reader_role
    apply_local_reader_grants(scratch, role)
    body = "hash-domain-sep-body-xyz"
    art_id = f"art-hash-{secrets.token_hex(6)}"
    _, ver, _ = upsert_artefact(
        scratch, art_id, content=body, mime="text/markdown", author="t"
    )
    row = scratch.execute(
        "SELECT content_hash FROM artefacts WHERE artefact_id = %s AND version = %s",
        (art_id, ver),
    ).fetchone()
    domain = artefact_hash(body, None, "text/markdown")
    raw = hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert domain == row[0]
    assert raw != domain, "fixture requires domain hash != raw sha256(body)"

    stage = _stage_dir(tmp_path)
    out, receipt = stage / "brief", stage / "receipt"
    dsn = _local_reader_dsn(schema, role, password)
    env = {**os.environ, "ARB_MEMORY_LOCAL_DSN": dsn, "ARB_MEMORY_DSN": os.environ["ARB_MEMORY_DSN"]}
    result = brief_hydrate.hydrate(
        artefact_id=art_id, version=ver, output_path=out, receipt_path=receipt, env=env
    )
    assert result["content_hash"] == domain
    assert result["content_hash"] != raw


def test_binary_kind_and_mime_participation(
    scratch, local_reader_role, tmp_path, monkeypatch
):
    from arb_memory import brief_hydrate

    schema = _schema(scratch)
    role, password = local_reader_role
    apply_local_reader_grants(scratch, role)
    payload = b"\x00\x01binary-brief\xff"
    art_id = f"art-bin-{secrets.token_hex(6)}"
    _, ver, _ = upsert_artefact(
        scratch,
        art_id,
        content_bytes=payload,
        mime="application/octet-stream",
        author="t",
    )
    stage = _stage_dir(tmp_path)
    out, receipt = stage / "brief", stage / "receipt"
    dsn = _local_reader_dsn(schema, role, password)
    env = {**os.environ, "ARB_MEMORY_LOCAL_DSN": dsn, "ARB_MEMORY_DSN": os.environ["ARB_MEMORY_DSN"]}
    result = brief_hydrate.hydrate(
        artefact_id=art_id, version=ver, output_path=out, receipt_path=receipt, env=env
    )
    assert out.read_bytes() == payload
    assert result["content_hash"] == artefact_hash(
        None, payload, "application/octet-stream"
    )


def test_traversal_and_nul_shaped_ids_rejected(tmp_path, monkeypatch):
    from arb_memory import brief_hydrate

    stage = _stage_dir(tmp_path)
    out, receipt = stage / "brief", stage / "receipt"
    env = {
        **os.environ,
        "ARB_MEMORY_LOCAL_DSN": os.environ.get("ARB_MEMORY_DSN", "host=x"),
        "ARB_MEMORY_DSN": os.environ.get("ARB_MEMORY_DSN", "host=x"),
    }
    for bad_id in (
        "../etc/passwd",
        "art/../x",
        "art\x00null",
        "/abs/path",
        "art\\win",
        "art/with/slash",
        "-option-shaped",  # startswith("-") branch
    ):
        with pytest.raises(brief_hydrate.BriefHydrationError) as exc:
            brief_hydrate.hydrate(
                artefact_id=bad_id,
                version=1,
                output_path=out,
                receipt_path=receipt,
                env=env,
            )
        assert exc.value.code == "brief_hydration_invalid_id"
        assert not out.exists()
        assert not receipt.exists()


def test_atomic_write_loops_until_complete_on_short_os_write(tmp_path, monkeypatch):
    """Partial os.write must not publish a short final file under a full-body hash.

    KILL target: a single os.write that returns half the bytes used to leave a
    mode-0600 final containing only the written prefix. The loop + size check
    must either complete the write or refuse to replace.
    """
    from arb_memory import brief_hydrate

    data = b"0123456789abcdef"  # 16 bytes
    dest = tmp_path / "body"
    real_write = os.write
    call_count = {"n": 0}

    def half_then_rest(fd, buf):
        call_count["n"] += 1
        # First call: write only half of whatever buffer is presented.
        if call_count["n"] == 1:
            chunk = bytes(buf)[: max(1, len(buf) // 2)]
            return real_write(fd, chunk)
        return real_write(fd, buf)

    monkeypatch.setattr(os, "write", half_then_rest)
    brief_hydrate._atomic_write(dest, data, mode=0o600)
    assert dest.is_file()
    assert dest.read_bytes() == data
    assert call_count["n"] >= 2, "short write must loop (second os.write required)"
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600


def test_atomic_write_refuses_when_write_never_completes(tmp_path, monkeypatch):
    """If os.write never delivers all bytes, no final file is published."""
    from arb_memory import brief_hydrate

    data = b"0123456789abcdef"
    dest = tmp_path / "body"

    def always_half(fd, buf):
        # Write at most 1 byte forever so the size check / loop cannot complete
        # a full buffer without infinite loop — return 0 to trip the failure.
        return 0

    monkeypatch.setattr(os, "write", always_half)
    with pytest.raises(brief_hydrate.BriefHydrationError) as exc:
        brief_hydrate._atomic_write(dest, data, mode=0o600)
    assert exc.value.code == "brief_hydration_write"
    assert not dest.exists()


def test_atomic_write_refuses_lying_write_count(tmp_path, monkeypatch):
    """os.write returns full length but only wrote half → size mismatch, no publish.

    The loop alone accepts a lying return count; the fstat size re-check is what
    refuses the replace. Dest must not be published.
    """
    from arb_memory import brief_hydrate

    data = b"0123456789abcdef"  # 16 bytes
    dest = tmp_path / "body"
    real_write = os.write

    def lie_full_length(fd, buf):
        chunk = bytes(buf)[: max(1, len(buf) // 2)]
        real_write(fd, chunk)
        return len(buf)  # claim full write while only half landed

    monkeypatch.setattr(os, "write", lie_full_length)
    with pytest.raises(brief_hydrate.BriefHydrationError) as exc:
        brief_hydrate._atomic_write(dest, data, mode=0o600)
    assert exc.value.code == "brief_hydration_write"
    assert "staged size" in (exc.value.message or str(exc.value))
    assert not dest.exists()


def test_filenames_id_independent(scratch, local_reader_role, tmp_path, monkeypatch):
    from arb_memory import brief_hydrate

    schema = _schema(scratch)
    role, password = local_reader_role
    apply_local_reader_grants(scratch, role)
    # Traversal-shaped *looking* id that is still valid after validation must not
    # influence filenames — helper always uses the caller-supplied paths.
    art_id = f"art-const-{secrets.token_hex(6)}"
    _, ver, _ = upsert_artefact(
        scratch, art_id, content=BODY_SENTINEL_V1, mime="text/markdown", author="t"
    )
    stage = _stage_dir(tmp_path)
    out, receipt = stage / "brief", stage / "receipt"
    dsn = _local_reader_dsn(schema, role, password)
    env = {**os.environ, "ARB_MEMORY_LOCAL_DSN": dsn, "ARB_MEMORY_DSN": os.environ["ARB_MEMORY_DSN"]}
    brief_hydrate.hydrate(
        artefact_id=art_id, version=ver, output_path=out, receipt_path=receipt, env=env
    )
    names = {p.name for p in stage.iterdir()}
    assert names == {"brief", "receipt"}
    assert art_id not in "".join(names)


def test_symlink_destination_refused(scratch, local_reader_role, tmp_path, monkeypatch):
    from arb_memory import brief_hydrate

    schema = _schema(scratch)
    role, password = local_reader_role
    apply_local_reader_grants(scratch, role)
    art_id = f"art-sym-{secrets.token_hex(6)}"
    _, ver, _ = upsert_artefact(
        scratch, art_id, content=BODY_SENTINEL_V1, mime="text/markdown", author="t"
    )
    stage = _stage_dir(tmp_path)
    target = tmp_path / "outside"
    target.write_text("pre", encoding="utf-8")
    out = stage / "brief"
    out.symlink_to(target)
    receipt = stage / "receipt"
    dsn = _local_reader_dsn(schema, role, password)
    env = {**os.environ, "ARB_MEMORY_LOCAL_DSN": dsn, "ARB_MEMORY_DSN": os.environ["ARB_MEMORY_DSN"]}
    with pytest.raises(brief_hydrate.BriefHydrationError) as exc:
        brief_hydrate.hydrate(
            artefact_id=art_id, version=ver, output_path=out, receipt_path=receipt, env=env
        )
    assert exc.value.code in (
        "brief_hydration_write",
        "brief_hydration_invalid_path",
    )
    # Outside target must not be replaced with brief body.
    assert target.read_text(encoding="utf-8") == "pre"


def test_cli_entrypoint_subprocess(scratch, local_reader_role, tmp_path, monkeypatch):
    schema = _schema(scratch)
    role, password = local_reader_role
    apply_local_reader_grants(scratch, role)
    art_id = f"art-cli-{secrets.token_hex(6)}"
    _, ver, _ = upsert_artefact(
        scratch, art_id, content=BODY_SENTINEL_V1, mime="text/markdown", author="t"
    )
    stage = _stage_dir(tmp_path)
    out, receipt = stage / "brief", stage / "receipt"
    dsn = _local_reader_dsn(schema, role, password)
    env = {
        **os.environ,
        "ARB_MEMORY_LOCAL_DSN": dsn,
        "ARB_MEMORY_DSN": os.environ["ARB_MEMORY_DSN"],
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")
        + os.pathsep
        + os.environ.get("PYTHONPATH", ""),
    }
    proc = _run_hydrate(
        artefact_id=art_id, version=ver, output=out, receipt=receipt, env=env
    )
    assert proc.returncode == 0, proc.stderr
    assert out.is_file()
    assert receipt.is_file()
    assert BODY_SENTINEL_V1 not in proc.stdout
    meta = json.loads(proc.stdout.strip().splitlines()[-1])
    assert meta["artefact_id"] == art_id
    assert "content" not in meta
