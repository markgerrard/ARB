# ARB Memory read-model export Implementation Plan

> **Status: round 2 panel-confirmed 2026-07-06 (3-seat independent panel both rounds: codex +
> agy-print + cold-Opus) — ready to dispatch.** Round 1: no P0/P1; two findings
> cross-validated by independent reviewers (unquoted YAML frontmatter, vestigial `ORDER BY
> id`) plus one spec-wording fix (DSN resolution) — all addressed. Round 2 (targeted
> confirmation): all three reviewers `approve`/severity-none, no remaining or new findings.
> See "Round 1 panel record" and "Round 2 panel record" near the end.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a nightly, read-only, one-way exporter that projects the latest version of every
`artefacts` row (plus deduplicated tags from any linked, non-deleted `hints`) into a plain
markdown file per artefact, so a human can `grep`/browse ARB Memory's accumulated knowledge
without an LLM client in the loop.

**Architecture:** One new dedicated read-only DB role (`arb_vault_export`, wired through the
existing `grants` CLI by reusing `apply_local_reader_grants` — no new grants function), one new
module `src/arb_memory/vault_export.py` (the exporter logic, independently testable against a
scratch DB), and one thin CLI wrapper `scripts/arb-memory-vault-export` that the MCP-host box's
nightly cron invokes. Collision-free filenames use a `slug-idhash.md` scheme (a short SHA-256
prefix of the raw `artefact_id` is the collision-free identity; the slug is only for human
legibility), with an in-run fail-loud check as a backstop.

**Tech Stack:** Python 3, `psycopg` (psycopg3), `pytest`, the existing `arb_memory` package
conventions (`scratch`/`conn_factory` fixtures, `upsert_artefact`/`upsert_hint` from `store.py`,
`fake_embed` fixture — all already in `tests/arb_memory/conftest.py`, reused as-is).

## Global Constraints

- Read `docs/superpowers/specs/2026-07-06-arb-memory-read-model-export-design.md` in full before
  starting — it is 2-round panel-reviewed (codex + agy-print + cold-Opus) and this plan
  implements it exactly, including every "Round 1/2 panel finding, addressed" fix (hash-suffixed
  collision-free filenames, deterministic multi-hint tag resolution with `deleted_at IS NULL`,
  `exported_at` excluded from the idempotency test's byte-comparison, the deploy-time CLI grants
  wiring, MCP-host-box placement).
- **Frontmatter fields are exactly**: `artefact_id`, `version`, `source`, `author`,
  `created_at`, `content_hash`, `tags`, `exported_at` — no `description` field (the design's
  step 3 mentions a "one-line description" in passing, but step 5's authoritative frontmatter
  field list — the one the panel reviewed and approved — does not include it; this plan follows
  the reviewed field list literally, so hint resolution only needs to produce `tags`).
- **No new function in `src/arb_memory/mcp/grants.py`.** The exporter role reuses
  `apply_local_reader_grants` verbatim, exactly as the design specifies.
- **Every new test that touches Postgres uses the existing `scratch`/`conn_factory` fixtures**
  from `tests/arb_memory/conftest.py` (which apply the *full* `schema.sql` to a fresh scratch
  schema per test) — never a bare/partial schema, per the design's explicit callout that
  `apply_local_reader_grants` assumes a fully-migrated schema.
- **Do not unit-test cron/launchd wiring** — mirrors how this repo tests every other
  `scripts/*` wrapper (thin wrapper, tested via the module it calls into).

---

### Task 1: Wire `ARB_VAULT_EXPORT_ROLE` through the existing `grants` CLI

**Files:**
- Modify: `src/arb_memory/run.py:198-230` (`run_grants`)
- Create: `tests/arb_memory/test_vault_export_grants.py`

**Interfaces:**
- Consumes: `apply_local_reader_grants(conn, role: str) -> None` (existing,
  `src/arb_memory/mcp/grants.py:6`, unchanged by this task).
- Produces: nothing new consumed by later tasks — Task 2 does not depend on this task's CLI
  wiring (it calls `apply_local_reader_grants` directly in its own test fixture, same as
  `test_local_reader_grants.py` does today). This task is independently testable and mergeable
  on its own.

