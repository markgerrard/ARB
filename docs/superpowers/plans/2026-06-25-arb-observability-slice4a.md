# ARB Visibility — Slice 4a Implementation Plan (SSE gateway backend)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A standalone OAuth-gated SSE service that lets a warm orchestrator watch its bound seats execute live — a global `events:live` feed grouped per-orchestrator, plus per-seat timelines with Postgres backfill.

**Architecture:** The bridge tees every task event (and panel votes) to one `events:live` stream on the bridge bus (db-12). A new `visibility` Starlette service (`run_visibility`) async-`XREAD`s that stream, reduces it to seat presence grouped by orchestrator (`to`), and pushes SSE. Per-seat detail backfills from `eval_event_raw` (+ `audit_events` votes) then tails the live stream. Auth = a thin bearer-token middleware over `oauth_store.get_access_token` (the MCP door's token table) + a shared `public_base_url` audience check. Read-only everywhere.

**Tech Stack:** Python 3.12, Starlette + uvicorn, `redis.asyncio` (async XREAD), sync `redis`/psycopg for the bridge tee + backfill, pytest + httpx (SSE client tests). Spec: `docs/superpowers/specs/2026-06-25-arb-observability-slice4-design.md` (v3).

## Global Constraints
- **`events:live`** = `{prefix}events:live` on the **bridge bus** (`self.redis`, db-12) — the producer tee needs NO new client (another `self.redis.xadd`). **The prefix is load-bearing (plan-panel P1):** the producer uses `self.redis_config.prefix` (e.g. `agent_scratch:`); the GATEWAY can't derive the prefix from a Redis URL, so it MUST be configured separately via `ARB_BRIDGE_BUS_PREFIX` and compute the same `f"{prefix}events:live"` key. A non-empty-prefix test is required end-to-end. Bounded `maxlen` + `ttl` like the task-events stream. Entry fields: `{run_id, task_id, seat_id (=from), orchestrator (=to), event_type, sent_at, data}` (string values for XADD).
- **The bridge tee is fail-soft** (try/except, never crash the turn) — same posture as `_tee_eval_event`. The `events:live` tee on `push_task_event` fires for ALL task events; the vote tee in `_emit_vote` fires for panel dispatches only.
- **SSE handlers MUST use `redis.asyncio`** (async `xread`) — never a sync blocking XREAD in an async handler (blocks the uvicorn loop). A test drives TWO concurrent SSE clients to prove non-blocking.
- **Auth:** every endpoint requires `Authorization: Bearer <token>`; validate via `oauth_store.get_access_token(conn, token)` (enforces `revoked_at IS NULL AND expires_at > now()`) AND `canonical_resource(row["resource"]) == canonical_resource(public_base_url)`. **The audience var is `ARB_MEMORY_MCP_PUBLIC_BASE_URL`** (plan-panel P1 — what door tokens actually bind to, `config.py:68-71`/`oauth.py:267-270`), NOT a new `ARB_MEMORY_PUBLIC_BASE_URL`; normalize via `canonical_resource` (handles trailing slash). 401 otherwise. Do NOT instantiate `ArbMemoryOAuthProvider` (needs login/TOTP secrets). Read-only on the token table.
- **Orchestrator = envelope `to` = the dispatcher's `FROM_AGENT_ID`** (e.g. `claude-bridge-dev`). Actor on votes = `"seat:"+seat_id`.

### Test harness
```bash
cd /Users/<user>/<workspace>
export PYTHONPATH="$(pwd):$(pwd)/src"
set -a; . envs/arb-memory-dev.env; set +a
export ARB_MEMORY_REDIS_URL=redis://127.0.0.1:6379/15
PYTEST=/Users/<user>/<workspace>/.venv/bin/pytest
```

---

### Task 1: Bridge `events:live` tee (lifecycle + votes)

**Files:** Modify `src/agent_redis_bridge/bridge.py` (`push_task_event` ~1602; `_emit_vote` ~1646). Test: `tests/test_events_live_tee.py` (create).

**Interfaces:** Produces `{prefix}events:live` stream entries. Adds `Bridge._tee_live_event(self, *, run_id, task_id, seat_id, orchestrator, event_type, sent_at, data)` (fail-soft).

- [ ] **Step 1: Failing test** — drive `push_task_event` + `_emit_vote` with a RecordingRedis and assert an `events:live` xadd with the right fields:
```python
import json
from types import SimpleNamespace
from agent_redis_bridge.bridge import Bridge
from agent_redis_bridge.envelope import Envelope
from agent_redis_bridge.redis_io import RedisConfig

class Rec:
    def __init__(self): self.xadds=[]
    def xadd(self, key, fields, **kw): self.xadds.append((key, fields)); return "1-0"

def _b():
    b = Bridge.__new__(Bridge)
    b.redis = Rec(); b.redis_config = RedisConfig("h","6379","12","agent_scratch:")
    b.args = SimpleNamespace(max_task_events=500, events_ttl=60)
    b.agent_id = "codex-bridge-dev"; b.audit_redis=None; b._audit_prefix=""
    return b

def _req(): return Envelope(id="t1", sender="claude-bridge-dev", branch="b", recipient="codex-bridge-dev",
                            kind="request", sent_at="x", payload={}, run_id="run-1")

def test_push_task_event_tees_events_live():
    b=_b(); b.push_task_event(_req(), "task_started", {"task_id":"t1"})
    live=[(k,f) for (k,f) in b.redis.xadds if k.endswith("events:live")]
    assert len(live)==1
    f=live[0][1]
    assert f["task_id"]=="t1" and f["orchestrator"]=="claude-bridge-dev" and f["seat_id"]=="codex-bridge-dev"
    assert f["event_type"]=="task_started" and f["run_id"]=="run-1"
```

- [ ] **Step 2: Run → FAIL** (`$PYTEST tests/test_events_live_tee.py -v`) — no `events:live` xadd.

- [ ] **Step 3: Implement.** Add the helper + call it from `push_task_event` (after the existing `self.redis.xadd(key, fields, ...)` and `self._tee_eval_event(...)`):
```python
    def _tee_live_event(self, *, run_id, task_id, seat_id, orchestrator, event_type, sent_at, data):
        if not run_id:
            return  # mistake-prevention: untracked events are not visibility-scoped
        try:
            self.redis.xadd(
                f"{self.redis_config.prefix}events:live",
                {"run_id": run_id, "task_id": task_id, "seat_id": seat_id or "",
                 "orchestrator": orchestrator or "", "event_type": event_type, "sent_at": sent_at,
                 "data": json.dumps(data, separators=(",", ":"))},
                maxlen=self.args.max_task_events, ttl=self.args.events_ttl)
        except Exception:
            logger.exception("events:live tee failed for task %s event %s", task_id, event_type)
```
In `push_task_event`, after `self._tee_eval_event(request, event, sent_at, data)`:
```python
        self._tee_live_event(run_id=getattr(request, "run_id", None), task_id=request.id,
                             seat_id=self.agent_id, orchestrator=request.sender,
                             event_type=event, sent_at=sent_at, data=data)
```
In `_emit_vote`, after a successful `AuditRun(...).emit(...)` (inside the try, panel-only path), tee the vote:
```python
            self._tee_live_event(run_id=envelope.run_id, task_id=envelope.id, seat_id=self.agent_id,
                                 orchestrator=getattr(envelope, "sender", "") , event_type="vote",
                                 sent_at=iso_now(), data={"stance": stance.get("stance")})
```
(`request.sender` / `envelope.sender` is the orchestrator; `prefix` via `self.redis_config.prefix` — confirm the attr name on RedisConfig.)

- [ ] **Step 4: Run → PASS.** Add a `_emit_vote` tee test mirroring Task-3's `test_bridge_emit_vote` setup — but **also set `b.redis = Rec()` and `b.redis_config` (plan-panel P2):** votes emit to `self.audit_redis` while the live tee writes to `self.redis`; if `b.redis` is unset, `_tee_live_event` AttributeErrors into the fail-soft `except` and the vote-tee assertion passes vacuously. Assert the `events:live` vote entry has `event_type=="vote"` + the stance. Run `$PYTEST tests/test_events_live_tee.py tests/test_bridge_emit_vote.py tests/test_push_task_event_tee.py -v`.

- [ ] **Step 5: Commit** `feat(visibility): bridge tees task events + panel votes to events:live stream`.

---

### Task 2: `eval_event_raw.orchestrator` column (5-touch, persisted)

**Files:** Modify `src/agent_redis_bridge/bridge.py` (`build_eval_record` ~93, `_tee_eval_event` ~1626), `src/arb_memory/eval.py` (`PostgresEvalSink.write`, `EvalConsumer._parse_event`), `src/arb_memory/schema.sql`. Test: `tests/arb_memory/test_eval_consumer.py`.

**Interfaces:** `build_eval_record(..., orchestrator=None)` adds `"orchestrator"` to the record; consumer persists it; `eval_event_raw` gains nullable `orchestrator`.

- [ ] **Step 1: Failing test** (extend the Slice-1 round-trip): xadd an eval event with `orchestrator="claude-bridge-dev"`, drain, assert the stored `orchestrator` column == that value (non-default sentinel, per the Slice-1 vacuous-green lesson).
- [ ] **Step 2: Run → FAIL** (column/field absent).
- [ ] **Step 3: Implement the 5 touches:**
  - `build_eval_record(*, run_id, task_id, seat_id, event, sent_at, data, orchestrator=None)` → add `"orchestrator": orchestrator or ""` to the returned dict.
  - `_tee_eval_event`: pass `orchestrator=getattr(request, "sender", None)`.
  - `PostgresEvalSink.write`: add `orchestrator` to the INSERT column list + `event.get("orchestrator")` value.
  - `EvalConsumer._parse_event`: add `"orchestrator": fields.get("orchestrator")` to the returned dict.
  - `schema.sql`: add `orchestrator text` to the `eval_event_raw` CREATE + `ALTER TABLE eval_event_raw ADD COLUMN IF NOT EXISTS orchestrator text;` (idempotent, Slice-1/3 pattern).
- [ ] **Step 4: Apply schema (`psycopg`, psql may be absent) + run → PASS.**
- [ ] **Step 5: Commit** `feat(eval): persist orchestrator (dispatcher) on eval_event_raw for per-orchestrator backfill`.

---

### Task 3: Visibility service skeleton + OAuth middleware + `/orchestrators`

**Files:** Create `src/arb_memory/visibility.py` (`build_visibility_app`), modify `src/arb_memory/run.py` (`run_visibility` + `"visibility"` choice). Test: `tests/arb_memory/test_visibility_auth.py`.

**Interfaces:** `build_visibility_app(*, bus_redis_url, bus_prefix, dsn, public_base_url) -> Starlette` (bus_prefix = `ARB_BRIDGE_BUS_PREFIX`, e.g. `agent_scratch:`; the stream key is `f"{bus_prefix}events:live"`). Auth middleware: `_principal(conn, token, public_base_url) -> dict | None`. `GET /orchestrators` → `{"orchestrators": [...]}`. `run_visibility()` reads `ARB_BRIDGE_BUS_URL`, `ARB_BRIDGE_BUS_PREFIX`, `ARB_MEMORY_DSN`, `ARB_MEMORY_MCP_PUBLIC_BASE_URL`.

- [ ] **Step 1: Failing tests** — 401 without/with-bad token; 200 + JSON with a valid token. Seed a token row via `oauth_store` (or a fixture) whose `resource == public_base_url`, `revoked_at IS NULL`, `expires_at > now()`.
```python
def test_orchestrators_401_without_token(client): assert client.get("/orchestrators").status_code == 401
def test_orchestrators_200_with_valid_token(client, valid_token):
    r = client.get("/orchestrators", headers={"Authorization": f"Bearer {valid_token}"})
    assert r.status_code == 200 and "orchestrators" in r.json()
```
(Use Starlette `TestClient`. The fixture seeds a token in the mcp_auth token table via `oauth_store`'s insert helper; mirror `tests/arb_memory/test_*oauth*` setup.)
- [ ] **Step 2: Run → FAIL** (module/app absent).
- [ ] **Step 3: Implement** `visibility.py` mirroring `writer.py`'s Starlette pattern:
  - `_principal(conn, token, public_base_url)`: `row = oauth_store.get_access_token(conn, token)`; return None if row is None or `canonical_resource(row["resource"]) != public_base_url`; else the row.
  - A middleware/decorator wrapping each route: extract bearer, open a psycopg conn (or pooled), call `_principal`, 401 if None.
  - `GET /orchestrators`: async-XREAD a recent window of `events:live` (or read presence keys) and return distinct `orchestrator` values with ≥1 live seat. (Minimal first: distinct orchestrators seen in the live stream's last N entries.)
  - `run_visibility()` in run.py: `uvicorn.run(build_visibility_app(bus_redis_url=os.environ["ARB_BRIDGE_BUS_URL"], bus_prefix=os.environ.get("ARB_BRIDGE_BUS_PREFIX",""), dsn=os.environ["ARB_MEMORY_DSN"], public_base_url=os.environ["ARB_MEMORY_MCP_PUBLIC_BASE_URL"]), host=..., port=int(os.environ.get("ARB_VISIBILITY_PORT","8810")))`; add `"visibility"` to argparse choices + dispatch.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** `feat(visibility): OAuth-gated Starlette service skeleton + /orchestrators`.

---

### Task 4: `GET /sse/orchestrator/{id}` — async live roster

**Files:** Modify `src/arb_memory/visibility.py` (SSE route + seat reducer). Test: `tests/arb_memory/test_visibility_sse.py`.

**Interfaces:** `_reduce_seat(state: dict, entry: dict) -> dict` (event_type → presence/state). SSE `text/event-stream` emitting `seat_appear`/`seat_update`/`seat_finish`.

- [ ] **Step 1: Failing tests** — (a) pure `_reduce_seat` unit: `task_started`→running, `task_finished`→done, `vote`→voted; (b) SSE integration: push two `events:live` entries for one orchestrator (two seats) via a real `redis.asyncio` client on a throwaway db, assert the stream yields `seat_appear` for each + a `seat_finish`; (c) **two concurrent SSE clients** both receive events (proves non-blocking).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — an async generator handler: `aioredis = redis.asyncio.from_url(bus_url)`; backfill recent entries then `await aioredis.xread({f"{bus_prefix}events:live": last_id}, block=ms)` loop; filter `orchestrator==id`; reduce; `yield` SSE `id:`/`event:`/`data:` frames; heartbeat comment every ~15s; resume from `Last-Event-ID` header. Use `EventSourceResponse` (sse-starlette) OR a raw `StreamingResponse` with a manual `text/event-stream` generator (prefer no new dep — raw generator).
  **Stale-seat age-out (plan-panel P2):** a crashed seat emits `task_started` but never `task_finished`, so it would show "running" forever. The reducer marks a seat `stale` (or drops it from the roster) when `now - last_event_ts > STALE_GRACE_S` (e.g. 120s); emit a `seat_finish`/`seat_stale` when that crosses. (The `events:live` stream's own TTL also ages the data out.) Add a unit test: a seat with an old `last_event_ts` reduces to stale/dropped.
- [ ] **Step 4: Run → PASS** (incl. the 2-client concurrency test).
- [ ] **Step 5: Commit** `feat(visibility): SSE /sse/orchestrator/{id} live roster (async XREAD + seat reducer)`.

---

### Task 5: `GET /sse/seat/{task_id}` — backfill join + live tail

**Files:** Modify `src/arb_memory/visibility.py` (backfill query + SSE). Test: `tests/arb_memory/test_visibility_seat.py`.

**Interfaces:** `_backfill_seat(conn, task_id) -> list[event]` joining eval + audit.

- [ ] **Step 1: Failing tests** — seed `eval_event_raw` rows for a `task_id` (gives run_id+seat_id) + an `audit_events` vote row for `(run_id, actor="seat:"+seat_id)`; assert `_backfill_seat` returns the eval events AND the vote, ordered by ts. Then SSE: after backfill, a new `events:live` entry for that task_id is pushed live.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** `_backfill_seat(conn, task_id)`:
  - `SELECT run_id, seat_id, event_type, sent_at, payload FROM eval_event_raw WHERE task_id=%s ORDER BY sent_at` → derive run_id, seat_id.
  - `SELECT seq, kind, ts, payload FROM audit_events WHERE run_id=%s AND payload->>'actor' = %s` with `'seat:'+seat_id` → the votes.
  - merge ordered by ts. Then the SSE route streams backfill, then async-XREAD `events:live` filtered to `task_id`.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** `feat(visibility): SSE /sse/seat/{task_id} backfill (eval+audit join) + live tail`.

---

### Task 6: Compose service + live E2E (curl the SSE)

**Files:** Modify `deploy/docker-compose.yml` (add `visibility` service, MERGE/append — protected file). Create `tests/e2e_visibility_roundtrip.py` (standalone, live infra). Modify `tests/arb_memory/test_compose_shape.py` (add `visibility` to the expected service set).

- [ ] **Step 1:** Add the `visibility` compose service (mirror `writer`): image, `command: ["visibility"]`, `restart: unless-stopped`, env `ARB_BRIDGE_BUS_URL`, `ARB_BRIDGE_BUS_PREFIX`, `ARB_MEMORY_DSN`, `ARB_MEMORY_MCP_PUBLIC_BASE_URL` (the audience var). Update `test_compose_shape` expected set + the no-ports/restart invariants (visibility publishes no host port; cloudflared is the ingress). `docker compose config --services` lists it. NOTE: `ARB_BRIDGE_BUS_URL` (db-12, the bridge bus) is a NEW cross-plane secret the operator supplies — distinct from `ARB_MEMORY_REDIS_URL`.
- [ ] **Step 2: E2E** (`tests/e2e_visibility_roundtrip.py`, mirror `e2e_audit_roundtrip.py`): start `build_visibility_app` via `httpx.ASGITransport` (no real port); seed a valid token; push two `events:live` entries for one orchestrator (real redis.asyncio, throwaway db) + an eval/audit backfill row; assert `/sse/orchestrator/<id>` streams the seats and `/sse/seat/<task_id>` streams backfill+live; assert 401 without the token. Cleanup own keys/rows. Run 3× isolated.
- [ ] **Step 3:** Run the E2E 3× + `docker compose config`. Expected PASS + 0 residue.
- [ ] **Step 4: Commit** `feat(visibility): prod compose service + live E2E (SSE roster + seat timeline)`.

---

## Self-Review
**Spec coverage:** global `events:live` tee → Task 1 (lifecycle + vote tees); orchestrator column (5-touch) → Task 2; OAuth-gated service + /orchestrators → Task 3; async roster + 2-client test → Task 4; backfill join (eval→audit by run_id+actor) → Task 5; compose + E2E → Task 6. All design v3 decisions covered.

**Placeholders:** the OAuth token-seeding fixture (Task 3) + the `oauth_store` insert helper aren't shown verbatim — the implementer must read `src/arb_memory/mcp/oauth_store.py` + the existing oauth tests to mirror the real token-row insert (named explicitly). The SSE transport (raw generator vs sse-starlette) is a bounded choice flagged in Task 4.

**Type consistency:** `_tee_live_event` kwargs match both call sites; `events:live` field set matches between producer (Task 1) and consumer/reducer (Task 4); `build_eval_record(orchestrator=)` matches `_tee_eval_event` (Task 2); `_principal`/`get_access_token` signatures match `oauth_store`; `_backfill_seat` join keys match `audit_events.payload.actor == "seat:"+seat_id` (Slice-3 contract).

**Coherence:** `self.redis_config.prefix` attr name must be confirmed (Task 1 — the producer prefix). `redis.asyncio` is in redis-py (already a dep). The gateway's `ARB_BRIDGE_BUS_URL` (db-12) is a NEW config the operator supplies (cross-plane, per the design's config surface). Live view works on fresh `events:live`; historical per-orchestrator scoping is partial until eval rows carry `orchestrator` (Task 2).
