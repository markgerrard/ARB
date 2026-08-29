# ARB Messages — Cloudflare `mint` Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or
> superpowers:executing-plans. Steps use `- [ ]` checkboxes.

**Goal:** Build ARB Messages — a fourth ARB plane, a Postgres-backed privileged-action broker
that lets agents request Cloudflare API-token minting from a privileged executor holding the
real root credentials, without ever handing agents a standing credential.

**Architecture:** A new `src/arb_messages/` package mirroring `src/arb_files/`/`src/arb_email/`'s
shape, but with a genuine **door/executor split** (not a single-process tool like the sibling
planes): the MCP door (`mcp/door_tools.py` + `mcp/door_wire.py`) runs inside the shared
`arb_memory.mcp.server` process with real OAuth context and does fast-reject pre-checks +
enqueue; a standalone `executor.py` process (no MCP context at all) independently re-validates
every claimed row against its own config, mints via `mint_cloudflare.py`, and runs a periodic
revocation sweep. Four Postgres tables (`arb_messages`, `arb_agent_keys`,
`arb_messages_settings`, `arb_messages_deadletter`) carry the queue, keys, kill-switch, and
orphan-token backstop respectively.

**Spec (authoritative):** `docs/superpowers/specs/2026-07-02-arb-messages-design.md` — read it in
full before starting. It went through **five rounds of a 4-seat security-review panel**
(unusually deep scrutiny for a credential-minting component); every `<!-- r1/r2/r3/r4/r5: ... -->`
inline marker records a real, panel-found gap and its fix. This plan operationalizes the
**converged, round-5 state** of that spec — do not re-derive the design from first principles or
"simplify" anything the spec's revision history explicitly rejected (the spec's own history
records *why* several tempting-looking simplifications are unsafe — read the markers, not just
the final prose, if a design choice looks over-engineered).

## Global Constraints

- **This is a security-critical, credential-minting component.** Every task below implements a
  specific mechanism the spec's panel review arrived at after multiple rounds of finding real
  gaps. Follow the spec's mechanisms exactly — this is not a place for "I found a simpler way."
  If you believe a spec mechanism is genuinely wrong, stop and flag it rather than silently
  deviating.
