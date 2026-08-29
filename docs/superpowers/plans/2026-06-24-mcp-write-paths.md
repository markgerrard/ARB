# MCP Write Paths Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `memory_store` + `memory_remember` write tools to the public ARB Memory MCP door, which (holding NO redis client) publishes write-intents via an authenticated HTTP call to an internal writer proxy that owns the bus credential — so the internet-exposed door cannot speak the bus by construction, and a synchronous publish fails loud rather than masking a lost write.

**Architecture (v6 — publish-proxy):** The door (internet-exposed, **holds NO redis client** — a tested invariant) validates + scope-gates a write, then makes one **authenticated HTTP POST** to an internal **writer** service. The writer (not internet-exposed, holds the bus credential) publishes the intent to the remote `writes_stream` synchronously and returns the ULID. The remote single-writer consumer is unchanged except its deterministic-bad path is upgraded from silent ack-drop to a deadletter table. **Fail-loud is structural:** the synchronous POST means writer/bus-down ⇒ the door returns "not accepted"; no buffer ⇒ no silent loss. (No sidecar / relay / heartbeat — removed in v6.)

**Tech Stack:** Python 3, psycopg (Postgres + pgvector), redis-py (bus, **writer-side only**), httpx (door→writer), Starlette (writer app), FastMCP / MCP Python SDK auth, pytest, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-06-24-mcp-write-paths-design.md` (v6). Read its lineage block before starting — especially **why the door must stay redis-free** (the structural invariant guarded by `test_mcp_readonly_import.py`).

## Bridge execution

This plan is executed by a **codex** subagent dispatched via the bridge into a **worktree** (so its commits don't interleave with the orchestrator's checkout). The orchestrator pre-creates the worktree and dispatches per task; codex `cd`s into the worktree path given in the task body, runs the TDD cycle, and commits there. Verify each task from git (the SHA + diff + test run), not from the reply prose.

## Global Constraints

- **The door holds NO redis/valkey client (structural invariant).** `tests/arb_memory/test_mcp_readonly_import.py` (both tests) MUST stay green — no redis import in any `mcp/` file, none in the runtime import graph of `arb_memory.mcp.server`. The door publishes via `httpx` to the writer; `import redis`/`from arb_memory import bus` must NOT appear anywhere reachable from the door.
- **Door DB role stays read-only.** The door never writes Postgres directly and never embeds; the read-only `conn_factory` is used only for the linked-artefact preflight (a `SELECT`). `test_mcp_role_positive_control_denies_memory_write` stays green.
- **Bus credential lives only in non-exposed processes** (the `writer` and `memory` services), never in the `mcp` door. The `mcp` compose service has **no** `REDIS`/`VALKEY` env (it gets `ARB_MEMORY_MCP_WRITER_URL` + `ARB_MEMORY_WRITER_TOKEN`).
- **`accepted` is structural fail-loud.** The door's POST to the writer is synchronous: the writer must return `2xx` (intent published) before the door returns `accepted`. Writer unreachable or bus down ⇒ loud `RuntimeError("memory store unavailable — item NOT stored")`, no `accepted`. No buffer ⇒ no silent loss.
- **No silent drops.** Deterministic-bad write intents deadletter at the consumer; infra errors retry. (memory `evidence-store-no-silent-drop`)
- **Provenance:** MCP-origin writes stamp `source="mcp"`, `author=<client>` on both artefacts and hints.
- **Single scope `memory.write`**, default-granted; resolved via `get_access_token().scopes`; anonymous/`None` ⇒ deny.
- **Defaults (verbatim):** `write_rate_per_min=30`, `write_max_content_bytes=262144`, `write_max_text_chars=8192`, mime allow-list `{"text/plain","text/markdown","application/json"}`, artefact_id regex `^[A-Za-z0-9_-]{1,64}$`, derived id `art-<sha256(content,mime)[:16]>`. Writer env: `ARB_MEMORY_MCP_WRITER_URL`, `ARB_MEMORY_WRITER_TOKEN`, `ARB_MEMORY_WRITER_HOST=0.0.0.0`, `ARB_MEMORY_WRITER_PORT=8800`.
- **TDD:** every step writes the failing test first, runs it red, implements minimal, runs green, commits. Tests requiring a DB use the `scratch`/`conn_factory` fixtures (skip without `ARB_MEMORY_DSN`); tests requiring Redis use `redis_bus` (skip without Redis). NEVER run the live-wiping MCP suite (`test_dcr.py clean_mcp_auth`) against a production DB.

## File Structure

- `src/arb_memory/schema.sql` — add `source`/`author` to `artefacts`; add `write_deadletter` table.
- `src/arb_memory/store.py` — `upsert_artefact` + `write_artefact_and_hints` + `fetch_artefact` carry `source`/`author`.
- `src/arb_memory/bus.py` — `write_deadletter` write in the consumer (Task 2). **No relay/heartbeat.**
- `src/arb_memory/writer.py` — **NEW** internal writer proxy: Starlette app, `POST /publish` (bearer auth) → `bus.memory_write`.
- `src/arb_memory/mcp/config.py` — new `Settings` fields (write caps/rate only).
- `src/arb_memory/mcp/tools.py` — validation/scope helpers + `memory_store` + `memory_remember` (publish via httpx). **No redis import.**
- `src/arb_memory/mcp/server.py` — `memory.write` scope; build httpx client + writer URL/token; register write tools. **No redis import.**
- `src/arb_memory/run.py` — `run_writer()` + `writer` route.
- `deploy/docker-compose.yml` — internal `writer` service (holds bus URL + token, no published port); door gets `ARB_MEMORY_MCP_WRITER_URL` + `ARB_MEMORY_WRITER_TOKEN` (no redis env).
- Tests: `test_store_provenance.py`, `test_write_deadletter.py`, `test_write_validation.py`, `test_write_tools.py`, `test_writer.py`; **update** `test_compose_shape.py` + `test_mcp_tools.py` (scope); `test_mcp_readonly_import.py` stays green unchanged.

---

### Task 1: Artefact provenance (`source`/`author`)

**Files:**
- Modify: `src/arb_memory/schema.sql:3-15` (artefacts table)
- Modify: `src/arb_memory/store.py:20-52` (`upsert_artefact`), `:98-129` (`write_artefact_and_hints`), `:203-226` (`fetch_artefact`)
- Test: `tests/arb_memory/test_store_provenance.py`

**Interfaces:**
- Produces: `store.upsert_artefact(conn, artefact_id, *, content=None, content_bytes=None, mime="text/plain", repo_pointer=None, source="seat", author="unknown")`; `store.write_artefact_and_hints` reads `artefact["source"]`/`artefact["author"]`; `store.fetch_artefact(...)` returns dict now including `"source"`, `"author"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/arb_memory/test_store_provenance.py
from arb_memory import store


