# Reliable-Inbox Owner-Fenced Acknowledgement (M2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the bridge's reliable-inbox processing-acknowledgement **owner-fenced** so a stale-but-live predecessor daemon cannot delete a successor's re-parked envelope — closing the pre-existing defect where a body-keyed `LREM :processing 1 <raw-body>` acknowledges *any* matching body regardless of who owns it.

**Architecture:** Reuse the codebase's existing atomic compare-token-then-act Lua pattern (already used for the identity lease in `RedisCli.register`/`cleanup`, `redis_io.py:139-225`) for the processing list. Tag each parked entry with the claiming daemon's per-boot `owner_token`; make removal a Lua compare-token-then-`LREM` that no-ops unless this daemon still holds the claim. A successor's re-park overwrites the claim, so the predecessor's stale `remove_processing` becomes a safe no-op.

**Tech Stack:** Python 3.14, `redis` (sync client, Lua `EVAL`), pytest (`uv run --extra arb-memory pytest`), `src/agent_redis_bridge/redis_io.py` + `bridge.py`.

**Context / why this is its own work item (Mark, 2026-07-14):** M2 is a bridge-*infra* correctness fix — a lost reliable-inbox request harms EVERY dispatch, not just the ARB Observability span layer. It is the cited **precondition** that Slice 5a-0's `attempt_epoch` guarantee ("survives the restart it marks") depends on: 5a-0's live gate **asserts** this fix has landed (fix-commit ancestry + a runtime probe), it does not implement it. Splitting it here keeps the capture panel from reviewing inbox-ownership concurrency, and lets this fix ship to every consumer without waiting on a capture plan. Cited defect from `docs/superpowers/specs/2026-07-13-arb-observability-slice5a-0-capture-normalization-SPEC.md` § "Precondition P-recovery (M2)".

## Global Constraints

- **Protected file (`bridge.py`, `redis_io.py`):** these steer the whole dispatch fleet. Edits are **append/merge** to the reliable-inbox path — do NOT restructure surrounding code. Run `git diff -- <file>` before committing and confirm: deleted existing content (no), moved (no), added (yes).
- **The fix must be atomic (Lua `EVAL`), never a GET-then-DEL/LREM sequence** — the same reason `cleanup` (`redis_io.py:198-201`) is Lua: a check-then-act window lets a concurrent re-claim slip between the two calls.
- **Fail-safe on claim absence:** if the claim key is missing/expired, `remove_processing` MUST be a **no-op** (leave the entry parked for recovery), NEVER a fallback to the old unconditional `LREM` — an unconditional fallback reintroduces the exact defect.
- **Back-compat:** the reliable-inbox path is gated by `self.reliable_inbox`; the non-reliable (plain `BLPOP`) path (`redis_io.py:293-298`) has no processing list and is untouched.
- **Test invocation:** `uv run --extra arb-memory pytest tests/...`.
- **CHANGELOG discipline:** ship with a `CHANGELOG.md` entry (what + why) ([[changelog-discipline]]).
- **Deploy gated:** fleet redeploy is PAUSED for Mark's deploy-review gate.

---

## File Structure

- **`src/agent_redis_bridge/redis_io.py`** *(modify)* — add `RedisConfig.processing_claim_key`; add `RedisCli.claim_processing`; change `RedisCli.remove_processing` to owner-fenced Lua compare-token-then-`LREM`.
- **`src/agent_redis_bridge/bridge.py`** *(modify)* — claim the body with `self.owner_token` right after it is parked (`pop_inbox`, `bridge.py:842-846`); thread `owner_token=self.owner_token` into both `remove_processing` call sites (`inbox_loop` finally `bridge.py:828-830`; `process_request` finally `bridge.py:1443-1447`).
- **Tests:** `tests/test_reliable_inbox_owner_fence.py` *(create)* — the M2 scenario + fail-safe cases; touch existing reliable-inbox tests if any assert `remove_processing`'s old 2-arg signature.

---

## Task 1: owner-fenced `remove_processing` + `claim_processing` (redis_io.py)

**Files:**
- Modify: `src/agent_redis_bridge/redis_io.py`
- Test: `tests/test_reliable_inbox_owner_fence.py`