- **Mirror `arb_files`/`arb_email`** (`src/arb_files/`, `src/arb_email/`) for style, injection
  seams (`now`/`audit_sink`/`client_factory`-style dependency injection), test conventions (flat
  `tests/arb_messages/` dir, fake collaborators, `FakeClient`, `FakePostgres` or a real Postgres
  test fixture per Task 2's note).
- **`arb_messages_settings` — pause-flag seed is `ON CONFLICT (key) DO NOTHING`**, never an
  unconditional upsert. Get this exactly right the first time (spec § `arb_messages_settings`
  / Containment executor-side step 4) — the failure mode (a restart silently un-pausing an
  active incident) is subtle and easy to miss in a casual re-read of the schema.
- **Every revoke call site treats "already revoked / not found" from Cloudflare as success**, not
  failure — this rule is stated once in the spec and applies uniformly to: the found-branch's two
  revocations, the fenced-out-worker's revoke, and all three sweep categories. Don't reinvent this
  per call site; factor it into one shared helper (e.g. `mint_cloudflare.revoke_idempotent(token_id)`)
  used everywhere revoke is called.
- **Postgres DSN for tests:** a throwaway local Postgres container is already running for this
  work — `postgresql://arb_messages_test:$ARB_MESSAGES_TEST_PASSWORD@127.0.0.1:5599/arb_messages_test`
  (container `arb-messages-test-pg`, separate from the shared `arb-memory-pg-dev` dev database —
  never point tests at that one). Use it for any test that genuinely needs `SELECT ... FOR UPDATE
  SKIP LOCKED` row-locking semantics (Task 2's claim/fencing tests, Task 5's sweep-locking tests) —
  a pure in-memory fake cannot prove two concurrent claimers don't double-claim. `psycopg` 3 is
  already installed in `.venv`.
- **Live Cloudflare verification is a separate, explicitly-flagged residual** (Task 9, below) —
  it needs a real `ARB_MESSAGES_CF_MINTING_TOKEN` that may not exist yet and needs operator
  action to provision. Do not treat Task 9 as blocking the rest of the plan; do not silently skip
  it either — if it's genuinely not completable in this pass, say so explicitly rather than
  quietly dropping it.
- **Run tests:** `PYTHONPATH=src ARB_MESSAGES_TEST_DSN=postgresql://arb_messages_test:$ARB_MESSAGES_TEST_PASSWORD@127.0.0.1:5599/arb_messages_test .venv/bin/python -m pytest tests/arb_messages/ -v`
- TDD throughout: failing test → run/confirm fail → implement → run/confirm pass → commit per task.

---

### Task 1: Schema + Config

**Files:** Create `src/arb_messages/__init__.py`, `src/arb_messages/run.py`,
`src/arb_messages/config.py`; Test `tests/arb_messages/__init__.py`, `tests/arb_messages/test_run.py`,
`tests/arb_messages/test_config.py`.

**Schema (`run.py::setup_schema(conn)`), matching `arb_memory/run.py:89-166`'s idempotent DDL
pattern exactly** (`CREATE TABLE IF NOT EXISTS`, `bigserial PRIMARY KEY`, additive
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS` — never a native
Postgres `ENUM`, plain `text` with app-level validation per spec § Request lifecycle):

- **`arb_messages`**: `id bigserial PRIMARY KEY`, `agent_id text NOT NULL`, `request_id text NOT NULL`,
  `request_type text NOT NULL`, `provider text NOT NULL`, `capability text NOT NULL`,
  `zone text NOT NULL`, `status text NOT NULL DEFAULT 'pending'`, `policy_decision text`,
  `policy_reason text`, `provider_token_id text`, `body bytea`, `attempts int NOT NULL DEFAULT 0`,
  `claimed_at timestamptz`, `completed_at timestamptz`, `delivered_at timestamptz`,
  `token_revoked_at timestamptz`, `created_at timestamptz NOT NULL DEFAULT now()`.
  <!-- Plan-review r1 (cold-Opus P1): `body` was `jsonb` in the original draft, but
  `write_sealed_result` stores raw PyNaCl `SealedBox` ciphertext (bytes), and psycopg3 cannot
  insert `bytes` into a `jsonb` column — the very first store test would fail at implementation
  time. `bytea` is the correct type for opaque sealed ciphertext; nothing about this column is
  ever meant to be queried/indexed as JSON. Plan-review r1 (agy-print P1, cold-Opus P1): added
  `completed_at timestamptz` — set once, alongside `status='done'`, in `write_sealed_result` —
  because the sweep's category-2 grace window (below) needs a timestamp to measure "how long has
  this row been done" for the case where `delivered_at` is still null; `delivered_at` itself
  can't serve that purpose precisely because it's null in exactly the case being measured. -->
  Constraints/indexes: `UNIQUE (agent_id, request_id)` (spec § Transport — compound, never a bare
  `request_id`); `CREATE INDEX ON (status, claimed_at)` (the claim/lease query's actual access
  pattern — do NOT also add a standalone `agent_id` index, the compound UNIQUE already covers
  agent-scoped lookups as a leftmost-prefix).
- **`arb_agent_keys`**: `agent_id text NOT NULL`, `pubkey text NOT NULL`, `fingerprint text NOT NULL`,
  `created_at timestamptz NOT NULL DEFAULT now()`, `revoked_at timestamptz`.
  Constraint: `CREATE UNIQUE INDEX ON arb_agent_keys (agent_id) WHERE revoked_at IS NULL`
  (one live key per agent, DB-enforced — spec § `arb_agent_keys`).
- **`arb_messages_settings`**: `key text PRIMARY KEY`, `value text NOT NULL`. Seed exactly one
  row: `INSERT INTO arb_messages_settings (key, value) VALUES ('paused', '0') ON CONFLICT (key)
  DO NOTHING` — **the `ON CONFLICT DO NOTHING` is load-bearing, not optional**, see Global
  Constraints above.
- **`arb_messages_deadletter`**: `provider_token_id text PRIMARY KEY`,
  `discovered_at timestamptz NOT NULL DEFAULT now()`, `reason text`,
  `token_revoked_at timestamptz`, `attempts int NOT NULL DEFAULT 0`,
  `last_attempt_at timestamptz`. Rows are never deleted (permanent audit record, spec §
  `arb_messages_deadletter`).

**Config (`config.py`) — `Settings` frozen dataclass + `load_settings(env)`, matching the
`arb_files/config.py:25-42` `_REQUIRED`-list-plus-joined-error pattern** (NOT `arb_email`'s
single-var pattern — spec explicitly corrects this citation):

```python
@dataclass(frozen=True)
class Settings:
    postgres_dsn: str
    cf_minting_token: str
    allowed_zones: frozenset[str]
    allowed_agents: frozenset[str]
    lease_seconds: int = 300
    delivered_grace_seconds: int = 3600
    max_retries: int = 3
    messages_enabled: bool = True
    poll_interval_seconds: float = 1.0  # Plan-review r3 (pi-GLM, codex, agy-print, cold-Opus --
    # 4-way convergence): referenced in run_claim_loop's wrapper (Task 5) but never actually
    # defined anywhere in round-1/2 drafts, which would have been a NameError at runtime. Lives
    # on Settings (not a bare module constant) so it's overridable per-deployment/per-test like
    # every other timing knob here.

def load_settings(env: Mapping[str, str]) -> Settings: ...
```

- Required: `postgres_dsn`, `cf_minting_token`, `allowed_zones`, `allowed_agents` — ALL four,
  collected into one `ValueError("ARB Messages config missing: <comma-joined names>")` (spec
  corrected this in round 2 then widened the required set in round 2's config-contradiction fix
  — `allowed_zones`/`allowed_agents` are required-and-non-empty, not defaulted-empty).
  Env vars: `ARB_MESSAGES_POSTGRES_DSN`, `ARB_MESSAGES_CF_MINTING_TOKEN`,
  `ARB_MESSAGES_ALLOWED_ZONES` (comma-split), `ARB_MESSAGES_ALLOWED_AGENTS` (comma-split,
  lower-cased), `ARB_MESSAGES_ENABLED` (`"1"`/`"0"`, default on — `arb_email`'s exact
  string-comparison pattern).
- Allowlists validated non-empty at load time (matching `arb_email/config.py:34-44`'s
  validate-once pattern) with their own named error, distinct from the four-var missing check.

- [ ] **Step 1 — failing tests:**
```python
# tests/arb_messages/test_run.py
import pytest, psycopg, os
from arb_messages.run import setup_schema

DSN = os.environ["ARB_MESSAGES_TEST_DSN"]

@pytest.fixture
def conn():
    # Plan-review r2 (cold-Opus P1): the original fixture had no TRUNCATE, unlike every sibling
    # test file's fixture (Task 2-5 all truncate before yielding) -- and with autocommit=True,
    # this file's own conn.rollback() calls after a deliberate UniqueViolation are no-ops (there's
    # no open transaction to roll back), so the row inserted by
    # test_creates_arb_messages_with_compound_unique persists into later tests in this same file
    # (including test_arb_messages_has_completed_at_and_body_is_bytea, whose INSERT for the same
    # (agent_id, request_id) pair would then hit a real UniqueViolation of its own). Matched to
    # the sibling fixtures' pattern.
    c = psycopg.connect(DSN, autocommit=True)
    setup_schema(c)
    with c.cursor() as cur:
        cur.execute("TRUNCATE arb_messages, arb_agent_keys, arb_messages_settings, arb_messages_deadletter RESTART IDENTITY")
    setup_schema(c)  # re-seed arb_messages_settings's 'paused' row via setup_schema's own
                      # idempotent ON CONFLICT DO NOTHING logic, not a duplicate raw INSERT
    yield c
    c.close()

def test_setup_schema_idempotent(conn):
    setup_schema(conn)
    setup_schema(conn)  # must not raise on second run

def test_creates_arb_messages_with_compound_unique(conn):
    setup_schema(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO arb_messages (agent_id, request_id, request_type, provider, capability, zone) VALUES ('a','r1','mint','cloudflare','zone_dns_edit','z1')")
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute("INSERT INTO arb_messages (agent_id, request_id, request_type, provider, capability, zone) VALUES ('a','r1','mint','cloudflare','zone_dns_edit','z1')")
    conn.rollback()

def test_arb_messages_allows_same_request_id_different_agent(conn):
    setup_schema(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO arb_messages (agent_id, request_id, request_type, provider, capability, zone) VALUES ('a','r1','mint','cloudflare','zone_dns_edit','z1')")
        cur.execute("INSERT INTO arb_messages (agent_id, request_id, request_type, provider, capability, zone) VALUES ('b','r1','mint','cloudflare','zone_dns_edit','z1')")
    conn.commit()

def test_arb_agent_keys_one_live_key_per_agent(conn):
    setup_schema(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO arb_agent_keys (agent_id, pubkey, fingerprint) VALUES ('a','pk1','fp1')")
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute("INSERT INTO arb_agent_keys (agent_id, pubkey, fingerprint) VALUES ('a','pk2','fp2')")
    conn.rollback()
    with conn.cursor() as cur:
        # revoke then re-register succeeds
        cur.execute("UPDATE arb_agent_keys SET revoked_at = now() WHERE agent_id = 'a'")
        cur.execute("INSERT INTO arb_agent_keys (agent_id, pubkey, fingerprint) VALUES ('a','pk2','fp2')")
    conn.commit()

def test_settings_paused_seed_survives_rerun(conn):
    setup_schema(conn)
    with conn.cursor() as cur:
        cur.execute("UPDATE arb_messages_settings SET value = '1' WHERE key = 'paused'")
    conn.commit()
    setup_schema(conn)  # must NOT reset paused back to '0'
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM arb_messages_settings WHERE key = 'paused'")
        assert cur.fetchone()[0] == "1"

def test_deadletter_schema_has_retry_columns(conn):
    setup_schema(conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO arb_messages_deadletter (provider_token_id, reason) VALUES ('tok1', 'test')")
        cur.execute("SELECT attempts, last_attempt_at, token_revoked_at FROM arb_messages_deadletter WHERE provider_token_id = 'tok1'")
        attempts, last_attempt, revoked = cur.fetchone()
        assert attempts == 0 and last_attempt is None and revoked is None
    conn.commit()

def test_arb_messages_has_completed_at_and_body_is_bytea(conn):
    # Plan-review r1 fix's own explicit schema guard, proactively added ahead of round-2 review.
    setup_schema(conn)
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO arb_messages (agent_id, request_id, request_type, provider,
            capability, zone, body, completed_at) VALUES ('a','r1','mint','cloudflare',
            'zone_dns_edit','z1', %s, now()) RETURNING completed_at, body""", (b"raw-bytes-not-json",))
        completed_at, body = cur.fetchone()
        assert completed_at is not None
        assert bytes(body) == b"raw-bytes-not-json"  # bytea round-trips raw bytes; jsonb would reject this insert
    conn.commit()
```
```python
# tests/arb_messages/test_config.py
import pytest
from arb_messages.config import load_settings
BASE = {
    "ARB_MESSAGES_POSTGRES_DSN": "postgresql://x",
    "ARB_MESSAGES_CF_MINTING_TOKEN": "tok",
    "ARB_MESSAGES_ALLOWED_ZONES": "z1,z2",
    "ARB_MESSAGES_ALLOWED_AGENTS": "agent-a,agent-b",
}
def test_loads_required_fields():
    s = load_settings(BASE)
    assert s.postgres_dsn == "postgresql://x"
    assert s.allowed_zones == frozenset({"z1", "z2"})
    assert s.allowed_agents == frozenset({"agent-a", "agent-b"})
    assert s.messages_enabled is True

def test_missing_required_vars_lists_all_in_one_error():
    with pytest.raises(ValueError) as exc:
        load_settings({})
    msg = str(exc.value)
    for var in ("ARB_MESSAGES_POSTGRES_DSN", "ARB_MESSAGES_CF_MINTING_TOKEN",
                "ARB_MESSAGES_ALLOWED_ZONES", "ARB_MESSAGES_ALLOWED_AGENTS"):
        assert var in msg

def test_empty_allowlist_after_strip_fails():
    with pytest.raises(ValueError):
        load_settings({**BASE, "ARB_MESSAGES_ALLOWED_ZONES": " , "})

def test_kill_switch_string_one_comparison():
    assert load_settings({**BASE, "ARB_MESSAGES_ENABLED": "0"}).messages_enabled is False
    assert load_settings({**BASE, "ARB_MESSAGES_ENABLED": "1"}).messages_enabled is True

def test_agent_ids_lowercased():
    s = load_settings({**BASE, "ARB_MESSAGES_ALLOWED_AGENTS": "Agent-A"})
    assert s.allowed_agents == frozenset({"agent-a"})

def test_defaults():
    s = load_settings(BASE)
    assert s.lease_seconds == 300 and s.delivered_grace_seconds == 3600 and s.max_retries == 3
```
- [ ] **Step 2** — run, confirm fail (ModuleNotFound).
- [ ] **Step 3** — implement `run.py` and `config.py` per the schema/interface above.
- [ ] **Step 4** — run, PASS.
- [ ] **Step 5** — commit `feat(arb-messages): schema + config`.

---

### Task 2: Audit + Store (queue mechanics — the security core of this plane)

**Files:** Create `src/arb_messages/audit.py`, `src/arb_messages/store.py`; Test
`tests/arb_messages/test_store.py`.

**Audit (`audit.py`)** — `default_audit_sink(event: dict)`, log-based, matching
`arb_email/audit.py:10-11`'s shape byte-for-byte (just `logging.getLogger("arb_messages.audit")`).
The sink itself does not need to be exception-safe — callers wrap it (spec § Audit).

**Store (`store.py`) — this is the biggest task in the plan.** Implement, in order:

1. `enqueue(conn, agent_id, request_id, request_type, provider, capability, zone,
   policy_decision, policy_reason) -> row_id` — `INSERT ... ON CONFLICT (agent_id, request_id) DO
   NOTHING RETURNING id`; on conflict (no row returned), look up and return the existing row's id
   (idempotent enqueue).
2. `claim(conn, lease_seconds) -> row | None` — the `SELECT ... FOR UPDATE SKIP LOCKED` claim
   query per spec § Request lifecycle: `WHERE status = 'pending' OR (status = 'claimed' AND
   claimed_at < now() - lease_seconds * interval '1 second')`, ordered for fairness (e.g. oldest
   first), `LIMIT 1`; the claiming `UPDATE` sets `status='claimed', claimed_at=now(),
   attempts=attempts+1, token_revoked_at=NULL` <!-- Plan-review r3 (codex P1, agy-print P1,
   cold-Opus P1 -- 3-way convergence, confirmed by direct re-verification against this exact
   UPDATE): round-2's `fenced_write` fix (item 3, below) added `AND token_revoked_at IS NULL` to
   close the sweep-vs-slow-worker race, but nothing ever RESET `token_revoked_at` back to null on
   a reclaim — it's a genuine regression the fix itself introduced. Reachable sequence: worker A
   claims, writes `provider_token_id`, stalls before sealing; sweep category 1 sees the
   lease-expired claimed row, revokes A's token, sets `token_revoked_at`; worker B legitimately
   reclaims the same row (a normal, expected lease-expiry reclaim, not a race) — but since
   nothing clears `token_revoked_at`, B's own `write_provider_token_id`/`write_sealed_result`
   (and even `mark_failed`) all fence out against B's own FRESH `claimed_at`, permanently
   wedging the row (every future reclaim hits the identical dead end). Fixed by resetting
   `token_revoked_at=NULL` as part of the SAME atomic UPDATE that advances `claimed_at` on
   reclaim — this does NOT reopen the original race: the original race's fenced-out slow worker
   is caught by the (unchanged) `claimed_at = %s` check alone, since a reclaim always advances
   `claimed_at` to a new value the slow worker doesn't hold; `token_revoked_at IS NULL` only
   needed to catch the narrower window BEFORE a reclaim happens (same `claimed_at`, sweep already
   revoked) — a window this reset does not touch. `provider_token_id` is deliberately NOT reset
   here — `mint()`'s found-branch (Task 4) needs to see the prior claimant's token ID to know to
   revoke it. -->
   and **returns the claimed row including the new `claimed_at`** (the
   caller needs this exact timestamp for the fencing guard on every subsequent write).
3. `fenced_write(conn, row_id, claimed_at, **fields) -> bool` — every write an executor makes to
   a row it claimed goes through this: `UPDATE arb_messages SET ... WHERE id = %s AND status =
   'claimed' AND claimed_at = %s AND token_revoked_at IS NULL`, returns `True` if it affected
   exactly one row, `False` if zero (the caller lost the fence — either a reclaim happened, OR
   the sweep already revoked this row's token out from under it — see below). This is the
   **shared primitive** every executor-side write in Tasks 4/5 must use — do not write ad-hoc
   `UPDATE`s elsewhere. <!-- Plan-review r2 (agy-print P1): the original condition
   (`status = 'claimed' AND claimed_at = %s`) had a real race with the sweep. Sweep category 1
   (Task 5) revokes a lease-expired **claimed** row's token — its own token, still on the row —
   without changing `status` or `claimed_at` (it isn't reclaiming the row, just cleaning up a
   token nobody is using anymore). If the ORIGINAL slow worker that claimed the row is still
   alive (just slow, not dead) and finally gets around to calling `write_sealed_result` with its
   own unchanged, still-matching `claimed_at`, the original fence condition would have let that
   write through — marking the row `done` with a `provider_token_id` pointing at a token the
   sweep already revoked. The agent would receive a sealed result for a dead token. Adding
   `AND token_revoked_at IS NULL` closes this at the root: any write attempt against a row whose
   token has already been swept fails the fence, exactly like a reclaim does, and the slow worker
   discovers it lost the row (via the same "affected zero rows, stop" handling Task 4 already
   specifies for a lease-reclaim fence failure — no new code path needed, just the wider
   condition). A new test (see Step 1 below) proves this: a row whose `token_revoked_at` is set
   by a simulated sweep, but whose `status`/`claimed_at` are otherwise untouched, must fence out
   a write attempting to use the row's own (still-technically-matching) `claimed_at`. -->
4. `read_and_mark_delivered(conn, agent_id, request_id) -> dict` — the shared atomic function
   both `messages_poll` and `messages_request`'s inline-wait call (spec § Containment's
   exactly-once-retrieval fix, r5-corrected). `SELECT ... FOR UPDATE` scoped to
   `WHERE agent_id = %s AND request_id = %s`; if no row, return `{"status": "not_found"}`
   (this is also the wrong-actor-poll response — never distinguish "wrong actor" from "doesn't
   exist" in the response, spec § Containment's ownership-check paragraph); if row status is
   `pending`/`claimed`/`failed`/`denied`, return `{"status": <that status>}` **without touching
   `delivered_at`** (a true no-op — this is the r4/r5 bugfix, get it right the first time); if
   row status is `done` and `delivered_at IS NULL`, mark `delivered_at = now()` and return
   `{"status": "delivered", "body": <sealed body>}`; if `done` and `delivered_at IS NOT NULL`,
   return `{"status": "already_delivered"}` without re-touching the row.
5. `write_sealed_result(conn, row_id, claimed_at, sealed_bytes: bytes, provider_token_id: str) ->
   bool` — the ONLY write path for a `mint` result's body (spec § Two-plane row split's structural
   sealing fix). Signature has NO plaintext-token parameter — this is enforced at the type level,
   not just by convention. Goes through `fenced_write`; sets `status='done', body=%s,
   provider_token_id=%s, completed_at=now()` <!-- Plan-review r1 (agy-print/cold-Opus): setting
   `completed_at` here, alongside the status transition to `done`, is what makes the sweep's
   category-2 grace window (§ schema, Task 5) actually measurable for the null-`delivered_at`
   case. --> (only if not already set by an earlier `write_provider_token_id` call — see next).
6. `write_provider_token_id(conn, row_id, claimed_at, provider_token_id: str) -> bool` — invariant
   7's immediate-record write, its own small transaction/call, separate from and before
   `write_sealed_result`. Goes through `fenced_write`.
7. `mark_failed(conn, row_id, claimed_at, reason: str) -> bool` and `mark_denied(...)` — terminal
   states, both through `fenced_write`.

- [ ] **Step 1 — failing tests** (this is the file where the spec's hardest-won correctness
  guarantees live — write real regression tests for each, not just happy-path coverage):
```python
# tests/arb_messages/test_store.py
import pytest, psycopg, os, time
from arb_messages.run import setup_schema
from arb_messages.store import (enqueue, claim, fenced_write, read_and_mark_delivered,
    write_sealed_result, write_provider_token_id, mark_failed)

DSN = os.environ["ARB_MESSAGES_TEST_DSN"]

@pytest.fixture
def conn():
    c = psycopg.connect(DSN, autocommit=True)
    setup_schema(c)
    with c.cursor() as cur:
        cur.execute("TRUNCATE arb_messages, arb_agent_keys, arb_messages_deadletter RESTART IDENTITY")
    yield c
    c.close()

def test_enqueue_dedupes_same_agent_request_id(conn):
    id1 = enqueue(conn, "a", "r1", "mint", "cloudflare", "zone_dns_edit", "z1", "approved", None)
    id2 = enqueue(conn, "a", "r1", "mint", "cloudflare", "zone_dns_edit", "z1", "approved", None)
    assert id1 == id2

def test_enqueue_allows_same_request_id_different_agent(conn):
    id1 = enqueue(conn, "a", "r1", "mint", "cloudflare", "zone_dns_edit", "z1", "approved", None)
    id2 = enqueue(conn, "b", "r1", "mint", "cloudflare", "zone_dns_edit", "z1", "approved", None)
    assert id1 != id2

def test_claim_two_concurrent_never_claim_same_row(conn):
    enqueue(conn, "a", "r1", "mint", "cloudflare", "zone_dns_edit", "z1", "approved", None)
    conn2 = psycopg.connect(DSN, autocommit=True)
    try:
        # both connections attempt to claim; SKIP LOCKED means at most one gets it per call
        row1 = claim(conn, lease_seconds=300)
        row2 = claim(conn2, lease_seconds=300)
        assert row1 is not None
        assert row2 is None  # already claimed by conn
    finally:
        conn2.close()

def test_lease_expiry_makes_claimed_row_reclaimable(conn):
    enqueue(conn, "a", "r1", "mint", "cloudflare", "zone_dns_edit", "z1", "approved", None)
    row = claim(conn, lease_seconds=0)  # zero-second lease: immediately "expired"
    time.sleep(0.05)
    row2 = claim(conn, lease_seconds=0)
    assert row2 is not None and row2["id"] == row["id"]

def test_fenced_write_stale_claim_affects_zero_rows(conn):
    enqueue(conn, "a", "r1", "mint", "cloudflare", "zone_dns_edit", "z1", "approved", None)
    row = claim(conn, lease_seconds=0)
    time.sleep(0.05)
    row2 = claim(conn, lease_seconds=0)  # reclaims, new claimed_at
    assert row2["id"] == row["id"]
    ok = fenced_write(conn, row["id"], row["claimed_at"], provider_token_id="stale-token")
    assert ok is False  # row["claimed_at"] is now stale, worker "row" lost the race

# Plan-review r2 (agy-print P1): the direct regression guard for the sweep-vs-slow-worker race.
# A slow worker whose claimed_at is STILL VALID (no reclaim happened -- only the sweep acted on
# the row) must still be fenced out once the sweep has revoked its token, or the agent would
# receive a sealed result pointing at an already-dead token.
def test_fenced_write_blocked_after_sweep_revokes_the_token(conn):
    enqueue(conn, "a", "r1", "mint", "cloudflare", "zone_dns_edit", "z1", "approved", None)
    row = claim(conn, lease_seconds=300)  # long lease -- no reclaim will happen
    with conn.cursor() as cur:
        # simulates the sweep: revokes the token, but does NOT touch status/claimed_at
        cur.execute("UPDATE arb_messages SET provider_token_id = 'tok-x', token_revoked_at = now() WHERE id = %s", (row["id"],))
    # the ORIGINAL slow worker, still holding the SAME (still-technically-valid) claimed_at,
    # now tries to complete the row it thinks it still owns
    ok = fenced_write(conn, row["id"], row["claimed_at"], status="done", body=b"stale-sealed-bytes")
    assert ok is False  # must be fenced out even though claimed_at still matches -- the sweep
                         # already revoked the token this write would have delivered

# Plan-review r3 (codex P1, agy-print P1, cold-Opus P1 -- 3-way convergence): the direct
# regression guard for the reclaim-after-sweep wedge the r2 fix introduced. A LEGITIMATE
# reclaiming worker (fresh claimed_at, not the stale slow worker from the test above) must be
# able to write successfully, proving claim()'s token_revoked_at=NULL reset actually works.
def test_reclaim_after_sweep_revoke_can_still_write(conn):
    enqueue(conn, "a", "r1", "mint", "cloudflare", "zone_dns_edit", "z1", "approved", None)
    row = claim(conn, lease_seconds=0)  # zero-lease so it's immediately reclaimable
    with conn.cursor() as cur:
        # simulates: worker A wrote provider_token_id then stalled; sweep category 1 revoked it
        cur.execute("UPDATE arb_messages SET provider_token_id = 'tok-a-stale', token_revoked_at = now() WHERE id = %s", (row["id"],))
    time.sleep(0.05)
    row_b = claim(conn, lease_seconds=0)  # worker B legitimately reclaims -- a normal lease-expiry reclaim
    assert row_b["id"] == row["id"]
    # worker B's fresh claimed_at must NOT be fenced out by the stale token_revoked_at left
    # behind by the sweep -- claim() must have reset it on reclaim
    ok = fenced_write(conn, row_b["id"], row_b["claimed_at"], provider_token_id="tok-b-fresh")
    assert ok is True  # the legitimate new claimant can write; the row is not permanently wedged
    with conn.cursor() as cur:
        cur.execute("SELECT token_revoked_at FROM arb_messages WHERE id = %s", (row_b["id"],))
        assert cur.fetchone()[0] is None  # reset by the reclaim, as claim()'s UPDATE now specifies

def test_read_and_mark_delivered_pending_row_is_true_noop(conn):
    # Plan-review r1 (agy-print P1, codex P1, cold-Opus P1 -- 3-way convergence): the original
    # version of this test only checked the returned status dict, never the DB's actual
    # delivered_at column. A buggy implementation that sets delivered_at=now() on every read
    # regardless of status (the exact round-4/5 bug the spec fixed) would still pass a
    # status-only assertion. Strengthened to check delivered_at directly, AND to prove the poll
    # sequence end-to-end: an early poll while pending must not consume the one-time slot once
    # the row genuinely completes.
    row_id = enqueue(conn, "a", "r1", "mint", "cloudflare", "zone_dns_edit", "z1", "approved", None)
    result = read_and_mark_delivered(conn, "a", "r1")
    assert result["status"] == "pending"
    with conn.cursor() as cur:
        cur.execute("SELECT delivered_at FROM arb_messages WHERE id = %s", (row_id,))
        assert cur.fetchone()[0] is None
    result2 = read_and_mark_delivered(conn, "a", "r1")
    assert result2["status"] == "pending"  # repeatable, not consumed
    with conn.cursor() as cur:
        cur.execute("SELECT delivered_at FROM arb_messages WHERE id = %s", (row_id,))
        assert cur.fetchone()[0] is None
    # Now complete the row and confirm the earlier pending polls did NOT consume the slot --
    # the subsequent poll must return the real sealed body, not already_delivered.
    row = claim(conn, lease_seconds=300)
    write_provider_token_id(conn, row["id"], row["claimed_at"], "tok-1")
    write_sealed_result(conn, row["id"], row["claimed_at"], b"sealed-bytes", "tok-1")
    result3 = read_and_mark_delivered(conn, "a", "r1")
    assert result3["status"] == "delivered" and result3["body"] == b"sealed-bytes"

def test_read_and_mark_delivered_consumes_exactly_once(conn):
    row_id = enqueue(conn, "a", "r1", "mint", "cloudflare", "zone_dns_edit", "z1", "approved", None)
    row = claim(conn, lease_seconds=300)
    write_provider_token_id(conn, row["id"], row["claimed_at"], "tok-1")
    write_sealed_result(conn, row["id"], row["claimed_at"], b"sealed-bytes", "tok-1")
    r1 = read_and_mark_delivered(conn, "a", "r1")
    assert r1["status"] == "delivered" and r1["body"] == b"sealed-bytes"
    r2 = read_and_mark_delivered(conn, "a", "r1")
    assert r2["status"] == "already_delivered"
    assert "body" not in r2

def test_read_and_mark_delivered_wrong_actor_not_found(conn):
    enqueue(conn, "a", "r1", "mint", "cloudflare", "zone_dns_edit", "z1", "approved", None)
    result = read_and_mark_delivered(conn, "b", "r1")  # wrong agent_id
    assert result["status"] == "not_found"

def test_read_and_mark_delivered_failed_and_denied_are_noops(conn):
    row_id = enqueue(conn, "a", "r1", "mint", "cloudflare", "zone_dns_edit", "z1", "approved", None)
    row = claim(conn, lease_seconds=300)
    mark_failed(conn, row["id"], row["claimed_at"], "test failure")
    result = read_and_mark_delivered(conn, "a", "r1")
    assert result["status"] == "failed"

def test_write_sealed_result_has_no_plaintext_parameter():
    import inspect
    sig = inspect.signature(write_sealed_result)
    assert "token" not in sig.parameters and "plaintext" not in sig.parameters
    assert "sealed_bytes" in sig.parameters

def test_attempts_column_increments_on_each_claim(conn):
    enqueue(conn, "a", "r1", "mint", "cloudflare", "zone_dns_edit", "z1", "approved", None)
    row1 = claim(conn, lease_seconds=0)
    assert row1["attempts"] == 1
    time.sleep(0.05)
    row2 = claim(conn, lease_seconds=0)
    assert row2["attempts"] == 2
```
- [ ] **Step 2** — run, confirm fail.
- [ ] **Step 3** — implement `store.py` per the numbered interface above.
- [ ] **Step 4** — run, PASS.
- [ ] **Step 5** — commit `feat(arb-messages): audit + store (enqueue/claim/fencing/exactly-once delivery)`.

---

### Task 3: Agent keys (registration, sealing)

**Files:** Create `src/arb_messages/keys.py`; Test `tests/arb_messages/test_keys.py`.

**Dependencies:** add `pynacl` to `pyproject.toml` (spec's PyNaCl-over-`age` decision — pure
Python, no external binary).

**Interface (`keys.py`):**
- `register_key(conn, agent_id: str, pubkey_bytes: bytes) -> None` — validates `pubkey_bytes` is
  a well-formed PyNaCl `SealedBox`-compatible public key (correct length: 32 bytes for
  Curve25519; decodable) *before* the `INSERT`, raising `ValueError` on a malformed key rather
  than locking in garbage (spec § `arb_agent_keys`, r5 fix). `INSERT INTO arb_agent_keys
  (agent_id, pubkey, fingerprint) VALUES (...)`; a second registration for an agent with a live
  key fails on the DB's own partial unique index — let that `UniqueViolation` propagate, don't
  catch-and-silently-ignore it.
- `live_key(conn, agent_id: str) -> bytes | None` — `SELECT pubkey FROM arb_agent_keys WHERE
  agent_id = %s AND revoked_at IS NULL`.
- `seal(plaintext: bytes, recipient_pubkey: bytes) -> bytes` — `nacl.public.SealedBox(PublicKey(
  recipient_pubkey)).encrypt(plaintext)`.
- `unseal(sealed: bytes, recipient_privkey: bytes) -> bytes` — the inverse, **for tests only**
  (server code never holds a private key).

- [ ] **Step 1 — failing tests:**
```python
# tests/arb_messages/test_keys.py
import pytest, psycopg, os
from nacl.public import PrivateKey
from arb_messages.run import setup_schema
from arb_messages.keys import register_key, live_key, seal, unseal

DSN = os.environ["ARB_MESSAGES_TEST_DSN"]

@pytest.fixture
def conn():
    c = psycopg.connect(DSN, autocommit=True)
    setup_schema(c)
    with c.cursor() as cur:
        cur.execute("TRUNCATE arb_agent_keys RESTART IDENTITY")
    yield c
    c.close()

def test_register_and_lookup(conn):
    sk = PrivateKey.generate()
    register_key(conn, "agent-a", bytes(sk.public_key))
    assert live_key(conn, "agent-a") == bytes(sk.public_key)

def test_no_live_key_returns_none(conn):
    assert live_key(conn, "agent-nobody") is None

def test_malformed_pubkey_rejected_before_insert(conn):
    with pytest.raises(ValueError):
        register_key(conn, "agent-a", b"too-short")
    assert live_key(conn, "agent-a") is None

def test_second_registration_for_live_key_rejected(conn):
    sk1 = PrivateKey.generate()
    register_key(conn, "agent-a", bytes(sk1.public_key))
    sk2 = PrivateKey.generate()
    with pytest.raises(psycopg.errors.UniqueViolation):
        register_key(conn, "agent-a", bytes(sk2.public_key))
    conn.rollback()

def test_revoked_key_excluded_from_live_lookup(conn):
    sk = PrivateKey.generate()
    register_key(conn, "agent-a", bytes(sk.public_key))
    with conn.cursor() as cur:
        cur.execute("UPDATE arb_agent_keys SET revoked_at = now() WHERE agent_id = 'agent-a'")
    assert live_key(conn, "agent-a") is None

def test_reregistration_after_revoke_succeeds(conn):
    sk1 = PrivateKey.generate()
    register_key(conn, "agent-a", bytes(sk1.public_key))
    with conn.cursor() as cur:
        cur.execute("UPDATE arb_agent_keys SET revoked_at = now() WHERE agent_id = 'agent-a'")
    sk2 = PrivateKey.generate()
    register_key(conn, "agent-a", bytes(sk2.public_key))
    assert live_key(conn, "agent-a") == bytes(sk2.public_key)

def test_seal_unseal_round_trip():
    sk = PrivateKey.generate()
    ciphertext = seal(b"a real token", bytes(sk.public_key))
    assert unseal(ciphertext, bytes(sk)) == b"a real token"
    assert b"a real token" not in ciphertext  # sanity: not accidentally cleartext
```
- [ ] **Step 2** — run, fail. **Step 3** — implement. **Step 4** — PASS.
- [ ] **Step 5** — commit `feat(arb-messages): agent-key registration + PyNaCl sealing`.

---

### Task 4: `mint_cloudflare.py` — the Cloudflare handler, revoke-then-remint

**Files:** Create `src/arb_messages/mint_cloudflare.py`; Test `tests/arb_messages/test_mint_cloudflare.py`.

**This is the mechanism that took five review rounds to get right** — implement it exactly per
spec § Security invariant 8, not from a fresh reading of "revoke and mint a token." Re-read the
spec's inline r2/r3/r4/r5 markers on invariant 8 before writing this file; they record specific,
concrete bugs (impossible token-secret recovery, asymmetric revocation, an ambiguous completion
condition) that a naive implementation will reintroduce.

**Interface:**
- `deterministic_name(agent_id: str, request_id: str) -> str` —
  `"arb-msg-" + sha256(sha256(agent_id).hexdigest() + ":" + sha256(request_id).hexdigest()).hexdigest()[:32]`
  (double-hash to avoid separator injection — spec r3 fix).
- `revoke_idempotent(cf_client, token_id: str) -> None` — calls the CF delete API; if the
  response indicates "already revoked / not found," treat as success (no exception). **Every
  revoke call site in this file and in `executor.py` (Task 5) must go through this function** —
  do not duplicate the 404-handling logic. <!-- Plan-review r3 (agy-print P2): the `FakeCFClient`
  test double (Step 1, below) raises `LookupError("not_found")` to model CF's 404 shape — the
  production `cf_client` wrapper (built against the real Cloudflare API in a later, separate
  integration step, not part of this pass's unit-test surface) must map a real HTTP 404 /
  "not found" response to that same `LookupError`, so `revoke_idempotent`'s exception handling
  works identically against the fake and the real client. Name this explicitly as a production
  wrapper requirement so the mapping isn't invented ad hoc later. -->
  Treat `LookupError` from `cf_client.revoke(...)` as the canonical "already gone" signal.
- `mint(conn, cf_client, keys_module, row) -> None` — the found/not-found branches:
  1. Compute `name = deterministic_name(row["agent_id"], row["request_id"])`.
  2. **If `row["provider_token_id"]` is already set (non-null): `revoke_idempotent(cf_client,
     row["provider_token_id"])` first, directly by ID** — before anything else, regardless of
     what the name lookup later finds.
  3. Perform `cf_client.list_by_name(name)` — may return zero, one, or more than one live token
     (CF permits duplicate names; do not assume at most one). **Revoke every one of them** via
     `revoke_idempotent`.
  4. Only after all of steps 2-3's revocations have succeeded (idempotent revoke counts as
     success): look up `live_key(conn, row["agent_id"])`; if `None`, this is a bug (the executor
     should have re-checked key liveness before calling `mint` — Task 5 owns that check) — raise.
  5. Call `cf_client.create(name=name, zone=row["zone"], capability=row["capability"])`
     (§ Containment executor-side step 2 — the capability→CF-permission-group mapping lives here,
     a small fixed dict, e.g. `{"zone_dns_edit": {...}, "zone_settings_read": {...}}`; **never
     accept or forward a caller-supplied policy blob**), returns `(token_id, secret)`.
  6. `store.write_provider_token_id(conn, row["id"], row["claimed_at"], token_id)` — immediately,
     its own call, before sealing.
  7. `sealed = keys_module.seal(secret, live_key(...))`.
  8. `store.write_sealed_result(conn, row["id"], row["claimed_at"], sealed, token_id)`.
  - **If any revocation in steps 2-3 fails for a reason other than "already gone":** do not
    proceed to step 4 onward. Insert the un-revoked token ID into `arb_messages_deadletter`
    (`ON CONFLICT (provider_token_id) DO NOTHING`, reason naming this failure) for each
    irrecoverable orphan, then `store.mark_failed(conn, row["id"], row["claimed_at"], reason)`
    naming every irrecoverable orphan token ID.
  - <!-- Plan-review r2 (codex P2): round-1's wording conflated two genuinely different cases
    under one bullet — an ordinary create() failure (no token exists at all) and a step-6 fence
    failure (a token DOES exist, the executor is just fenced out of recording it). Separated
    explicitly below; an implementation that treats them the same risks referencing an unbound
    `token_id` in the create-failure case. -->
  - **If step 5 (the actual CF create call) itself raises, with no token returned at all:**
    there is nothing to revoke or record — `store.mark_failed(conn, row["id"], row["claimed_at"],
    reason)` naming the create error, and stop. Do not attempt to reference a `token_id` that was
    never assigned.
  - **If step 5 succeeds but the write in step 6 is fenced out (returns `False`):** this is the
    round-4 P0's scenario — the executor already has the just-minted `(token_id, secret)` in
    hand. **Immediately `revoke_idempotent(cf_client, token_id)`** using the ID already held
    (never look it up by name — a lookup could find a different worker's token); if that revoke
    itself fails, insert into `arb_messages_deadletter`; if even that insert fails, emit a
    `CRITICAL` log line naming the orphaned `token_id` as the last-resort discoverability path
    (spec's acknowledged irreducible residual). Do not raise past this point — the claim/mint
    loop (Task 5) continues to the next row.
  - <!-- Plan-review r1 (pi-GLM P1): the original plan was silent on step 8's fence-failure case
    specifically, distinct from step 6's. This distinction matters -- applying step 6's
    "revoke the token" logic to a step-8 fence failure would be WRONG, because at step 8 the
    token is already correctly recorded (step 6 already succeeded and its fence passed). -->
    **If step 8's write (`write_sealed_result`) is fenced out (returns `False`), but step 6
    already succeeded:** the token IS already recorded on the row via `provider_token_id` — do
    **not** revoke it. Simply stop; a later reclaim's found-branch (§ Security invariant 8) will
    see `provider_token_id` already set and body still null, and correctly run revoke-then-remint
    against it. Revoking here would be both unnecessary and wrong (it would create exactly the
    "found token with recorded provider_token_id but the ID itself is already gone" confusion the
    found-branch isn't designed to handle).

- [ ] **Step 1 — failing tests** (a `FakeCFClient` that tracks live/revoked token sets, and can
  be seeded with pre-existing tokens to simulate orphans — this is the load-bearing test
  infrastructure for this whole file):
```python
# tests/arb_messages/test_mint_cloudflare.py
import pytest, psycopg, os, hashlib
from nacl.public import PrivateKey
from arb_messages.run import setup_schema
from arb_messages.store import enqueue, claim, write_provider_token_id
from arb_messages.keys import register_key, live_key as get_live_key, unseal
from arb_messages.mint_cloudflare import deterministic_name, mint, revoke_idempotent

DSN = os.environ["ARB_MESSAGES_TEST_DSN"]

class FakeCFClient:
    """Models CF's real one-time-secret behavior: create() is the ONLY call that
    ever returns the plaintext secret; list_by_name() returns metadata only.

    Plan-review r1 (codex P1, cold-Opus P1 -- both independently found the same weakness:
    the original test seeded the row's own recorded token INSIDE list_by_name's results, so a
    buggy implementation that skips the required direct-by-ID revoke of the row's own
    provider_token_id and relies ENTIRELY on list_by_name's results would still pass -- the
    lookup would happen to find and revoke it anyway. `self.call_log` (ordered, records both
    which method and which token_id/name) lets a test prove the by-ID revoke of the row's own
    recorded token happens BEFORE and INDEPENDENTLY of whatever list_by_name returns, not merely
    that both end up revoked by the end. `exclude_from_lookup` lets a test simulate the row's
    own token existing on CF but NOT being returned by a name lookup for any reason (e.g. a
    provider-side propagation delay) -- proving the by-ID path doesn't depend on lookup
    completeness at all."""
    def __init__(self):
        self._live = {}       # token_id -> {"name":..., "secret":...}
        self._exclude_from_lookup = set()
        self.create_calls = []
        self.revoke_calls = []
        self.call_log = []    # ordered list of ("revoke"|"list_by_name"|"create", token_id_or_name)

    def seed_live(self, token_id, name, exclude_from_lookup=False):
        self._live[token_id] = {"name": name, "secret": None}  # secret unknown, like a real orphan
        if exclude_from_lookup:
            self._exclude_from_lookup.add(token_id)

    def create(self, *, name, zone, capability):
        token_id = f"tok-{len(self.create_calls)}"
        secret = f"secret-for-{token_id}"
        self._live[token_id] = {"name": name, "secret": secret}
        self.create_calls.append((name, zone, capability))
        self.call_log.append(("create", token_id))
        return token_id, secret

    def list_by_name(self, name):
        self.call_log.append(("list_by_name", name))
        return [tid for tid, v in self._live.items()
                if v["name"] == name and tid not in self._exclude_from_lookup]

    def revoke(self, token_id):
        self.revoke_calls.append(token_id)
        self.call_log.append(("revoke", token_id))
        if token_id not in self._live:
            raise LookupError("not_found")  # the 404-shaped case
        del self._live[token_id]

@pytest.fixture
def conn():
    c = psycopg.connect(DSN, autocommit=True)
    setup_schema(c)
    with c.cursor() as cur:
        cur.execute("TRUNCATE arb_messages, arb_agent_keys, arb_messages_deadletter RESTART IDENTITY")
    yield c
    c.close()

@pytest.fixture
def agent_key(conn):
    sk = PrivateKey.generate()
    register_key(conn, "agent-a", bytes(sk.public_key))
    return sk

def test_not_found_mints_fresh_and_records_before_sealing(conn, agent_key):
    enqueue(conn, "agent-a", "r1", "mint", "cloudflare", "zone_dns_edit", "z1", "approved", None)
    row = claim(conn, lease_seconds=300)
    cf = FakeCFClient()
    mint(conn, cf, __import__("arb_messages.keys", fromlist=["x"]), row)
    with conn.cursor() as cur:
        cur.execute("SELECT status, provider_token_id, body FROM arb_messages WHERE id = %s", (row["id"],))
        status, pid, body = cur.fetchone()
    assert status == "done" and pid is not None and body is not None
    assert unseal(bytes(body), bytes(agent_key)) == f"secret-for-{pid}".encode()

def test_provider_token_id_recorded_before_sealing_fault_injection(conn, agent_key):
    # Plan-review r2 (codex P1): the previous test only checked the FINAL state, so an
    # implementation that skipped invariant 7's separate immediate write and only set
    # provider_token_id inside write_sealed_result at the very end would still pass it. This is
    # the exact crash window invariant 7 exists to close (spec § Security invariant 7): CF
    # create() succeeds, then something between there and the final DB write fails, and without
    # the separate immediate write, the token would be live with no durable ID anywhere. Fault
    # injection: make sealing itself raise, and assert provider_token_id already landed anyway.
    enqueue(conn, "agent-a", "r1", "mint", "cloudflare", "zone_dns_edit", "z1", "approved", None)
    row = claim(conn, lease_seconds=300)
    cf = FakeCFClient()
    class ExplodingKeysModule:
        live_key = staticmethod(get_live_key)
        @staticmethod
        def seal(secret, pubkey):
            raise RuntimeError("simulated crash during sealing")
    with pytest.raises(RuntimeError):
        mint(conn, cf, ExplodingKeysModule, row)
    with conn.cursor() as cur:
        cur.execute("SELECT status, provider_token_id, body FROM arb_messages WHERE id = %s", (row["id"],))
        status, pid, body = cur.fetchone()
    assert pid is not None  # invariant 7's immediate write landed despite the later crash
    assert status != "done" and body is None  # sealing never completed -- correctly incomplete

def test_create_failure_with_no_token_marks_failed_cleanly(conn, agent_key):
    # Plan-review r2 (codex P2): distinct from a step-6 fence failure (where a token WAS
    # returned) -- an ordinary create() exception returns no token_id at all. The plan's
    # error-handling prose previously conflated these two cases; this test guards the one where
    # there is genuinely no token to revoke or record.
    enqueue(conn, "agent-a", "r1", "mint", "cloudflare", "zone_dns_edit", "z1", "approved", None)
    row = claim(conn, lease_seconds=300)
    cf = FakeCFClient()
    def broken_create(**kw):
        raise RuntimeError("CF create API down")
    cf.create = broken_create
    mint(conn, cf, __import__("arb_messages.keys", fromlist=["x"]), row)  # must not raise past mint()
    with conn.cursor() as cur:
        cur.execute("SELECT status, provider_token_id FROM arb_messages WHERE id = %s", (row["id"],))
        status, pid = cur.fetchone()
    assert status == "failed" and pid is None  # no token was ever created; nothing to revoke/record

def test_structural_sealing_no_plaintext_reaches_row(conn, agent_key):
    enqueue(conn, "agent-a", "r1", "mint", "cloudflare", "zone_dns_edit", "z1", "approved", None)
    row = claim(conn, lease_seconds=300)
    cf = FakeCFClient()
    mint(conn, cf, __import__("arb_messages.keys", fromlist=["x"]), row)
    with conn.cursor() as cur:
        cur.execute("SELECT provider_token_id, body FROM arb_messages WHERE id = %s", (row["id"],))
        pid, body = cur.fetchone()
        secret = f"secret-for-{pid}"
        cur.execute("SELECT * FROM arb_messages WHERE id = %s", (row["id"],))
        cols = cur.fetchone()
    assert bytes(body) != secret.encode()  # sealed, not raw (Plan-review r2 pi-GLM P2: simplified from a convoluted `or`)
    assert str(secret) not in str(cols)  # nowhere in the row in plaintext

def test_found_branch_revokes_own_recorded_token_first_then_name_lookup(conn, agent_key):
    row_id = enqueue(conn, "agent-a", "r1", "mint", "cloudflare", "zone_dns_edit", "z1", "approved", None)
    row = claim(conn, lease_seconds=300)
    name = deterministic_name("agent-a", "r1")
    cf = FakeCFClient()
    # Plan-review r1 (codex P1, cold-Opus P1): tok-B is EXCLUDED from list_by_name's results --
    # this is the load-bearing change. If the implementation revoked only what list_by_name
    # returns (skipping the required direct-by-ID revoke of the row's own recorded token), tok-B
    # would NEVER be revoked and this test would catch it. Under the original (buggy) test, tok-B
    # was visible to list_by_name too, so a lookup-only implementation would accidentally pass.
    cf.seed_live("tok-B", name, exclude_from_lookup=True)  # row's own recorded token (crash after invariant-7 write)
    cf.seed_live("tok-A-orphan", name)  # a second live token: deadletter'd orphan under same name, IS found by lookup
    write_provider_token_id(conn, row["id"], row["claimed_at"], "tok-B")
    mint(conn, cf, __import__("arb_messages.keys", fromlist=["x"]), row)
    # both pre-existing tokens must be revoked; a THIRD fresh token is minted
    assert "tok-B" in cf.revoke_calls
    assert "tok-A-orphan" in cf.revoke_calls
    # Ordering proof: the row's own recorded token (tok-B) must be revoked BEFORE the
    # list_by_name call runs at all -- not merely "eventually revoked by the end".
    revoke_b_index = cf.call_log.index(("revoke", "tok-B"))
    lookup_index = next(i for i, (op, _) in enumerate(cf.call_log) if op == "list_by_name")
    assert revoke_b_index < lookup_index
    with conn.cursor() as cur:
        cur.execute("SELECT status, provider_token_id FROM arb_messages WHERE id = %s", (row["id"],))
        status, final_pid = cur.fetchone()
    assert status == "done"
    assert final_pid not in ("tok-B", "tok-A-orphan")  # a genuinely fresh token
    live_ids = set(cf._live.keys())
    assert live_ids == {final_pid}  # exactly one live token at the end -- both prior tokens
                                     # gone, only the freshly-minted one remains

def test_two_worker_version_from_round3_still_passes(conn, agent_key):
    # simpler case: exactly one pre-existing orphan, provider_token_id NOT already set
    enqueue(conn, "agent-a", "r1", "mint", "cloudflare", "zone_dns_edit", "z1", "approved", None)
    row = claim(conn, lease_seconds=300)
    name = deterministic_name("agent-a", "r1")
    cf = FakeCFClient()
    cf.seed_live("tok-orphan", name)
    mint(conn, cf, __import__("arb_messages.keys", fromlist=["x"]), row)
    assert "tok-orphan" in cf.revoke_calls
    live_ids = set(cf._live.keys())
    assert len(live_ids) == 1  # exactly one live token, the fresh one

def test_lookup_returns_multiple_orphans_all_get_revoked(conn, agent_key):
    # Plan-review r2 (agy-print P1): the r1 found-branch test only ever seeded a single
    # lookup-visible orphan, so an implementation that revoked only lookup_results[0] instead of
    # looping over all of them would have still passed. This test seeds THREE lookup-visible
    # live tokens under the same deterministic name -- CF's own duplicate-name permission makes
    # this a real case, not a hypothetical -- and asserts every one is revoked.
    enqueue(conn, "agent-a", "r1", "mint", "cloudflare", "zone_dns_edit", "z1", "approved", None)
    row = claim(conn, lease_seconds=300)
    name = deterministic_name("agent-a", "r1")
    cf = FakeCFClient()
    cf.seed_live("tok-orphan-1", name)
    cf.seed_live("tok-orphan-2", name)
    cf.seed_live("tok-orphan-3", name)
    mint(conn, cf, __import__("arb_messages.keys", fromlist=["x"]), row)
    assert {"tok-orphan-1", "tok-orphan-2", "tok-orphan-3"} <= set(cf.revoke_calls)
    live_ids = set(cf._live.keys())
    assert len(live_ids) == 1  # all three orphans gone; exactly the fresh token remains

# Plan-review r1 (codex P1, cold-Opus P1, agy-print P1 -- 3-way convergence): this exact
# scenario (the round-3 P0's direct regression guard) was entirely absent from the original
# plan -- only the round-4 "found branch on a pre-existing orphan" tests existed, which don't
# exercise write_provider_token_id itself getting fenced out AFTER a successful create() call.
def test_fenced_out_worker_revokes_its_own_just_created_token_by_id(conn, agent_key):
    enqueue(conn, "agent-a", "r1", "mint", "cloudflare", "zone_dns_edit", "z1", "approved", None)
    row_a = claim(conn, lease_seconds=0)  # worker A's view of the row (zero-lease so it's immediately "expired")
    import time; time.sleep(0.05)
    row_b = claim(conn, lease_seconds=0)  # worker B reclaims first -- A's claimed_at is now stale
    assert row_b["id"] == row_a["id"]
    cf = FakeCFClient()
    # Worker A now runs mint() against its STALE row dict -- create() succeeds (A doesn't know
    # yet that it lost the row), but the subsequent write_provider_token_id will be fenced out.
    mint(conn, cf, __import__("arb_messages.keys", fromlist=["x"]), row_a)
    # A's create() call happened -- assert exactly one token was created by A's attempt
    assert len(cf.create_calls) == 1
    created_token_id = list(cf._live.keys())[0] if cf._live else cf.revoke_calls[-1]
    # A must have revoked the token it JUST created, by ID directly -- not via a name lookup
    # (a lookup could find a different worker's token instead, per the round-4 fix's own
    # reasoning). Assert no list_by_name call preceded this revoke.
    assert created_token_id in cf.revoke_calls
    revoke_index = cf.call_log.index(("revoke", created_token_id))
    create_index = cf.call_log.index(("create", created_token_id))
    assert create_index < revoke_index
    no_lookup_between = not any(op == "list_by_name" for op, _ in cf.call_log[create_index:revoke_index])
    assert no_lookup_between
    # The row itself must be untouched by A's attempt (B's claim/write path owns it now)
    with conn.cursor() as cur:
        cur.execute("SELECT provider_token_id FROM arb_messages WHERE id = %s", (row_a["id"],))
        assert cur.fetchone()[0] is None  # A's fenced write never landed

def test_fenced_out_worker_deadletters_when_its_own_revoke_fails(conn, agent_key):
    enqueue(conn, "agent-a", "r1", "mint", "cloudflare", "zone_dns_edit", "z1", "approved", None)
    row_a = claim(conn, lease_seconds=0)
    import time; time.sleep(0.05)
    claim(conn, lease_seconds=0)  # worker B reclaims, fencing A out
    cf = FakeCFClient()
    def broken_revoke(token_id):
        raise RuntimeError("CF API down")
    cf.revoke = broken_revoke
    mint(conn, cf, __import__("arb_messages.keys", fromlist=["x"]), row_a)
    created_token_id = list(cf._live.keys())[0]
    with conn.cursor() as cur:
        cur.execute("SELECT reason FROM arb_messages_deadletter WHERE provider_token_id = %s", (created_token_id,))
        assert cur.fetchone() is not None  # A's un-revokable orphan is deadlettered, not dropped

def test_revoke_failure_deadletters_and_fails_closed(conn, agent_key):
    enqueue(conn, "agent-a", "r1", "mint", "cloudflare", "zone_dns_edit", "z1", "approved", None)
    row = claim(conn, lease_seconds=300)
    name = deterministic_name("agent-a", "r1")
    cf = FakeCFClient()
    cf.seed_live("tok-stuck", name)
    def broken_revoke(token_id):
        raise RuntimeError("CF API down")
    cf.revoke = broken_revoke
    mint(conn, cf, __import__("arb_messages.keys", fromlist=["x"]), row)
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM arb_messages WHERE id = %s", (row["id"],))
        assert cur.fetchone()[0] == "failed"
        cur.execute("SELECT reason FROM arb_messages_deadletter WHERE provider_token_id = 'tok-stuck'")
        assert cur.fetchone() is not None

def test_revoke_idempotent_treats_already_gone_as_success():
    cf = FakeCFClient()
    revoke_idempotent(cf, "already-gone-token")  # not in cf._live -> LookupError from fake -> must not raise

def test_deterministic_name_length_and_stability():
    n1 = deterministic_name("agent-a", "r1")
    n2 = deterministic_name("agent-a", "r1")
    assert n1 == n2
    assert len(n1) < 120  # CF's token-name limit
    n3 = deterministic_name("agent-a:", "r1")  # would collide under naive concatenation
    n4 = deterministic_name("agent-a", ":r1")
    assert n3 != n4
```
- [ ] **Step 2** — run, confirm fail. **Step 3** — implement. **Step 4** — PASS.
- [ ] **Step 5** — commit `feat(arb-messages): Cloudflare mint handler (revoke-then-remint, structural sealing)`.

---

### Task 5: `executor.py` — standalone claim loop, independent policy re-validation, sweep

**Files:** Create `src/arb_messages/executor.py`; Test `tests/arb_messages/test_executor.py`.

**This process has NO MCP/OAuth context** — it's a bare script against a Postgres connection and
a `Settings` object, loaded from env exactly like the door but never touching
`mcp.server.auth.middleware.auth_context`. Interface:

- <!-- Plan-review r2 (agy-print P1): round-1's `run_claim_loop` was a bare `while True:` with no
  way to test a single iteration — passing `sleep=lambda s: None` (a no-op) into a `while True:`
  loop, as the round-1 tests did, produces a genuinely infinite loop with a busy-wait once the
  queue is empty, hanging the test runner. Split into the actual testable unit
  (`claim_and_process_one`, returns a bool so a test can call it exactly once) and a trivial loop
  wrapper around it (`run_claim_loop`, which is NOT itself unit-tested — only
  `claim_and_process_one` is). Every Task 5 test below calls `claim_and_process_one` directly. -->
  `claim_and_process_one(settings, cf_client=None, keys_module=None) -> bool` — a single
  claim-and-handle cycle, returns `True` if a row was claimed and processed (denied, failed, or
  minted), `False` if the queue was empty (nothing to claim) **or the plane is paused** (checked
  first, against the `arb_messages_settings` pause flag — a paused plane's `claim_and_process_one`
  is a no-op returning `False`, it does not claim). If not paused and a row was claimed:
  1. **Independently re-check the allowlist** against `settings.allowed_agents`/
     `settings.allowed_zones` — never trust `row["policy_decision"]`. Deny (mark_denied) if not
     allowed.
  2. **Re-check key liveness** — `keys.live_key(conn, row["agent_id"])` must be non-None (TOCTOU
     guard vs. a key revoked between enqueue and claim). Deny if absent.
  3. **Re-check the kill switch** against this process's own `settings.messages_enabled` (not
     just the pause flag, which is the runtime one — this is defense in depth per spec § d
     Containment executor-side step 4).
  4. **Check `row["attempts"] > settings.max_retries`** — if exceeded, `mark_failed` with
     "max retries exceeded", do NOT attempt to mint.
  5. Call `mint_cloudflare.mint(conn, cf_client, keys_module, row)` (Task 4).
- `run_claim_loop(settings, cf_client=None, keys_module=None, sleep=time.sleep, stop_after=None)
  -> None` — `while stop_after is None or stop_after > 0: processed =
  claim_and_process_one(settings, cf_client, keys_module); if stop_after is not None: stop_after
  -= 1; sleep(0 if processed else settings.poll_interval_seconds)` (see `Settings.
  poll_interval_seconds`, Task 1 — round-3 fix, this was referenced but undefined in earlier
  drafts). `stop_after` exists purely so a smoke test can run the real wrapper loop for a
  bounded number of iterations without hanging (see the smoke test in Step 1 below) — the
  substantive tests all call `claim_and_process_one` directly, never `run_claim_loop`.
- `run_sweep(settings, cf_client=None) -> None` — the periodic sweep, **run in a separate loop
  from `run_claim_loop`, independent of both kill switches** (pause flag and
  `settings.messages_enabled`) — a paused plane must still revoke. Three categories, each using
  `SELECT ... FOR UPDATE SKIP LOCKED`:
  1. `arb_messages` rows `status IN ('failed', 'claimed') AND provider_token_id IS NOT NULL AND
     token_revoked_at IS NULL AND (status != 'claimed' OR claimed_at < now() - lease_seconds *
     interval '1 second')` — revoke, set `token_revoked_at`.
  2. <!-- Plan-review r1 (agy-print P1, cold-Opus P1, both independently confirmed by direct
     re-verification against this exact SQL): the original condition was
     `(delivered_at IS NULL OR delivered_at < now() - grace) AND (delivered_at IS NULL OR now() -
     delivered_at > grace)` — when `delivered_at IS NULL`, BOTH OR-clauses evaluate to
     unconditional TRUE regardless of elapsed time, so a row that became `done` a millisecond ago
     (before the agent has any chance to poll) would already match and get swept/revoked
     immediately. This is exactly backwards from the spec's "still null **past a grace window**"
     intent (spec § Security invariant 8's sweep). Fixed using the new `completed_at` column
     (set in `write_sealed_result`, see § schema above) to measure elapsed time for the
     null-`delivered_at` case specifically, since `delivered_at` itself can't measure "how long
     ago did this row complete" when it's null. --> `arb_messages` rows `status = 'done' AND
     provider_token_id IS NOT NULL AND token_revoked_at IS NULL AND ((delivered_at IS NULL AND
     completed_at < now() - delivered_grace_seconds * interval '1 second') OR (delivered_at IS
     NOT NULL AND delivered_at < now() - delivered_grace_seconds * interval '1 second'))` —
     revoke, set `token_revoked_at`.
  3. `arb_messages_deadletter` rows `token_revoked_at IS NULL` — revoke (via
     `mint_cloudflare.revoke_idempotent`), increment `attempts`, set `last_attempt_at`; on
     success also set `token_revoked_at`. Never delete the row.

- [ ] **Step 1 — failing tests** (this file's tests are the direct regression guards for four of
  the five review rounds' P0s — write them deliberately against each):
```python
# tests/arb_messages/test_executor.py
import pytest, psycopg, os, time
from arb_messages.run import setup_schema
from arb_messages.config import Settings
from arb_messages.store import enqueue, claim
from arb_messages.executor import claim_and_process_one, run_claim_loop, run_sweep

DSN = os.environ["ARB_MESSAGES_TEST_DSN"]

@pytest.fixture
def conn():
    c = psycopg.connect(DSN, autocommit=True)
    setup_schema(c)
    with c.cursor() as cur:
        cur.execute("TRUNCATE arb_messages, arb_agent_keys, arb_messages_settings, arb_messages_deadletter RESTART IDENTITY")
        cur.execute("INSERT INTO arb_messages_settings (key, value) VALUES ('paused', '0')")
    yield c
    c.close()

def settings(**overrides):
    base = dict(postgres_dsn=DSN, cf_minting_token="tok", allowed_zones=frozenset({"z1"}),
                allowed_agents=frozenset({"agent-a"}), lease_seconds=300)
    base.update(overrides)
    return Settings(**base)

def test_executor_independently_denies_row_outside_its_own_allowlist(conn):
    # row's stored policy_decision says approved, but executor's OWN settings disagree
    enqueue(conn, "agent-evil", "r1", "mint", "cloudflare", "zone_dns_edit", "z1", "approved", None)
    calls = []
    class RefusingCFClient:
        def create(self, **kw): calls.append(kw); raise AssertionError("CF should never be called")
    claim_and_process_one(settings(), cf_client=RefusingCFClient())
    assert not calls
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM arb_messages WHERE agent_id = 'agent-evil'")
        assert cur.fetchone()[0] == "denied"

def test_executor_denies_when_key_revoked_before_claim(conn):
    from nacl.public import PrivateKey
    from arb_messages.keys import register_key
    sk = PrivateKey.generate()
    register_key(conn, "agent-a", bytes(sk.public_key))
    enqueue(conn, "agent-a", "r1", "mint", "cloudflare", "zone_dns_edit", "z1", "approved", None)
    with conn.cursor() as cur:
        cur.execute("UPDATE arb_agent_keys SET revoked_at = now() WHERE agent_id = 'agent-a'")
    claim_and_process_one(settings())
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM arb_messages WHERE agent_id = 'agent-a'")
        assert cur.fetchone()[0] == "denied"

def test_executor_never_calls_get_access_token():
    import arb_messages.executor as ex
    import inspect
    src = inspect.getsource(ex)
    assert "get_access_token" not in src
    assert "auth_context" not in src

def test_max_retries_exceeded_fails_without_minting(conn):
    from nacl.public import PrivateKey
    from arb_messages.keys import register_key
    sk = PrivateKey.generate()
    register_key(conn, "agent-a", bytes(sk.public_key))
    enqueue(conn, "agent-a", "r1", "mint", "cloudflare", "zone_dns_edit", "z1", "approved", None)
    with conn.cursor() as cur:
        cur.execute("UPDATE arb_messages SET attempts = 5 WHERE agent_id = 'agent-a'")
    calls = []
    class RefusingCFClient:
        def create(self, **kw): calls.append(kw); raise AssertionError("should not mint")
    claim_and_process_one(settings(max_retries=3), cf_client=RefusingCFClient())
    assert not calls
    with conn.cursor() as cur:
        cur.execute("SELECT status, reason FROM arb_messages WHERE agent_id = 'agent-a'")
        status, reason = cur.fetchone()
    assert status == "failed" and "retries" in reason.lower()

def test_pause_flag_stops_claims_but_sweep_still_runs(conn):
    with conn.cursor() as cur:
        cur.execute("UPDATE arb_messages_settings SET value = '1' WHERE key = 'paused'")
        cur.execute("INSERT INTO arb_messages (agent_id, request_id, request_type, provider, capability, zone, status, provider_token_id) VALUES ('a','r1','mint','cloudflare','zone_dns_edit','z1','failed','tok-x')")
    revoked = []
    class RevokeTrackingCF:
        def revoke(self, token_id): revoked.append(token_id)
    run_sweep(settings(), cf_client=RevokeTrackingCF())
    assert "tok-x" in revoked  # sweep ran despite pause

# Plan-review r3 (codex P1, cold-Opus P1, pi-GLM F1 -- convergent, though the test above only
# ever proved the SWEEP half of "pause stops X but not the sweep"; the "stops claims" half had
# ZERO coverage. An implementation that ignored the pause flag entirely in
# claim_and_process_one, but kept the sweep correctly unconditional, would have passed the whole
# suite. This is the missing direct guard.
def test_pause_flag_stops_claim_and_process_one(conn):
    from nacl.public import PrivateKey
    from arb_messages.keys import register_key
    sk = PrivateKey.generate()
    register_key(conn, "agent-a", bytes(sk.public_key))
    enqueue(conn, "agent-a", "r1", "mint", "cloudflare", "zone_dns_edit", "z1", "approved", None)
    with conn.cursor() as cur:
        cur.execute("UPDATE arb_messages_settings SET value = '1' WHERE key = 'paused'")
    calls = []
    class RefusingCFClient:
        def create(self, **kw): calls.append(kw); raise AssertionError("should not mint while paused")
    processed = claim_and_process_one(settings(), cf_client=RefusingCFClient())
    assert processed is False
    assert not calls
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM arb_messages WHERE agent_id = 'agent-a'")
        assert cur.fetchone()[0] == "pending"  # never claimed at all

def test_env_kill_switch_stops_minting_distinct_from_pause_flag(conn):
    # Plan-review r3 (codex P1): the executor's OWN messages_enabled re-check (§ Containment
    # executor-side step 4, defense in depth against the pause flag alone) had no direct test
    # either -- an implementation that checked only the pause flag and ignored
    # settings.messages_enabled would still have passed every existing test.
    from nacl.public import PrivateKey
    from arb_messages.keys import register_key
    sk = PrivateKey.generate()
    register_key(conn, "agent-a", bytes(sk.public_key))
    enqueue(conn, "agent-a", "r1", "mint", "cloudflare", "zone_dns_edit", "z1", "approved", None)
    calls = []
    class RefusingCFClient:
        def create(self, **kw): calls.append(kw); raise AssertionError("should not mint when disabled")
    claim_and_process_one(settings(messages_enabled=False), cf_client=RefusingCFClient())
    assert not calls
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM arb_messages WHERE agent_id = 'agent-a'")
        assert cur.fetchone()[0] == "denied"  # claimed (pause flag doesn't block this path),
                                                # but then correctly denied by the kill-switch re-check
    # Plan-review r4 (cold-Opus P2, non-blocking design note, not a defect): this is
    # deliberately asymmetric with the pause-flag path (which leaves the row `pending`, freely
    # reclaimable) -- messages_enabled=False terminally denies instead. Acceptable: the
    # kill-switch is meant for "this capability is permanently/administratively off," not a
    # transient pause, so a terminal `denied` is the right semantics; an agent that needs to
    # retry after the switch is re-enabled does so with a new `request_id`, same as any other
    # `denied` row.

# Plan-review r3 (cold-Opus P2): run_claim_loop's `stop_after` wrapper existed to make bounded
# smoke-testing possible, but nothing actually exercised it -- add one minimal smoke test.
def test_run_claim_loop_stop_after_bounds_iterations(conn):
    enqueue(conn, "a", "r1", "mint", "cloudflare", "zone_dns_edit", "z1", "approved", None)
    call_count = [0]
    def counting_sleep(_):
        call_count[0] += 1
    run_claim_loop(settings(), sleep=counting_sleep, stop_after=3)
    assert call_count[0] == 3  # ran exactly the bounded number of iterations, then returned

# Plan-review r1 (agy-print P1, cold-Opus P1 -- 3-way convergence including my own direct
# SQL re-verification): these two tests are the direct regression guards for the sweep
# category-2 grace-window bug -- a row that JUST completed (delivered_at still null) must NOT
# be swept immediately; only once completed_at is past the grace window.
def test_sweep_category2_does_not_revoke_freshly_completed_undelivered_row(conn):
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO arb_messages (agent_id, request_id, request_type, provider,
            capability, zone, status, provider_token_id, completed_at)
            VALUES ('a','r1','mint','cloudflare','zone_dns_edit','z1','done','tok-fresh', now())""")
    revoked = []
    class RevokeTrackingCF:
        def revoke(self, token_id): revoked.append(token_id)
    run_sweep(settings(delivered_grace_seconds=3600), cf_client=RevokeTrackingCF())
    assert "tok-fresh" not in revoked  # completed a moment ago -- well within the grace window

def test_sweep_category2_revokes_undelivered_row_past_grace_window(conn):
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO arb_messages (agent_id, request_id, request_type, provider,
            capability, zone, status, provider_token_id, completed_at)
            VALUES ('a','r1','mint','cloudflare','zone_dns_edit','z1','done','tok-stale',
            now() - interval '2 hours')""")
    revoked = []
    class RevokeTrackingCF:
        def revoke(self, token_id): revoked.append(token_id)
    run_sweep(settings(delivered_grace_seconds=3600), cf_client=RevokeTrackingCF())
    assert "tok-stale" in revoked  # completed 2h ago, grace is 1h -- past the window

def test_sweep_category2_revokes_delivered_row_past_grace_window(conn):
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO arb_messages (agent_id, request_id, request_type, provider,
            capability, zone, status, provider_token_id, delivered_at)
            VALUES ('a','r1','mint','cloudflare','zone_dns_edit','z1','done','tok-delivered',
            now() - interval '2 hours')""")
    revoked = []
    class RevokeTrackingCF:
        def revoke(self, token_id): revoked.append(token_id)
    run_sweep(settings(delivered_grace_seconds=3600), cf_client=RevokeTrackingCF())
    assert "tok-delivered" in revoked  # delivered 2h ago -- consumed and destroyed long since

def test_sweep_deadletter_category_revokes_and_marks(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO arb_messages_deadletter (provider_token_id, reason) VALUES ('tok-orphan', 'test')")
    revoked = []
    class RevokeTrackingCF:
        def revoke(self, token_id): revoked.append(token_id)
    run_sweep(settings(), cf_client=RevokeTrackingCF())
    assert "tok-orphan" in revoked
    with conn.cursor() as cur:
        cur.execute("SELECT token_revoked_at, attempts FROM arb_messages_deadletter WHERE provider_token_id = 'tok-orphan'")
        revoked_at, attempts = cur.fetchone()
    assert revoked_at is not None and attempts == 1

def test_sweep_deadletter_permanently_failing_entry_tracks_attempts_without_giving_up(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO arb_messages_deadletter (provider_token_id, reason) VALUES ('tok-stuck', 'test')")
    class AlwaysFailsCF:
        def revoke(self, token_id): raise RuntimeError("permanent failure")
    for _ in range(3):
        run_sweep(settings(), cf_client=AlwaysFailsCF())
    with conn.cursor() as cur:
        cur.execute("SELECT attempts, token_revoked_at FROM arb_messages_deadletter WHERE provider_token_id = 'tok-stuck'")
        attempts, revoked_at = cur.fetchone()
    assert attempts == 3 and revoked_at is None  # tracked, never deleted, never silently given up

# Plan-review r1 (agy-print P1, codex P2 -- both independently found the original version
# opened a second connection but never actually used it to hold a lock, so it would pass even
# with SKIP LOCKED entirely omitted from the sweep query). Rewritten to genuinely hold a row
# lock on one connection while the sweep runs on another, matching Task 2's
# test_claim_two_concurrent_never_claim_same_row's real-locking pattern.
def test_sweep_skips_a_row_locked_by_another_connection(conn):
    # Plan-review r2 (pi-GLM P2): removed a confusing no-op -- `conn` fixture uses
    # autocommit=True, so the preceding INSERT is already committed; no explicit commit needed.
    with conn.cursor() as cur:
        cur.execute("INSERT INTO arb_messages_deadletter (provider_token_id, reason) VALUES ('tok-x', 'test')")
    conn2 = psycopg.connect(DSN, autocommit=False)
    with conn2.cursor() as cur2:
        cur2.execute("SELECT * FROM arb_messages_deadletter WHERE provider_token_id = 'tok-x' FOR UPDATE")
        # conn2 now holds the row lock, uncommitted -- a concurrent sweep on `conn` must SKIP it
        revoked = []
        class RevokeTrackingCF:
            def revoke(self, token_id): revoked.append(token_id)
        run_sweep(settings(), cf_client=RevokeTrackingCF())
        assert "tok-x" not in revoked  # SKIP LOCKED means conn's sweep didn't wait or double-process
    conn2.rollback()
    conn2.close()
    # Now that the lock is released, a normal sweep pass DOES process it.
    revoked2 = []
    class RevokeTrackingCF2:
        def revoke(self, token_id): revoked2.append(token_id)
    run_sweep(settings(), cf_client=RevokeTrackingCF2())
    assert "tok-x" in revoked2

def test_sweep_revoke_of_already_gone_token_treated_as_success(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO arb_messages_deadletter (provider_token_id, reason) VALUES ('tok-gone', 'test')")
    class AlreadyGoneCF:
        def revoke(self, token_id): raise LookupError("not_found")  # 404-shaped
    run_sweep(settings(), cf_client=AlreadyGoneCF())
    with conn.cursor() as cur:
        cur.execute("SELECT token_revoked_at FROM arb_messages_deadletter WHERE provider_token_id = 'tok-gone'")
        assert cur.fetchone()[0] is not None  # treated as success, not left unrevoked
```
- [ ] **Step 2** — run, confirm fail. **Step 3** — implement. **Step 4** — PASS.
- [ ] **Step 5** — commit `feat(arb-messages): standalone executor (independent re-validation, sweep)`.

---

### Task 6: Door tools (`mcp/door_tools.py`)

**Files:** Create `src/arb_messages/mcp/__init__.py`, `src/arb_messages/mcp/door_tools.py`; Test
`tests/arb_messages/test_door_tools.py`.

**Interface — `MessagesTools(store_conn_factory, settings, *, require_scope=None, actor=None,
audit_sink=None)`:**

- `_actor()` — pulls `client_id` off the OAuth access token, matching `arb_email/mcp/door_tools.py
  :38-42`'s shape, but **must NOT inherit its `or "mcp"` fallback** — raise `PermissionError` on
  an absent/`None` `client_id` (spec § Containment door-side step 2's fail-closed divergence).
  <!-- Plan-review r3 (agy-print P2): `_actor()` should lower-case the returned `client_id`
  before it's used as `agent_id` anywhere. `Settings.allowed_agents` (Task 1) is lower-cased at
  config-load time; if a real OAuth provider ever returns a mixed-case `client_id`, an
  otherwise-legitimate agent would be spuriously denied by the allowlist comparison. Cheap,
  cheap to get right the first time. -->
  Return the lower-cased value.
- `messages_request(request_id: str, capability: str, zone: str) -> dict` <!-- Plan-review r2
  (agy-print P1): the original signature omitted `request_id` entirely, meaning the door would
  have had to generate one server-side. That breaks the spec's entire client-retry-idempotency
  design (§ Transport / § Containment's dedup-key discussion): `request_id` is explicitly a
  CLIENT-supplied value so a caller whose first attempt's response was lost (network drop,
  timeout) can retry with the identical `request_id` and land on the SAME row via the
  `UNIQUE (agent_id, request_id)` dedup key, rather than silently enqueueing a second, duplicate
  request. Fixed: `request_id` is now a required caller-supplied parameter — still never
  `agent_id`, which remains derived from `_actor()` exclusively. --> — no `agent_id` parameter
  (never accept one, anywhere): scope check → `agent_id = self._actor()` → allowlist pre-check
  (zone AND agent, both against `settings`) → key pre-check → `store.enqueue(conn, agent_id,
  request_id, ...)` (idempotent — a retry with the same `request_id` returns the existing row,
  per `enqueue`'s `ON CONFLICT DO NOTHING` behavior, Task 2) → bounded inline-wait (poll
  `store.read_and_mark_delivered` every ~1s for up to ~15s) → return sealed result inline if
  ready, else `{"status": "pending", "request_id": request_id}` (echoing back the caller's own
  ID, not a server-generated one).
- `messages_register_key(pubkey: str) -> dict` — no `agent_id` parameter: scope check →
  `agent_id = self._actor()` → `keys.register_key(conn, agent_id, base64_decode(pubkey))`.
- `messages_poll(request_id: str) -> dict` — no `agent_id` parameter: scope check →
  `agent_id = self._actor()` → `store.read_and_mark_delivered(conn, agent_id, request_id)`.
- Every pre-check failure calls `self._deny(reason)` which audits (best-effort, wrapped
  `try/except`) then raises; never enqueues on a pre-check failure.

- [ ] **Step 1 — failing tests:**
```python
# tests/arb_messages/test_door_tools.py
import pytest
from arb_messages.config import Settings
from arb_messages.mcp.door_tools import MessagesTools

def settings(**overrides):
    base = dict(postgres_dsn="unused-in-these-tests", cf_minting_token="tok",
                allowed_zones=frozenset({"z1"}), allowed_agents=frozenset({"agent-a"}))
    base.update(overrides)
    return Settings(**base)

class FakeConnFactory:
    def __init__(self): self.enqueued = []
    def __call__(self): return self
    def __enter__(self): return self
    def __exit__(self, *a): pass

def test_scope_denial_emits_audit_and_never_enqueues():
    # Plan-review r2 (agy-print P1): request_id is now the first positional parameter --
    # all messages_request call sites in these tests updated accordingly.
    audits = []
    tools = MessagesTools(FakeConnFactory(), settings(),
        require_scope=lambda s: (_ for _ in ()).throw(PermissionError("no scope")),
        actor=lambda: "agent-a", audit_sink=audits.append)
    with pytest.raises(PermissionError):
        import asyncio; asyncio.run(tools.messages_request("r1", "zone_dns_edit", "z1"))
    assert any(a.get("event") == "denied" for a in audits)

def test_missing_client_id_fails_closed_not_mcp_fallback():
    tools = MessagesTools(FakeConnFactory(), settings(), require_scope=lambda s: None,
        actor=lambda: None)  # simulates absent client_id
    with pytest.raises(PermissionError):
        import asyncio; asyncio.run(tools.messages_register_key("some-pubkey-b64"))

def test_agent_id_always_from_actor_never_a_parameter():
    import inspect
    sig = inspect.signature(MessagesTools.messages_request)
    assert "agent_id" not in sig.parameters
    sig2 = inspect.signature(MessagesTools.messages_register_key)
    assert "agent_id" not in sig2.parameters
    sig3 = inspect.signature(MessagesTools.messages_poll)
    assert "agent_id" not in sig3.parameters

# Plan-review r4 (codex P2): the door's actor lower-casing (§ Interface, _actor()) was specified
# but never directly tested -- an implementation could still pass every other test while missing
# it, and Settings.allowed_agents is lower-cased at config load, so a mixed-case real OAuth
# client_id would otherwise be spuriously denied.
def test_actor_mixed_case_client_id_is_normalized_to_lowercase():
    tools = MessagesTools(FakeConnFactory(), settings(allowed_agents=frozenset({"agent-a"})),
        require_scope=lambda s: None, actor=lambda: "Agent-A")
    assert tools._actor() == "agent-a"

def test_zone_not_in_allowlist_denied():
    audits = []
    tools = MessagesTools(FakeConnFactory(), settings(), require_scope=lambda s: None,
        actor=lambda: "agent-a", audit_sink=audits.append)
    import asyncio
    with pytest.raises(ValueError):
        asyncio.run(tools.messages_request("r1", "zone_dns_edit", "not-an-allowed-zone"))
    assert any(a.get("event") == "denied" for a in audits)

def test_agent_not_in_allowlist_denied():
    tools = MessagesTools(FakeConnFactory(), settings(), require_scope=lambda s: None,
        actor=lambda: "agent-not-allowed")
    import asyncio
    with pytest.raises(ValueError):
        asyncio.run(tools.messages_request("r1", "zone_dns_edit", "z1"))

def test_request_id_is_a_caller_supplied_parameter():
    # Plan-review r2 (agy-print P1): request_id must be a real, caller-supplied parameter, not
    # server-generated -- the direct regression guard for the missing-parameter bug. (The actual
    # retry-lands-on-the-same-row dedup behavior is covered by Task 2's
    # test_enqueue_dedupes_same_agent_request_id, exercised against the real store; this test
    # guards the door-tool signature specifically.)
    import inspect
    sig = inspect.signature(MessagesTools.messages_request)
    assert "request_id" in sig.parameters
```
- [ ] **Step 2** — run, fail. **Step 3** — implement (integration-tests against a real
  throwaway-Postgres-backed `store.py` belong in a follow-up integration test file or Task 7's
  door-wiring tests — the unit tests above use fakes for the pre-check logic specifically).
  **Step 4** — PASS.
- [ ] **Step 5** — commit `feat(arb-messages): door-side MessagesTools (pre-checks, enqueue, poll)`.

---

### Task 7: Door wiring + `server.py` scope registration

**Files:** Create `src/arb_messages/mcp/door_wire.py`; Modify `src/arb_memory/mcp/server.py`;
Test `tests/arb_messages/test_door_wiring.py`.

- **`server.py`:** add `"messages.request"` to **both** `valid_scopes` AND `default_scopes`
  (`arb_memory/mcp/server.py:323-330`) — NOT files/email's valid-scopes-only pattern; this plane
  needs it in `default_scopes` too so ChatGPT's connector picks it up on a fresh DCR registration
  (matching the `chatgpt-connector-scope-grant` lesson already in this codebase's history — any
  scope ChatGPT needs must be in both lists, not just `valid_scopes`). Call
  `register_messages_tools(server, os.environ)` after the email registration.
- **`door_wire.register_messages_tools(server, env, *, client_factory=None) -> bool`:**
  mirrors `register_email_tool`'s exact shape (`arb_email/mcp/door_wire.py:9-36`) — check
  required env + kill-switch first, return `False` fast; wrap setup in `try/except Exception:
  log.exception(...); return False`; build settings → `MessagesTools`; register **all three**
  tools (`messages_request`, `messages_register_key`, `messages_poll`) via `server.add_tool(...)`.

- [ ] **Step 1 — failing tests:**
<!-- Plan-review r1 (pi-GLM P1, verified by direct re-read of src/arb_memory/mcp/server.py):
the original plan's FakeServer + bare server_mod.valid_scopes access do not match how this
codebase actually exposes scopes -- valid_scopes/default_scopes are local list literals passed
into ClientRegistrationOptions(...) INSIDE build_server(), never module-level attributes. The
real, established pattern is tests/arb_email/test_door_wiring.py's FastMCP-based tests. Rewrite
this whole file against that real pattern, not a hand-rolled fake, matching the sibling planes'
own test convention exactly. -->
```python
# tests/arb_messages/test_door_wiring.py
import anyio
from mcp.server.fastmcp import FastMCP
from arb_messages.mcp.door_wire import register_messages_tools

ENV = {"ARB_MESSAGES_POSTGRES_DSN": "postgresql://x", "ARB_MESSAGES_CF_MINTING_TOKEN": "tok",
       "ARB_MESSAGES_ALLOWED_ZONES": "z1", "ARB_MESSAGES_ALLOWED_AGENTS": "agent-a"}

def _tool_names(server):
    return {tool.name for tool in anyio.run(server.list_tools)}

def test_noop_without_required_env():
    server = FastMCP("test")
    assert register_messages_tools(server, {}) is False
    assert not _tool_names(server)

def test_registers_all_three_tools_on_valid_config():
    server = FastMCP("test")
    assert register_messages_tools(server, ENV) is True
    assert _tool_names(server) == {"messages_request", "messages_register_key", "messages_poll"}

def test_kill_switch_off_prevents_registration():
    server = FastMCP("test")
    assert register_messages_tools(server, {**ENV, "ARB_MESSAGES_ENABLED": "0"}) is False
    assert not _tool_names(server)

def test_fail_soft_on_construction_error():
    server = FastMCP("test")
    def boom(): raise RuntimeError("backend down")
    assert register_messages_tools(server, ENV, client_factory=boom) is False
    assert not _tool_names(server)

def test_scope_in_both_valid_and_default_scopes():
    # Matches tests/arb_email/test_door_wiring.py:58-77's exact real pattern -- scopes live on
    # the constructed server's settings, not a module attribute.
    from arb_memory.mcp.config import Settings
    from arb_memory.mcp.oauth import ArbMemoryOAuthProvider
    from arb_memory.mcp.server import build_server

    settings = Settings(public_base_url="https://mem.example.com", mcp_dsn="postgresql://example",
                         login_secret="passphrase", totp_secret="totp")
    provider = ArbMemoryOAuthProvider(settings=settings, conn_factory=lambda: None)
    server = build_server(settings=settings, provider=provider, conn_factory=lambda: None,
                           embed=lambda _t: [])
    scopes = server.settings.auth.client_registration_options
    assert "messages.request" in scopes.valid_scopes
    assert "messages.request" in scopes.default_scopes  # in default_scopes too, matching the
    # chatgpt-connector-scope-grant lesson: ChatGPT only requests its DCR-registered/default set
```
- [ ] **Step 2** — run, fail. **Step 3** — implement. **Step 4** — PASS.
- [ ] **Step 5** — commit `feat(arb-messages): door wiring + messages.request scope (valid + default)`.

---

### Task 8: Postgres role restriction (deployment verification, not code)

**This is a checklist item, not a code task** — per spec § Live code location, Postgres write
access to `arb_messages`/`arb_agent_keys`/`arb_messages_settings`/`arb_messages_deadletter` must
be restricted to the door and executor processes' own DB roles in the real deployment, so a box
holding a general-purpose Postgres DSN can't write a self-approved-looking row directly. Document
this requirement in a short `docs/superpowers/plans/2026-07-02-arb-messages-deployment-checklist.md`
(or fold into this plan's own completion notes) rather than silently skipping it — it's a
deployment-time action, not something the test suite can verify.

- [ ] Write the deployment checklist noting: (a) the role-restriction requirement above, (b) the
  live-verification items from Task 9, (c) that this pass builds Cloudflare `mint` only —
  `proxy`/DigitalOcean are explicitly out of scope (spec § Scope).

---

### Task 9: Live verification (env-guarded, may be blocked by operator permissions)

**Files:** Create `tests/arb_messages/e2e_live_cloudflare.py` (NOT collected by default — guarded
by `ARB_MESSAGES_E2E=1`, matching `arb_email`'s `e2e_send.py` convention).

**Explicitly flagged by the spec as needing operator action** (a real
`ARB_MESSAGES_CF_MINTING_TOKEN` that may not exist yet) — attempt this, but if genuinely blocked,
report it as a named residual, not a silent skip.

Verify, against the real Cloudflare API:
1. The minted token is genuinely scoped to the requested zone only.
2. Sub-day TTL is honored (mint with a 5-minute `expires_on`, confirm it's not silently
   day-granular).
3. The token can be revoked via `provider_token_id`.
4. The restricted minting token carries **list/read** permission (list-by-name works).
5. A token's name is settable at create and queryable by that exact name afterward.
6. Whether the token-management permission scopes to tokens the holder created, or to the whole
   account (§ Cloudflare capability facts' fourth item) — if account-wide, note this explicitly
   and flag the dedicated-CF-account mitigation as a follow-up task, don't silently proceed as if
   it were narrowly scoped.

- [ ] Attempt Task 9; document the outcome (pass / blocked-with-reason) in the deployment
  checklist from Task 8.

---

## Self-review (spec requirements → tasks)

- Door/executor split (spec's central round-1 fix) → Task 6 (door) + Task 5 (executor) are
  genuinely separate modules with no shared MCP-context assumption; Task 5's tests explicitly
  assert no `get_access_token`/`auth_context` reference.
- `agent_id` bound to authenticated actor everywhere (round-1 P0-2) → Task 6's tests assert no
  tool signature accepts an `agent_id` parameter.
- Mint idempotency / revoke-then-remint (rounds 2-5's repeated fix target) → Task 4, implemented
  exactly per the spec's final converged mechanism, with regression tests for the specific
  three-worker scenario cold-Opus traced in round 4.
- Fencing guard + stale-worker-revokes-its-own-token (round 3/4 P0s) → Task 2's `fenced_write` +
  Task 4's fail-closed/deadletter path.
- Deadletter mechanization (round 4 P1) → Task 5's `run_sweep` category 3.
- `read_and_mark_delivered` premature-delivery bug (round 4/5 P1) → Task 2, tested explicitly for
  pending/claimed/failed/denied no-ops.
- Retry-tracking gaps (round 5 P1s) → Task 1's `attempts` columns + Task 5's max-retries
  enforcement and deadletter backoff-observability.
- `messages_poll` ownership/DoS fix (round 2 P1) → Task 2's `read_and_mark_delivered` scoping +
  Task 6/7's explicit three-tool registration.
- PyNaCl sealing, structural (not conventional) secret-handling → Task 3 + Task 2's
  `write_sealed_result` signature test + Task 4's structural-sealing test.
- Pause-flag `ON CONFLICT DO NOTHING` seed → Task 1's schema + explicit regression test.
- Scope wiring in both `valid_scopes` and `default_scopes` → Task 7.
- Postgres role restriction + live-verification residuals → Tasks 8/9, explicitly not silently
  dropped.
