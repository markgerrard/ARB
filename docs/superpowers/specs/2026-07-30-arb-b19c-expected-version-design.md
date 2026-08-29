# Optimistic concurrency for ARB Memory writes (`expected_version`) + hash-preimage disclosure

**Status:** design note, first draft. Author seat: cold-opus-author-b19c, 2026-07-30.
**Scope:** ARB-B19 parts (b), (c), (d). Part (a) — whether the defect class promotes — is NOT in scope.
**Out of scope, explicitly:** deploying or restarting the live MCP/writer/consumer processes is owner-gated
and not part of this design; it lands as code + tests only.

## 1. What actually happened, and why discipline could not fix it

The `arb-backlog` artefact was clobbered three times on 2026-07-29 (merge notes at v7, v9, v11: v6 over
v4/v5, v8 over v7, v10 over v9). Every clobber came from a session that could not see the other, and every
one returned success. The artefact's own rule — "re-read the head immediately before claiming" — cannot
help, because the race window sits *between* the read and the store, and nothing in the write path compares
the caller's base against the head. `content_hash` made all three recoverable; none were prevented.

## 2. The write path as it exists (every claim below is cited)

1. `memory_store` (`src/arb_memory/mcp/tools.py:152`) validates, derives an id if absent
   (`tools.py:31-32`), builds an intent with an auto-index hint (`tools.py:177-187`) and calls `_publish`
   (`tools.py:133-150`). **It never touches Postgres.**
2. `_publish` POSTs to the writer service (`tools.py:140-145`). The writer (`src/arb_memory/writer.py:33`)
   authenticates, rejects a client-supplied `request_id` with 400 (`writer.py:39-40`), mints its own
   `request_id` only when `await` is set (`writer.py:48`), and XADDs the intent
   (`writer.py:50-57` → `bus.memory_write`, `src/arb_memory/bus.py:58`). The artefact dict travels
   **verbatim** inside the stream payload (`bus.py:84-89`), so an extra key on it survives the hop with no
   writer change.
3. A consumer persists later: `WriteLoop._handle_entry` (`bus.py:206`) → `handle_write_intent`
   (`bus.py:111`) → one transaction (`bus.py:116`) claiming a ulid idempotency key (`bus.py:117-126`) and
   calling `store.write_artefact_and_hints` (`bus.py:127`).
4. `upsert_artefact` (`src/arb_memory/store.py:20`) reads the latest `(version, content_hash)`
   (`store.py:36-40`), dedups against the latest only (`store.py:41-42`), allocates `max(version)+1`
   (`store.py:44-48`) and INSERTs (`store.py:49-68`).
5. If a `request_id` was present, the receipt is pushed to a result list (`bus.py:196-204`); the awaiting
   writer `BLPOP`s it and returns it to the tool (`writer.py:61-69`).

So today the fire-and-forget caller gets `{"accepted": true, "ulid", "artefact_id"}` (`tools.py:195`)
**before any comparison could have happened**. `accepted` means "queued", not "stored".

## 3. Fork 1 — the async gap

**Decision: require `await_result` whenever `expected_version` is set, refused synchronously at the tool
layer before anything is published, and enforced again at the writer.**

- `tools.memory_store` raises `ValueError("expected_version requires await_result")` *before* `_publish`,
  so no intent reaches the bus.
- `writer.py/publish` returns **400 `{"error": "expected_version requires await"}`** when
  `intent["artefact"].get("expected_version") is not None` and `intent.get("await")` is falsey — the writer
  is a second door (anyone holding `ARB_MEMORY_WRITER_TOKEN` can POST), and there is already precedent for a
  400 on a forbidden intent field at `writer.py:39-40`. **`is not None`, not key-presence** (r1 F5,
  cold-Opus): a tool that naturally serializes `expected_version: null` on every ordinary write must not be
  400'd; acceptance criterion 12 is the negative control that pins this.
- **Third door (r1 F1, cold-Opus): `bus.memory_write` itself refuses.** Five production callers publish
  intents without any result channel (`src/arb_memory/client.py:34`, `tools/faba/faba_launch.py:403,563`,
  `src/agent_redis_bridge/wiki_refresh.py:491`, `src/agent_redis_bridge/learn_intake.py:231`); a refusal
  minted for them would be committed, acked, and never observed (`bus.py:197-199` drops the receipt when
  `request_id` is None). So `bus.memory_write` raises `ValueError("expected_version requires a result
  channel")` when the artefact carries a non-None `expected_version` and no `request_id` is being minted
  for the write. The v1 "honest limit" (client.write unguarded) is thereby converted into an enforced door:
  those callers cannot silently opt into a guarantee that cannot reach them.