- [ ] **Step 1: Write the failing grants-shape test**

Create `tests/arb_memory/test_vault_export_grants.py`:

```python
import os
import secrets
import subprocess
import sys

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg import sql

from arb_memory.mcp.grants import apply_local_reader_grants


VAULT_EXPORT_ROLE_PREFIX = "arbmem_vault_export_test_"
SENSITIVE_TABLES = (
    "mcp_auth.oauth_clients",
    "mcp_auth.auth_codes",
    "mcp_auth.access_tokens",
    "mcp_auth.refresh_tokens",
    "mcp_auth.login_sessions",
    "mcp_auth.login_attempts",
    "audit_events",
    "audit_deadletter",
    "eval_event_raw",
    "eval_deadletter",
    "transcript_io",
    "transcript_deadletter",
    "write_deadletter",
    "idempotency_keys",
)


@pytest.fixture
def vault_export_role(scratch):
    role = f"{VAULT_EXPORT_ROLE_PREFIX}{secrets.token_hex(4)}"
    password = f"vault-export-test-{secrets.token_hex(16)}"
    try:
        scratch.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(role),
                sql.Literal(password),
            )
        )
    except psycopg.errors.InsufficientPrivilege:
        scratch.rollback()
        pytest.skip("substrate disallows CREATE ROLE; vault-export deny-proof requires role creation")
    try:
        yield role, password
    finally:
        scratch.execute("RESET ROLE")
        scratch.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        scratch.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def _has_priv(conn, role, obj, privilege):
    return conn.execute(
        "SELECT has_table_privilege(%s, %s, %s)",
        (role, obj, privilege),
    ).fetchone()[0]


def _dsn_with_schema(dsn, schema):
    params = conninfo_to_dict(dsn)
    params["options"] = f"-csearch_path={schema},public"
    return make_conninfo(**params)


def test_vault_export_role_is_select_only_on_hints_and_artefacts(scratch, vault_export_role):
    role, _password = vault_export_role

    apply_local_reader_grants(scratch, role)

    assert _has_priv(scratch, role, "hints", "SELECT")
    assert _has_priv(scratch, role, "artefacts", "SELECT")
    for obj in ("hints", "artefacts"):
        for privilege in ("INSERT", "UPDATE", "DELETE"):
            assert not _has_priv(scratch, role, obj, privilege), (
                f"{role} must not {privilege} {obj}"
            )
    for obj in SENSITIVE_TABLES:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert not _has_priv(scratch, role, obj, privilege), (
                f"{role} must not {privilege} {obj}"
            )


def test_grants_command_applies_vault_export_role(scratch, vault_export_role):
    schema = scratch.execute("SELECT current_schema()").fetchone()[0]
    role, _password = vault_export_role

    env = os.environ.copy()
    env["ARB_MEMORY_DSN"] = _dsn_with_schema(os.environ["ARB_MEMORY_DSN"], schema)
    env["ARB_VAULT_EXPORT_ROLE"] = role
    env["PYTHONPATH"] = os.environ.get("PYTHONPATH", "")

    res = subprocess.run(
        [sys.executable, "-m", "arb_memory", "grants"],
        capture_output=True,
        text=True,
        env=env,
    )

    assert res.returncode == 0, res.stderr
    assert f"vault-export-role='{role}'" in res.stdout
    assert _has_priv(scratch, role, "hints", "SELECT")
    assert _has_priv(scratch, role, "artefacts", "SELECT")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python3 -m pytest tests/arb_memory/test_vault_export_grants.py -v`

Expected:
```
test_vault_export_role_is_select_only_on_hints_and_artefacts PASSED
test_grants_command_applies_vault_export_role FAILED
  (AssertionError: assert "vault-export-role='...'" in res.stdout -- run.py doesn't print or apply it yet)
```
The first test passes already (it calls `apply_local_reader_grants` directly, which already
exists and already does the right thing — this is expected, not a bug; it's a smoke check that
the fixture works). The second test is the real red — it drives the actual CLI entrypoint,
which doesn't know about `ARB_VAULT_EXPORT_ROLE` yet.

- [ ] **Step 3: Wire `ARB_VAULT_EXPORT_ROLE` into `run_grants()`**