**Interfaces:**
- Produces:
  - `RedisConfig.processing_claim_key(agent_id: str, body: str) -> str` — `agent:{agent_id}:processing_claim:{sha256(body)[:32]}`.
  - `RedisCli.claim_processing(agent_id: str, body: str, owner_token: str, ttl: int) -> None` — `SET claim_key = owner_token EX ttl`. A successor's re-park overwrites the predecessor's claim.
  - `RedisCli.remove_processing(agent_id: str, body: str, owner_token: str) -> int` — **owner-fenced** Lua: `LREM :processing 1 body` + `DEL claim_key` **only if** `GET claim_key == owner_token`; else no-op returning 0. **Signature changes** from `(agent_id, body)` to `(agent_id, body, owner_token)` — update all callers (Task 2).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_reliable_inbox_owner_fence.py
# Uses fakeredis (pip: fakeredis) if the suite already provides it, else a small Lua-capable fake.
# If the repo lacks a Lua-capable fake, use fakeredis.FakeStrictRedis (supports EVAL). Mirror the
# RedisConfig/RedisCli construction the existing reliable-inbox tests use.
import hashlib
from agent_redis_bridge.redis_io import RedisCli, RedisConfig


def _cli():
    import fakeredis
    cfg = RedisConfig(prefix="agent_scratch:", host="localhost", port=6379, db=0, user="", password="", tls=False)
    cli = RedisCli.__new__(RedisCli)   # bypass real-redis __init__
    cli.config = cfg
    cli.client = fakeredis.FakeStrictRedis(decode_responses=True)
    return cli


AGENT = "codex-bridge-dev-example"
BODY = '{"id":"task-R","kind":"request"}'


def test_stale_predecessor_remove_does_not_delete_successors_reparked_entry():
    cli = _cli()
    # A parks + claims R.
    cli.client.rpush(cli.config.processing_key(AGENT), BODY)
    cli.claim_processing(AGENT, BODY, "owner-A", ttl=3600)
    # B recovers (R leaves processing) then re-parks + re-claims R (claim now B).
    cli.client.lrem(cli.config.processing_key(AGENT), 1, BODY)          # recover_processing_to_inbox effect
    cli.client.rpush(cli.config.processing_key(AGENT), BODY)            # B re-parks
    cli.claim_processing(AGENT, BODY, "owner-B", ttl=3600)             # B re-claims (overwrites A)
    # A's STALE finally fires: must be a NO-OP.
    removed = cli.remove_processing(AGENT, BODY, "owner-A")
    assert removed == 0
    assert cli.client.lrange(cli.config.processing_key(AGENT), 0, -1) == [BODY]   # B's entry survives
    assert cli.client.get(cli.config.processing_claim_key(AGENT, BODY)) == "owner-B"


def test_owner_removes_its_own_entry_and_clears_claim():
    cli = _cli()
    cli.client.rpush(cli.config.processing_key(AGENT), BODY)
    cli.claim_processing(AGENT, BODY, "owner-A", ttl=3600)
    removed = cli.remove_processing(AGENT, BODY, "owner-A")
    assert removed == 1
    assert cli.client.lrange(cli.config.processing_key(AGENT), 0, -1) == []
    assert cli.client.get(cli.config.processing_claim_key(AGENT, BODY)) is None


def test_missing_claim_is_a_noop_not_an_unconditional_lrem():
    # FAIL-SAFE: no claim key (expired) → remove must NOT delete the parked body.
    cli = _cli()
    cli.client.rpush(cli.config.processing_key(AGENT), BODY)   # parked, but no claim key
    removed = cli.remove_processing(AGENT, BODY, "owner-A")
    assert removed == 0
    assert cli.client.lrange(cli.config.processing_key(AGENT), 0, -1) == [BODY]   # stays for recovery


def test_claim_key_is_body_scoped():
    cli = _cli()
    assert cli.config.processing_claim_key(AGENT, BODY).endswith(hashlib.sha256(BODY.encode()).hexdigest()[:32])
    assert cli.config.processing_claim_key(AGENT, BODY) != cli.config.processing_claim_key(AGENT, "other")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --extra arb-memory pytest tests/test_reliable_inbox_owner_fence.py -v`
Expected: FAIL — `remove_processing` takes 2 args (TypeError on the 3-arg call); no `claim_processing`/`processing_claim_key`.

- [ ] **Step 3: Implement**

Add to `RedisConfig` (near `processing_key`, `redis_io.py:68-69`):

```python
    def processing_claim_key(self, agent_id: str, body: str) -> str:
        import hashlib
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]
        return self.key(f"agent:{agent_id}:processing_claim:{digest}")