- The compare itself still runs at persist time (fork 2), and the refusal reaches the caller through the
  existing result channel (`bus.py:196-204` → `writer.py:61-69`).

**Rejected:** *check-at-publish-time in the writer* — the writer holds no DB connection at all
(`writer.py` imports only `bus`), so it would have to open one, and its answer would be stale by the time
the consumer runs; racy exactly as the brief says. **Rejected:** *allow `expected_version` without
`await_result`* — the refusal would be published to a result key with a TTL (`bus.py:204`) that nobody
reads. That is the original defect (a silent success) wearing a new hat.

**What the caller observes, precisely:**

| situation | observed |
| --- | --- |
| base matched, content new | `accepted: true`, `artefact_outcome: "stored"`, `version == expected_version + 1` |
| base matched, content identical to head | `accepted: true`, `artefact_outcome: "deduped"`, `version == expected_version` |
| head moved | `accepted: false`, `artefact_outcome: "refused_version_mismatch"`, `version = <head at refusal>`, `hints_stored: 0` |
| PK race (compare passed, INSERT lost) | the same refusal envelope, produced by the consumer's immediate re-delivery (§4) |
| await elapsed (≤30 s, `writer.py:15,44-47`) | `accepted: false`, `artefact_outcome: "unknown"`, `timed_out: true` — **UNKNOWN, not refused and not stored**; the caller must resolve by reading the head |
| transport/writer down | `RuntimeError("memory store unavailable - item NOT stored; retry shortly")` (`tools.py:147,149`) — an exception, never a payload |

The race window is therefore never observed as success. Its worst case is one extra DB round trip: the
poison branch returns without acking and without setting `_infra_this_iteration`
(`src/arb_memory/consumer_loop.py:239-241`), so `run()` resets `failures = 0` and does **not** sleep
(`consumer_loop.py:85-89`) before `_tick` re-reads the still-pending entry (`consumer_loop.py:91-104`).

## 4. Fork 2 — where the compare happens, and what Postgres actually guarantees

**Decision: in `upsert_artefact` (`store.py:20`), immediately after the existing head SELECT.**

That SELECT already returns the head's `(version, content_hash)` (`store.py:36-40`), so the guard costs
**zero extra queries**. It guards every caller of the function, including the direct-DB callers in
`vault_export`/`brief_hydrate` test paths, and it keeps the consumer free of store semantics. Consumer-level
placement was rejected: `handle_write_intent` (`bus.py:111`) would have to re-read the head itself,
duplicating the query and leaving direct callers unguarded.

Order relative to dedup: **compare first, then dedup.** A caller with a moved base gets the same answer
regardless of what the winner happened to write; deduping first would make the guard's behaviour
content-dependent and therefore unpredictable from the caller's side. (Defensible alternative: dedup first,
on the grounds that an identical head cannot be clobbered. Panel may overrule; state the choice in the
docstring either way.)

**TOCTOU — the honest answer.** The read at `store.py:36-40` and the INSERT at `store.py:49-68` are two
statements. At READ COMMITTED (the psycopg/Postgres default; no isolation level is set anywhere on the
write connection — `journey_export.py:303` is the only `SET TRANSACTION ISOLATION LEVEL` in the package, and
it is a different lane), a row a concurrent uncommitted transaction is about to insert is invisible. So
Postgres guarantees **nothing** for this read-then-insert: two writers can both see head = 5, both pass an
`expected_version = 5` compare, and both compute version 6.

The only real serialization point is **`PRIMARY KEY (artefact_id, version)` at
`src/arb_memory/schema.sql:14`** — yes, the uniqueness already exists, as the table's PK, not a separate
constraint (`schema.sql:17` drops an unrelated legacy `artefact_id, content_hash` unique constraint). The
loser's INSERT blocks until the winner commits and then fails with SQLSTATE `23505`.