In `src/arb_memory/run.py`, `run_grants()` currently reads (lines 198-230):

```python
def run_grants() -> None:
    import psycopg

    from arb_memory.mcp.config import mcp_role_name
    from arb_memory.mcp.grants import (
        apply_eval_grants,
        apply_local_reader_grants,
        apply_mcp_grants,
        apply_transcript_grants,
        apply_visibility_grants,
    )

    dsn = os.environ["ARB_MEMORY_DSN"]
    with psycopg.connect(dsn) as conn:
        consumer_role = os.environ.get("ARB_EVAL_CONSUMER_ROLE") or conn.info.user
        transcript_role = os.environ.get("ARB_TRANSCRIPT_CONSUMER_ROLE") or consumer_role
        visibility_role = os.environ.get("ARB_VISIBILITY_GATEWAY_ROLE")
        local_reader_role = os.environ.get("ARB_MEMORY_LOCAL_READER_ROLE")
        mcp_role = mcp_role_name()
        apply_eval_grants(conn, consumer_role)
        apply_transcript_grants(conn, transcript_role)
        apply_mcp_grants(conn, mcp_role)
        if visibility_role:
            apply_visibility_grants(conn, visibility_role)
        if local_reader_role:
            apply_local_reader_grants(conn, local_reader_role)
        conn.commit()
    print(
        f"grants applied: eval-consumer={consumer_role!r} "
        f"transcript-consumer={transcript_role!r} mcp-role={mcp_role!r} "
        f"visibility-gateway-role={visibility_role!r} "
        f"local-reader-role={local_reader_role!r}"
    )
```

Change to:

```python
def run_grants() -> None:
    import psycopg

    from arb_memory.mcp.config import mcp_role_name
    from arb_memory.mcp.grants import (
        apply_eval_grants,
        apply_local_reader_grants,
        apply_mcp_grants,
        apply_transcript_grants,
        apply_visibility_grants,
    )

    dsn = os.environ["ARB_MEMORY_DSN"]
    with psycopg.connect(dsn) as conn:
        consumer_role = os.environ.get("ARB_EVAL_CONSUMER_ROLE") or conn.info.user
        transcript_role = os.environ.get("ARB_TRANSCRIPT_CONSUMER_ROLE") or consumer_role
        visibility_role = os.environ.get("ARB_VISIBILITY_GATEWAY_ROLE")
        local_reader_role = os.environ.get("ARB_MEMORY_LOCAL_READER_ROLE")
        vault_export_role = os.environ.get("ARB_VAULT_EXPORT_ROLE")
        mcp_role = mcp_role_name()
        apply_eval_grants(conn, consumer_role)
        apply_transcript_grants(conn, transcript_role)
        apply_mcp_grants(conn, mcp_role)
        if visibility_role:
            apply_visibility_grants(conn, visibility_role)
        if local_reader_role:
            apply_local_reader_grants(conn, local_reader_role)
        if vault_export_role:
            apply_local_reader_grants(conn, vault_export_role)
        conn.commit()
    print(
        f"grants applied: eval-consumer={consumer_role!r} "
        f"transcript-consumer={transcript_role!r} mcp-role={mcp_role!r} "
        f"visibility-gateway-role={visibility_role!r} "
        f"local-reader-role={local_reader_role!r} "
        f"vault-export-role={vault_export_role!r}"
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python3 -m pytest tests/arb_memory/test_vault_export_grants.py -v`

Expected: both `PASSED`.

- [ ] **Step 5: Run the full arb_memory grants test suite to confirm no regression**

Run: `.venv/bin/python3 -m pytest tests/arb_memory/test_local_reader_grants.py tests/arb_memory/test_visibility_grants.py tests/arb_memory/test_run_grants.py tests/arb_memory/test_vault_export_grants.py -v`

Expected: all `PASSED` (the existing grants tests must be unaffected by the new `if
vault_export_role:` branch — it's additive and only fires when the env var is set, which none
of the existing tests set).

- [ ] **Step 6: Commit**