```

Replace `RedisCli.remove_processing` and add `claim_processing` (`redis_io.py:317-318`):

```python
    def claim_processing(self, agent_id: str, body: str, owner_token: str, ttl: int) -> None:
        # Tag the just-parked entry with the claiming daemon's per-boot owner_token. A successor's
        # re-park overwrites this, so a stale predecessor's owner_token no longer matches (M2).
        self.client.set(self.config.processing_claim_key(agent_id, body), owner_token, ex=ttl)

    def remove_processing(self, agent_id: str, body: str, owner_token: str) -> int:
        # Owner-fenced acknowledgement (M2): LREM the parked body only if THIS daemon still holds
        # the claim. Atomic Lua (same shape as the register/cleanup lease compare-then-act) so a
        # concurrent re-claim cannot slip between check and act. Missing/mismatched claim ⇒ no-op
        # (the entry stays parked for recovery) — NEVER an unconditional LREM.
        script = (
            "if redis.call('GET', KEYS[2]) == ARGV[2] then "
            "  local n = redis.call('LREM', KEYS[1], 1, ARGV[1]); "
            "  redis.call('DEL', KEYS[2]); "
            "  return n "
            "end "
            "return 0"
        )
        return int(self.client.eval(
            script, 2,
            self.config.processing_key(agent_id),
            self.config.processing_claim_key(agent_id, body),
            body, owner_token,
        ))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --extra arb-memory pytest tests/test_reliable_inbox_owner_fence.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Deny-proof (delete-to-red).** Temporarily replace the Lua body with the OLD unconditional `LREM` (`return int(self.client.lrem(self.config.processing_key(agent_id), 1, body))`), run `test_stale_predecessor_remove_does_not_delete_successors_reparked_entry`, CONFIRM IT REDS (the stale remove deletes B's entry), then restore the fence. Record the red output ([[deny-proofs-need-adversarial-verification]]).

- [ ] **Step 6: Commit**

```bash
git add src/agent_redis_bridge/redis_io.py tests/test_reliable_inbox_owner_fence.py
git commit -m "fix(M2): owner-fenced remove_processing (atomic compare-token-then-LREM) + claim_processing"
```

---

## Task 2: wire the claim + owner_token through the bridge (bridge.py)

**Files:**
- Modify: `src/agent_redis_bridge/bridge.py`
- Test: `tests/test_reliable_inbox_owner_fence.py` (integration-shape) + run the existing reliable-inbox suite

**Interfaces:**
- Consumes: `claim_processing` + owner-fenced `remove_processing` (Task 1); the existing per-boot `self.owner_token` (`bridge.py:274`).
- Produces: every parked body is claimed under `self.owner_token`; both `remove_processing` call sites pass `owner_token=self.owner_token`.

- [ ] **Step 1: Write the failing/guarding test**

```python
# tests/test_reliable_inbox_owner_fence.py — append. Assert the bridge claims on park and passes
# owner_token on remove. Construct a Bridge with a fake RedisCli (mirror the existing bridge unit
# tests' construction); drive pop_inbox and the two finally paths; assert claim_processing was
# called with self.owner_token and remove_processing received owner_token=self.owner_token.
def test_pop_inbox_claims_parked_body_under_owner_token():
    ...  # spy RedisCli: pop_inbox → blmove returns BODY → claim_processing(agent, BODY, owner_token, ttl)

def test_both_remove_sites_pass_owner_token():
    ...  # inbox_loop finally (unowned path) and process_request finally both call
    #     remove_processing(agent, raw, owner_token=self.owner_token)
```

- [ ] **Step 2: Run to verify it fails** — the current `pop_inbox` does not claim; `remove_processing` is called 2-arg.

- [ ] **Step 3: Implement**

In `pop_inbox` (`bridge.py:842-846`), claim the body immediately after it is parked:

```python
    def pop_inbox(self, timeout: float | None = None) -> tuple[str | None, bool]:
        timeout = self.args.blpop_timeout if timeout is None else timeout
        if self.reliable_inbox:
            try:
                raw = self.redis.blmove_to_processing(self.agent_id, timeout)
                if raw is not None:
                    # Claim the parked entry under THIS daemon's boot token (M2). TTL generously
                    # exceeds the longest turn so the claim outlives the request; the successful
                    # remove deletes it. An early-expired claim degrades to no-op remove (safe).
                    self.redis.claim_processing(
                        self.agent_id, raw, self.owner_token,
                        ttl=max(int(self.args.turn_timeout) * 2, self.args.events_ttl),
                    )
                return raw, True
            except ResponseError as exc:
                ...  # unchanged
```

Thread `owner_token=self.owner_token` into both removes:

- `inbox_loop` finally (`bridge.py:830`): `self.redis.remove_processing(self.agent_id, raw, owner_token=self.owner_token)`
- `process_request` finally (`bridge.py:1445`): `self.redis.remove_processing(self.agent_id, processing_raw, owner_token=self.owner_token)`

- [ ] **Step 4: Run to verify pass + no regression**

Run: `uv run --extra arb-memory pytest tests/test_reliable_inbox_owner_fence.py tests/ -k "reliable_inbox or processing or recover or inbox_loop or pop_inbox" -v`
Expected: PASS. Fix any existing test that asserted the old 2-arg `remove_processing` signature.

- [ ] **Step 5: Commit + CHANGELOG**

```bash
git add src/agent_redis_bridge/bridge.py tests/test_reliable_inbox_owner_fence.py CHANGELOG.md
git commit -m "fix(M2): claim parked bodies under owner_token; owner-fence both remove_processing sites"
```

---

## Task 3: live gate — the M2 takeover scenario (deploy-gated)

**Files:**
- Verify only (integration + a real bus).

**This is the gate 5a-0's live gate asserts as landed. PAUSED for Mark's deploy-review gate before any prod redeploy.**

- [ ] **Step 1: Run the full reliable-inbox suite green** — `uv run --extra arb-memory pytest tests/ -k "reliable_inbox or processing or recover or inbox" -v`.

- [ ] **Step 2: Live takeover gate (the spec's M2 gate).** Against a real bus: predecessor A parks + claims R and stays live; B takes over the identity, recovers + re-parks R (re-claims under B), A reaches its stale `finally` (owner-fenced remove is a no-op — assert R survives in `:processing` under B's claim); B is killed; a third owner C must STILL recover R and run it. Assert R is processed exactly once end-to-end and no request is lost. This proves the acknowledgement is owner-fenced, not body-keyed.

- [ ] **Step 3: Runtime probe (what 5a-0's Task 13 M2 gate calls).** Provide a small probe (script or CLI) that a downstream gate can invoke to assert the fenced behaviour at runtime: park a body under token X, attempt `remove_processing` under token Y (≠X), assert the body survives. 5a-0's live gate calls this + checks the M2 fix commit is an ancestor of the deployed SHA.

- [ ] **Step 4: Fleet redeploy — PAUSED for Mark's deploy gate.** Redeploy all seats after the fix lands + the gate is green.

---

## Self-Review

**1. Coverage.** The cited defect has two halves: (a) body-keyed `remove_processing` deletes any matching body → **fixed** (Tasks 1–2, owner-fence); (b) ownership-loss cleanup does not join in-flight request threads (`bridge.py:620-653,671-694`), which is what makes a *live* stale predecessor possible → see Open Question (the fence neutralizes the destructive effect; the join is defense-in-depth). The M2 gate (Task 3) asserts the fixed behaviour.

**2. Placeholder scan.** Task 2's test bodies (`...`) specify the assertion contract in prose, to be instantiated from the existing bridge unit-test construction idioms. Task 1's tests are complete runnable code.

**3. Type/name consistency.** `processing_claim_key` / `claim_processing` / `remove_processing(…, owner_token)` used consistently across Tasks 1–2. `self.owner_token` is the existing per-boot token (`bridge.py:274`), not a new primitive.

## Open Question (surface to the M2 panel; genuine fork → Mark — do NOT vote-count)

**Thread-join half of the defect.** The owner-fence makes a stale predecessor's `remove_processing` a safe no-op, which is sufficient for the M2 gate. But a live stale predecessor is only *possible* because ownership-loss cleanup (`heartbeat_loop`/`inbox_loop` set `stop_event` but do NOT join in-flight `process_request` threads; `cleanup` joins only telemetry flushers, `bridge.py:620-653`). Should M2 ALSO join/drain in-flight request threads on ownership loss (defense-in-depth), or is the fence alone the right scope? The join is a larger change with restart-latency trade-offs. Recommended: **fence only for M2** (it fully closes the request-loss); track the join as a separate hardening item.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-14-reliable-inbox-owner-fence-M2.md`. Next step per the pipeline: the **M2 plan panel** (codex + agy + grok certify; cold-Opus non-cert), running in parallel with the 5a-0 track so its panel + build latency overlaps 5a-0's Tasks 1–12. Then execution, its own live gate, and merge — after which 5a-0's Task 13 M2 gate can assert it as landed.