def test_artefact_persists_source_and_author(conn_factory, fake_embed):
    conn = conn_factory()
    store.write_artefact_and_hints(
        conn,
        artefact={"artefact_id": "art-prov1", "content": "hello", "mime": "text/plain",
                  "source": "mcp", "author": "cid-abc"},
    )
    row = store.fetch_artefact(conn, "art-prov1", 1)
    assert row["source"] == "mcp"
    assert row["author"] == "cid-abc"


def test_artefact_defaults_source_author(conn_factory):
    conn = conn_factory()
    store.upsert_artefact(conn, "art-prov2", content="x", mime="text/plain")
    row = store.fetch_artefact(conn, "art-prov2", 1)
    assert row["source"] == "seat"
    assert row["author"] == "unknown"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ARB_MEMORY_DSN=$ARB_MEMORY_DSN pytest tests/arb_memory/test_store_provenance.py -v`
Expected: FAIL — `KeyError: 'source'` (fetch_artefact doesn't return it) / column does not exist.

- [ ] **Step 3: Add columns to schema (fresh + migration)**

In `src/arb_memory/schema.sql`, change the artefacts table and add idempotent ALTERs right after the `CREATE INDEX ... artefacts_id_idx` line:

```sql
CREATE TABLE IF NOT EXISTS artefacts (
    artefact_id   text    NOT NULL,
    version       int     NOT NULL,
    content       text,
    content_bytes bytea,
    content_mime  text    NOT NULL DEFAULT 'text/plain',
    repo_pointer  text,
    content_hash  text    NOT NULL,
    source        text    NOT NULL DEFAULT 'seat',
    author        text    NOT NULL DEFAULT 'unknown',
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (artefact_id, version),
    UNIQUE (artefact_id, content_hash)
);
CREATE INDEX IF NOT EXISTS artefacts_id_idx ON artefacts (artefact_id);
ALTER TABLE artefacts ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'seat';
ALTER TABLE artefacts ADD COLUMN IF NOT EXISTS author text NOT NULL DEFAULT 'unknown';
```

- [ ] **Step 4: Thread source/author through store.py**

`upsert_artefact` — add params and include in INSERT:

```python
def upsert_artefact(
    conn,
    artefact_id,
    *,
    content=None,
    content_bytes=None,
    mime="text/plain",
    repo_pointer=None,
    source="seat",
    author="unknown",
) -> tuple[str, int]:
    _register_vector(conn)
    content_hash = artefact_hash(content, content_bytes, mime)
    row = conn.execute(
        "SELECT version FROM artefacts WHERE artefact_id = %s AND content_hash = %s",
        (artefact_id, content_hash),
    ).fetchone()
    if row is not None:
        return artefact_id, row[0]
    row = conn.execute(
        "SELECT COALESCE(max(version), 0) + 1 FROM artefacts WHERE artefact_id = %s",
        (artefact_id,),
    ).fetchone()
    version = row[0]
    conn.execute(
        """
        INSERT INTO artefacts (
            artefact_id, version, content, content_bytes, content_mime, repo_pointer,
            content_hash, source, author
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (artefact_id, version, content, content_bytes, mime, repo_pointer,
         content_hash, source, author),
    )
    return artefact_id, version
```

In `write_artefact_and_hints`, pass them through from the artefact dict:

```python
        if artefact is not None:
            artefact_ref = upsert_artefact(
                conn,
                artefact["artefact_id"],
                content=artefact.get("content"),
                content_bytes=artefact.get("content_bytes"),
                mime=artefact.get("mime", "text/plain"),
                repo_pointer=artefact.get("repo_pointer"),
                source=artefact.get("source", "seat"),
                author=artefact.get("author", "unknown"),
            )
```

In `fetch_artefact`, add the columns to the SELECT and `cols` tuple (both occurrences of the column list — `fetch_artefact` and `recent_artefacts`):

```python
        SELECT artefact_id, version, content, content_bytes, content_mime,
               repo_pointer, content_hash, source, author, created_at
        ...
    cols = (
        "artefact_id", "version", "content", "content_bytes", "content_mime",
        "repo_pointer", "content_hash", "source", "author", "created_at",
    )
```

(Apply the identical SELECT-column + `cols` change in `recent_artefacts` too.)

- [ ] **Step 5: Run tests green + commit**

Run: `ARB_MEMORY_DSN=$ARB_MEMORY_DSN pytest tests/arb_memory/test_store_provenance.py -v`
Expected: PASS (2 passed). Then run the existing store/consumer tests to confirm no regression:
Run: `ARB_MEMORY_DSN=$ARB_MEMORY_DSN pytest tests/arb_memory/ -k "store or write or consumer" -q`
Expected: all green.

```bash
git add src/arb_memory/schema.sql src/arb_memory/store.py tests/arb_memory/test_store_provenance.py
git commit -m "feat(arb-memory): artefact source/author provenance columns"
```

---

### Task 2: Consumer deadletter (no silent drop)

**Files:**
- Modify: `src/arb_memory/schema.sql` (add `write_deadletter` table near `idempotency_keys`)
- Modify: `src/arb_memory/bus.py:194-212` (`_handle_entry`), add `_deadletter(...)` to `WriteLoop`
- Test: `tests/arb_memory/test_write_deadletter.py`

**Interfaces:**
- Consumes: `WriteLoop(redis, conn, *, embed, prefix, consumer, block_ms)` (Task baseline), `handle_write_intent`.
- Produces: a `write_deadletter` row `(ulid text, payload jsonb, error text, created_at timestamptz)` for any deterministic-bad intent; the entry is still XACKed.

- [ ] **Step 1: Write the failing test**

A hint linking to a nonexistent `(artefact_id, version)` raises a non-retryable FK `IntegrityError`; the consumer must deadletter it (not silently drop) and ack it.

```python
# tests/arb_memory/test_write_deadletter.py
import json
from arb_memory import bus


class _StubRedis:
    def __init__(self):
        self.acked = []

    def xgroup_create(self, *a, **k):
        pass

    def xack(self, stream, group, entry_id):
        self.acked.append(entry_id)


def test_deterministic_bad_intent_is_deadlettered_not_dropped(conn_factory, fake_embed):
    conn = conn_factory()
    loop = bus.WriteLoop(_StubRedis(), conn, embed=fake_embed)
    intent = {
        "ulid": "ulid-dl-1",
        "kind": "hints",
        "artefact": None,
        "hints": [{"text": "orphan", "artefact_id": "nope", "artefact_version": 7}],
    }
    fields = {"ulid": "ulid-dl-1", "payload": json.dumps(intent)}

    handled = loop._handle_entry("1-0", fields)

    assert handled is True  # acked, not left pending
    assert loop.redis.acked == ["1-0"]
    row = conn.execute(
        "SELECT error FROM write_deadletter WHERE ulid = %s", ("ulid-dl-1",)
    ).fetchone()
    assert row is not None  # deadlettered, not silently dropped
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ARB_MEMORY_DSN=$ARB_MEMORY_DSN pytest tests/arb_memory/test_write_deadletter.py -v`
Expected: FAIL — `write_deadletter` relation does not exist / row is None (currently ack-dropped).

- [ ] **Step 3: Add the deadletter table**

In `src/arb_memory/schema.sql`, after the `idempotency_keys` table:

```sql
CREATE TABLE IF NOT EXISTS write_deadletter (
    ulid       text NOT NULL,
    payload    jsonb NOT NULL,
    error      text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS write_deadletter_ulid_idx ON write_deadletter (ulid);
```

- [ ] **Step 4: Deadletter in `_handle_entry`**

Add a `_deadletter` method to `WriteLoop` and call it on both the parse-fail and non-retryable branches:

```python
    def _deadletter(self, ulid, payload, error):
        from psycopg.types.json import Jsonb
        try:
            with self.conn.transaction():
                self.conn.execute(
                    "INSERT INTO write_deadletter (ulid, payload, error) VALUES (%s, %s, %s)",
                    (ulid, Jsonb(payload), str(error)[:2000]),
                )
        except Exception:
            logger.exception("failed to deadletter write entry %s", ulid)

    def _handle_entry(self, entry_id, fields):
        try:
            intent = self._parse_intent(fields)
        except (KeyError, TypeError, json.JSONDecodeError, ValueError) as exc:
            logger.exception("deadlettering malformed write entry %s", entry_id)
            self._deadletter(fields.get("ulid", "unknown"), {"raw": fields}, exc)
            self._ack(entry_id)
            return True

        try:
            handle_write_intent(self.conn, intent, embed=self.embed)
        except Exception as exc:
            if not _is_retryable_write_error(exc):
                logger.exception("deadlettering invalid write entry %s", entry_id)
                self._deadletter(intent["ulid"], intent, exc)
                self._ack(entry_id)
                return True
            logger.exception("write loop failed to handle entry %s", entry_id)
            return False
        self._ack(entry_id)
        return True
```

- [ ] **Step 5: Run tests green + deny-proof + commit**

Run: `ARB_MEMORY_DSN=$ARB_MEMORY_DSN pytest tests/arb_memory/test_write_deadletter.py -v`
Expected: PASS. Deny-proof: temporarily replace the `self._deadletter(...)` call in the non-retryable branch with nothing → re-run → the test REDS (row is None). Restore.

```bash
git add src/arb_memory/schema.sql src/arb_memory/bus.py tests/arb_memory/test_write_deadletter.py
git commit -m "feat(arb-memory): deadletter deterministic-bad write intents (no silent drop)"
```

---

### Task 3: Write settings + validation + scope helpers

**Files:**
- Modify: `src/arb_memory/mcp/config.py:22-24` (`Settings` fields)
- Modify: `src/arb_memory/mcp/tools.py` (helpers + module constants)
- Test: `tests/arb_memory/test_write_validation.py`

**Interfaces:**
- Produces (module-level in `tools.py`): `WRITE_MIME_ALLOWLIST: set[str]`, `ARTEFACT_ID_RE` (compiled), `derive_artefact_id(content, mime) -> str`, `validate_content(content, mime, settings)`, `validate_text(text, settings)`, `validate_artefact_id(artefact_id)`; (on `MemoryTools`) `_check_write_allowed(token)`, `_require_write_scope()`. New `Settings` fields listed in Global Constraints.

- [ ] **Step 1: Write the failing test**

```python
# tests/arb_memory/test_write_validation.py
import re
import pytest
from arb_memory.mcp import tools
from arb_memory.mcp.config import Settings

S = Settings(public_base_url="https://x", mcp_dsn="postgresql://x",
             login_secret="l", totp_secret="t")


def test_derive_artefact_id_is_deterministic_and_prefixed():
    a = tools.derive_artefact_id("hello world", "text/plain")
    b = tools.derive_artefact_id("hello world", "text/plain")
    assert a == b and a.startswith("art-") and re.fullmatch(r"art-[0-9a-f]{16}", a)


def test_validate_content_rejects_empty_oversize_badmime():
    with pytest.raises(ValueError):
        tools.validate_content("", "text/plain", S)
    with pytest.raises(ValueError):
        tools.validate_content("x" * (S.write_max_content_bytes + 1), "text/plain", S)
    with pytest.raises(ValueError):
        tools.validate_content("ok", "application/x-evil", S)
    tools.validate_content("ok", "text/markdown", S)  # allowed, no raise


def test_validate_artefact_id_charset():
    tools.validate_artefact_id("art-abc_123")
    with pytest.raises(ValueError):
        tools.validate_artefact_id("bad id with spaces")
    with pytest.raises(ValueError):
        tools.validate_artefact_id("x" * 65)


def test_require_write_scope_denies_anonymous_and_missing(monkeypatch):
    mt = tools.MemoryTools(S, conn_factory=lambda: None, embed=lambda t: [])
    monkeypatch.setattr(tools, "get_access_token", lambda: None)
    with pytest.raises(PermissionError):
        mt._require_write_scope()

    class _Tok:
        scopes = ["memory.read"]
    monkeypatch.setattr(tools, "get_access_token", lambda: _Tok())
    with pytest.raises(PermissionError):
        mt._require_write_scope()

    class _Tok2:
        scopes = ["memory.read", "memory.write"]
    monkeypatch.setattr(tools, "get_access_token", lambda: _Tok2())
    mt._require_write_scope()  # no raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/arb_memory/test_write_validation.py -v`
Expected: FAIL — `AttributeError: module 'arb_memory.mcp.tools' has no attribute 'derive_artefact_id'` and `Settings` has no `write_max_content_bytes`.

- [ ] **Step 3: Add Settings fields**

In `src/arb_memory/mcp/config.py`, append to the `Settings` dataclass (after `login_global_fail_cap`):

```python
    write_rate_per_min: int = 30
    write_max_content_bytes: int = 262144
    write_max_text_chars: int = 8192
    write_backlog_max: int = 8000
    relay_heartbeat_max_age: int = 30
```

- [ ] **Step 4: Add helpers to tools.py**

Add near the top of `src/arb_memory/mcp/tools.py` (after the existing imports — note `get_access_token` is already imported):

```python
import re
from arb_memory.hash import artefact_hash

WRITE_MIME_ALLOWLIST = {"text/plain", "text/markdown", "application/json"}
ARTEFACT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def derive_artefact_id(content: str, mime: str) -> str:
    return f"art-{artefact_hash(content, None, mime)[:16]}"


def validate_content(content: str, mime: str, settings) -> None:
    if not content:
        raise ValueError("content must not be empty")
    if len(content.encode("utf-8")) > settings.write_max_content_bytes:
        raise ValueError("content too large")
    if mime not in WRITE_MIME_ALLOWLIST:
        raise ValueError(f"unsupported mime {mime!r}")


def validate_text(text: str, settings) -> None:
    if not text:
        raise ValueError("text must not be empty")
    if len(text) > settings.write_max_text_chars:
        raise ValueError("text too long")


def validate_artefact_id(artefact_id: str) -> None:
    if not ARTEFACT_ID_RE.fullmatch(artefact_id):
        raise ValueError("invalid artefact_id")
```

Add the methods to `MemoryTools` (and init `self._write_hits = {}` in `__init__`):

```python
    def _require_write_scope(self) -> None:
        token = get_access_token()
        if token is None or "memory.write" not in (getattr(token, "scopes", None) or []):
            raise PermissionError("memory.write scope required")

    def _check_write_allowed(self, access_token: str) -> None:
        now = time.monotonic()
        window_start = now - 60.0
        hits = [s for s in self._write_hits.get(access_token, []) if s >= window_start]
        if len(hits) >= self.settings.write_rate_per_min:
            self._write_hits[access_token] = hits
            raise ValueError("write rate limit exceeded")
        hits.append(now)
        self._write_hits[access_token] = hits
```

- [ ] **Step 5: Run tests green + commit**

Run: `pytest tests/arb_memory/test_write_validation.py -v`
Expected: PASS (4 passed).

```bash
git add src/arb_memory/mcp/config.py src/arb_memory/mcp/tools.py tests/arb_memory/test_write_validation.py
git commit -m "feat(arb-memory): write validation, rate-limit, and memory.write scope helpers"
```

---

### Task 4: Door write tools (publish via authenticated HTTP) + registration

**Files:**
- Modify: `src/arb_memory/mcp/tools.py` (`MemoryTools.__init__` gains `writer_url`/`writer_token`/`http_client`; `_publish`; `memory_store`; `memory_remember`) — **remove any `import redis` / `from arb_memory import bus`**
- Modify: `src/arb_memory/mcp/server.py:276-280` (scopes), build httpx client + writer url/token, register tools — **no `import redis`**
- Modify: `tests/arb_memory/test_mcp_tools.py` (scope/tool assertions)
- Test: `tests/arb_memory/test_write_tools.py`

**Interfaces:**
- Consumes: `store.fetch_artefact`, Task 3 helpers (`validate_content/text/artefact_id`, `derive_artefact_id`, `_check_write_allowed`, `_require_write_scope`), `arb_memory.hash.artefact_hash`.
- Produces: `MemoryTools(settings, *, conn_factory, embed, writer_url=None, writer_token=None, http_client=None)`; `await memory_store(content, *, artefact_id=None, mime="text/plain", access_token=None) -> {"accepted","ulid","artefact_id"}`; `await memory_remember(text, *, tags=None, artefact_id=None, artefact_version=None, access_token=None) -> {"accepted","ulid"}`. The writer receives intent JSON `{"artefact": {...}|None, "hints": [...], "author": <str>}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/arb_memory/test_write_tools.py
import anyio
import pytest
from arb_memory.mcp import tools
from arb_memory.mcp.config import Settings

S = Settings(public_base_url="https://x", mcp_dsn="postgresql://x",
             login_secret="l", totp_secret="t")


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeHttp:
    """Captures POSTs; returns 200 {ulid} by default."""
    def __init__(self, status_code=200, ulid="ulid-1", boom=False):
        self.status_code = status_code
        self.ulid = ulid
        self.boom = boom
        self.posts = []

    def post(self, url, json=None, headers=None):
        self.posts.append((url, json, headers))
        if self.boom:
            raise ConnectionError("writer down")
        return _Resp(self.status_code, {"ulid": self.ulid})


def _mt(http, conn_factory=lambda: None):
    return tools.MemoryTools(S, conn_factory=conn_factory, embed=lambda t: [0.0],
                             writer_url="http://writer:8800", writer_token="sek", http_client=http)


def _grant(monkeypatch, scopes=("memory.read", "memory.write"), client_id="cid-1"):
    monkeypatch.setattr(tools, "get_access_token",
                        lambda: type("T", (), {"scopes": list(scopes), "client_id": client_id})())


def test_memory_store_posts_mcp_artefact_with_bearer(monkeypatch):
    _grant(monkeypatch)
    http = _FakeHttp()
    res = anyio.run(lambda: _mt(http).memory_store("hello", access_token="tok"))
    assert res["accepted"] is True and res["ulid"] == "ulid-1"
    url, body, headers = http.posts[0]
    assert url == "http://writer:8800/publish"
    assert headers["Authorization"] == "Bearer sek"
    assert body["artefact"]["source"] == "mcp" and body["artefact"]["author"] == "cid-1"
    assert body["author"] == "cid-1"


def test_memory_store_same_content_same_id(monkeypatch):
    _grant(monkeypatch)
    http = _FakeHttp()
    mt = _mt(http)
    a = anyio.run(lambda: mt.memory_store("dup", access_token="t"))["artefact_id"]
    b = anyio.run(lambda: mt.memory_store("dup", access_token="t"))["artefact_id"]
    assert a == b and a.startswith("art-")


def test_memory_store_fails_loud_on_writer_5xx(monkeypatch):
    _grant(monkeypatch)
    http = _FakeHttp(status_code=503)
    with pytest.raises(RuntimeError):
        anyio.run(lambda: _mt(http).memory_store("x", access_token="t"))


def test_memory_store_fails_loud_on_transport_error(monkeypatch):
    _grant(monkeypatch)
    http = _FakeHttp(boom=True)
    with pytest.raises(RuntimeError):
        anyio.run(lambda: _mt(http).memory_store("x", access_token="t"))
    assert http.posts  # attempted, but raised


def test_write_without_scope_denied(monkeypatch):
    _grant(monkeypatch, scopes=("memory.read",))
    with pytest.raises(PermissionError):
        anyio.run(lambda: _mt(_FakeHttp()).memory_store("x", access_token="t"))


def test_memory_remember_linked_requires_existing_artefact(monkeypatch, conn_factory, fake_embed):
    from arb_memory import store
    _grant(monkeypatch)
    conn = conn_factory()
    store.write_artefact_and_hints(conn, artefact={"artefact_id": "art-link", "content": "c",
                                                   "mime": "text/plain", "source": "mcp", "author": "c"})
    http = _FakeHttp()
    mt = tools.MemoryTools(S, conn_factory=conn_factory, embed=fake_embed,
                           writer_url="http://writer:8800", writer_token="sek", http_client=http)
    with pytest.raises(ValueError):  # missing version
        anyio.run(lambda: mt.memory_remember("n", artefact_id="art-link", access_token="t"))
    with pytest.raises(ValueError):  # nonexistent pair
        anyio.run(lambda: mt.memory_remember("n", artefact_id="art-link", artefact_version=99, access_token="t"))
    res = anyio.run(lambda: mt.memory_remember("n", artefact_id="art-link", artefact_version=1, access_token="t"))
    assert res["accepted"] is True and http.posts
```

- [ ] **Step 2: Run test to verify it fails**

Run: `ARB_MEMORY_DSN=$ARB_MEMORY_DSN pytest tests/arb_memory/test_write_tools.py -v`
Expected: FAIL — `MemoryTools` has no `writer_url`/`memory_store`.

- [ ] **Step 3: Implement the tools (httpx publish, NO redis)**

In `src/arb_memory/mcp/tools.py` — ensure imports are `import re, time`, `from arb_memory import store` (NO `bus`), `from arb_memory.hash import artefact_hash`, and the existing `from mcp.server.auth.middleware.auth_context import get_access_token`. Update `__init__` and add methods:

```python
    def __init__(self, settings=None, *, conn_factory=None, embed=None,
                 writer_url=None, writer_token=None, http_client=None):
        self.settings = settings or load_settings()
        self.conn_factory = conn_factory or _default_conn_factory
        self.embed = embed or default_embed
        self.writer_url = writer_url
        self.writer_token = writer_token
        self.http_client = http_client
        self._search_hits = {}
        self._write_hits = {}

    def _author_from_token(self) -> str:
        token = get_access_token()
        return getattr(token, "client_id", None) or "mcp"

    def _publish(self, intent: dict) -> dict:
        if self.http_client is None or not self.writer_url:
            raise RuntimeError("write transport not configured")
        try:
            resp = self.http_client.post(
                f"{self.writer_url}/publish",
                json=intent,
                headers={"Authorization": f"Bearer {self.writer_token}"},
            )
        except Exception as exc:
            raise RuntimeError("memory store unavailable — item NOT stored; retry shortly") from exc
        if resp.status_code // 100 != 2:
            raise RuntimeError("memory store unavailable — item NOT stored; retry shortly")
        return resp.json()

    async def memory_store(self, content, *, artefact_id=None, mime="text/plain", access_token=None):
        self._require_write_scope()
        token = access_token or _current_access_token()
        self._check_write_allowed(token)
        validate_content(content, mime, self.settings)
        if artefact_id is None:
            artefact_id = derive_artefact_id(content, mime)
        else:
            validate_artefact_id(artefact_id)
        author = self._author_from_token()
        intent = {"artefact": {"artefact_id": artefact_id, "content": content, "mime": mime,
                               "source": "mcp", "author": author},
                  "hints": [], "author": author}
        res = self._publish(intent)
        return {"accepted": True, "ulid": res["ulid"], "artefact_id": artefact_id}

    async def memory_remember(self, text, *, tags=None, artefact_id=None, artefact_version=None,
                              access_token=None):
        self._require_write_scope()
        token = access_token or _current_access_token()
        self._check_write_allowed(token)
        validate_text(text, self.settings)
        if artefact_id is not None or artefact_version is not None:
            if artefact_id is None or artefact_version is None:
                raise ValueError("artefact_id and artefact_version must both be set")
            validate_artefact_id(artefact_id)
            conn = self.conn_factory()
            if store.fetch_artefact(conn, artefact_id, artefact_version) is None:
                raise ValueError("linked artefact not found")
        author = self._author_from_token()
        metadata = {"tags": list(tags)} if tags else {}
        hint = {"text": text, "metadata": metadata,
                "artefact_id": artefact_id, "artefact_version": artefact_version}
        intent = {"artefact": None, "hints": [hint], "author": author}
        res = self._publish(intent)
        return {"accepted": True, "ulid": res["ulid"]}
```

- [ ] **Step 4: Run tool tests green + commit**

Run: `ARB_MEMORY_DSN=$ARB_MEMORY_DSN pytest tests/arb_memory/test_write_tools.py -v`
Expected: PASS (6 passed).

```bash
git add src/arb_memory/mcp/tools.py tests/arb_memory/test_write_tools.py
git commit -m "feat(arb-memory): memory_store/memory_remember publish via authenticated HTTP (door redis-free)"
```

- [ ] **Step 5: Register tools + scope in server.py (NO redis import)**

Set scopes (`server.py:276-280`):

```python
                valid_scopes=["memory.read", "memory.write"],
                default_scopes=["memory.read", "memory.write"],
            ),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=["memory.read"],
```

In `build_server`, add `http_client=None` to the signature, build the httpx client + read writer env (NOT redis), and pass to `MemoryTools` (replace the `tools = MemoryTools(...)` line at ~232):

```python
    if http_client is None:
        import httpx
        http_client = httpx.Client(timeout=10.0)
    writer_url = os.environ.get("ARB_MEMORY_MCP_WRITER_URL")
    writer_token = os.environ.get("ARB_MEMORY_WRITER_TOKEN")
    tools = MemoryTools(settings, conn_factory=conn_factory, embed=embed,
                        writer_url=writer_url, writer_token=writer_token, http_client=http_client)
```

Register the two tools next to the read tools (`server.py:293-295`):

```python
    async def memory_store(content: str, artefact_id: str | None = None,
                           mime: str = "text/plain") -> dict:
        return await tools.memory_store(content, artefact_id=artefact_id, mime=mime)

    async def memory_remember(text: str, tags: list[str] | None = None,
                              artefact_id: str | None = None,
                              artefact_version: int | None = None) -> dict:
        return await tools.memory_remember(text, tags=tags, artefact_id=artefact_id,
                                           artefact_version=artefact_version)

    server.add_tool(memory_store, name="memory_store")
    server.add_tool(memory_remember, name="memory_remember")
```

**Confirm there is NO `import redis` anywhere in `src/arb_memory/mcp/`.** (`grep -rn "import redis\|from arb_memory import bus" src/arb_memory/mcp/` must return nothing.)

- [ ] **Step 6: Update the scope/tool guard test + verify the redis-free invariant**

In `tests/arb_memory/test_mcp_tools.py::test_build_server_wires_auth_settings_and_tools`, update the expected scopes/tools to the new reality:

```python
    assert auth.client_registration_options.valid_scopes == ["memory.read", "memory.write"]
    assert auth.client_registration_options.default_scopes == ["memory.read", "memory.write"]
    assert auth.revocation_options.enabled is True
    assert auth.required_scopes == ["memory.read"]
    ...
    assert {"memory_search", "memory_get", "memory_recent",
            "memory_store", "memory_remember"}.issubset(tool_names)
```

Run the guard tests — the readonly-import invariant must be GREEN unchanged:

Run: `ARB_MEMORY_DSN=$ARB_MEMORY_DSN pytest tests/arb_memory/test_mcp_readonly_import.py tests/arb_memory/test_mcp_tools.py -v`
Expected: PASS — including `test_mcp_package_does_not_import_redis_or_valkey` and `test_mcp_server_runtime_import_does_not_load_redis_or_valkey` (the door stays redis-free).

```bash
git add src/arb_memory/mcp/server.py tests/arb_memory/test_mcp_tools.py
git commit -m "feat(arb-memory): register memory_store/memory_remember + memory.write scope (door redis-free)"
```

---

### Task 5: Writer proxy service

**Files:**
- Create: `src/arb_memory/writer.py`
- Modify: `src/arb_memory/run.py` (`run_writer` + `writer` choice)
- Test: `tests/arb_memory/test_writer.py`

**Interfaces:**
- Consumes: `bus.memory_write(redis, *, artefact, hints, source, author)`.
- Produces: `writer.build_writer_app(redis_client, *, token) -> Starlette`; route `POST /publish` (Bearer auth) accepting `{"artefact", "hints", "author"}`, returns `{"ulid"}` 200 / 401 bad-token / 503 bus-error. `run.py` `writer` service builds the bus redis client from `ARB_MEMORY_REDIS_URL`.

- [ ] **Step 1: Write the failing test**

```python
# tests/arb_memory/test_writer.py
from starlette.testclient import TestClient
from arb_memory.writer import build_writer_app


class _FakeBus:
    def __init__(self, boom=False):
        self.boom = boom
        self.calls = []

    def xadd(self, stream, fields, **k):
        if self.boom:
            raise RuntimeError("bus down")
        self.calls.append((stream, fields))
        return "1-0"


def test_writer_rejects_bad_token():
    c = TestClient(build_writer_app(_FakeBus(), token="secret"))
    r = c.post("/publish", json={"artefact": None, "hints": [{"text": "x"}]},
               headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_writer_rejects_missing_token():
    c = TestClient(build_writer_app(_FakeBus(), token="secret"))
    r = c.post("/publish", json={"artefact": None, "hints": [{"text": "x"}]})
    assert r.status_code == 401


def test_writer_publishes_with_valid_token():
    fake = _FakeBus()
    c = TestClient(build_writer_app(fake, token="secret"))
    r = c.post("/publish", json={"artefact": None, "hints": [{"text": "hello"}], "author": "cid-1"},
               headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200 and "ulid" in r.json()
    assert fake.calls  # forwarded to the bus stream


def test_writer_returns_503_on_bus_error():
    c = TestClient(build_writer_app(_FakeBus(boom=True), token="secret"))
    r = c.post("/publish", json={"artefact": None, "hints": [{"text": "x"}], "author": "c"},
               headers={"Authorization": "Bearer secret"})
    assert r.status_code == 503
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/arb_memory/test_writer.py -v`
Expected: FAIL — `No module named 'arb_memory.writer'`.

- [ ] **Step 3: Implement the writer app**

```python
# src/arb_memory/writer.py
import hmac
import os

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from arb_memory import bus


def build_writer_app(redis_client, *, token=None):
    token = token if token is not None else os.environ.get("ARB_MEMORY_WRITER_TOKEN", "")

    async def publish(request: Request):
        auth = request.headers.get("Authorization", "")
        presented = auth[7:] if auth.startswith("Bearer ") else ""
        if not token or not hmac.compare_digest(presented, token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        intent = await request.json()
        try:
            ulid = bus.memory_write(
                redis_client,
                artefact=intent.get("artefact"),
                hints=intent.get("hints", []),
                source="mcp",
                author=intent.get("author", "mcp"),
            )
        except Exception:
            return JSONResponse({"error": "bus unavailable"}, status_code=503)
        return JSONResponse({"ulid": ulid})

    return Starlette(routes=[Route("/publish", publish, methods=["POST"])])
```

- [ ] **Step 4: Run writer tests green + commit**

Run: `pytest tests/arb_memory/test_writer.py -v`
Expected: PASS (4 passed).

```bash
git add src/arb_memory/writer.py tests/arb_memory/test_writer.py
git commit -m "feat(arb-memory): internal writer proxy (bearer-auth POST /publish -> bus)"
```

- [ ] **Step 5: Wire run.py (`writer` route)**

In `src/arb_memory/run.py`:

```python
def run_writer() -> None:
    import uvicorn
    from arb_memory.writer import build_writer_app

    app = build_writer_app(_redis_client(), token=os.environ["ARB_MEMORY_WRITER_TOKEN"])
    uvicorn.run(app, host=os.environ.get("ARB_MEMORY_WRITER_HOST", "0.0.0.0"),
                port=int(os.environ.get("ARB_MEMORY_WRITER_PORT", "8800")))
```

Add `writer` to the choices and dispatch:

```python
    parser.add_argument("service", choices=("memory", "audit", "eval", "mcp", "writer"))
    ...
    elif args.service == "writer":
        run_writer()
    else:
        run_mcp()
```

- [ ] **Step 6: Verify route parses + commit**

Run: `python -m arb_memory writer 2>&1 | head -2 || true` (with no `ARB_MEMORY_WRITER_TOKEN` it raises `KeyError` — fine; the point is argparse accepts `writer`, no "invalid choice").
Expected: no "invalid choice: 'writer'" error.

```bash
git add src/arb_memory/run.py
git commit -m "feat(arb-memory): writer run route"
```

---

### Task 6: Deploy — internal writer service + door writer URL (no door redis)

**Files:**
- Modify: `deploy/docker-compose.yml`
- Modify: `tests/arb_memory/test_compose_shape.py` (expected service set)
- Test: `docker compose config` + the updated compose-shape test

**Interfaces:**
- Consumes: image `arb-memory:phase3` with the `writer` command (Task 5).
- Produces: an internal `writer` service (holds `ARB_MEMORY_REDIS_URL` + token, no published port); the `mcp` door gets `ARB_MEMORY_MCP_WRITER_URL=http://writer:8800` + `ARB_MEMORY_WRITER_TOKEN` (and NO redis env).

- [ ] **Step 1: Add the writer service + door env**

In `deploy/docker-compose.yml`, add the `writer` service:

```yaml
  writer:
    image: arb-memory:phase3
    command: ["writer"]
    environment:
      ARB_MEMORY_REDIS_URL: ${ARB_MEMORY_REDIS_URL}
      ARB_MEMORY_WRITER_TOKEN: ${ARB_MEMORY_WRITER_TOKEN}
      ARB_MEMORY_WRITER_HOST: 0.0.0.0
      ARB_MEMORY_WRITER_PORT: "8800"
    restart: unless-stopped
```

Under the `mcp` service `environment:` add (NO redis):

```yaml
      ARB_MEMORY_MCP_WRITER_URL: http://writer:8800
      ARB_MEMORY_WRITER_TOKEN: ${ARB_MEMORY_WRITER_TOKEN}
```

Add `mcp`'s `depends_on: [writer]`.

- [ ] **Step 2: Update the compose-shape test**

In `tests/arb_memory/test_compose_shape.py`, widen the expected service set to include `writer` (the no-REDIS-on-mcp assertion stays unchanged and must still pass — the door has WRITER_URL/TOKEN, not REDIS):

```python
    assert set(services) == {"memory", "audit", "mcp", "cloudflared", "writer"}
```

- [ ] **Step 3: Validate + run the test + commit**

Run: `cd deploy && ARB_MEMORY_REDIS_URL=redis://x TUNNEL_TOKEN=x ARB_MEMORY_WRITER_TOKEN=t ARB_MEMORY_MCP_PUBLIC_BASE_URL=https://x docker compose config >/dev/null && echo OK`
Expected: `OK`.
Run: `cd /Users/<user>/<workspace>/.claude/worktrees/mcp-write-paths && ARB_MEMORY_DSN=$ARB_MEMORY_DSN pytest tests/arb_memory/test_compose_shape.py -v`
Expected: PASS (the no-redis-on-mcp assertion green; services set includes `writer`).

```bash
git add deploy/docker-compose.yml tests/arb_memory/test_compose_shape.py
git commit -m "feat(arb-memory): internal writer service + door writer URL (door redis-free)"
```

## Self-Review

**Spec coverage:**
- §1 door tools → Task 4. §2 validation/rate-limit/scope helpers → Task 3; preflight + scope wiring → Task 4. §3 writer proxy → Task 5. §4 registration + scope config → Task 4 (Steps 5-6). §5 schema provenance → Task 1. §6 consumer deadletter → Task 2. §Security posture (door redis-free; bus cred only in writer/consumer) → Task 4 (httpx, no redis) + Task 5 (writer holds bus) + Task 6 (compose). §Error handling (structural fail-loud) → Task 4 (`_publish` raises on non-2xx/transport error) + Task 2 (deadletter). All covered.
- **Redis-free invariant:** `test_mcp_readonly_import.py` must stay GREEN unchanged (verified Task 4 Step 6); `test_compose_shape.py` widened to include `writer` (no-REDIS-on-mcp assertion still green); `test_mcp_tools` scope/tool assertions updated for `memory.write` + the two tools.
- **Deferred from spec (noted):** the `title` param on `memory_store` is dropped (no artefact column/metadata field; YAGNI). The sidecar/relay/heartbeat are removed (v6). v5's `write_backlog_max`/`relay_heartbeat_max_age` settings are NOT added (no buffer).

**Placeholder scan:** no TBD/TODO; every code step shows complete code; commands have expected output.

**Type consistency:** `MemoryTools(..., writer_url=, writer_token=, http_client=)` used consistently (Tasks 4); `_publish` posts `{"artefact","hints","author"}` which `writer.build_writer_app` consumes (Task 5); `derive_artefact_id`/`validate_*`/`_require_write_scope`/`_check_write_allowed` defined in Task 3 and consumed in Task 4; `source`/`author` columns from Task 1 consumed by the `source="mcp"` artefact dict in Task 4. No `redis`/`RelayLoop`/`writes_redis` references remain in the door path.

## Execution Handoff

The orchestrator dispatches this plan task-by-task to a **codex** worktree seat via the bridge (subagent-driven-development), reviewing each task from git between dispatches, then runs the tri-model review + the relay E2E (simulating a claude.ai/ChatGPT write) as the final gates.