```bash
git add src/arb_memory/run.py tests/arb_memory/test_vault_export_grants.py
git commit -m "$(cat <<'EOF'
feat(arb-memory): wire ARB_VAULT_EXPORT_ROLE through the grants CLI

run_grants() only applied ARB_MEMORY_LOCAL_READER_ROLE -- there was no path
to apply or deploy-time-audit grants for a second, independent read-only
role. Reuses apply_local_reader_grants verbatim (same privilege shape, a
different role name so the two credentials can be revoked independently).

Guardrail #1 from docs/superpowers/specs/2026-07-06-arb-memory-read-model-export-design.md
requires this be wired at deploy time, not just possible in principle.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0181p416dRegs8A3Msq5WLkp
EOF
)"
```

---

### Task 2: `vault_export.py` — the exporter module

**Files:**
- Create: `src/arb_memory/vault_export.py`
- Create: `tests/arb_memory/test_vault_export.py`

**Interfaces:**
- Consumes: `upsert_artefact(conn, artefact_id, *, content=None, content_bytes=None,
  mime="text/plain", repo_pointer=None, source="seat", author="unknown") -> tuple[str, int]`
  and `upsert_hint(conn, text, embedding, *, artefact_id=None, artefact_version=None,
  repo_pointer=None, metadata=None, source="seat", author="unknown") -> int` (both existing,
  `src/arb_memory/store.py`, unchanged by this task). `fake_embed` fixture (existing,
  `tests/arb_memory/conftest.py`, returns a 1536-dim unit vector for any text).
- Produces: `export_vault(conn, vault_root: str) -> dict` — the function Task 3's CLI wrapper
  calls. Returns `{"written": int}`. Also produces `resolve_settings(env) ->
  VaultExportSettings` (dataclass with `.dsn: str`, `.vault_root: str`) and `main() -> int`,
  both consumed only by Task 3.

- [ ] **Step 1: Write the failing tests**

Create `tests/arb_memory/test_vault_export.py`:

```python
from pathlib import Path

import pytest

from arb_memory.store import upsert_artefact, upsert_hint
from arb_memory.vault_export import export_vault, _filename, _idhash, _slug


def _read(vault_root, artefact_id):
    path = Path(vault_root) / _filename(artefact_id)
    return path.read_text()


def test_only_latest_version_is_rendered(scratch, tmp_path):
    upsert_artefact(scratch, "spec-a", content="v1 body", source="seat", author="mark")
    upsert_artefact(scratch, "spec-a", content="v2 body", source="seat", author="mark")

    export_vault(scratch, str(tmp_path))

    body = _read(tmp_path, "spec-a")
    assert "v2 body" in body
    assert "v1 body" not in body
    assert "version: 2" in body


def test_tags_from_single_linked_hint_appear_in_frontmatter(scratch, tmp_path, fake_embed):
    _, version = upsert_artefact(scratch, "spec-b", content="body", source="seat", author="mark")
    upsert_hint(
        scratch, "a hint about spec-b", fake_embed("a hint about spec-b"),
        artefact_id="spec-b", artefact_version=version,
        metadata={"tags": ["review", "design"]},
    )

    export_vault(scratch, str(tmp_path))

    body = _read(tmp_path, "spec-b")
    assert 'tags: ["design", "review"]' in body


def test_tags_from_multiple_linked_hints_are_deduplicated_and_sorted(scratch, tmp_path, fake_embed):
    _, version = upsert_artefact(scratch, "spec-c", content="body", source="seat", author="mark")
    upsert_hint(
        scratch, "first hint", fake_embed("first hint"),
        artefact_id="spec-c", artefact_version=version,
        metadata={"tags": ["zeta", "review"]},
    )
    upsert_hint(
        scratch, "second hint", fake_embed("second hint"),
        artefact_id="spec-c", artefact_version=version,
        metadata={"tags": ["review", "alpha"]},
    )

    export_vault(scratch, str(tmp_path))

    body = _read(tmp_path, "spec-c")
    assert 'tags: ["alpha", "review", "zeta"]' in body


def test_deleted_hint_is_excluded_from_tags(scratch, tmp_path, fake_embed):
    _, version = upsert_artefact(scratch, "spec-d", content="body", source="seat", author="mark")
    hint_id = upsert_hint(
        scratch, "soft-deleted hint", fake_embed("soft-deleted hint"),
        artefact_id="spec-d", artefact_version=version,
        metadata={"tags": ["should-not-appear"]},
    )
    scratch.execute("UPDATE hints SET deleted_at = now() WHERE id = %s", (hint_id,))

    export_vault(scratch, str(tmp_path))

    body = _read(tmp_path, "spec-d")
    assert "should-not-appear" not in body
    assert "tags: []" in body


def test_artefact_with_no_linked_hint_has_empty_tags(scratch, tmp_path):
    upsert_artefact(scratch, "spec-e", content="body", source="seat", author="mark")

    export_vault(scratch, str(tmp_path))

    body = _read(tmp_path, "spec-e")
    assert "tags: []" in body


def test_binary_artefact_gets_placeholder_not_decode_attempt(scratch, tmp_path):
    upsert_artefact(
        scratch, "spec-f", content_bytes=b"\x00\x01\x02",
        mime="application/octet-stream", source="seat", author="mark",
    )

    export_vault(scratch, str(tmp_path))

    body = _read(tmp_path, "spec-f")
    assert "binary artefact" in body
    assert "application/octet-stream" in body
    assert "3 bytes" in body


def test_distinct_slug_colliding_artefact_ids_get_distinct_files(scratch, tmp_path):
    upsert_artefact(scratch, "docs/foo.md", content="first", source="seat", author="mark")
    upsert_artefact(scratch, "docsfoo.md", content="second", source="seat", author="mark")

    export_vault(scratch, str(tmp_path))

    assert _slug("docs/foo.md") == _slug("docsfoo.md") == "docsfoo.md"
    assert _idhash("docs/foo.md") != _idhash("docsfoo.md")
    first_body = _read(tmp_path, "docs/foo.md")
    second_body = _read(tmp_path, "docsfoo.md")
    assert "first" in first_body
    assert "second" in second_body


def test_hostile_artefact_id_cannot_escape_vault_root(scratch, tmp_path):
    upsert_artefact(scratch, "../../etc/passwd", content="hostile", source="seat", author="mark")

    export_vault(scratch, str(tmp_path))

    # slug() strips "/" (not "."), so "../../etc/passwd" -> "....etcpasswd" -- a filename
    # made only of dots and letters, no path separator, so it cannot address a parent
    # directory. The safety property to assert is "the written file's parent is exactly
    # vault_root, nothing escaped it" -- not "no dots in the name" (dots are allowed and
    # expected here; only "/" enables traversal).
    written = list(Path(tmp_path).iterdir())
    assert len(written) == 1
    assert written[0].parent == Path(tmp_path)
    assert "/" not in written[0].name


def test_idhash_collision_fails_loud(scratch, tmp_path, monkeypatch):
    upsert_artefact(scratch, "id-one", content="a", source="seat", author="mark")
    upsert_artefact(scratch, "id-two", content="b", source="seat", author="mark")

    import arb_memory.vault_export as vault_export_module

    monkeypatch.setattr(vault_export_module, "_idhash", lambda artefact_id: "deadbeef")

    with pytest.raises(RuntimeError, match="idhash collision"):
        export_vault(scratch, str(tmp_path))


def test_rerun_is_idempotent_except_exported_at(scratch, tmp_path):
    upsert_artefact(scratch, "spec-g", content="stable body", source="seat", author="mark")

    export_vault(scratch, str(tmp_path))
    first = _read(tmp_path, "spec-g")

    export_vault(scratch, str(tmp_path))
    second = _read(tmp_path, "spec-g")

    def _strip_exported_at(text):
        return "\n".join(line for line in text.splitlines() if not line.startswith("exported_at:"))

    assert _strip_exported_at(first) == _strip_exported_at(second)
    assert "exported_at: " in first
    assert "exported_at: " in second
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python3 -m pytest tests/arb_memory/test_vault_export.py -v`

Expected: every test `ERROR`s on collection with `ModuleNotFoundError: No module named
'arb_memory.vault_export'` — `vault_export.py` doesn't exist yet.

- [ ] **Step 3: Implement `vault_export.py`**

Create `src/arb_memory/vault_export.py`:

```python
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import psycopg


SLUG_DISALLOWED_RE = re.compile(r"[^A-Za-z0-9._-]")

_ARTEFACT_COLS = (
    "artefact_id",
    "version",
    "content",
    "content_bytes",
    "content_mime",
    "repo_pointer",
    "content_hash",
    "source",
    "author",
    "created_at",
)


@dataclass
class VaultExportSettings:
    dsn: str
    vault_root: str


def resolve_settings(env) -> VaultExportSettings:
    dsn = env["ARB_VAULT_EXPORT_DSN"]
    vault_root = env["ARB_VAULT_EXPORT_ROOT"]
    return VaultExportSettings(dsn=dsn, vault_root=vault_root)


def _slug(artefact_id: str) -> str:
    return SLUG_DISALLOWED_RE.sub("", artefact_id)


def _idhash(artefact_id: str) -> str:
    return hashlib.sha256(artefact_id.encode("utf-8")).hexdigest()[:8]


def _filename(artefact_id: str) -> str:
    return f"{_slug(artefact_id)}-{_idhash(artefact_id)}.md"


def _latest_artefacts(conn) -> list[dict]:
    rows = conn.execute(
        f"""
        SELECT DISTINCT ON (artefact_id) {", ".join(_ARTEFACT_COLS)}
        FROM artefacts
        ORDER BY artefact_id, version DESC
        """
    ).fetchall()
    return [dict(zip(_ARTEFACT_COLS, row)) for row in rows]


def _linked_tags(conn, artefact_id: str, version: int) -> list[str]:
    # No ORDER BY: tags are unioned into a set and sorted below, so row order from
    # Postgres is irrelevant here (unlike an earlier design iteration that also picked a
    # "first hint wins" description field by lowest id -- that field was dropped to match
    # the panel-approved frontmatter list, and this query no longer needs a tie-break).
    rows = conn.execute(
        """
        SELECT metadata
        FROM hints
        WHERE artefact_id = %s AND artefact_version = %s AND deleted_at IS NULL
        """,
        (artefact_id, version),
    ).fetchall()
    tags: set[str] = set()
    for (metadata,) in rows:
        tags.update(metadata.get("tags", []) or [])
    return sorted(tags)


def _frontmatter(artefact: dict, tags: list[str], exported_at: str) -> str:
    # json.dumps() for every caller-influenced string field: artefact_id/source/author are
    # caller-chosen strings with no format validation (store.py's write path enforces none),
    # and tags come from hint metadata a caller also controls. A YAML-hostile value (a colon
    # followed by a space, a "#", an embedded quote) in an unquoted or naively-quoted field
    # would either break frontmatter parsers or silently truncate the value. JSON string/array
    # syntax is valid YAML flow syntax, so this is also valid frontmatter.
    lines = [
        "---",
        f"artefact_id: {json.dumps(artefact['artefact_id'])}",
        f"version: {artefact['version']}",
        f"source: {json.dumps(artefact['source'])}",
        f"author: {json.dumps(artefact['author'])}",
        f"created_at: {artefact['created_at'].isoformat()}",
        f"content_hash: {json.dumps(artefact['content_hash'])}",
        f"tags: {json.dumps(tags)}",
        f"exported_at: {exported_at}",
        "---",
        "",
    ]
    return "\n".join(lines)


def _body(artefact: dict) -> str:
    if artefact["content"] is not None:
        return artefact["content"]
    content_bytes = artefact["content_bytes"] or b""
    return (
        f"_binary artefact, content_mime: {artefact['content_mime']}, "
        f"{len(content_bytes)} bytes — not rendered_\n"
    )


def export_vault(conn, vault_root: str) -> dict:
    root = Path(vault_root)
    root.mkdir(parents=True, exist_ok=True)
    exported_at = datetime.now(timezone.utc).isoformat()

    seen_hashes: dict[str, str] = {}
    written = 0
    for artefact in _latest_artefacts(conn):
        artefact_id = artefact["artefact_id"]
        idhash = _idhash(artefact_id)
        if idhash in seen_hashes and seen_hashes[idhash] != artefact_id:
            raise RuntimeError(
                f"idhash collision: {artefact_id!r} and {seen_hashes[idhash]!r} "
                f"both hash to {idhash!r}"
            )
        seen_hashes[idhash] = artefact_id

        tags = _linked_tags(conn, artefact_id, artefact["version"])
        text = _frontmatter(artefact, tags, exported_at) + _body(artefact)
        (root / _filename(artefact_id)).write_text(text, encoding="utf-8")
        written += 1

    return {"written": written}


def main() -> int:
    settings = resolve_settings(os.environ)
    with psycopg.connect(settings.dsn) as conn:
        result = export_vault(conn, settings.vault_root)
    print(f"vault export complete: {result['written']} artefacts written to {settings.vault_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python3 -m pytest tests/arb_memory/test_vault_export.py -v`