**Design consequence:** let `23505` propagate; the poison-retry path converts it into a refusal on the
next delivery. **Rationale corrected in v2 (r1 F3, cold-Opus):** v1 claimed in-transaction recovery "would
need a savepoint … for no benefit"; in fact `write_artefact_and_hints` already opens a nested
`conn.transaction()` (`store.py:115`) inside `bus.py:116`'s outer transaction, which psycopg emits as a
SAVEPOINT — so catching the UniqueViolation and re-running the compare in the still-usable outer
transaction IS viable. Retry-then-refuse is still chosen, on honest grounds: zero new error-handling code
on the persist path, and the cost is one extra delivery round-trip in a race that three-collisions-per-day
makes rare-per-write. `psycopg.Error` classifies as `"poison"`
(`consumer_loop.py:28-40`), so `_retry_or_exhaust` (`consumer_loop.py:224-253`) re-delivers the intent in a
**fresh transaction** (the rolled-back idempotency-key claim is gone too), where the compare now sees the
moved head and returns a proper refusal. Retry limit is 5 (`consumer_loop.py:14`), so a single-loser race
consumes one of five attempts and resolves on the next. The store-level compare is the mechanism; the PK is
the backstop that makes it sound.

## 5. Fork 3 — backwards compatibility, wire shape, refusal payload

**Absent `expected_version` = today's behaviour, byte for byte.** The parameter defaults to `None`; the
compare block is skipped; `upsert_artefact`'s existing three-tuple and the existing four-field receipt are
unchanged. The four assertions at `tests/arb_memory/test_store.py:66-69` must keep passing verbatim.

- **Wire shape:** `intent["artefact"]["expected_version"]: int | None`. Nested inside the artefact object
  deliberately — that dict crosses the writer and the stream verbatim (`writer.py:50-57`, `bus.py:84-89`),
  so no writer forwarding change is needed. `store.write_artefact_and_hints` reads artefact keys explicitly
  (`store.py:120-129`), so the one new key must be threaded there by hand.
- **MCP signature:** `memory_store(content, artefact_id=None, mime="text/plain", await_result=False,
  expected_version: int | None = None)` — in **both** `tools.py:152` and the registration wrapper at
  `src/arb_memory/mcp/server.py:387-400`. A parameter added only to `MemoryTools` does not reach clients;
  that exact failure is recorded in `docs/superpowers/plans/2026-07-13-item2-write-result-plan.md:37-40`.
- **Validation (tightened in v2, r1 sol P1-2):** `expected_version` must be an `int >= 0` with `bool`
  explicitly excluded (`isinstance(v, bool)` refuses — `True` is an `int` subclass in Python), and requires
  an explicit `artefact_id` (a content-derived id, `tools.py:31-32`, changes whenever the content changes,
  so guarding a version of it is incoherent). Violations raise `ValueError` at the tool, before `_publish`.
  `expected_version = 0` means **create-only: refuse if any version exists** — which is also the cheap
  partial answer to ARB-B19(c)(iii), since both id-reuse collisions would have refused.
- **Absent-head semantics (pinned in v2, r1 sol P1-2 + grok F-1):** when `expected_version >= 1` and the
  head SELECT returns no row, the outcome is the same `refused_version_mismatch` with **`version: 0`**,
  meaning "no versions exist" — matching create-only's zero-based vocabulary and keeping `version` an
  `int` on every refusal. The docstring states that a refusal's `version` is observational head state
  (0 = none), never a version this request created.
- **Refusal payload — still exactly four fields.**
  `{"artefact_outcome": "refused_version_mismatch", "artefact_id": <id>, "version": <head at refusal>,
  "hints_stored": 0}`. `upsert_artefact` returns `(artefact_id, head_version,
  "refused_version_mismatch")`; `write_artefact_and_hints` short-circuits and writes **no hints AND runs
  no hint retirement** — the `replaced_index_for` UPDATE at `store.py:158-168` soft-deletes older index
  hints for the artefact, and running it on a refusal would retire the *winner's* live index hints
  (r1 F4, cold-Opus; v1's `schema.sql:36` FK-backstop claim was wrong — the head version exists, so the FK
  accepts, and the retirement UPDATE was the real hazard). Keeping four fields is deliberate: the
  persisted receipt is pinned to "exactly 4
  fields" by `docs/superpowers/specs/2026-07-13-shared-prep-slice-SPEC.md:24-31`, and the only channel from
  consumer to caller is that receipt (`bus.py:200` builds the envelope from it). `version` therefore does
  double duty as "the head you lost to" — the one wart in this design, mitigated by the two rules below.
  A separate `head_content_hash` field was considered and dropped: the caller can read it with one
  `memory_get(artefact_id, version)`.
