# ARB Memory — local read-side MCP Implementation Plan (v4, post plan-panel + 2 re-reviews)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **v4 changes (v3 confirmation pass — codex NEEDS-CHANGES + cold-Opus build-ready:no + agy APPROVE):**
> all v3 schema/defaults/SDK-shape fixes verified correct; folded 3 remaining P1s the deeper seats found —
> (1) **Tasks 2/3 signature contradiction** (tests passed `conn_factory=` but the impl skeleton dropped
> it): unified to `ReadMemoryTools(settings, *, conn_factory=None, embed=None)` + `build_local_server`
> likewise; (2) **Task 8 ceiling membership** — `decide()` denies `not in self.ceiling` *before* the
> `KNOWN_TOOLS` check, and `self.ceiling` is set once at construction and never augmented; v3 only fixed
> `KNOWN_TOOLS`, so the tools were still denied "outside ceiling" at runtime — Task 8 now augments
> `self.ceiling` too and the test drives the engine launch path (not a hand-built ceiling); (3) **the
> inject-revoke proof only covered `hints`** — extended to a representative grant on every revoked plane
> so dropping any REVOKE reds the test. Also: `_ensure_role` already exists (NOLOGIN) — use the LOGIN
> `_ensure_mcp_role` variant for connect-as-role.

> **v3 changes (re-review of v2 — codex + cold-Opus + agy; both v1 P0s confirmed resolved):** caught
> bugs the v2 warm-seat edits introduced — Task 8 `McpServerConfig` is a non-callable `Union` (→ plain
> dict / `McpStdioServerConfig`); the agent-sdk `can_use_tool`/`decide` gate would **deny the injected
> `mcp__arb-memory-local__*` tools at runtime** while a config-shape test passed green (Task 8 now must
> reconcile the gate + test execution); `LocalReadSettings` defaults corrected to the door's `2000/30`
> (were a fabricated `4000/60`); deny-proof now enumerates all 6 `mcp_auth` tables + all sensitive
> tables (grants impl list kept in sync), uses fully-valid DML, and adds an inject-grant-then-revoke
> step so the REVOKE is load-bearing (not vacuous vs the no-PUBLIC-grant default); env snippets forward
> `PATH`/`PYTHONPATH`; tests moved to `tests/arb_memory/`; launch-config tests assert spec-shape.

> **v2 changes (3-seat plan panel — codex + cold-Opus + agy):** P0 fixes — deny-proof now probes
> the **real** table `mcp_auth.oauth_clients` with **valid** DML (was nonexistent `mcp_auth.clients` +
> type-invalid `INSERT INTO hints(id) VALUES (uuid)`; `hints.id` is bigserial); `ReadMemoryTools` no
> longer calls `load_settings()` (it hard-raises without door secrets → server never booted); Task 1
> adopts the repo's `scratch`/`_ensure_role`/search-path test convention + role provisioning; Task 2
> `_conn()` is a real method. Tasks 7/8 MCP-injection mechanisms are now **pinned** (agy verified):
> codex `-c 'mcp_servers.…'`, agent-sdk `ClaudeAgentOptions(mcp_servers=…)`. Added: `OPENAI_API_KEY`
> subprocess forwarding, all 5 ACP engines, a stdio-level JSON-RPC error test, role/DSN provisioning
> task, tombstone resolution, and the missing security gates.

**Goal:** Give the local fleet (orchestrator + every bridge seat) a read-only path to ARB Memory — `memory_search` / `memory_get` / `memory_recent` over a local stdio MCP, backed by a dedicated read-only Postgres role.

**Architecture:** Reuse the existing Python retrieval (`store`/`embed`) behind a new read-only tools class and a bare (non-OAuth) stdio FastMCP server. A new SELECT-only DB role isolates local readers from the OAuth-door role. The bridge injects the stdio server into each engine's MCP config behind an env flag; the orchestrator adds it via `claude mcp add`.

**Tech Stack:** Python 3, `mcp` (FastMCP), `psycopg`, `pgvector`, OpenAI embeddings (`text-embedding-3-small`). Design: `docs/superpowers/specs/2026-06-26-arb-memory-local-read-mcp-design.md`.