Expected: all 10 tests `PASSED`.

- [ ] **Step 5: Commit**

```bash
git add src/arb_memory/vault_export.py tests/arb_memory/test_vault_export.py
git commit -m "$(cat <<'EOF'
feat(arb-memory): vault_export module — markdown export of latest artefacts

One markdown file per artefact_id (latest version only), frontmatter with
provenance + deduplicated-sorted hint tags (deleted_at IS NULL filtered,
matching every other hint read path in store.py). Filenames are
slug-idhash.md, where idhash is a short SHA-256 prefix of the raw
artefact_id -- the collision-free identity; an in-run map fails loud on the
(cryptographically negligible) case of two ids sharing an idhash. Binary
artefacts get a placeholder line instead of a decode attempt.

Implements docs/superpowers/specs/2026-07-06-arb-memory-read-model-export-design.md
Architecture steps 2-9, 2-round panel-reviewed (codex + agy-print + cold-Opus).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0181p416dRegs8A3Msq5WLkp
EOF
)"
```

---

### Task 3: `scripts/arb-memory-vault-export` — thin CLI wrapper

**Files:**
- Create: `scripts/arb-memory-vault-export`

**Interfaces:**
- Consumes: `arb_memory.vault_export.main() -> int` (from Task 2, unchanged).
- Produces: nothing consumed by a later task — this is the last task in this plan.

- [ ] **Step 1: Create the wrapper**

Create `scripts/arb-memory-vault-export`:

```python
#!/usr/bin/env python3
"""Nightly one-way ARB Memory read-model export to a markdown vault.

Requires ARB_VAULT_EXPORT_DSN (a DSN authenticating as the arb_vault_export
role -- see `python -m arb_memory grants` with ARB_VAULT_EXPORT_ROLE set) and
ARB_VAULT_EXPORT_ROOT (the output directory) in the process environment.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from arb_memory.vault_export import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Make it executable**

Run: `chmod +x scripts/arb-memory-vault-export`

- [ ] **Step 3: Manually verify it runs end-to-end against a real (scratch) database**

This is the one step in this plan that isn't a `pytest` assertion — it proves the wrapper's
`sys.path` bootstrap and env-var reading actually work outside the test harness, which
`test_vault_export.py` (calling `export_vault` directly, in-process) cannot prove by itself.

```bash
export ARB_VAULT_EXPORT_DSN="$ARB_MEMORY_DSN"   # any reachable dev/test Postgres is fine here
export ARB_VAULT_EXPORT_ROOT=/tmp/arb-vault-export-manual-check
rm -rf "$ARB_VAULT_EXPORT_ROOT"
python3 scripts/arb-memory-vault-export
ls "$ARB_VAULT_EXPORT_ROOT" | head
```

Expected: prints `vault export complete: N artefacts written to /tmp/arb-vault-export-manual-check`
and the directory contains one `.md` file per artefact currently in whichever DB
`ARB_VAULT_EXPORT_DSN` points at. Clean up afterward: `rm -rf /tmp/arb-vault-export-manual-check`.

- [ ] **Step 4: Commit**

```bash
git add scripts/arb-memory-vault-export
git commit -m "$(cat <<'EOF'
feat(arb-memory): scripts/arb-memory-vault-export CLI wrapper