- **`accepted` becomes outcome-derived, but only on the guarded path.** When `expected_version` is set,
  `tools.memory_store` returns `accepted: false` for `refused_version_mismatch`, `unknown` and `failed` —
  concretely `accepted = res.get("artefact_outcome") in ("stored", "deduped")` (r1 agy F1). When it is
  absent, `accepted` keeps today's always-`true` meaning (`tools.py:190`) so nothing existing changes. The
  key-passthrough allowlist at `tools.py:191-194` already contains all four receipt keys plus
  `duplicate`/`timed_out`, so it needs no edit — verify this rather than assuming it.
- **Detecting a consumer that predates the guard — ENFORCED IN CODE, not documented (rewritten in v2, r1
  sol P1-1 + cold-Opus F2).** The deployment hazard: the tool accepts `expected_version`, an old
  `WriteLoop` ignores it, the caller believes it is protected. The tool holds both operands, so
  `tools.memory_store` itself asserts the receipt arithmetic on every guarded awaited response:
  `artefact_outcome == "stored" ⇒ version == expected_version + 1` and
  `"deduped" ⇒ version == expected_version`. A receipt that violates the arithmetic fails closed as
  `accepted: false, artefact_outcome: "guard_not_live"` (a tool-layer synthetic outcome — it never appears
  in the persisted receipt, so it does not touch the O-1 enum). A caller-side note remains in the tool
  description, but the invariant lives at the production boundary, not in caller discipline — v1's
  prose-only detector was the same shape §1 says cannot work.