## Global Constraints

- **Python only; reuse `store.retrieve/fetch_artefact/recent_artefacts` + `arb_memory.embed` verbatim.** No reimplementation of search ranking (RRF + pgvector + tsquery) anywhere ([[go-python-boundary]]).
- **Read-only by construction:** the local tools class has **no** writer/bus fields and **no** write methods — write must be impossible structurally, not merely by the DB role failing.
- **Dedicated DB role `arbmem_local_reader`:** `SELECT` on `hints`/`artefacts` only. **No** access to `mcp_auth`, `audit_events`, `eval_*`, `transcript_*`. Never reuse the door's `arbmemory-mcp` role (it has DML on `mcp_auth`).
- **stdio transport**, structured JSON-RPC errors only — never a raw traceback to stdout (it corrupts the channel and crashes the parent seat's parser).
- **No global config-file mutation** (`~/.codex/config.toml` etc.) at launch. Inject via in-memory config (ACP), per-launch CLI override (codex), or SDK options (agent-sdk).
- **Injection gated by env flag `ARB_MEMORY_LOCAL_MCP`** — a seat without it is byte-for-byte unchanged.
- **Exactly three tools** registered locally: `memory_search`, `memory_get`, `memory_recent`. The **server** test + the **whole-pipeline** test assert the tool list (`list_tools()` == the 3 reads, no writes). **Engine launch-config tests** can only assert the *launch spec* (command + env shape) — a static config can't enumerate the server's runtime tools — so they assert the injected server is the local read server with the right env, and the no-write guarantee is proven at the server/pipeline layer.
- **Env-coherent DSN:** the local read DSN (`ARB_MEMORY_LOCAL_DSN`) targets the same store the env's *writer* targets (orchestrator→prod, dev seats→dev); prod reads are a deliberate opt-in, never a silent default.
- **Partial availability:** `memory_get`/`memory_recent` must work without `OPENAI_API_KEY`; only `memory_search` may degrade (clean structured error, not a crash). The OpenAI client stays **lazily** constructed inside `embed()` (never at import) so get/recent never need the key.
- **Subprocess env forwarding:** every engine adapter forwards `OPENAI_API_KEY` (+ `PATH`/`PYTHONPATH`) AND the read-only `ARB_MEMORY_LOCAL_DSN` into the spawned MCP subprocess — the child has its own env.
- **No secret leakage:** the DSN/`OPENAI_API_KEY` must not be written into any persistent/global config snapshot or logged. A gate test greps the launched config artifacts for the secret values and asserts absence.
- **Subprocess lifecycle:** a test asserts the stdio MCP child is reaped (and its PG connection closed) when the parent transport closes.
- **Whole-pipeline gate:** a test launches the server **through the same path an engine uses** and asserts exactly `memory_search/get/recent` and no write tool.

---

### Task 1: `arbmem_local_reader` DB role + grants + deny-proof

**Files:**
- Modify: `src/arb_memory/mcp/grants.py` (add `apply_local_reader_grants`)
- Modify: `src/arb_memory/run.py` (extend `run_grants` to apply the local-reader role)
- Test: `tests/arb_memory/test_local_reader_grants.py`

**Interfaces:**
- Produces: `apply_local_reader_grants(conn, role: str) -> None` — SELECT on `hints`/`artefacts`; REVOKE write on them; REVOKE USAGE/ALL on `mcp_auth`; REVOKE ALL on `audit_*`/`eval_*`/`transcript_*`/`write_deadletter`/`idempotency_keys`.

**Test conventions (USE THESE — do not invent fixtures):** follow `tests/arb_memory/test_mcp_role.py`
+ `conftest.py`: the `scratch` fixture migrates `schema.sql` into a scratch schema; `_ensure_mcp_role`
provisions a LOGIN role; `_mcp_dsn(schema)` builds a DSN with `options=-csearch_path=<schema>`. Add a
`_local_reader_dsn(schema)` helper + `_ensure_role(scratch, "arbmem_local_reader")` (model on
`_ensure_mcp_role`). The deny-proof connects **AS the role** with the scratch schema on the search path.

- [ ] **Step 1: Write the failing deny-proof test** (real PG; gate/skip on the repo's test-DSN env like `test_mcp_role.py`). **Primary check = `has_table_privilege` introspection** — it returns the granted privilege as a boolean and **cannot** fail for the wrong reason (no parse/type/undefined-table trap, the failure mode that bit the v1 plan). Run it on the admin (`scratch`) connection, passing the role name. Verified schema facts: `hints` columns are `id bigserial, created_at, text, embedding vector(1536), metadata, source, author, artefact_id, artefact_version, repo_pointer, content_hash, deleted_at, search_tsv` (**no `kind`/`body`**); the auth table is `mcp_auth.oauth_clients`.

```python
def test_local_reader_privileges(scratch):
    _ensure_role(scratch, "arbmem_local_reader")
    apply_local_reader_grants(scratch, "arbmem_local_reader")
    role = "arbmem_local_reader"
    def priv(obj, p):
        return scratch.execute("SELECT has_table_privilege(%s, %s, %s)", (role, obj, p)).fetchone()[0]
    # Allowed: read the memory tables.
    assert priv("hints", "SELECT") and priv("artefacts", "SELECT")
    # Denied: write on memory tables.
    for obj in ("hints", "artefacts"):
        for p in ("INSERT", "UPDATE", "DELETE"):
            assert not priv(obj, p), f"{role} must not {p} {obj}"
    # Denied: ALL mcp_auth tables (6) + the eval/audit/transcript planes. Enumerate every sensitive
    # table from schema.sql — a subset would leave a hole the impl revokes but the test never proves.
    AUTH = ["oauth_clients", "auth_codes", "access_tokens", "refresh_tokens", "login_sessions", "login_attempts"]
    SENSITIVE = [f"mcp_auth.{t}" for t in AUTH] + [
        "audit_events", "audit_deadletter", "eval_event_raw", "eval_deadletter",
        "transcript_io", "transcript_deadletter", "write_deadletter", "idempotency_keys",
    ]
    for obj in SENSITIVE:
        for p in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert not priv(obj, p), f"{role} must not {p} {obj}"
    # Belt-and-suspenders: a live denied write raises InsufficientPrivilege. Insert ALL NOT-NULL
    # columns (text, embedding, content_hash) so the statement is fully valid DML — if INSERT were
    # ever granted it would SUCCEED, making the deny load-bearing (not a NotNullViolation false-red).
    with psycopg.connect(_local_reader_dsn(scratch_schema)) as r:  # helper takes the SCHEMA name
        r.execute("SELECT 1 FROM hints LIMIT 1")  # allowed
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with r.transaction():
                r.execute("INSERT INTO hints (text, embedding, content_hash) VALUES (%s, %s, %s)",
                          ("x", "[" + ",".join(["0"] * 1536) + "]", "deadbeef"))
```

- [ ] **Step 1b: Make the REVOKEs load-bearing — for ALL planes, not just `hints`.** Because `schema.sql`
  has no PUBLIC grants, a fresh role is denied *by absence of grant*, so a bare deny-assertion is
  **vacuous** ([[deny-proofs-need-adversarial-verification]]). Add an inject-revert proof that covers a
  representative grant on **each plane the impl revokes** (not just hints), so dropping any REVOKE line
  in `apply_local_reader_grants` reds the test:

```python
def test_revokes_are_load_bearing(scratch):
    _ensure_role(scratch, "arbmem_local_reader")
    role = "arbmem_local_reader"
    injected = [("hints", "INSERT"), ("artefacts", "UPDATE"),
                ("mcp_auth.oauth_clients", "SELECT"), ("audit_events", "SELECT"),
                ("eval_event_raw", "INSERT"), ("transcript_io", "SELECT"),
                ("write_deadletter", "SELECT"), ("idempotency_keys", "SELECT")]
    for obj, p in injected:
        scratch.execute(f"GRANT {p} ON {obj} TO {role}")
        assert scratch.execute("SELECT has_table_privilege(%s,%s,%s)", (role, obj, p)).fetchone()[0]
    apply_local_reader_grants(scratch, role)   # the function under test must REVOKE every one
    for obj, p in injected:
        assert not scratch.execute("SELECT has_table_privilege(%s,%s,%s)", (role, obj, p)).fetchone()[0], \
            f"REVOKE missing for {p} {obj} — drop-a-line acceptance would not red without this"
```

(`has_table_privilege` proves `mcp_auth` denial without depending on schema-USAGE error timing. Re-confirm
the full sensitive-table list against `schema.sql` at write time; `scratch_schema` is the scratch
schema name the `scratch` fixture creates — the DSN helper puts it on the search_path.)

- [ ] **Step 2: Run it — fails** (`apply_local_reader_grants` undefined). `pytest tests/arb_memory/test_local_reader_grants.py -v`
- [ ] **Step 3: Implement `apply_local_reader_grants`** — model on `apply_mcp_grants` but **omit the entire `mcp_auth` block** and add explicit denies:

```python
def apply_local_reader_grants(conn, role: str) -> None:
    schema = conn.execute("SELECT current_schema()").fetchone()[0]
    role_ident, schema_ident = sql.Identifier(role), sql.Identifier(schema)
    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema_ident, role_ident))
    conn.execute(sql.SQL("GRANT SELECT ON {}, {} TO {}").format(
        sql.Identifier(schema, "hints"), sql.Identifier(schema, "artefacts"), role_ident))
    conn.execute(sql.SQL("REVOKE INSERT, UPDATE, DELETE ON {}, {} FROM {}").format(
        sql.Identifier(schema, "hints"), sql.Identifier(schema, "artefacts"), role_ident))
    # No mcp_auth grant at all; revoke everything sensitive defensively.
    conn.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA mcp_auth FROM {}").format(role_ident))
    conn.execute(sql.SQL("REVOKE USAGE ON SCHEMA mcp_auth FROM {}").format(role_ident))
    # Every sensitive table from schema.sql — keep this list == the deny-proof's SENSITIVE list.
    for tbl in ("audit_events", "audit_deadletter", "eval_event_raw", "eval_deadletter",
                "transcript_io", "transcript_deadletter", "write_deadletter", "idempotency_keys"):
        conn.execute(sql.SQL("REVOKE ALL ON {} FROM {}").format(sql.Identifier(schema, tbl), role_ident))
```

- [ ] **Step 4: Run — passes.** **Acceptance is the Step-1b inject-revert** (drop any REVOKE line in `apply_local_reader_grants` → `test_revokes_are_load_bearing` reds for that plane). Verify by deleting one REVOKE and confirming the red, then restore.
- [ ] **Step 5: Provision + wire.** `run_grants` applies the role's grants when `ARB_MEMORY_LOCAL_READER_ROLE` is set. **Provisioning is an explicit operator step** (documented, not silent): `CREATE ROLE arbmem_local_reader LOGIN PASSWORD '…'` on prod+dev, then `apply_local_reader_grants`, then the read-only `ARB_MEMORY_LOCAL_DSN` (for that role) goes to seat boxes / `~/.arb-memory-prod`. **Test helpers:** `_ensure_role` already exists in `tests/arb_memory/test_run_grants.py` but creates a **NOLOGIN** role — the `has_table_privilege` checks (run on the `scratch` admin conn) don't need login, but the belt-and-suspenders connect-AS-role + `_local_reader_dsn` need the **LOGIN** variant (model that helper on `_ensure_mcp_role` in `conftest.py`, which is LOGIN). Commit.

---

### Task 2: `ReadMemoryTools` — read-only by construction

**Files:**
- Create: `src/arb_memory/mcp/read_tools.py`
- Test: `tests/arb_memory/test_read_tools.py`

**Interfaces:**
- Consumes: `store.retrieve/fetch_artefact/recent_artefacts`, `arb_memory.embed.embed`, `Settings`.
- Produces: `ReadMemoryTools(settings: LocalReadSettings, *, conn_factory=None, embed=None)` with async `memory_search(query, k=8)`, `memory_get(artefact_id, version)`, `memory_recent(limit=10)`. `settings` is **required**; `conn_factory` is an optional test/runtime seam (defaults to connecting `settings.dsn`). **No** writer/bus fields, **no** write methods.

- [ ] **Step 1: Failing test** — structural read-only + behaviour (note the unified signature — `settings` required, `conn_factory` injected for tests):

```python
def test_read_tools_has_no_write_surface():
    rt = ReadMemoryTools(LocalReadSettings(dsn="postgresql://ignored"), conn_factory=fake_conn, embed=fake_embed)
    for attr in ("memory_store", "memory_remember", "_publish", "writer_url", "writer_token", "http_client"):
        assert not hasattr(rt, attr)

async def test_read_tools_search_get_recent(rt_with_seeded_store):
    rt = rt_with_seeded_store  # ReadMemoryTools(LocalReadSettings(dsn=…), conn_factory=<seeded>, embed=fake_embed)
    assert await rt.memory_recent(limit=5)
    assert await rt.memory_search("anything", k=3) is not None
    assert await rt.memory_get(known_id, 1)["artefact_id"] == known_id
```

- [ ] **Step 2: Run — fails** (no `ReadMemoryTools`).
- [ ] **Step 3: Implement** — a *new* class (do NOT subclass `MemoryTools`). **Do NOT call `load_settings()`** — it hard-raises without the OAuth door secrets (`ARB_MEMORY_MCP_LOGIN_SECRET`/`…_TOTP_SECRET`/`…_PUBLIC_BASE_URL`) a local box won't have. Take a minimal local config (just the two read-relevant knobs) + an explicit DSN:

```python
@dataclass
class LocalReadSettings:
    dsn: str
    search_max_query_chars: int = 2000   # MUST match config.Settings (door read path) — verified
    search_rate_per_min: int = 30        # MUST match config.Settings — verified

class ReadMemoryTools:
    def __init__(self, settings: LocalReadSettings, *, conn_factory=None, embed=None):
        self.settings = settings
        self.conn_factory = conn_factory   # optional injection; None → connect settings.dsn
        self.embed = embed or default_embed
        self._search_hits: list[float] = []   # per-process; not the door's "anonymous" token bucket
        self._conn_obj = None
    def _conn(self):                            # lazy, reused, reconnect-on-closed (Task 4)
        if self._conn_obj is None or getattr(self._conn_obj, "closed", False):
            self._conn_obj = self.conn_factory() if self.conn_factory else psycopg.connect(self.settings.dsn)
        return self._conn_obj
    async def memory_search(self, query, k=8): ...   # store.retrieve(self._conn(), query, k, embed=self.embed)
    async def memory_get(self, artefact_id, version): ...  # store.fetch_artefact(self._conn(), ...)
    async def memory_recent(self, limit=10): ...           # store.recent_artefacts(self._conn(), ...)
```

(Read-method bodies mirror `MemoryTools` but with the local rate-limit + `_conn()`. Defaults for the two knobs match `config.Settings` so behaviour is identical to the door's read path.)

- [ ] **Step 4: Run — passes. Commit.**

---

### Task 3: `build_local_server` + `run_local_read_mcp` (stdio entrypoint)

**Files:**
- Create: `src/arb_memory/mcp/local_server.py` (`build_local_server`)
- Modify: `src/arb_memory/run.py` (`run_local_read_mcp`), `pyproject.toml` ([project.scripts] `arb-memory-local-mcp`)
- Test: `tests/arb_memory/test_local_server.py`

**Interfaces:**
- Consumes: `ReadMemoryTools`.
- Produces: `build_local_server(settings: LocalReadSettings, *, conn_factory=None, embed=None) -> FastMCP` registering exactly `memory_search/get/recent`; `run_local_read_mcp()` runs it over stdio. (Same unified signature as `ReadMemoryTools` — `settings` required, `conn_factory` optional injection.)

- [ ] **Step 1: Failing test** — tool list is exactly the three reads, no writes:

```python
async def test_local_server_registers_only_read_tools():
    server = build_local_server(LocalReadSettings(dsn="postgresql://ignored"), conn_factory=fake_conn, embed=fake_embed)
    names = {t.name for t in await server.list_tools()}
    assert names == {"memory_search", "memory_get", "memory_recent"}
    assert "memory_store" not in names and "memory_remember" not in names
```

- [ ] **Step 2: Run — fails.**
- [ ] **Step 3: Implement** a bare server (do NOT call `build_server` — it is OAuth-welded and `load_settings` hard-requires door secrets):

```python
from mcp.server.fastmcp import FastMCP
def build_local_server(settings: LocalReadSettings, *, conn_factory=None, embed=None) -> FastMCP:
    tools = ReadMemoryTools(settings, conn_factory=conn_factory, embed=embed)
    server = FastMCP("arb-memory-local")
    server.add_tool(tools.memory_search, name="memory_search")
    server.add_tool(tools.memory_get, name="memory_get")
    server.add_tool(tools.memory_recent, name="memory_recent")
    return server
```

- [ ] **Step 4: `run_local_read_mcp`** in run.py: read `ARB_MEMORY_LOCAL_DSN` (**fail loud** if unset), build `LocalReadSettings(dsn=…)`, `embed` from `arb_memory.embed`, then `server.run(transport="stdio")`. Add console script `arb-memory-local-mcp` **and** a `local-read-mcp` choice in `run.py:main()` (repo convention is `python -m arb_memory <service>`).
- [ ] **Step 5: stdio-level error-hygiene test** (this is a server protocol property, not a method property): spawn the server over stdio, call `memory_search` with `OPENAI_API_KEY` unset, assert the response is a **JSON-RPC error** (clean structured message) with **no traceback / no stray stdout** on the protocol stream. Run — passes. Commit.

---

### Task 4: connection + client reuse, partial availability, error hygiene, rate-limit

**Files:** Modify `src/arb_memory/mcp/read_tools.py`, `src/arb_memory/embed.py`; Test: `tests/arb_memory/test_read_tools_runtime.py`

- [ ] **Step 1: Failing tests:**
  - `memory_recent`/`memory_get` succeed with `OPENAI_API_KEY` unset; `memory_search` raises a clean structured error (not a stack trace).
  - The OpenAI client is built **once across N `embed()` calls but lazily on first use** (assert a cached client created inside `embed()`, **not** at import — so the prod `run_memory` path that imports `embed` without immediately embedding is unaffected).
  - A second `memory_recent` reuses the same PG connection (no new `connect`); after the connection is force-closed, the next call reconnects.
- [ ] **Step 2: Run — fails.**
- [ ] **Step 3: Implement:** a lazily-initialised cached OpenAI client *inside* `embed()` (module-level cache var, constructed on first call — never at import); `_conn()` reconnect-on-closed (Task 2); `memory_search` guards missing key/network → structured error; per-process rate-limit. Add a test asserting the prod `run_memory` embed path is byte-unchanged.
- [ ] **Step 4: Run — passes. Commit.**

---

### Task 5: orchestrator wiring + first live E2E

**Files:** Create `scripts/arb-memory-local-mcp-register` (or doc), `tools/arb-memory-local/README.md`; Test: `tests/arb_memory/e2e_local_read_mcp.py` (pytest marker/skip — gated on `ARB_MEMORY_LOCAL_DSN`; not a Go build tag)

- [ ] **Step 1: E2E test** (skips if no DSN): spawn the stdio server, list tools (**== exactly** `{memory_search, memory_get, memory_recent}`, `memory_store`/`memory_remember` absent), always call `memory_recent(limit=1)` and (if seeded) `memory_get`. Call `memory_search("x")` **only when `OPENAI_API_KEY` is present**; otherwise assert the clean structured "search unavailable" error (partial availability). Read-only is proven by the tool list, not by a failed write.
- [ ] **Step 2: Run — fails / skips.**
- [ ] **Step 3:** document/script `claude mcp add --transport stdio arb-memory-local -- arb-memory-local-mcp` with `ARB_MEMORY_LOCAL_DSN` (read-only role) + `OPENAI_API_KEY` in its env. Run the E2E against the live store. Commit.

---

### Task 6: `LocalMemoryMCPConfig` + ACP injection (flag-gated)

**Files:** Create `src/agent_redis_bridge/local_memory_mcp.py` (`local_memory_mcp_config()`); Modify **every ACP engine that sends `mcpServers`** — grep `mcpServers` under `engines/` (today: `grok_acp.py`, `gemini_acp.py`, `cursor_acp.py`; **also check `kimi_code_acp.py`, `mini_agent_acp.py`** — there are 5 ACP engines, "full fleet" = all that have the seam); Test: `tests/arb_memory/test_local_memory_injection_acp.py`

**Interfaces:**
- Produces: `local_memory_mcp_config() -> dict | None` — when `ARB_MEMORY_LOCAL_MCP` is set, the MCP-server **launch spec**: `{command: "arb-memory-local-mcp", args: [], env: {ARB_MEMORY_LOCAL_DSN, OPENAI_API_KEY, PATH, PYTHONPATH}}` (forwards the read DSN **and** the OpenAI key + PATH/PYTHONPATH so the child can embed); else `None`.

- [ ] **Step 1: Failing test:** with `ARB_MEMORY_LOCAL_MCP` set, each ACP `session/new` payload's `mcpServers` has exactly one entry whose **command/env shape** is the local server (assert the *config shape* — command + that the env carries the DSN + `OPENAI_API_KEY` — NOT the dynamic tool list, which the launch config can't know); with it unset, `mcpServers == []` (byte-unchanged). For any ACP engine without an `mcpServers` seam, assert the flag is a no-op there.
- [ ] **Step 2: Run — fails.**
- [ ] **Step 3:** implement `local_memory_mcp_config()`; in each ACP `start()` replace `"mcpServers": []` with the maybe-injected list. Run — passes. Commit.

---

### Task 7: codex injection (per-launch `-c` override, no global TOML mutation)

**Files:** Modify `src/agent_redis_bridge/engines/codex.py` (`command_args()`); Test: `tests/arb_memory/test_local_memory_injection_codex.py`

**Mechanism (PINNED — agy verified via `codex app-server --help`):** codex takes per-invocation config
overrides via `-c 'key=value'`. Inject the MCP server as:
`-c 'mcp_servers.arb-memory-local={command="arb-memory-local-mcp", args=[], env={ARB_MEMORY_LOCAL_DSN="…", OPENAI_API_KEY="…", PATH="…", PYTHONPATH="…"}}'`
appended to the existing `codex … app-server --listen stdio://` args. **No `~/.codex/config.toml` write.**

- [ ] **Step 1: Failing test:** with `ARB_MEMORY_LOCAL_MCP` set, `command_args()` contains the `-c mcp_servers.arb-memory-local=…` override (env carries DSN + `OPENAI_API_KEY`) and the test asserts `~/.codex/config.toml` is **not** modified (mtime/content unchanged); unset → args byte-unchanged.
- [ ] **Step 2: Run — fails.**
- [ ] **Step 3:** build the `-c` override string from `local_memory_mcp_config()` and append in `command_args()`. Run — passes. Commit.

---

### Task 8: agent-sdk injection (`ClaudeAgentOptions(mcp_servers=…)`) + can_use_tool gate

**Files:** Modify `src/agent_redis_bridge/engines/agent_sdk.py` (options) **and** `agent_sdk_mediation.py` (the gate); Test: `tests/arb_memory/test_local_memory_injection_agent_sdk.py`

**Mechanism (PINNED — re-verified):** `ClaudeAgentOptions` accepts `mcp_servers: dict[str, McpServerConfig]`, but **`McpServerConfig` is a `Union` alias — NOT callable**. Use `McpStdioServerConfig` or a plain dict matching it:
`{"arb-memory-local": {"command": "arb-memory-local-mcp", "args": [], "env": {ARB_MEMORY_LOCAL_DSN, OPENAI_API_KEY, PATH, PYTHONPATH}}}`.

**CRITICAL — the runtime gate (cold-Opus + codex re-reviews — TWO layers):** agent-sdk seats route every
tool through `can_use_tool` → `decide(tool_name, ceiling, policy)` (`agent_sdk_mediation.py`).
`decide()` denies in this order: **(1) `tool_name not in ceiling` → deny "outside ceiling"; (2) `not in
KNOWN_TOOLS` → deny.** `self.ceiling = parse_ceiling(tool_ceiling)` is built **once at engine
construction** from `BRIDGE_AGENT_SDK_TOOLS` and **never augmented**. So admitting the tools to
`KNOWN_TOOLS`/`parse_ceiling` alone is **necessary but NOT sufficient** — at real launch the operator's
ceiling (e.g. `Read,Grep,Glob`) won't contain `mcp__arb-memory-local__memory_*`, so check (1) still
denies them. The injected tools arrive as `mcp__arb-memory-local__memory_{search,get,recent}`. A test
that asserts `options.mcp_servers` shape — or that calls `decide` with a hand-built ceiling already
containing the names — passes **vacuously-green while the tools are dead at runtime**.

- [ ] **Step 1: Failing tests:** (a) options carry `arb-memory-local` (dict shape, env has DSN + `OPENAI_API_KEY` + PATH/PYTHONPATH); (b) **drive the engine launch path** — construct `AgentSdkEngine` with `ARB_MEMORY_LOCAL_MCP` set and assert the *engine's own* `self.ceiling` / `_gate` **allows** `mcp__arb-memory-local__memory_search` (NOT a hand-built ceiling); unset → the engine's gate denies it and no option is present.
- [ ] **Step 2: Run — fails** (wrong type if `McpServerConfig` used; gate denies the mcp tool as outside-ceiling).
- [ ] **Step 3:** (i) add the dict-shaped server from `local_memory_mcp_config()`; (ii) add the three read names to `KNOWN_TOOLS` (so `parse_ceiling` tolerates them and `decide` step-2 admits them); (iii) **augment `self.ceiling`** with the three `mcp__arb-memory-local__memory_*` names when `ARB_MEMORY_LOCAL_MCP` is set — this is the load-bearing fix for `decide` step-1. They are read-only, consistent with the fail-closed posture. Run — passes. Commit.
- [ ] **Step 4:** if reconciling **both** gate layers proves larger than this slice, the fallback is to **exclude agent-sdk from injection this slice** (flag is a no-op there) + a test asserting that + a documented follow-up. Do not ship a green config test over a dead tool.

---

### Task 9: env-coherent DSN policy + tombstone resolution + docs

**Files:** Modify `src/agent_redis_bridge/local_memory_mcp.py` (DSN resolution), `tools/arb-memory-local/README.md`, `CHANGELOG.md`

- [ ] **Step 1: Failing test:** `local_read_dsn(env)` returns the env's read-only DSN coherent with its writer target; an explicit prod opt-in is required to read prod from a dev box (no silent default-to-prod or default-to-dev).
- [ ] **Step 2: Run — fails.**
- [ ] **Step 3:** implement the resolution. **Tombstone decision (resolve explicitly):** local `memory_get`/`memory_recent` **mirror the door** — `store.fetch_artefact`/`recent_artefacts` do not filter `deleted_at`, so local reads surface tombstoned artefacts exactly as the door does (documented; changing it would diverge local from the door). `search_hints` already filters `deleted_at IS NULL`. Document the policy + the opt-in. CHANGELOG entry. Run — passes. Commit.

---

## Notes for the implementer
- Real-PG tests follow `tests/arb_memory/test_mcp_role.py` + `conftest.py` (`scratch` schema, `_ensure_role`, search-path DSN); skip cleanly when the test DSN env is unset.
- Every "exactly 3 tools / no writes" assertion is a load-bearing security check — keep them, and confirm denied SQL is *valid* DML so the deny proves privilege, not a type/undefined error.
- The codex (`-c mcp_servers.…`) and agent-sdk (`ClaudeAgentOptions(mcp_servers=…)`) mechanisms are pinned (Tasks 7/8); re-verify the exact override string against the installed codex/`claude_agent_sdk` versions before writing the test, and keep the no-global-mutation constraint.