Thin wrapper over arb_memory.vault_export.main(), matching this repo's
established scripts/ convention (thin wrapper, src/ does the work). Deploy
target is the MCP-host box's nightly cron per the design's placement
decision -- not the mac-mini, and not a new container.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_0181p416dRegs8A3Msq5WLkp
EOF
)"
```

---

## Definition of Done

- [ ] `tests/arb_memory/test_vault_export_grants.py` — both tests pass.
- [ ] `tests/arb_memory/test_vault_export.py` — all 10 tests pass.
- [ ] `.venv/bin/python3 -m pytest tests/arb_memory/ -q` — full suite green, no regressions.
- [ ] `scripts/arb-memory-vault-export` is executable and manually verified end-to-end against
  a real (scratch or dev) Postgres instance.
- [ ] All three commits reference the design spec's panel-review provenance.
- [ ] This plan does **not** cover: standing up the `arb_vault_export` role in prod, wiring the
  actual nightly cron entry on the MCP-host box, or setting `ARB_VAULT_EXPORT_DSN`/
  `ARB_VAULT_EXPORT_ROOT`/`ARB_VAULT_EXPORT_ROLE` in any real env file — those are deploy-time
  operational steps for whoever has MCP-host-box access, not code changes, and are explicitly
  out of scope for a codex dispatch (no such access from inside a bridge worktree).

## Self-review notes

- **Spec coverage:** Architecture steps 2-3 (latest-version query, deterministic tag
  resolution with `deleted_at IS NULL`) → Task 2 `_latest_artefacts`/`_linked_tags`. Step 4
  (hash-suffixed collision-free filenames, fail-loud) → Task 2 `_filename`/`_idhash` +
  `export_vault`'s `seen_hashes` check. Step 5 (frontmatter fields, `exported_at` excluded from
  idempotency) → Task 2 `_frontmatter` + `test_rerun_is_idempotent_except_exported_at`. Step 6
  (binary placeholder) → Task 2 `_body` + `test_binary_artefact_gets_placeholder_not_decode_attempt`.
  Steps 7-9 (full-rewrite, no GC, idempotent overwrite) → Task 2's `export_vault` design (no
  incremental/diff logic, no deletion pass). The deploy-time CLI grants wiring finding → Task 1.
  The MCP-host-box placement decision is deploy-time, not code — explicitly listed as out of
  scope in Definition of Done, not silently dropped.
- **Placeholder scan:** no TBD/TODO. Every file path, function name, env var name, and test
  name is concrete.
- **Type consistency:** `export_vault(conn, vault_root: str) -> dict` in Task 2 is the exact
  signature Task 3's `main()` calls; `VaultExportSettings.dsn`/`.vault_root` match
  `resolve_settings`'s construction; `_filename`/`_idhash`/`_slug` names match between Task 2's
  implementation and its own tests (no drift, single file).

## Round 1 panel record

Independent 3-seat panel (codex-bridge-dev, agy-bridge-dev, cold-Opus subagent), run
`panel-vault-export-plan-20260706T122927Z-bb3ef6`. Stances: codex `needs-changes`/P2 (DSN
resolution wording — traced to an imprecise sentence in the *spec*, fixed there, see that
document's "Round 1 plan-review finding, addressed" callout), agy `needs-changes`/P2 (unquoted
YAML frontmatter fields — fixed above via `json.dumps()` on every caller-influenced string
field), cold-Opus `approve`/P2 (three cosmetic notes: the same unquoted-frontmatter finding
independently convergent with agy's, a vestigial `ORDER BY id` in `_linked_tags` independently
convergent with agy's informational note, and a test-comment dot-count error). No P0/P1 from
any seat; all reviewers independently verified every literal code claim (SQL, collision
defense, path-safety, idempotency test) against real source and found them sound. All findings
addressed inline above. Plan is ready to dispatch.

## Round 2 panel record

Same three seats, targeted confirmation pass, run
`panel-vault-export-plan-r2-20260706T123650Z-9f44cc`, `supersedes` round 1's run. Stances:
codex `approve`/none, agy `approve`/none, cold-Opus `approve`/none — unanimous, no remaining
or new findings from any seat. Cold-Opus additionally verified `content_hash` is safe under
`json.dumps()` (a `str` from `hashlib.hexdigest()`, per `schema.sql:10` and `hash.py:11-12`,
not `bytes`/`memoryview`, which would have raised `TypeError`). Plan is ready to dispatch.