- **Coverage of no-result-channel callers (superseded v1's honest limit):** `client.write`
  (`src/arb_memory/client.py:17-38`) and the four other direct `bus.memory_write` callers publish with no
  `request_id` and cannot receive a refusal (`bus.py:197-199`) — so the third door in §3 makes
  `expected_version` on such a write an immediate `ValueError`, never a silent no-op guarantee.
  `memory_remember` is untouched — hints carry no artefact version of their own.
- **In-repo `artefact_outcome` reader inventory (corrected in v2, r1 sol P2 — v1's three-reader claim was
  incomplete):** `bus.py:126,200`, `writer.py:65`, `tools.py:191-194,233`, plus
  `scripts/arb-memory-fetch-e2e:165-167`, `tools/faba/faba_launch.py:213-215,331-344`,
  `tools/faba/faba_record.py:77-83`, `tools/faba/subagent/run_probe_round.py:544-550`,
  `tools/faba/subagent/run_author_round.py:712-718`. Every one gates success on exact membership in
  `{stored, deduped}`, so `refused_version_mismatch` fails closed everywhere found. External readers remain
  O-3.

## 6. ARB-B19(b) — the preimage sentence

`src/arb_memory/hash.py:4-12` is the whole answer, and a remote MCP caller cannot see it. Exact drafts,
one sentence each, to be appended to the descriptions:

**`memory_get`** (append to `server.py:364`, and add as the first docstring of `read_tools.py:297` — which
today has **no** docstring at all, so `local_server.py:22` registers `memory_get` with an empty description):

> `content_hash` is not a digest of `content` alone — it is
> `sha256(b"arbmem:artefact:v1\0" + content_mime + b"\0" + kind + b"\0" + payload)` where `kind` is
> `b"text"` with `payload` = the UTF-8 bytes of `content`, or `b"binary"` with `payload` = the raw bytes,
> and each `\0` is one NUL byte.

**`memory_store`** (append to the docstring at `server.py:387-395`):

> The stored document's `content_hash` (returned by a subsequent `memory_get`) is
> `sha256(b"arbmem:artefact:v1\0" + mime + b"\0" + b"text" + b"\0" + content.encode("utf-8"))` for text
> writes (`\0` = one NUL byte), and an omitted `artefact_id` is derived as `art-` plus that hash's first 16
> hex characters.

Rename to `artefact_hash` and a parallel `sha256_content` field were both considered and rejected for this
slice: a rename breaks every existing caller and every stored citation of the field name, and a second hash
field invites the same wrong-oracle guess in the other direction. Disclosure is the cheap, complete fix.

## 7. Acceptance criteria (each names the value a test asserts)

1. **Refusal, no DB** — extend `test_store.py:26-38`'s fake conn: with head at version 2,
   `upsert_artefact(conn, "doc", content="C", expected_version=1)` returns exactly
   `("doc", 2, "refused_version_mismatch")` **and** `conn.rows` is unchanged (zero
   `INSERT INTO artefacts`). Fails to `("doc", 3, "stored")` if the compare is missing.
2. **Back-compat** — `test_store.py:66-69` passes unmodified.
3. **Receipt + no orphan hint** — `write_artefact_and_hints` on a refusing artefact returns
   `{"artefact_outcome": "refused_version_mismatch", "artefact_id": "doc", "version": 2, "hints_stored": 0}`
   with `upsert_hint` monkeypatched to raise (proving it is never called).
4. **TOCTOU, live DB, two connections** — both read head 1; T1 commits version 2; T2's INSERT raises
   `psycopg.errors.UniqueViolation` and the test asserts `exc.sqlstate == "23505"` (not a bare
   `pytest.raises(Exception)`); re-running T2's upsert on a fresh transaction then returns
   `("doc", 2, "refused_version_mismatch")`.
5. **Consumer acks a refusal, never deadletters it** — `WriteLoop` style of
   `tests/arb_memory/test_write_result.py:34-68`: the entry id appears in `redis.acked` on the **first**
   delivery and `redis.published` carries
   `{"artefact_outcome": "refused_version_mismatch", ..., "duplicate": False}`. This test is what
   distinguishes refusal-as-receipt from refusal-as-exception: the latter would publish
   `{"artefact_outcome": "failed", "reason": "deadlettered"}` after five deliveries — the exact shape
   already asserted at `test_write_result.py:63-66`.
6. **Tool layer fails closed** — `memory_store(..., expected_version=1)` without `await_result` raises
   `ValueError("expected_version requires await_result")` **and** the fake HTTP client's `posts == []`
   (`tests/arb_memory/test_write_tools.py:24-48` style).
7. **Writer fails closed** — POST `/publish` with `artefact.expected_version` and no `await` returns
   status `400` and body `{"error": "expected_version requires await"}`.
8. **Refusal survives the tool boundary** — fake writer returns the refusal envelope; the tool returns
   `accepted is False` and `artefact_outcome == "refused_version_mismatch"`, which a test asserts is
   *distinct* from the transport failure path (`RuntimeError`, `tools.py:147,149`).
9. **Guard-not-live detector, at the production boundary (rewritten in v2)** — call the real
   `MemoryTools.memory_store(..., expected_version=1, await_result=True)` against a fake writer whose
   response is `{"artefact_outcome": "stored", "version": 3, ...}` (an unguarded old consumer's answer);
   the tool returns `accepted is False` and `artefact_outcome == "guard_not_live"`. Fails if the arithmetic
   invariant is missing (the tool would return `accepted: true, stored`).
10. **Descriptions are actually served** — assert the literal substring `arbmem:artefact:v1` appears in the
    registered tool descriptions for `memory_get` and `memory_store` as read back from the MCP tool
    listing (both `server.py:420-426` and `local_server.py:21-25`), not merely in the source docstrings.
    This fails today for the local server, where `read_tools.py:297` has no docstring.
11. **The DB test cannot go skip-green** — the new live-DB module is added to `scripts/graph-sql-gate:19-20`
    and `EXPECTED_MIN_PASSED` at `:14` is raised by the new count. Without this the `scratch` fixture skips
    silently with no DSN (`tests/arb_memory/conftest.py:84-87`) and criterion 4 is decorative.
12. **Negative control: ordinary writes are untouched (new in v2, r1 F5)** — a `memory_store` WITHOUT
    `expected_version` and a direct writer POST whose artefact carries `expected_version: null` both
    succeed exactly as today (no 400, `artefact_outcome: "stored"`); fails if the writer's check is
    key-presence instead of `is not None`, or if the tool starts serializing a non-None default.
13. **`accepted: false` on UNKNOWN and failed (new in v2, r1 grok F-2)** — with `expected_version` set, a
    fake writer returning `{"artefact_outcome": "unknown", "timed_out": true}` yields `accepted is False`;
    a deadletter envelope (`artefact_outcome: "failed"`) likewise. Fails against today's hardcoded
    `accepted: True` at `tools.py:190`.
14. **Absent-head refusals and validation (new in v2, r1 sol P1-2)** — absent head + `expected_version=0`
    stores version 1; existing head + `expected_version=0` refuses with `version = <head>`; absent head +
    `expected_version=2` refuses with `version == 0`; `expected_version=-1`, `True`, `"1"`, and
    `expected_version` with omitted `artefact_id` each raise `ValueError` at the tool with `posts == []`.
15. **The third door refuses (new in v2, r1 cold-Opus F1)** — `bus.memory_write` with an artefact carrying
    `expected_version: 3` and no result channel raises
    `ValueError("expected_version requires a result channel")` and XADDs nothing (stream length unchanged).
16. **Refusal retires nothing (new in v2, r1 cold-Opus F4)** — seed an artefact with a live
    `artefact_index` hint; drive a refusing write through `write_artefact_and_hints`; assert the winner's
    hint still has `deleted_at IS NULL`. Fails if the refusal path reaches the `replaced_index_for` UPDATE
    at `store.py:158-168`.

## 8. OPEN questions — panel-adjudicated status (v2)

- **O-1 — OWNER MERGE GATE (unanimous, r1: sol P1, agy P1, grok F-3).** The pinned SPEC at
  `docs/superpowers/specs/2026-07-13-shared-prep-slice-SPEC.md:28,37-38` enumerates
  `artefact_outcome ∈ {stored, deduped}` (+ `none`); adding `refused_version_mismatch` amends a pinned
  contract, which is a constitution-layer act. **Implementation may proceed; the merge may not, until Mark
  co-signs the enum amendment.** The amendment itself: add the value to `:28` and `:37`, plus one sentence
  — "readers must treat unknown outcomes as non-success."
- **O-2 — resolved for design purposes (r1 agy):** an added optional parameter changes neither tool name
  nor scopes; standard MCP allows schema additions without re-registration. A live-client probe after
  deploy stays on the deploy checklist (owner-gated anyway).
- **O-3 — in-repo half resolved (r1 sol):** full sweep in §5; every in-repo reader fails closed on exact
  `{stored, deduped}` membership. External readers remain unknowable from this checkout — owner inventory
  question, does not block implementation.
- **O-4.** ARB-B19(c)(ii) — returning the prior head's version+hash on *every* store, not only refusals —
  would need a fifth receipt field and so collides with O-1. Deferred, not rejected.

## 9. v2 changelog (fold of panel r1 — run `panel-b19c-design-r1-20260730T020435Z-a0a364`, closed
`needs-changes`, votes: sol block/P1, agy needs-changes/P1, cold-Opus needs-changes/P1, grok
needs-changes/P2)

- Third door added: `bus.memory_write` refuses `expected_version` without a result channel (cold-Opus F1;
  closes the five unawaited production callers, supersedes v1's client.write honest limit).
- Guard-not-live detection moved from caller prose into the tool as a fail-closed arithmetic invariant with
  synthetic `guard_not_live` outcome (sol P1-1, cold-Opus F2); AC9 rewritten to exercise the production
  boundary.
- Absent-head refusal pinned to `version: 0`; validation tightened (int ≥ 0, bool excluded, artefact_id
  required) with AC14 (sol P1-2, grok F-1).
- Writer check pinned to `is not None` with negative-control AC12 (cold-Opus F5).
- Refusal path must skip hint retirement; v1's FK-backstop claim corrected (cold-Opus F4); AC16 added.
- 23505 rationale corrected: savepoint recovery IS viable (nested txn at `store.py:115`); retry-then-refuse
  kept on cost-honest grounds (cold-Opus F3).
- `accepted: false` extended to unknown/failed with AC13 (grok F-2, agy F1).
- O-3 in-repo reader inventory corrected — nine readers, all fail closed (sol P2).
- `memory_store` disclosure sentence rephrased — the tool returns a write envelope, not the document
  (sol P2).
- O-1 elevated to owner merge gate (unanimous).
