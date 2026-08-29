# Served-hint record — design

**Status:** DRAFT v6 — folds decision record `panel-shr-v5-20260728T185603Z-5063f2` (review round 5:
no overall verdict or merge label declared in the record; roster `codex` `block`/P1 certifying,
`agy-print` `needs-changes`/P1 certifying, `grok-acp` `needs-changes`/P1 adjunct — label advisory,
findings binding, `asdk-opus5` `block`/P1 non-certifying this round, lineage — plus one seat's
executed PostgreSQL check, independently reproduced by the record) and a separate §9 execution
pass against v5 (PostgreSQL 17.7, 32 checks named, 11 runnable, 9 pass, 2 fail). Not
user-approved. Not re-panelled.
**Date:** 2026-07-28
**Branch:** `feat/served-hint-record`
**Provenance:** adapted from cognee (`github.com/topoteretes/cognee`, Apache-2.0). Their
record-keeping is taken; their auto-tuning is rejected (§3).

**Change summary:** v6 folds round 5's twelve findings (J-01..J-12) plus the two §9-execution FAILs
into v5. **Tier 1 — J-01, J-02, J-03 (three seats each, no dissent on the defect):** J-01 defines one
wire/event contract end-to-end for the bus tier — the record-intent `hits` field is now stated as
flat on the wire, a new `_parse_hint_read_event` (mirroring `eval.py:443`, which v5 claimed to mirror
but never called) parses the stringy Redis fields, and `HintReadSink.write` reads hits flat instead
of re-applying H-02's nested accessor to a surface that was never nested. J-02 hoists the per-class
attempt timestamp above the recorder call so it advances on attempt, not success, and §9's assertion
(2) is restated to say so explicitly. J-03 adds the bus-tier mirror of the H-02 tripwire test, built
from parsed, wire-shaped fields rather than a hand-built nested event, and a blanket rule requires
every other bus-tier test to do the same. **Tier 2:** J-05's index predicate gets the parenthesised
form Postgres's catalog actually writes, confirmed by executing it twice, in both index states,
against a live instance; J-06 splits the door-role and local-reader clauses of §9's self-contradicting
grant row and closes the executed PUBLIC-inheritance gap on `hint_read_deadletter` by adding that
table to both isolating functions' `REVOKE ALL ... FROM PUBLIC` line; J-07 defines the `_cap` helper
the local-tier snippet has called, undefined, since v4. **Tier 3:** J-11 states the `autocommit` proxy
immediately under the boxed invariant, not only beside it, so the change-table claim about it is now
literally true; J-10 wraps the bus XADD in `try/except`, making §7's and §9's existing ISOLATION
claims true instead of aspirational; J-08 deletes the orphaned `SearchRateLimitExceeded` class and
corrects the two comments that still referenced it; J-09 corrects §6's prose describing
`apply_mcp_grants`'s structure (discrete grouped statements, not a tuple loop). **Not resolved,
flagged for the operator, as before:** J-12 (H-11's `autocommit` proxy vs. a direct
`transaction_status` check — unchanged 1-block/2-accept split), J-04 (§3's MUST-strength snapshot
obligation — three seats propose the identical fix, but it is a constitution-layer addition, not an
author's call), and §11 Q2 (query text handling, untouched). **H-09 is no longer contested** — round
5's full panel, including the seat that raised it, accepted the narrowed claim; only the underlying
product question (§11 Q7: build a non-blocking handoff or not) stays open. **Structure, per the
standing fold instructions:** the "What changed" table, the `CLOSED` section, and the `Settled scope`
section move to `2026-07-27-served-hint-record-PANEL-RECORD.md` — this draft keeps only the pointer
below in the spec itself, and the corresponding append is provided as this artefact's second part for
the orchestrator to apply. **§4 compression stays deferred to v7**, per the same instructions — J-01
is fixed and stated correctly in both tiers rather than collapsed into one, on purpose. **Line count:
the design-doc portion below (Part 1, title through the end of §11) is 1,321 lines against the
1,400 ceiling** (raised from 1,300 for v6, reasoning on record in the fold instructions) — counted
directly from the written file (the line immediately before the `---`/`# APPENDIX` separator that
marks the start of Part 2, which is not part of the design doc and does not count against this
budget). The largest single addition is J-01's parser and its surrounding correction in §4's bus
tier; the largest saving is the three relocated sections (the "What changed" table, `CLOSED`, and
`Settled scope`), now in Part 2 below for the orchestrator to append to the panel-record file.

> **Settled findings and round history:** see `2026-07-27-served-hint-record-PANEL-RECORD.md` — do
> not re-open items marked settled there.

---

## 1. The problem, and exactly what this does and does not answer

ARB Memory serves hints and records nothing about having done so. Verified: `store.py:302`
(`retrieve`), `bus.py:242` (`handle_read_request`), and the three MCP search entry points
(`bus.py`'s wire path, `read_tools.py:63` local, `mcp/tools.py:238` public) all return rows and
write nothing. `grep` for usage/quality columns across `src/arb_memory/` finds only
`mcp_auth.oauth_clients.last_used_at` — unrelated to hints.

**What this slice delivers:**

- **Read liveness** — is memory being read at all, per tier. §6 of the architecture doc names the
  failure this guards: *"'fine because of the fallback' silently becomes 'dead three weeks'."*
- **Zero-hit rate** — how often a read returns nothing, distinguishable from errors because
  `outcome` exists (§6).
- **What a seat received — with two named limitations, not one:**
  1. `store.py:307-312` withholds artefact bodies for `learn_proposal` hints. `withheld` is
     returned by `store.retrieve` itself and recorded per hit, so this is visible rather than
     inferred.
  2. `hint_id` carries no FK, deliberately (§6, CLOSED-3). A hint that is later hard-deleted
     (`scripts/arb-memory-seat-e2e`'s `cleanup_rows`) leaves its `hint_read_hit.hint_id`
     unreconstructable. **A `hint_read_hit` row proves *that* a hint at that id was served, not
     what it said, once the hint is gone.**

**What this slice does NOT deliver, stated so no one builds on it:**

- **It does not measure retrieval quality.** No relevance signal. Scoring is the eval harness's
  job and out of scope (§2).
- **It does not support "this hint is never retrieved, therefore prune it."** §3 forbids using it
  as a deletion input.
- **It does not attribute reads to a seat or a run, on either tier, in this slice.**
  `run_id`/`seat_id` are schema columns with indexes (forward-compatible, now partial — G-10), but
  every row this slice writes carries them NULL — no caller on the bus path or the local path
  currently has, or passes, that identity. Per-seat/per-run breakdowns are a Take 2 item.

## 2. Scope

| Decision | Value |
|---|---|
| Takes in scope | Take 1 (served-hint record) only |
| Read paths covered | **Bus tier AND local-MCP tier** |
| Read path excluded | Public MCP door only |
| Branch endpoint | Code + schema + tests. No deploy, no live-DB apply |
| `store.retrieve` return shape | Each item carries a `withheld` key. Shared function — bus (`bus.py:251`), local (`read_tools.py:69`), **and** the public door (`mcp/tools.py:244`) all call it. Additive key, non-breaking. |

**Why the coverage is bus + local.** `src/agent_redis_bridge/local_memory_mcp.py` injects a
direct-DSN local memory MCP across four engines (injection tests exist for `codex`, `agent_sdk`,
`acp`, `pi_sdk`), reaching `mcp/read_tools.py` → `store.retrieve` **without touching the bus**.
Verified: `local_memory_mcp_config()` (`local_memory_mcp.py:62-83`) builds one server process per
seat per engine injection — a fact this draft also relies on for G-08's scoping, below. It is
model-invoked at up to 30 reads/min per seat (`read_tools.py:20`); the bus tier is
orchestrator-driven at roughly one read per panel.

**The public door stays excluded.** §8a's load-bearing property is that `arbmem_mcp` holds
SELECT-only on `hints`/`artefacts` (`grants.py:79-92`), so a leaked token exposes recall but not
corruption; recording would add a write path this design does not want to open there. Its absence
is recorded in the data by the `door` column, not left implicit.

## 3. Non-goals, and one question for the operator

- **No ranking change.** `search_hints` returns the same rows in the same order. Nothing on the
  retrieval path reads these tables.
- **No auto-tuning from feedback.** cognee propagates ratings into a `feedback_weight` that
  reweights retrieval. Not done here.
- **Served-hint statistics MUST NOT drive deletion, retirement, or ranking of a hint without a
  human evidence artifact.** The pressure will not arrive as reweighting — it will arrive as
  retention ("prune hints not served in 90 days"), which routes around a rule written only about
  ranking. `docs/calibration-loop.md:22-26` is the binding half: *"Ground truth is manual …
  `annotate` requires `--evidence`. No commit pattern is treated as fact."* A usage statistic is a
  commit pattern by another name.

> **CO-SIGNED 2026-07-27, Mark — binds at MUST strength. Settled; not reopened in v4.**
> `calibration-loop.md`'s report-only rule was co-signed for **reviewer weighting**. This extends
> it to **retrieval and retention**.
>
> **Rationale worth keeping attached to the rule.** The measurement undercounts by construction:
> the public door is unrecorded (§2), and a read returns only `k` hints, so most of the corpus is
> never sampled in any given window. "Never served" therefore usually means *not yet looked at*,
> not *useless*. Deleting on it reads a sampling artefact as a verdict.
>
> **Migration obligation:** a MUST-strength rule governing future sessions belongs in `CLAUDE.md`,
> not only in a design doc. Move it there when the feature lands, via the protected-file merge
> discipline (read first, classify append/merge, never blind overwrite).

## 4. Architecture — one invariant, two tiers

This is the change that fixes the round-1 P0 and the head-of-line finding together, and it is why
v2 was a rewrite rather than a patch: wrapping the inline INSERT in a transaction (needed for
correctness) makes it slower, worsening the blocking it already caused. Both dissolve once
recording leaves the read path.

**Why this section is restructured, not just re-patched (owner-approved, round-3 directive).**
Three consecutive rounds each fixed a real defect and introduced a new one on the *same* axis —
v1 never committed the receipt; v2's fix broke `served_at`; v3's fix for `served_at` broke the
local tier's commit again. Three different authors, one failure shape each time, because the
document described connection/transaction handling per tier, in prose: each fix was locally
correct and globally wrong. The remedy is structural, not another patch.

> ## RECEIPT INVARIANT
>
> For every served-hint read, on either tier:
>
> 1. **COMMIT.** The receipt — parent row, and any child rows, together — is written inside a
>    transaction that actually commits, opened on a connection that was `IDLE` at the moment that
>    transaction began. A receipt is never left uncommitted because something else already put
>    the connection in a transaction state the receipt's own commit can't reach.
> 2. **ISOLATION.** A receipt failure — of any kind: connection, constraint, precondition, a bug
>    in the recorder itself — is caught at the recording boundary and never changes what the read
>    returns to its caller: not the result, not the exception, not that exception's chain.
>
> Everything below is a *consequence* of this pair, named as such per tier. Nothing below is an
> independent per-tier rule.

> **Proxy note, added this fold (J-11 — H-11's minimum remediation, executed correctly this
> time).** `autocommit=True` is the accepted, verified proxy for `IDLE` used by the local-tier
> guard (below), because an autocommit connection is `IDLE` between statements by construction
> (proven in the round-1 P0 paragraph, next). The guard cannot observe the converse — an `IDLE`
> connection without `autocommit`, or an autocommit connection already inside an open explicit
> transaction — which is why whether to switch the guard to a direct
> `conn.info.transaction_status == IDLE` check instead is still open: H-11/**J-12**, contested,
> one seat blocking; see "Author choices requiring operator adjudication," end of this section.

**Round 4 did not find a defect in this pair (preserved, per the fold instructions).** All three
reporting seats credited the restructure as genuine — grok: "real, not decorative"; asdk: "the
invariant genuinely closed that axis" — and no seat could construct a commit-or-isolation failure
from v4's transaction mechanics. v4's *new* defects (H-01, H-02, H-04..H-11) sit on **data-shape**
and **guard-boundary** axes the invariant's two clauses were never written to cover — wrong
dictionary nesting (H-02), a table missing from a REVOKE list (H-06), a bound that doesn't cover
every rejection path (H-08), a `UNIQUE` constraint colliding across deployments (H-04). This draft
therefore extends the invariant's **test coverage** onto those axes (§9); the invariant's own two
clauses, above, are untouched.

**The round-1 P0, precisely — CLOSED, do not reopen; the case that motivates clause 1.**
`ReadLoop`'s connection is non-autocommit (`run.py:18-21`, `_memory_conn()` calls bare
`psycopg.connect(DSN)`; `MemoryConsumer` calls `conn_factory()` **once** at construction,
`bus.py:368`, and `ReadLoop` reuses that single connection for every request it ever serves). A
`SELECT` on that connection leaves it `INTRANS`, so a later `with conn.transaction():` evaluates
`self._outer_transaction = (transaction_status == IDLE)` as **False**
(`psycopg/transaction.py:215`) and takes the savepoint branch — `COMMIT` is yielded only in the
outer branch (`transaction.py:188-190`). An inline record on that connection would never commit,
and `now()` is `transaction_timestamp()`, so every row would carry the timestamp of the first read
after process start. Two independently verified facts underlie clause 1: (a) an `IDLE` connection
entering `with conn.transaction()` takes the outer branch and issues a real `COMMIT`
(`transaction.py:215-216`, `188-190`); (b) an **autocommit** connection is `IDLE` between
statements by construction, so entering `with conn.transaction()` on one also takes the outer
branch — grouping the statements inside it into one real, committed transaction, not merely
appearing to.

### Bus tier — satisfies the invariant by never touching the read connection

`handle_read_request` does one extra thing after replying: it XADDs a **record-intent** to a new
stream, `hint_reads_stream()` → `f"{prefix}arbmem:hint-reads"` (naming mirrors `reads_stream` /
`writes_stream`, `bus.py:22-27`). The XADD is fire-and-forget **relative to the reply**: the read
already answered via `lpush` (`bus.py:256`) before the XADD runs, so a slow or failed XADD cannot
delay or break the reply the caller is blocked on. **This is clause 2 (ISOLATION) on the bus tier,
stated at the scope it actually holds: the recording path and the read path share no connection and
no synchronous call *that the already-sent reply depends on*, so a recording failure or delay
structurally cannot reach the caller waiting on that reply.**

**Narrowed from v4's wording, not broadened (H-09 — accepted, round 5, 4/4; full record in "Author
choices requiring operator adjudication," end of this section).** v4 additionally claimed the two
paths share "no synchronous call," unqualified. codex traced a real, narrower gap: `ReadLoop.step()`
reads and handles one stream entry at a time (`bus.py:295-306`), so the inline `redis.xadd(...)`
call inside `handle_read_request` — however fast — still runs **before** `step()` returns control
to `xreadgroup` for the next entry. A slow XADD cannot corrupt or delay the reply already sent, but
it **can** delay the *next* request this same `ReadLoop` serves. The wording above states the
narrower, true claim instead of the broader one.

**Record-intent stream fields:**

| Field | Source | Notes |
|---|---|---|
| `query` | `request["query"]`, truncated to `search_max_query_chars` (2000) **before** XADD | truncation happens once, here; the consumer's parser (`_parse_hint_read_event`, below) renames this to `query_text` internally — same value, wire name vs. parsed name (J-01) |
| `query_truncated` | computed alongside `query` | `"1"`/`"0"` |
| `k` | `request.get("k", 8)` | as today |
| `outcome` | `"ok"` / `"error"` | from the existing try/except in `handle_read_request` (`bus.py:250-254`) |
| `hit_count` | `len(hits)` when `outcome="ok"`, else absent | |
| `hits` | JSON-encoded array of **flat** elements `{hint_id, rank, withheld, vector_distance, lexical_rank}` (`json.dumps`) — **flattened here, at XADD time**, from `store.retrieve`'s nested `hit["hint"]["id"]` / `hit["hint"].get("vector_distance"/"lexical_rank")` / `hit["withheld"]` (`store.py:302-315`, identical shape on both tiers). **The wire element has no `hint` sub-key — the nesting is resolved before XADD, not after (J-01a: v5's wording here was readable two ways, and the consumer applied the wrong one — see the parser, below)** | mirrors `memory_write`'s `json.dumps(payload)` shape, `bus.py:82` |
| `run_id`, `seat_id` | not present on the current wire request — omitted; consumer stores `NULL` | forward-compatible slot |
| `cid` | `request["cid"]` | correlation only |
| `served_at` | `datetime.now(timezone.utc).isoformat()`, captured in `handle_read_request` at reply-build time | producer timestamp, not consumer-insert time |

**The XADD call itself is bounded (G-04), and now guarded (J-10).** Every existing producer in this
module trims — `bus.py:85` (writes), `bus.py:397` (reads, shown above), `audit.py:71`,
`fetch.py:57` — all `maxlen=MAXLEN, approximate=True` with `MAXLEN = 10_000` (`bus.py:16`). The
record-intent XADD gets the identical treatment, wrapped in the `try/except` clause 2 (ISOLATION)
requires and §7/§9 already claimed but v5 never wrote:

```python
try:
    redis.xadd(
        hint_reads_stream(prefix),
        fields,
        maxlen=MAXLEN,
        approximate=True,
    )
except Exception:
    # ISOLATION (clause 2), made real rather than merely claimed (J-10). The reply is already
    # sent (lpush, above) by the time this runs, so a failed XADD must not propagate into
    # ReadLoop._handle_entry and strand the entry, unacked, in the PEL.
    logger.exception("hint-read record-intent XADD failed (reply already sent, unaffected)")
```

Left unbounded, this would have been the *only* unbounded stream in `arb_memory`, and §2 stops
this branch at "code + schema + tests, no deploy" — meaning the producer would run in production
before `HintReadConsumer` exists to drain it, growing unconsumed in the same Redis the read path
depends on. Bounding it is not optional hygiene here; it is the difference between "acceptable
loss on flush" (§7) and "unbounded growth in the shared dependency."

The stream entry's own Redis-assigned id (`entry_id`) becomes the basis for both `read_id` and the
`stream_entry_id` column, next — the latter carrying a fully-qualified value now, not the bare id
(H-04, below).

`HintReadConsumer` drains this stream (new module `arb_memory/hint_reads.py`, mirroring
`arb_memory/eval.py`'s shape), following `EvalConsumer` (`arb_memory/eval.py`, started by
`run.py:59` `run_eval()`) as the template: `StreamConsumerLoop`, at-least-once, and — per clause 1
— a fresh `conn_factory()` call inside `_handle_entry`, never once at `start()`.

**`read_id` minting must survive PEL redelivery, and the namespace must not collide across
deployments (G-09, folds F-03).** For bus rows, `HintReadConsumer` derives `read_id` deterministically
from the entry id, so redelivery of the same entry always derives the same value and the parent
INSERT's `ON CONFLICT (read_id) DO NOTHING` is a safe no-op on replay rather than a duplicate or a
lost child-FK reference (precedent: `eval.py:378-386`'s `ON CONFLICT (stream_entry_id) DO NOTHING
RETURNING id` returns `None` on conflict; redelivery is not hypothetical — `ReadLoop.drain_pending`,
`bus.py:287-298`, exists because the PEL redelivers, and the equivalent group-consumer path for
this new stream inherits the same semantics).

`uuid.uuid5(NAMESPACE, stream_entry_id)` as drafted in v3 is unimplementable — `NAMESPACE` names
nothing (`uuid` exposes only `NAMESPACE_DNS/OID/URL/X500`) — and even fixed, deriving from
`stream_entry_id` alone collides across deployments: Redis entry ids are unique **per stream**,
and streams are prefix-parameterised (`bus.py:15`, `22-27`, `PREFIX = os.environ.get(
"ARB_MEMORY_PREFIX", "")`). Two differently-prefixed deployments writing into one shared
`arb_memory` database would derive the **same** `read_id` for their own, unrelated entry `1-0`,
and `ON CONFLICT (read_id) DO NOTHING` would then silently discard the second deployment's row —
data loss, not an error, the worst shape for this kind of collision.

**Fix:** pin a literal namespace constant, and derive from the fully-qualified, prefixed stream
key plus the entry id — not the entry id alone:

```python
import uuid

HINT_READ_NAMESPACE = uuid.UUID("2f8e6b1a-4b76-4b8e-9a2b-6f6e6a2e6a11")


def _qualified_entry_id(prefix: str, entry_id: str) -> str:
    """The fully-qualified stream key + entry id -- fed into read_id's uuid5 below, AND
    reused as the stream_entry_id COLUMN's value (H-04) so that column also carries a
    globally-unique value, not just read_id itself. Closes H-04's cross-prefix collision on
    hint_read AND hint_read_deadletter (§6) without changing either table's single-column
    UNIQUE (stream_entry_id) shape, which StreamConsumerLoop's generic dead-letter canary
    depends on (consumer_loop.py:189-194 -- its probe always writes the literal sentinel
    "__canary__", never a real entry id, so it never collides regardless of this change)."""
    return f"{hint_reads_stream(prefix)}/{entry_id}"


def _bus_read_id(prefix: str, stream_entry_id: str) -> uuid.UUID:
    return uuid.uuid5(HINT_READ_NAMESPACE, _qualified_entry_id(prefix, stream_entry_id))
```

`hint_reads_stream(prefix)` already resolves to the full, prefixed key
(`f"{prefix}arbmem:hint-reads"`), so two deployments with different `ARB_MEMORY_PREFIX` values now
derive different `read_id`s for their respective entry `1-0`s, closing the collision axis on
`read_id` itself, while staying deterministic under redelivery of the *same* deployment's *same*
entry (the prefix and stream name are constant for a given consumer). For **local rows**, there is
no stream and no redelivery (below), so `read_id = uuid.uuid4()` minted once at write time remains
sufficient.

**`UNIQUE (stream_entry_id)` closed the same collision axis a second, independent way, and v4 left
it open (H-04).** v4 kept the belt-and-braces `stream_entry_id` column storing the **bare** Redis
entry id, under `UNIQUE (stream_entry_id)`. Two deployments with different prefixes that both
happen to produce entry `1-0` now derive **different** `read_id`s (fixed, above) — but the
bare-entry-id column still collides: the *second* deployment's insert raises a real `23505` on that
constraint alone, converting a read that should succeed into a poison/dead-letter cycle, a hard
failure where none was intended. Because `hint_read_deadletter`'s identically-shaped
`UNIQUE (stream_entry_id)` constraint is *also* the target of `StreamConsumerLoop`'s generic
dead-letter canary (`consumer_loop.py:189-194`), the fix cannot change that constraint's **shape**
— Postgres requires an `ON CONFLICT (stream_entry_id)` clause to match a single-column unique
constraint on exactly that column. The fix instead changes **what value the real write puts in the
column** — `_qualified_entry_id`, above, the identical string already fed into `read_id`'s `uuid5`.
`hint_read`'s `UNIQUE (stream_entry_id)` was never the dedup mechanism (`read_id` is, via
`ON CONFLICT (read_id) DO NOTHING`, next) — it stays a genuinely safe belt-and-braces column now
that the value it holds cannot collide across deployments. `hint_read_deadletter`'s identical
constraint **is** load-bearing — it is `deadletter_malformed_hint_read`'s own dedup mechanism
against redelivery of an already-dead-lettered entry, below — and the same value-level fix closes
the same collision there without touching that mechanism.

**The write itself, made explicit (H-10 — v4 asserted the net effect and gave no SQL for it), and
the event must be parsed before it reaches that write (J-01 — three seats independently found no
parse step existed at all).** No function anywhere in v4 wrote the bus-tier parent + hits together;
the eval lane's precedent (`PostgresEvalSink.write`, `eval.py:369-395`) has no child table to compare
against. `HintReadConsumer._handle_entry` first parses the raw Redis stream fields — every value in
a stream entry's field dict is a **string**, never a bool or int or nested structure — exactly as
`EvalConsumer._handle_entry` does via `_parse_event` (`eval.py:443`, called at `eval.py:479`), the
in-repo precedent v5 claimed to mirror but did not actually call:

```python
import json


def _parse_hint_read_event(entry_id: str, fields: dict[str, str]) -> dict:
    """Mirrors EvalConsumer._parse_event (eval.py:443). Renames the wire field query ->
    query_text to match HintReadSink.write's and _record_local_read's shared parameter name
    (J-01b), parses hits from its JSON-encoded, already-flat string form (J-01c), and coerces
    the boolean/int fields the wire only ever carries as strings."""
    return {
        "query_text": fields["query"],
        "query_truncated": fields.get("query_truncated") == "1",
        "k": int(fields["k"]),
        "outcome": fields["outcome"],
        "hit_count": int(fields.get("hit_count", 0)),
        "hits": json.loads(fields.get("hits", "[]")),
        "run_id": fields.get("run_id") or None,
        "seat_id": fields.get("seat_id") or None,
        "cid": fields.get("cid"),
        "served_at": fields["served_at"],
    }
```

Mirroring `PostgresEvalSink.write`'s `ON CONFLICT ... RETURNING` shape, extended for
`hint_read_hit`, and called with this **already-parsed** event — `event["hits"]` here is the flat,
wire-shaped list `_parse_hint_read_event` just produced, not `store.retrieve`'s nested return shape:

```python
class HintReadSink:
    """Mirrors PostgresEvalSink.write (eval.py:369-395): the ON-CONFLICT-then-RETURNING shape
    is the actual parent+hits write SQL H-10 found missing from v4 entirely. Unlike eval's
    single-table sink, hint_read has real children (hint_read_hit), so a duplicate parent must
    skip the children too, not just no-op the parent insert. Takes an ALREADY-PARSED event
    (_parse_hint_read_event, above) -- hits here are flat {hint_id, rank, withheld,
    vector_distance, lexical_rank} dicts, matching the wire shape (J-01: v5 applied H-02's
    nested accessor here by mistake, to a surface that was never nested)."""

    def write(self, conn, prefix, entry_id, event):
        qualified_id = _qualified_entry_id(prefix, entry_id)
        read_id = _bus_read_id(prefix, entry_id)
        with conn.transaction():
            row = conn.execute(
                "INSERT INTO hint_read (read_id, door, outcome, run_id, seat_id, query_text, "
                "query_truncated, k, hit_count, cid, stream_entry_id, served_at) "
                "VALUES (%s, 'bus', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (read_id) DO NOTHING RETURNING read_id",
                (read_id, event["outcome"], event.get("run_id"), event.get("seat_id"),
                 event["query_text"], event["query_truncated"], event["k"],
                 event.get("hit_count", 0), event.get("cid"), qualified_id,
                 event["served_at"]),
            ).fetchone()
            if row is None:
                # Redelivery of an already-committed entry -- read_id is deterministic under
                # redelivery by construction (above), so the parent, and therefore its hits
                # (they always commit together, below), already exist. Skip re-inserting hits:
                # an ON CONFLICT on hint_read_hit's own PRIMARY KEY (read_id, rank) would
                # otherwise fail the whole transaction on a CLEAN redelivery -- exactly the
                # failure H-10 traced, where a good receipt gets dead-lettered by mistake.
                return "duplicate"
            for rank, hit in enumerate(event.get("hits", ()), start=1):
                # FLAT access (J-01) -- event["hits"] is the wire shape produced by
                # _parse_hint_read_event, not store.retrieve's nested shape. Do NOT apply
                # H-02's "inner = hit['hint']" accessor here; that was v5's mistake.
                conn.execute(
                    "INSERT INTO hint_read_hit (read_id, rank, hint_id, withheld, "
                    "vector_distance, lexical_rank) VALUES (%s, %s, %s, %s, %s, %s)",
                    (read_id, rank, hit["hint_id"], hit["withheld"],
                     hit.get("vector_distance"), hit.get("lexical_rank")),
                )
        return "recorded"
```

`HintReadConsumer._handle_entry` calls `event = _parse_hint_read_event(entry_id, fields)` once per
entry — mirroring `EvalConsumer._handle_entry`'s own call to `_parse_event(entry_id, fields)` before
its sink loop — then calls `sink.write(conn, self.prefix, entry_id, event)` for each configured
sink, exactly where `EvalConsumer._handle_entry` calls `sink.write(conn, event)`
(`eval.py:494-499`). **Corrected parity claim (J-01c — v5 asserted parity with a step it never
called):** the parse step is the *same shape* as eval's, not new machinery; the only signature
difference at the `sink.write` call itself is the two extra positional arguments
`HintReadSink.write` needs for the qualified-id derivation, above. Net effect under redelivery, now
backed by SQL rather than asserted in prose: exactly one `hint_read` row, exactly one set of
`hint_read_hit` rows, no FK violation, no dead-letter, `_ack` proceeds — because a clean redelivery
takes the `row is None` branch and returns `"duplicate"` without touching `hint_read_hit` at all.

**Dead-lettering is provisioned, not merely promised (G-05).** v3 required EvalConsumer-style
dead-lettering in five places and tested for it, but defined no dead-letter table — a mechanism
promised and never shipped. `StreamConsumerLoop` probes a `deadletter_table` attribute generically
(`consumer_loop.py:165-197`) and its fallback branch does
`INSERT INTO {table} (stream_entry_id) VALUES (%s) ON CONFLICT (stream_entry_id) DO NOTHING` for
its canary probe only (always the literal sentinel `"__canary__"`, above) —
`HintReadConsumer.deadletter_table = "hint_read_deadletter"`, and the real write for a malformed or
poison-exhausted entry goes through its own function, mirroring `deadletter_malformed_eval_event`
(`eval.py:398-412`) with one deliberate deviation for H-04:

```python
def deadletter_malformed_hint_read(conn, prefix, entry_id, fields, error):
    """Mirrors deadletter_malformed_eval_event (eval.py:398-412), with one deliberate change:
    stream_entry_id stores the FULLY-QUALIFIED key (_qualified_entry_id, above), not the bare
    Redis entry id -- closing the same cross-prefix collision axis as read_id itself (H-04).
    eval_deadletter's own stream_entry_id stays bare-entry-id; that table is existing precedent,
    out of scope for this fold."""
    with conn.transaction():
        conn.execute(
            "INSERT INTO hint_read_deadletter (stream_entry_id, raw_entry, error) "
            "VALUES (%s, %s, %s) ON CONFLICT (stream_entry_id) DO NOTHING",
            (_qualified_entry_id(prefix, entry_id), Jsonb(fields), str(error)),
        )
```

malformed/unparseable entries and poison-retry-exhausted entries both route there, inside their own
`with conn.transaction():`, **before** `_ack` — the same commit-then-ack ordering already
established for the parent table (F-08, §7). Only the bus tier ever writes here; the local tier has
no stream to dead-letter from (below).

**Wiring, not just definition (H-12).** v4 said `HintReadConsumer` "drains this stream," but
specified no way to start one, and no way to invoke `run_hint_read_purge()` (§8) either — both
existed only as importable functions. Mirroring `run_eval`/`run_eval_purge`'s wiring
(`run.py:59-64`, `:629-673`) exactly:

```python
# run.py
def run_hint_reads() -> None:
    from arb_memory.hint_reads import HintReadConsumer

    consumer = HintReadConsumer(_redis_client(), _memory_conn)
    consumer.start()
    _wait_forever()


def run_hint_read_purge() -> None:
    from arb_memory.hint_reads import purge_expired

    days = int(os.environ.get("ARB_HINT_READ_RETENTION_DAYS", "30"))
    with _memory_conn() as conn:
        deleted = purge_expired(conn, older_than_days=days)
    print(f"hint-read purge deleted {deleted} rows older_than_days={days}")
```

and two new entries in `main()`'s `services` tuple / `handlers` dict (`run.py:632-671`):
`"hint-reads"` → `run_hint_reads`, `"hint-read-purge"` → `run_hint_read_purge` —
`python -m arb_memory hint-reads` / `hint-read-purge`, the invocation shape every other lane in
this module already uses. `HintReadConsumer` uses `_redis_client()` (the main bus client), not
`_eval_redis_client()` — the hint-reads stream lives on the same bus as `reads_stream`/
`writes_stream`, not the eval lane's separate Redis DB. §2's "no deploy" rule is unaffected:
neither entry is added to any compose/supervisor config in this branch; only the entrypoint exists
and is callable, closing the completeness gap without touching deployment.

### Local tier — satisfies the invariant by asserting what it needs, not assuming it

`read_tools.py` has **no bus access** — verified, it is DSN-only — so it cannot emit an intent and
must write directly, on the connection it already has.

**COMMIT, asserted rather than assumed (G-02, folds F-05).** v3 stated the local recorder's
safety as a citation to *where* `_conn()` sets autocommit: `ReadMemoryTools._conn()`
(`read_tools.py:34-41`) does `psycopg.connect(dsn, autocommit=True)` — but only in the `else`
branch. `build_local_server` (`local_server.py:10-11`) publicly accepts a `conn_factory`, and every
existing test supplies one (`test_read_tools_runtime.py:118-119`'s `FakeConn`,
`test_read_tools.py:37-39`'s `FakeConn` — neither object even *has* an `autocommit` attribute).
Production (`run.py:494`, `build_local_server(LocalReadSettings(dsn=dsn), embed=embed)`) passes no
factory and so *is* autocommit. But a location-anchored claim ("`_conn()` is autocommit") stops
being true the moment a factory is supplied, which every test already does — on a non-autocommit
injected connection, `store.retrieve`'s SELECTs leave it `INTRANS`, and clause 1 is violated
exactly as in the round-1 P0, reproduced on the local tier.

**The guard checks a proxy, not the invariant's own precondition, and this draft keeps it that way
on purpose (H-11 — contested, J-12; full record in "Author choices requiring operator
adjudication," end of this section).** The fix below checks `getattr(conn, "autocommit", False)`.
The invariant itself (above) states the precondition as **`IDLE`** at the moment the transaction
begins, and the round-1 P0 paragraph explains that transaction status, not the autocommit flag, is
what actually selects the outer-transaction branch. `autocommit=True` **implies** `IDLE` between
statements by construction — that half of the equivalence is proven, above — but the guard cannot
observe the reverse: a connection can be `IDLE` without `autocommit`, and (codex's finding) an
autocommit connection can still be **inside** an already-open explicit transaction at the moment
`_record_local_read` runs, in which case `getattr(conn, "autocommit", False)` reads `True` while
`transaction_status` is not `IDLE`. This draft keeps `autocommit` as the accepted, named proxy
rather than switching to a direct `transaction_status` check — see the end-of-section callout for
why, and for the case against this choice.

**Fix — state and check the property, not the location:**

```python
def _cap(query: str, max_chars: int) -> tuple[str, bool]:
    """Truncates to max_chars; returns (possibly-truncated text, was_truncated). Judged
    against the ORIGINAL, uncapped length by the query-too-long guard below -- _cap itself
    never rejects, only truncates for storage (J-07: this helper was called, undefined, since
    v4; the local-tier snippet now defines it)."""
    if len(query) <= max_chars:
        return query, False
    return query[:max_chars], True


async def memory_search(self, query: str, k: int = 8) -> list[dict]:
    query_text, truncated = _cap(query, self.settings.search_max_query_chars)
    rejection_class: str | None = None
    try:
        rejection_class = "query_too_long"
        if len(query) > self.settings.search_max_query_chars:
            raise ValueError("query too long")
        rejection_class = "missing_api_key"
        if self._uses_default_embed and not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("memory_search unavailable: OPENAI_API_KEY is not set")
        rejection_class = "rate_limited"
        self._check_search_allowed()   # raises ValueError("search rate limit exceeded"),
                                        # read_tools.py:52 -- rejection_class (above), not
                                        # exception type, is what distinguishes it (J-08: v5
                                        # declared and referenced a SearchRateLimitExceeded
                                        # class that nothing raised, caught, or inspected)
        rejection_class = None         # every guard passed; a store.retrieve() failure below
                                        # is not one of the three bounded rejection classes —
                                        # a separate, deliberately unbounded axis (H-08 relation
                                        # note, below)
        hits = store.retrieve(self._conn(), query, k=k, embed=self.embed)
    except Exception as exc:
        try:
            if rejection_class is not None:
                last = self._last_rejection_receipt_at.get(rejection_class)
                should_record = last is None or (time.monotonic() - last) >= 60.0
                if should_record:
                    # Advance on ATTEMPT, not on successful persistence (J-02). This line runs
                    # whether or not _record_local_read below succeeds, so a recorder that
                    # raises on every call still bounds ATTEMPTS at one per class per window --
                    # not just rows, which would already be zero regardless of this line.
                    self._last_rejection_receipt_at[rejection_class] = time.monotonic()
            else:
                should_record = True
            if should_record:
                _record_local_read(self._conn(), door="local", outcome="error", hit_count=0,
                                    query_text=query_text, query_truncated=truncated, k=k)
        except Exception:
            logger.exception("local read receipt failed (rejected/errored read unaffected)")
        raise
    try:
        _record_local_read(self._conn(), door="local", outcome="ok", hit_count=len(hits),
                            query_text=query_text, query_truncated=truncated, k=k, hits=hits)
    except Exception:
        logger.exception("local read receipt failed (served read unaffected)")
    return [_json_safe_search_hit(hit) for hit in hits]
```

`getattr(conn, "autocommit", False)` rather than bare `conn.autocommit` deliberately: the existing
`FakeConn` fixtures carry no such attribute at all, and treating "attribute absent" the same as
"not proven autocommit" is the fail-safe direction — it routes into the guard below rather than
raising a bare `AttributeError` from inside a transaction block that may have partially executed.
Verified against both fixtures (`test_read_tools.py:11-13`, `test_read_tools_runtime.py:15-17`):
neither `FakeConn` defines `autocommit`, so `getattr(..., False)` correctly routes both into the
guard rather than raising.

**Two data-shape defects fixed together, because they share one cause (H-02).** v4's
`_record_local_read` indexed `hit["hint_id"]`, `hit["withheld"]`, `hit.get("vector_distance")`,
`hit.get("lexical_rank")` — but `store.retrieve` (`store.py:302-315`) returns
`{"hint": {...}, "artefact", "repo_pointer", "withheld"}` per item, with the hint's own `id`,
`vector_distance`, and `lexical_rank` **one level down**, inside `hit["hint"]`, as `search_hints`
builds them (`store.py:229-245`). The failure was asymmetric and therefore doubly dangerous:
`hit["hint_id"]` raises `KeyError` — caught by the guard below and logged, not silent — but
`hit.get("vector_distance")` and `hit.get("lexical_rank")` are `.get()`, not `[]`: they never
raise, they silently return `None` forever. Fixing only the `KeyError` half would have left both
scores permanently `NULL`. The `KeyError` fired **inside** `with conn.transaction():`, which G-06's
atomicity then rolled back **in full** — parent row included — so the local tier's success path,
the tier §2 names as the high-volume one and the entire reason coverage was widened past the bus,
wrote **zero** `hint_read` rows, forever, while `memory_search` kept returning correct results.
ISOLATION was doing exactly its job, which is what made the total loss invisible: no test in v4's
§9 called the real `memory_search` method end-to-end and inspected the row it wrote (§9, below,
adds the one that does).

```python
        for rank, hit in enumerate(hits, start=1):
            inner = hit["hint"]   # H-02: hint_id/vector_distance/lexical_rank live one level
                                   # down, inside store.retrieve's "hint" key (store.py:302-315)
            conn.execute(
                "INSERT INTO hint_read_hit (read_id, rank, hint_id, withheld, "
                "vector_distance, lexical_rank) VALUES (%s, %s, %s, %s, %s, %s)",
                (read_id, rank, inner["id"], hit["withheld"],
                 inner.get("vector_distance"), inner.get("lexical_rank")),
            )
```

(the same `_record_local_read` function whose full body is under "Fix — state and check the
property," above — this is its hit loop, reproduced here to show the local tier's own nested
accessor is the mirror image of the bus tier's flat one, and neither shares the other's shape,
by design, on purpose, since they read from `store.retrieve` directly and from a parsed wire
event respectively.)

**Atomicity, as a further consequence of clause 1 (G-06, new — grok).** The bus tier already gets
a per-entry `transaction()` (F-01, mirroring `eval.py:369-371`); v3 never required the equivalent
for local's parent + hits, which share the one reused connection and have no redelivery to fall
back on — a partial hit set written there is permanent. The function above answers this directly:
parent and every hit insert sit inside one `with conn.transaction():` block. If any hit insert
fails, the whole block rolls back — parent included — rather than leaving an orphan `hint_read`
row with a truncated or absent hit set. No hit ever survives without its parent, and no parent
ever survives with only some of its hits.

**ISOLATION — the entire recording decision inside one guard, not just the write (G-01, H-05).**
v3's success path recorded *after* the `except` block closed, itself unguarded, and the failure
path recorded *inside* the except block with a bare `raise` after it — so a failing recorder
replaced a successfully-served read's rows with an error, and on the failure path replaced the
caller's own exception (and its exception chain) with whatever the recorder raised. v4's first fix
wrapped the recorder *call* in `try/except`, but left the suppression-state read
(`self._last_rate_limit_receipt_at`) and its write **outside** that inner `try` — and the attribute
was never initialized in `__init__`, and `read_tools.py` had no module-level `logger` at all
(neither `import logging` nor `logger = logging.getLogger(__name__)`, unlike `bus.py:13` and
`consumer_loop.py:13`, which both have one). Both omissions raised **from inside the except block
itself** — the one place clause 2 forbids raising from — substituting an `AttributeError` or
`NameError` for the caller's real exception and setting its `__context__`, which is verbatim what
G-01 exists to prevent, and breaks the standing test G-01 itself cites as motivation
(`test_read_tools_runtime.py:126`, `assert exc.value.__cause__ is None`). The code above (under
"Fix — state and check the property, not the location") moves *all* of that recording machinery
inside one `try/except`, not just the call.

`should_record`'s computation, the call, and the timestamp write are now all inside the single
`try/except Exception` that also guards the recorder call itself — there is no line of recording
machinery left outside a guard, closing the structural gap H-05 named. **The timestamp write
happens as soon as `should_record` is true — on ATTEMPT — and before the recorder call, not after
it (J-02: v5 wrote the timestamp update after the call, so a recorder that raised on every attempt
never advanced it, and the per-class bound never engaged).** `__init__` gains
`self._last_rejection_receipt_at: dict[str, float] = {}` (replacing v4's single
`_last_rate_limit_receipt_at: float | None`, generalised below for H-08); `read_tools.py` gains
`import logging` and a module-level `logger = logging.getLogger(__name__)`, matching `bus.py`'s and
`consumer_loop.py`'s existing pattern. Because the outer `except` block's own recorder call is fully
swallowed before the outer `raise` executes, the bare `raise` re-raises the *original* exception
with its original `__context__`/`__cause__` untouched. Every local attempt — rejected, errored, or
successful — still produces at most one row (below decides the "at most" precisely per rejection
class). An over-cap query is still stored truncated (`query_truncated=true`) even though the read
itself was **rejected**, not served.

**Recording throttled reads without disarming the limiter — now covering every pre-`retrieve`
rejection, not only rate-limiting (G-08, H-08).** `_check_search_allowed()` (`read_tools.py:47-53`)
raises **in-process**, touching no database — that is the point of a limiter — and v4 bounded
recording of *that one* rejection to at most one row per 60s window. But `query too long` and the
missing-`OPENAI_API_KEY` `RuntimeError` both fire **before** `_check_search_allowed()` runs
(`read_tools.py:63-69`, ordering preserved above), so neither ever sets
`rejection_class = "rate_limited"` and neither one was bounded at all (**J-08**: v4's single-window
state only ever keyed on the rate-limit class, and v5's own prose still framed the distinction by
an exception type, `SearchRateLimitExceeded`, that no code in this draft raises, catches, or
inspects — corrected here).

The fix generalises the single timestamp into a **per-rejection-class** bound
(`self._last_rejection_receipt_at: dict[str, float]`, above) rather than hoisting
`_check_search_allowed()` to the top of the function — hoisting would change the exception ordering
`test_read_tools.py:233-238` already asserts (asdk's comparison). Each of the three classes —
`"query_too_long"`, `"missing_api_key"`, `"rate_limited"` — gets its own independent 60-second
window, tracked by the `rejection_class` marker set immediately before the guard that can raise for
that reason (code above): a query-too-long rejection and a rate-limited rejection **in the same
60-second window** each still produce their own row, because they are different classes with
independent state, matching asdk's "one shared 'at most one error row per 60s window per rejection
*class*' bound" recommendation exactly. §6's "not amplification" claim and §9's corresponding row
are corrected to match: the bound now covers all three pre-`retrieve` rejection paths, not one.

**Not addressed here, and not claimed to be (H-08's third, deliberately separate axis).** A genuine
`store.retrieve` failure (a real database error, after every guard has passed) sets
`rejection_class = None` above and is recorded unconditionally, every time — this is codex's "does
not bound failed attempts" axis from H-05/H-08's relation note, and it is a different failure mode
(a broken *database*, not a runaway *caller*) that this draft does not bound. Named, not silently
dropped: see §10.

### The single-writer question — CLOSED-1: verified, holds. F-14 completes the argument.

This gives `hint_read` **two writers**. §4 of the architecture doc's rationale for single-writer is
**embedding coherence** — *"two writers with divergent embedding models put incompatible vectors in
one column and cosine distance silently becomes noise."* `hint_read`/`hint_read_hit` contain no
embedding column and no vector of any kind; the drift failure mode cannot occur in a table with
nothing to drift. **Verification confirmed this holds**, independent of embedding specifics:
`AuditConsumer` (`audit.py:283`), `EvalConsumer` (`eval.py:415`), `TranscriptConsumer`
(`transcript.py:77`), and the MCP host on `mcp_auth.*` (`grants.py:63-73`) are already **four
distinct writer processes** into `arb_memory`. Multi-writer on non-vector tables is the status quo.

**F-14 — the write privilege is enforced by GRANT, not argued in prose alone.** The architecture
doc's single-writer section's bullet 3: *"Preserving single-writer is the in-repo expression of
`control-proves-only-its-path`... the guarantee lives in what's deployed where, not in good
intentions."* Applied here: §9's deny-proof tests assert, by failure code, that only two roles can
write `hint_read`/`hint_read_hit` — the bus consumer's role (`apply_hint_read_consumer_grants`,
§6, new in this fold per G-05) and the local reader's role (`apply_hint_read_local_writer_grants`,
§6, new in this fold per G-03) — and that every other role, **including the public-door role and
`vault_export_role`**, cannot. The guarantee lives in that deployed GRANT state, checked by a test
that fails if it regresses.

The bus writer's idempotency is the deterministic `read_id` plus `ON CONFLICT DO NOTHING`; the
local writer's posture is write-once/no-retry (a crash loses the row rather than duplicating it,
§7) — two different mechanisms achieving two different guarantees (at-least-once vs.
best-effort-once), not one shared "idempotency key."

### Author choices requiring operator adjudication (H-09, H-11)

**H-09 was a 1-vs-2 conflict when v5 wrote this section; round 5's full four-seat review converged
on accepting the narrowed claim below — see the round-5 update at the end of its subsection. It is
kept here for its evidentiary trail, not because it is still contested.** H-11 remains genuinely
contested — **J-12**, one seat blocking — and follows the same discipline the decision record set:
not resolved by counting, each side's evidence recorded, and the choice of *which* minimum
remediation to apply flagged for **operator adjudication**, not settled by this draft's authority.

**H-09 — does the bus record-intent XADD's "fire-and-forget" claim hold for the read loop, not just
the reply?**

- **codex (P1, High):** the current LPUSH-before-XADD ordering protects the *reply already sent*; it
  does not protect the *read loop* from XADD latency, because `ReadLoop.step()` (`bus.py:295-306`)
  handles one entry at a time and cannot `xreadgroup` the next one until `_handle_entry` — which
  calls `handle_read_request`, which calls the inline XADD — returns. Two remediation paths named:
  (a) a bounded non-blocking handoff (a small producer thread/queue between the reply and the XADD),
  or (b) narrow the claimed invariant to what it actually protects and explicitly accept
  subsequent-read head-of-line delay. Either way, any test asserting non-blocking behaviour "must be
  event-controlled (block XADD, prove the loop proceeds), never wall-clock."
- **grok-acp:** "G-11 Yes — Spy counts, not wall clock." **asdk-opus5:** G-11 lands; explicitly
  credits the count-not-timing restatement.
- **Why both are right, per the decision record:** grok and asdk assessed whether the **test** (§9's
  spy-count assertion) is deterministic and can fail on the axis it measures (it is). codex assessed
  whether the **claim the test guards** — "no synchronous call," unqualified — is true (it is not,
  on the read-loop axis). Both assessments are correct simultaneously; they are not competing votes
  on one question.

**Remediation applied in this draft: (b), the narrower claim** (Bus tier, above) — not (a), the
non-blocking handoff. **Not chosen, and why this needed operator sign-off:** implementing a bounded
producer/queue is new concurrency machinery on a lane that has already failed on connection/
transaction mechanics three rounds running (the round-1-P0 history, above); the narrower-claim path
costs nothing beyond wording and is reversible, while the handoff path is exactly the kind of new
surface most likely to introduce the next undetected defect.

**Round 5 update — accepted, 4/4.** All four round-5 seats, including `codex` (who raised H-09
originally), confirmed the narrowed claim above is internally consistent and none re-filed it.
`codex`: *"H-09's narrower claim is internally consistent: it protects the sent reply and explicitly
accepts head-of-line delay for the next read."* `grok-acp`: *"Non-blocking handoff is an operator
product call, not a fold defect. Do not block on H-09 itself."* **H-09 itself is settled; what
remains is the product question this draft never claimed to answer** — build the bounded
non-blocking handoff, or keep the narrower claim as the long-term posture — carried forward at
§11 Q7.

**H-11 — does the local COMMIT guard assert the invariant's own precondition, or a proxy for it?**

- **codex (P1; High on the mismatch, medium on production frequency):** the guard checks
  `getattr(conn, "autocommit", False)`, but the invariant's clause 1 names `IDLE` transaction status
  as the actual precondition. An autocommit connection can still be inside an explicit transaction at
  the moment the guard runs (`build_local_server` accepts arbitrary `conn_factory` injection,
  `local_server.py:10-11`); the guard would pass while the real precondition fails. "The fold
  replaced one location-based assumption with another indirect assumption."
- **grok-acp:** "G-02 Yes — Property assert + fail-safe getattr." **asdk-opus5:** "a real
  location→property conversion" — and verified the fail-safe direction against source: neither
  `FakeConn` fixture (`test_read_tools.py:11-13`, `test_read_tools_runtime.py:15-17`) carries an
  `autocommit` attribute, so `getattr(..., False)` correctly routes both into the guard.
- **Why both are right:** grok/asdk answered "is the rule stated as an assertable property with a
  safe default?" (yes). codex answered "is the property asserted the *same* property the invariant
  names?" (no — `autocommit` is a sufficient, not necessary, condition for `IDLE`).

**Remediation applied in this draft: document `autocommit` as the accepted, verified proxy and why —
now literally in the invariant's own text, immediately under the box (§4, above), not only beside
it (J-11: round 5 found v5's change-table row claimed this and the box itself was byte-identical to
v4; this draft's proxy note is the fix for the row's claim, not a new decision).** Not chosen: switch
the guard to `conn.info.transaction_status == IDLE`. **Not chosen, and why this needs operator
sign-off (J-12, unresolved):** the direct-property check is more correct on codex's exact axis, but
neither existing `FakeConn` fixture models `transaction_status` at all (both were checked against
`autocommit` only, above), so landing it would mean extending both fixtures and every test that
constructs one — real work, not a two-line change, on a fixture surface the record already flags
(H-11's own citations) as the thing three rounds of prior defects were reproduced against. Whether
that engineering cost is worth closing the gap between "safe proxy, verified" and "the exact
property named" is the operator's call, not this draft's.

## 5. Alternatives weighed

**The reads stream already retains the request side.** `bus.py:16` sets `MAXLEN = 10_000`, and
`memory_query` XADDs `{cid, reply, query, k}` — so the last ~10k bus read *requests*, query text
included, are already in Redis, bounded and free. `XLEN` answers "is memory being read at all" for
the bus tier today with no schema change.

It is insufficient for three reasons, and only these three: (a) a 10k horizon with no retention
policy — a busy period silently evicts history; (b) a Redis flush or restart loses it entirely, and
§6 of the architecture doc treats exactly that class of silent loss as the thing to guard; (c) it
holds no **response** side at all — no hits, no ranks, no zero-hit signal, no join. The genuinely
new data here is what came *back*, and four of `hint_read`'s columns duplicating the wire is a fair
price for that join. **It covers only the bus tier**, so it cannot answer the coverage question §2
exists to fix.

**`eval_event_raw` already solves the retention residual.** The eval lane (`schema.sql:101-121`)
has `run_id`/`task_id`/`seat_id`/`payload`/`stream_entry_id UNIQUE`, a **built** purge job
(`run.py:67-73`, `ARB_EVAL_RETENTION_DAYS=30`), a consumer with dead-lettering, its own connection,
and enumerated grants. Recording hint reads as eval events would inherit all of it.

Rejected, but narrowly: the eval lane's payload column carries the comment *"allowlisted metadata
only; raw I/O excluded at the tee"* (`schema.sql:110`). Query text is seat-authored input — closer
to raw I/O than to allowlisted metadata — so putting it there would either violate that contract or
require hashing the query, which destroys the ability to read what was actually asked. What this
design takes instead is the lane's **shape**: consumer, own connection, dead-letter, and a purge
job built now rather than deferred (§8).

## 6. Schema

Appended to `src/arb_memory/schema.sql`.

```sql
CREATE TABLE IF NOT EXISTS hint_read (
    read_id         uuid PRIMARY KEY,          -- deterministic (bus) / uuid4 (local); see below
    door            text NOT NULL CHECK (door IN ('bus', 'local')),
    outcome         text NOT NULL CHECK (outcome IN ('ok', 'error')),
    run_id          text,                      -- NULL on every row in this slice; indexed for forward compat
    seat_id         text,                      -- NULL on every row in this slice; indexed for forward compat
    query_text      text NOT NULL,             -- capped at record time, see below
    query_truncated boolean NOT NULL DEFAULT false,
    k               int  NOT NULL,
    hit_count       int  NOT NULL,             -- 0 is meaningful ONLY when outcome='ok'
    cid             text,                      -- bus correlation only, NOT unique
    stream_entry_id text,                      -- bus tier: the FULLY-QUALIFIED stream key + entry
                                                -- id (H-04), not the bare Redis entry id; belt-
                                                -- and-braces / ops-correlation only, never the
                                                -- dedup mechanism (read_id is). NULL on local rows.
    served_at       timestamptz NOT NULL DEFAULT now(),  -- bus: producer time; local: insert time IS serve time
    UNIQUE (stream_entry_id)
);
CREATE INDEX IF NOT EXISTS hint_read_served_idx ON hint_read (served_at DESC);
-- G-10: both columns are NULL on every row this slice writes (§1, §10). A plain btree still
-- inserts an index entry for a NULL key, so a full-width index here would be two guaranteed-empty
-- writes per row on the highest-volume new table. Partial indexes stay genuinely empty (near-zero
-- cost) until Take 2 plumbs identity, and the column is kept so no migration is needed then.
CREATE INDEX IF NOT EXISTS hint_read_run_idx  ON hint_read (run_id)  WHERE run_id  IS NOT NULL;
CREATE INDEX IF NOT EXISTS hint_read_seat_idx ON hint_read (seat_id) WHERE seat_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS hint_read_door_idx   ON hint_read (door, served_at DESC);

CREATE TABLE IF NOT EXISTS hint_read_hit (
    read_id         uuid NOT NULL REFERENCES hint_read(read_id) ON DELETE CASCADE,
    rank            int  NOT NULL,             -- 1-based, 1 = top result
    hint_id         bigint NOT NULL,           -- NO FK: see below
    withheld        boolean NOT NULL DEFAULT false,
    vector_distance double precision,
    lexical_rank    real,
    PRIMARY KEY (read_id, rank)
);
CREATE INDEX IF NOT EXISTS hint_read_hit_hint_idx ON hint_read_hit (hint_id);

-- G-05: mirrors eval_deadletter (schema.sql:123-137) exactly. stream_entry_id here is likewise
-- the FULLY-QUALIFIED stream key + entry id (H-04) -- the same value deadletter_malformed_hint_read
-- (§4) writes, and _bus_read_id derives read_id from -- not the bare entry id eval_deadletter
-- stores (that table's identically-shaped collision axis is existing precedent, out of scope for
-- this fold). UNIQUE (stream_entry_id) stays single-column: StreamConsumerLoop's generic
-- canary/fallback INSERT (consumer_loop.py:189-194) requires exactly that shape to match its own
-- ON CONFLICT (stream_entry_id) clause. Only the bus tier ever writes here; the local tier has no
-- stream to dead-letter from (§4).
CREATE TABLE IF NOT EXISTS hint_read_deadletter (
    id              bigserial PRIMARY KEY,
    stream_entry_id text,
    raw_entry       jsonb,
    error           text,
    ts              timestamptz NOT NULL DEFAULT now(),
    UNIQUE (stream_entry_id)
);
```

**`read_id` minting (G-09, folds F-03).** Bus rows: `uuid.uuid5(HINT_READ_NAMESPACE,
_qualified_entry_id(prefix, stream_entry_id))` (§4) — deterministic under redelivery of the *same*
entry, and distinct across differently-prefixed deployments sharing one database, closing the
collision axis a bare `uuid5(NAMESPACE, stream_entry_id)` (undefined constant, entry-id-only) would
not have. `HINT_READ_NAMESPACE = uuid.UUID("2f8e6b1a-4b76-4b8e-9a2b-6f6e6a2e6a11")` is a literal,
checked-in constant — pin it once and never change it; changing it would silently change every
future `read_id` derivation. Local rows: `uuid.uuid4()` minted once at write time — no stream, no
redelivery, nothing to derive from. The `stream_entry_id` **column** (distinct from `read_id`
itself) gets the identical fully-qualified treatment for the same reason — H-04, §4.
`_parse_request` (`bus.py:333-338`) only checks the client-supplied `cid` field is *present*, not
unique (`test_bus_pel.py:129-130` already publishes an arbitrary `cid`, `"dead-reader"`), which is
why `cid` was never a safe primary key and remains a plain correlation column.

**`door`, not `tier`.** `local_memory_mcp.py:27` already defines `_TIER_VALUES = ("dev", "prod")`
and selects `ARB_MEMORY_LOCAL_DSN_{flag.upper()}` — a different, pre-existing use of "tier."
Reusing that word for `bus`/`local` collides with load-bearing vocabulary; `door` matches the prose
already in use elsewhere ("Public MCP door," §2).

**`served_at`.** For bus rows, this is populated from the producer timestamp captured in
`handle_read_request` at reply time and carried in the record-intent stream (§4), **not** left to
the column's `DEFAULT now()` — a lagging consumer would otherwise stamp every row with its own
catch-up time instead of when the read actually happened. For local rows, `DEFAULT now()` is
correct as-is: the write happens inline at serve time, so insert time genuinely *is* serve time —
no queue sits between them.

**`outcome` exists because the error path is real, and also covers local rejections.**
`bus.py:250-254`: when `store.retrieve` raises, the handler builds `{"status": "error"}` with no
`hits`, then replies regardless — recording that as `hit_count=0` would make a crashed read
indistinguishable from a genuine empty result. On the local tier, the same `outcome='error'` value
also covers the three pre-`retrieve` rejection points (§4) — query-too-long, missing API key,
rate-limited — **each independently bounded to at most one row per 60-second window (H-08)** — so a
burst of any one of the three is visible as a bounded burst of `error` rows, not silence and not
amplification. (A genuine `store.retrieve` failure after every guard passes is recorded
unconditionally and is deliberately not part of this bound — §4, §10.)

**No FK on `hint_id`, deliberately (CLOSED-3 — do not reopen).** `scripts/arb-memory-seat-e2e` does
a real bus read and then `cleanup_rows` hard-deletes those hints inside a single transaction; an FK
raises `23503`, rolls back both deletes, and strands rows in the live database. The in-repo
precedent agrees: `audit_events`, `transcript_io`, and `eval_event_raw` all store subject ids
unconstrained. Cost, stated precisely in §1: a hard-deleted hint's row survives but is
unreconstructable.

**`query_text` is capped at record time**, meaning in `handle_read_request` before the XADD (bus)
or at the top of `memory_search` before the guard checks (local) — to `search_max_query_chars`
(2000, `mcp/config.py:22`). Truncation sets `query_truncated` so a truncated query is never
mistaken for a short one. Screening beyond length is a named residual (§10).

**`withheld` is sourced from `store.retrieve`, not inferred.** `store.py:311` previously computed
`withhold` as a local variable and discarded it; `store.py:314`'s return dict omitted it, so a
consumer could only infer `withheld = (artefact is None)` — also true whenever a hint simply has no
artefact, conflating the two. `store.retrieve` now returns `{"hint", "artefact", "repo_pointer",
"withheld"}` per item.

**Grants — three functions, one dedicated table each, restructured from v3 (G-03, G-05, G-07).**

`apply_local_reader_grants` (`grants.py:6-54`) isolates every table it knows about via a
hand-enumerated `REVOKE ALL ... FROM <role>` **tuple loop**; `hint_read`/`hint_read_hit` are already
in that tuple, and **`hint_read_deadletter` joins it in this fold (H-06)**. `apply_mcp_grants`
(`grants.py:57-121`) isolates the same set of tables, but through **discrete, grouped
`REVOKE ALL ON {}, {} FROM <role>` statements, not a tuple loop (J-09 — v5's prose described both
functions as sharing one shape; they do not, and an implementer following the old wording into
`apply_mcp_grants` would look for a loop that isn't there)** — `hint_read_deadletter` joins its
existing grouped-statement set the same way. v4 added the first two tables to both functions but
left the dead-letter table out of either, which meant §9's own "local reader and public-door roles
cannot touch `hint_read_deadletter`" row proved nothing on a clean install (§9, below, fixes the
test itself too).

**Also in this fold (G-07): both functions gain `REVOKE ALL ON hint_read, hint_read_hit,
hint_read_deadletter FROM PUBLIC`, mirroring `apply_eval_grants` (`grants.py:250-254`) —
`hint_read_deadletter` joins this line in this fold too (J-06, executed).** Without the line at all,
an ambient `GRANT ... TO PUBLIC` (exactly what `test_eval_grants.py:141-142`'s deny-proof pattern
seeds before asserting revocation) would leave excluded roles holding access inherited through
`PUBLIC` regardless of the per-role revoke, and the deny-proof would pass without proving anything.
**v5 argued `hint_read_deadletter`'s own `PUBLIC` exposure was already closed independently by
`apply_hint_read_consumer_grants`'s own `REVOKE ALL ... FROM PUBLIC` (below) and left it out of this
line — round 5's executed §9 pass seeded ambient PUBLIC, applied *only* the two isolating functions
below (exactly the check-1127 scoping §9 itself specifies), and found the door role still holding
both `INSERT` and `SELECT` on `hint_read_deadletter` through `PUBLIC`.** A per-role `REVOKE` cannot
remove a privilege a function never itself revoked from `PUBLIC`. Duplicating the line here — rather
than relying on `apply_hint_read_consumer_grants` having already run first — closes the gap under
both the full `run_grants()` sequence and the narrower one §9 exercises:

```python
# grants.py, added to apply_local_reader_grants AND apply_mcp_grants, before each function's
# existing per-role REVOKE ALL loop. hint_read_deadletter joins this line in this fold (J-06):
conn.execute(
    sql.SQL("REVOKE ALL ON {}, {}, {} FROM PUBLIC").format(
        sql.Identifier(schema, "hint_read"), sql.Identifier(schema, "hint_read_hit"),
        sql.Identifier(schema, "hint_read_deadletter"),
    )
)
```

**The INSERT-only grant moves to its own function (G-03).** v3 put `GRANT INSERT ON hint_read,
hint_read_hit` inside `apply_local_reader_grants` itself — but `run.py:368-371` calls that same
function for **two** roles, `local_reader_role` **and** `vault_export_role`:

```python
if local_reader_role:
    apply_local_reader_grants(conn, local_reader_role)
if vault_export_role:
    apply_local_reader_grants(conn, vault_export_role)
```

Putting the write grant inside the shared function creates a **third** writer of `hint_read` — one
this design's own §4 single-writer argument says does not exist. The fix is a dedicated function,
applied to `local_reader_role` only:

```python
def apply_hint_read_local_writer_grants(conn, role: str) -> None:
    """INSERT-only on hint_read/hint_read_hit, for the local-MCP reader role — and ONLY that
    role. Deliberately NOT folded into apply_local_reader_grants, which run.py also applies to
    vault_export_role; doing so there would create a third hint_read writer (G-03)."""
    schema = conn.execute("SELECT current_schema()").fetchone()[0]
    role_ident = sql.Identifier(role)
    tables = sql.SQL(", ").join(
        [sql.Identifier(schema, "hint_read"), sql.Identifier(schema, "hint_read_hit")]
    )
    conn.execute(sql.SQL("REVOKE ALL ON {} FROM PUBLIC").format(tables))
    conn.execute(sql.SQL("GRANT INSERT ON {} TO {}").format(tables, role_ident))
    conn.execute(sql.SQL("REVOKE SELECT, UPDATE, DELETE ON {} FROM {}").format(tables, role_ident))
```

```python
# run.py, run_grants():
if local_reader_role:
    apply_local_reader_grants(conn, local_reader_role)
    apply_hint_read_local_writer_grants(conn, local_reader_role)
if vault_export_role:
    apply_local_reader_grants(conn, vault_export_role)   # unchanged: no hint_read access
```

No `SELECT`: the local reader can write its own read-receipts but must not read the aggregate table
back — `SELECT` would let a compromised local reader token enumerate cross-seat query history
through the very table meant to observe it, not leak it. `apply_mcp_grants` (the public-door role)
gets **neither** `INSERT` nor `SELECT` — the door is excluded from this feature entirely (§2).

**The bus consumer's own write grant, and the sequence grant v4 omitted (surfaced while specifying
G-05's "its grants" requirement; the sequence half is H-01).** No function in v3 granted the bus
consumer role write access to `hint_read`/`hint_read_hit`/`hint_read_deadletter` at all. In the
common case this is silently harmless — `run_grants()`'s `consumer_role` defaults to
`conn.info.user` (`run.py:330`, `"ARB_EVAL_CONSUMER_ROLE" or conn.info.user`), the schema owner, who
already holds every privilege on its own objects regardless of REVOKE — but the moment a deployment
sets `ARB_EVAL_CONSUMER_ROLE` to a distinct, lower-privilege role for defense-in-depth (the same
posture `apply_eval_grants` exists to support), `HintReadConsumer` — which shares that role, since
it and `EvalConsumer` both connect via `_memory_conn()` / `ARB_MEMORY_DSN`, `run.py:18-21`, `328-330`
— would fail every INSERT with `42501`. **v4 fixed the table-level grant but not the sequence one
(H-01):** `hint_read_deadletter.id` is `bigserial`, which creates `hint_read_deadletter_id_seq`;
Postgres does not grant sequences anything to `PUBLIC` by default, and `nextval()` requires
`USAGE`. `apply_eval_grants` already handles the identical shape for `eval_deadletter_id_seq`
(`grants.py:307-312`) — v4's `apply_hint_read_consumer_grants` never mirrored that half:

```python
def apply_hint_read_consumer_grants(conn, role: str) -> None:
    """SELECT+INSERT for HintReadConsumer's role, mirroring apply_eval_grants's shape for
    eval_event_raw/eval_deadletter (grants.py:246-323) -- INCLUDING the sequence grant v4 omitted
    (H-01): apply_eval_grants grants USAGE on eval_deadletter_id_seq (grants.py:307-312) for
    exactly this reason -- bigserial's nextval() needs SEQUENCE USAGE, which PUBLIC does not carry
    by default, and hint_read_deadletter.id is bigserial too. Applied to the SAME role as
    apply_eval_grants -- HintReadConsumer and EvalConsumer share the bus-consumer identity."""
    schema = conn.execute("SELECT current_schema()").fetchone()[0]
    role_ident = sql.Identifier(role)
    tables = sql.SQL(", ").join([
        sql.Identifier(schema, "hint_read"),
        sql.Identifier(schema, "hint_read_hit"),
        sql.Identifier(schema, "hint_read_deadletter"),
    ])
    seq = sql.Identifier(schema, "hint_read_deadletter_id_seq")
    conn.execute(sql.SQL("REVOKE ALL ON {} FROM PUBLIC").format(tables))
    conn.execute(sql.SQL("REVOKE ALL ON SEQUENCE {} FROM PUBLIC").format(seq))
    conn.execute(sql.SQL("GRANT SELECT, INSERT ON {} TO {}").format(tables, role_ident))
    conn.execute(sql.SQL("REVOKE UPDATE, DELETE ON {} FROM {}").format(tables, role_ident))
    conn.execute(sql.SQL("GRANT USAGE ON SEQUENCE {} TO {}").format(seq, role_ident))
```

```python
# run.py, run_grants(), immediately after apply_eval_grants(conn, consumer_role):
apply_eval_grants(conn, consumer_role)
apply_hint_read_consumer_grants(conn, consumer_role)
```

**Consequence chain, uncorroborated part contributed by asdk alone but not contradicted (H-01).**
Without the sequence grant, `_canary_deadletter_sink()` returns `False` on any exception
(`consumer_loop.py:189-194`), so `_deadletter_failed` (`:218-222`) sets `_deadletter_sink_open =
True`, clears the poison map, and the consumer wedges in a permanent `deadletter-sink-poison` retry
loop rather than dead-lettering — under the one configuration `hint_read_deadletter` exists to
support, G-05's headline claim ("dead-lettering is provisioned, not merely promised") does not
hold. Three seats independently required the fixture role in §9's grant test to be a real,
distinct, non-owner role; the sequence gap is invisible under the schema-owner default every prior
round's test ran as.

## 7. Failure posture

- **Recording is isolated from the read, not absent (H-14 — the original heading, "read path does
  no database work," overstated the local case: local *does* write to the database, on the guarded
  path below, just never in a way the read's own outcome depends on).** The bus tier XADDs after
  `lpush`; the local tier's write wraps the guard checks and `store.retrieve` on the already-cached
  connection. This is RECEIPT INVARIANT clause 2 (ISOLATION), stated per tier: on the bus tier it
  holds at the scope named in §4 (the reply, not necessarily this `ReadLoop`'s next request — H-09);
  on the local tier it holds because the entire recording decision, not only the write, sits inside
  one guard at both call sites (G-01, H-05, §4).
- **Recording failure never fails a read.** The bus producer wraps the emit in `try/except Exception`
  (§4, **J-10** — v5 claimed this and never wrote the guard); on exception it is logged via
  `logger.exception(...)`, not counted (no metrics counter is specified anywhere in this slice — the
  earlier wording overclaimed one), and never blocks the reply, which has already been sent by the
  time the XADD runs. The local tier's guard does the equivalent (§4). Bounded and observable, not
  silent.
- **A malformed or exhausted bus entry is preserved, not dropped (G-05).** `hint_read_deadletter`
  (§6) receives it via `deadletter_malformed_hint_read` (§4), inside its own
  `with conn.transaction():`, before `_ack` — mirroring the commit-then-ack ordering established
  next for the parent table. A dead-lettered entry is never silently discarded, and it is never
  double-dead-lettered on redelivery of the same entry (`UNIQUE (stream_entry_id)` over the
  fully-qualified value, H-04) — nor, now, falsely treated as a collision with an unrelated entry
  from a differently-prefixed deployment sharing the same database.
- **Loss posture.** `HintReadSink.write` (§4; the actual write SQL H-10 found missing, which
  commits, mirroring `eval.py:499`) runs **before** `self._ack` (`eval.py:533`) — **commit-then-ack**
  — so a crash before ack leaves the entry in the PEL, which redelivers it, and (per §4's `read_id`
  derivation) redelivery resolves to the same idempotent `"duplicate"` outcome rather than a
  duplicate row or a lost one. The only genuine bus-side loss window is upstream of the consumer
  entirely: an undrained record-intent lost to a Redis flush or restart before `HintReadConsumer`
  ever reads it (bounded by the G-04 `maxlen`, so at worst the *oldest* 10k entries are what's
  exposed to that window, never unbounded backlog). That is a message-bus loss, not a commit/ack
  race, and it is the accepted posture — a lost receipt is a shrug; neither loss window can stall a
  seat, which is the property that matters, though H-09 (§4) narrows exactly how strongly "cannot
  stall a seat" is claimed for a *subsequent* read on the same `ReadLoop`. The local tier's posture
  is different: a crash before its single inline commit genuinely loses that one row (no stream, no
  redelivery), which remains the accepted posture there. A partial write (parent committed, some
  hits missing, or vice versa) is **not** a local-tier loss mode this design accepts silently — G-06's
  `with conn.transaction():` wrap means the commit is all-or-nothing for parent + hits together, and
  the bus tier gets the identical all-or-nothing property from `HintReadSink.write`'s own
  transaction (§4).

## 8. Retention — built in this slice, not deferred

§2 of the architecture doc warns that unbounded growth *"silently balloons the one DB everything
depends on,"* and these tables grow with every read on the higher-volume tier — so deferring it was
the wrong call. `run_hint_read_purge()` mirrors `run_eval_purge()` (`run.py:67-73`) with
`ARB_HINT_READ_RETENTION_DAYS`, default **30**, matching the eval lane, and is wired to
`python -m arb_memory hint-read-purge` (§4, H-12) rather than left importable-only.
`ON DELETE CASCADE` on `hint_read_hit` means purging parents clears hits — **verified safe
(CLOSED-2, do not reopen):** `PRIMARY KEY (read_id, rank)` puts `read_id` leading in the btree, so
the cascade's RI lookup is indexed, and `eval.purge_expired` (`eval.py:564-583`) already batches at
10000 rows inside a per-batch transaction, a shape this purge job copies directly.

**`hint_read_deadletter` is deliberately NOT purged by this job**, matching the existing precedent:
`purge_expired` (`eval.py:564-583`) deletes only from `eval_event_raw`, never `eval_deadletter`.
Dead-letters are expected to be rare (poison/malformed entries only) and are kept for operator
investigation rather than aged out automatically.

**Grants corollary.** `apply_retention_grants` (`grants.py:325-344`) grants `DELETE`+`SELECT` on
`eval_event_raw`/`transcript_io` to the retention role via the same enumerated-list pattern. It
must also gain `hint_read` in that per-table loop, or `run_hint_read_purge()` fails `42501` on its
first `DELETE`. `hint_read_hit` needs no entry of its own (`ON DELETE CASCADE` means only the
parent is ever directly deleted from). `hint_read_deadletter` needs no entry either, for the same
reason `eval_deadletter` has none — it is not purged by this job at all.

## 9. Testing

Every test names the behaviour whose breakage it would catch. **H-03 is folded throughout this
section — three seats independently found that several tests below did not exercise the behaviour
they were labelled as tripwires for. Every row marked (H-03 fix) replaces a v4 row that could not
fail on the defect it named.**

### The invariant tests — one shape, both tiers (structural, per the round-3 directive)

These come first because everything else in this section is a per-mechanism elaboration of them,
not a replacement for them.

| Test | Fails if |
|---|---|
| **(a) COMMIT, bus:** a record-intent entry processed by `HintReadConsumer` produces a `hint_read` row visible from a **second**, independent connection | The round-1 P0 regresses on the bus tier — the row never commits |
| **(a) COMMIT, local:** `_record_local_read` on the production-shape (autocommit) connection produces a `hint_read` row visible from a **second**, independent connection | The round-1 P0 regresses on the local tier |
| **(b) ISOLATION, bus:** forcing the record-intent XADD to raise (a spy `redis` client whose `xadd` raises) leaves `handle_read_request`'s reply (`lpush` payload) byte-identical to a run with no forced failure, and raises nothing to `handle_read_request`'s own caller | A recording failure on the bus tier reaches the reply the caller is waiting on |
| **(b) ISOLATION, local (H-03 fix — was one forcing mechanism, now two):** forcing a recording failure two independent ways — **(i)** a **non-autocommit** injected `conn_factory` (G-02's precondition failure), and **(ii)** a spy connection whose `execute()` raises on the `hint_read` INSERT itself, exercised at least once through the **rejected/rate-limited** path so the `should_record` computation is inside the forced failure, not just the write — leaves `memory_search`'s return value (success case) or raised exception, type, message, and `__cause__`/`__context__` (rejection/error case) identical to a run where recording is a no-op, in every one of the (i)/(ii) combinations | Either recorder call site regresses to an unguarded state (G-01), the autocommit precondition stops being checked (G-02), or the H-05 fix regresses to guarding only the DB write and not the whole recording decision |

**The test whose *absence*, not failure, let H-02 ship (H-03's headline fix).** Every test above and
in v4's §9 either called `_record_local_read` directly with a hand-built, already-correctly-shaped
`hits` tuple, or asserted something about `memory_search`'s *return value* — which was never wrong,
because it comes straight from `store.retrieve`, untouched by the recorder's bug. **No test called
the real, public `memory_search` method and inspected the row it actually wrote.** Added:

| Test | Fails if |
|---|---|
| **A successful `memory_search()` call, through the public method end-to-end (not `_record_local_read` called directly), produces exactly one `hint_read` row and one `hint_read_hit` row per returned hit, with `hint_read_hit.hint_id` NON-NULL and `hint_read_hit.vector_distance` NON-NULL** | The hit-shape nesting regresses (H-02): `hit["hint"]["id"]` misread as `hit["hint_id"]` raises `KeyError` inside the transaction (rolled back whole, per G-06, and swallowed by the guard, per G-01) — zero rows, forever, on the success path, while `memory_search` keeps returning correct results; or `vector_distance`/`lexical_rank` misread via `.get()` on the wrong level silently record `NULL` forever |
| **A record-intent event built exactly as `handle_read_request` would emit it — flat `hits`, stringy Redis values, the `query` field name, not a hand-built `event` dict — parsed via `_parse_hint_read_event` and written via `HintReadSink.write`, produces exactly one `hint_read` row and one `hint_read_hit` row per hit, with `hint_id` AND `vector_distance` both NON-NULL (J-03, mirrors the row above on the bus tier)** | The bus tier's parser or sink regresses to expecting `store.retrieve`'s nested shape again (J-01 reopens), or any bus-tier test — including the redelivery tests below — keeps driving `HintReadSink.write` with a hand-built nested `event` and stays green while production XADD emits a flat one, the exact gap that let J-01 ship under v5's own suite |

**Every bus-tier row below that exercises `HintReadSink.write` must build its event via
`_parse_hint_read_event` from stringy, flat, wire-shaped fields — mirroring what
`handle_read_request`'s XADD actually emits — never a hand-built nested `event` dict (J-01/J-03: v5's
own redelivery tests used a pre-formed nested event and stayed green while production XADD emitted
something the real consumer could not parse; the row above is the specific tripwire this closes, and
this rule closes it for every row that follows too).**

### Per-tier and per-finding tests

| Test | Fails if |
|---|---|
| Redelivered stream entry does not double-record, and the row survives (is not dead-lettered) | The `read_id` derivation breaks under PEL redelivery |
| **A clean redelivery of an already-recorded bus entry (same entry, nothing changed) produces `HintReadSink.write`'s `"duplicate"` outcome, inserts no additional `hint_read_hit` rows, and is NOT dead-lettered (H-10)** | The parent+hits write SQL treats a clean redelivery as a failure — the exact gap H-10 traced: a good receipt would be dead-lettered by mistake because the `hint_read_hit` insert collides on `PRIMARY KEY (read_id, rank)` after the parent's `ON CONFLICT DO NOTHING` silently no-ops |
| **Two deployments with different `ARB_MEMORY_PREFIX` values processing an entry with the SAME Redis-assigned id perform their real `hint_read` INSERTs (not just a comparison of derived uuid5 strings) and both succeed, producing two distinct rows with two distinct `read_id`s and two distinct `stream_entry_id` values (G-09, H-03 fix, doubles as H-04's regression test)** | The namespace/derivation collision axis reopens on `read_id` (G-09), or the belt-and-braces `stream_entry_id` column's `UNIQUE` constraint collides across deployments even though `read_id` does not (H-04) — the v4 version of this test compared derived strings only and never touched the database, so a real `UNIQUE` violation on `stream_entry_id` would not have been caught by it |
| Consumer commit precedes ack; killing the process between them leaves the row in the PEL, and redelivery reaches the same idempotent end state | Commit/ack ordering regresses, or redelivery loses or duplicates the record |
| Consumer tests run at **`autocommit=False`**, production-equivalent | The fixture masks transaction semantics (v1's flaw) |
| A malformed record-intent entry is written to `hint_read_deadletter` **before** `_ack`, with `stream_entry_id` equal to the fully-qualified key (H-04), and is not silently dropped (G-05) | The five-times-promised dead-lettering stays unprovisioned, commit precedes ack only for the happy path, or `deadletter_malformed_hint_read` regresses to writing the bare entry id |
| Redelivery of an already-dead-lettered entry (same deployment, same entry) does not produce a second `hint_read_deadletter` row (G-05) | `UNIQUE (stream_entry_id)` on the dead-letter table is not honoured |
| **Local tier reuses the cached connection for recording** — assert no second `psycopg.connect` call is made per read | The mandate reverts to a fresh connection per read, reintroducing latency/connection-churn cost |
| **A forced failure on the SECOND `hint_read_hit` insert rolls back the PARENT `hint_read` row too, on the local tier** (G-06) | Parent and hits are not committed atomically — an orphan parent with a partial or missing hit set survives |
| **Local tier records every rejected/errored read** — query-too-long, missing key, rate-limited (each independently bounded, below), and a genuine handler exception (unbounded, deliberately — §4, §10) all produce `outcome='error', hit_count=0` | A guard-path rejection bypasses recording |
| **Repeated rejections of the SAME class within one 60s window produce at most ONE `outcome='error'` row for that class; a rejection of a DIFFERENT class in the same window produces its own row; a rejection in the NEXT window produces a second (G-08, H-08, H-03 fix)** — asserted two ways: **(1)** row-count, as v4 had, and **(2)** a spy wrapping the actual INSERT call asserts **attempts ≤ 1 per class per window even when every attempt raises** — not merely that *rows* stay ≤1, since a recorder that raises on every call yields **zero** rows regardless of the bound (**J-02 fix**: v5's own wording here contradicted its own mechanism, which advanced the timestamp only after a successful `_record_local_read` call — fixed in §4, above) | Either the limiter's teeth are removed again (unbounded rows, on any of the three classes), the per-class independence collapses into one shared window, or (assertion 2) the attempt count exceeds one per class per window — meaning the bound only holds when persistence happens to succeed, the exact gap J-02 found in v5's mismatched wording |
| **Over-long query stored truncated, `query_truncated=true`, `query_text` length equals the cap — asserted on BOTH doors, not bus-only** (G-12) | The local tier's own cap-before-reject path (§4) ships untested; marking it bus-only would pass vacuously without exercising local truncation at all |
| Deny-proof seeds ambient PUBLIC grants first (`test_eval_grants.py:141-142` pattern: `GRANT ... TO PUBLIC` then `apply_*_grants`), then proves revocation, **for `hint_read`/`hint_read_hit` specifically** (G-07) | The proof passes on clean-install defaults and proves nothing — an ambient PUBLIC grant survives an unrevoked-from-PUBLIC apply path |
| **Local reader role can `INSERT` into `hint_read`/`hint_read_hit` but CANNOT `SELECT` from them** | Either the write grant is missing (recording fails `42501`) or the read grant was mistakenly also opened |
| **`vault_export_role` cannot `INSERT` into `hint_read`/`hint_read_hit`** (G-03) | `apply_hint_read_local_writer_grants` was folded back into the shared function, recreating the third writer |
| **The bus consumer role, granted via a real, non-owner `CREATE ROLE` and `SET ROLE` (precedent: `test_eval_grants.py:118-129`) — not the schema-owner default — can `INSERT`/`SELECT` on `hint_read`/`hint_read_hit`/`hint_read_deadletter`, AND can `nextval()` `hint_read_deadletter_id_seq` (asserted directly with a real dead-letter `INSERT`, not just a privilege lookup) (G-05 grants, H-01, H-03 fix)** | `apply_hint_read_consumer_grants` was never wired into `run_grants()`, its scope leaked, or — the specific gap H-01 found — the sequence grant is missing and every dead-letter `INSERT` fails `42501` the moment the consumer role is distinct from the schema owner, which the v4 version of this test (run as the owner, the default) could not detect |
| **Public-door role, under the same real non-owner-role pattern as above, has neither `INSERT` nor `SELECT` on `hint_read`/`hint_read_hit`/`hint_read_deadletter` (H-03, H-06, J-06 fix)** | The door's exclusion (§2) is not actually enforced by GRANT — the v4 version of this test passed on clean-install defaults (the fixture role already being the schema owner), which is the identical "proves nothing" shape v4 itself names and fixes for the other two grant functions |
| **Local-reader role, same non-owner-role pattern, has no access at all to `hint_read_deadletter` and no `SELECT` on `hint_read`/`hint_read_hit` (its `INSERT` on those two is asserted by the row above and is by design, J-06)** | The G-03 writer grant leaked into `SELECT`, or `hint_read_deadletter` access was not actually excluded for the local reader — J-06's executed pass found the previous single combined row could not pass against a correct implementation because it contradicted the row above |
| **Local reader still cannot write `hints`/`artefacts`** | The grant widening escaped its intended scope |
| Genuine empty result records `outcome='ok'`, `hit_count=0` | The zero-hit signal is lost |
| **Recording issues exactly one Redis command (the record-intent XADD) and zero DB round-trips beyond what the read itself already performs, measured with a spy `redis` client and a spy connection, counted, not timed — scope note (H-03, H-09): this asserts call COUNT only; it does not and cannot assert that the call does not BLOCK the read loop's next iteration, which §4's narrowed claim (H-09, accepted round 5) now states explicitly rather than implying this test covers it (G-11)** | A future change makes bus-tier recording synchronous DB work, or more than one Redis round-trip, instead of a single fire-and-forget XADD |
| `run_id`/`seat_id` are `NULL` on every row this slice writes, on both tiers | A row is written with non-NULL identity without the plumbing that would make that value trustworthy |
| **A partial index exists on `run_id` and on `seat_id`, asserted via `pg_indexes.indexdef LIKE '%WHERE (run_id IS NOT NULL)%'` (and the `seat_id` equivalent) — parenthesised, because Postgres's catalog reconstruction of a partial-index predicate always wraps the `NullTest` in parentheses, confirmed by executing this exact predicate against a live PostgreSQL 17.7 instance in both the partial and full-width states (G-10, H-13, J-05 fix)** | The index was shipped full-width instead of partial, or the partial predicate doesn't match what's actually written — a row-count assertion of "zero rows" needs `pageinspect` or an unreliable post-`ANALYZE` `pg_class.reltuples` read to be trustworthy at all; the parenthesised predicate is deterministic and was confirmed, by execution, to fire when the index ships full-width and stay silent when it doesn't (the unparenthesised v5 literal matched nothing in either state — the exact "cannot fail on the defect it names" shape this section exists to close) |
| `door` correctly distinguishes bus from local rows | Door bias becomes invisible again |
| `withheld` on a hit matches `store.retrieve`'s returned value, not `artefact is None` | The two are conflated again for a hint with no artefact at all |
| **`schema.sql` applied twice against the same scratch DB inside one test succeeds and leaves an identical schema state, including `hint_read_deadletter`** | The DDL is not actually re-appliable |
| `run_hint_read_purge()` retention role can `DELETE`+`SELECT` on `hint_read`, mirroring the eval retention deny-proof | `apply_retention_grants` was not extended, purge fails `42501` |
| `run_hint_read_purge()` never deletes from `hint_read_deadletter` | The deadletter-is-not-purged posture (§8) silently changes |

The deny-proof asserts the specific failure code, never a bare refusal
(`docs/defect-classes/refusal-is-ambient-assert-the-code.md`).

**Verification note, carried forward.** The panel's verify command (`pytest
tests/arb_memory/test_schema.py`) reported **4 skipped** for want of `ARB_MEMORY_DSN` in every
seat across all five rounds now — no schema behaviour has been exercised through that command by
anyone yet. **Round 5 is the first round anyone exercised real behaviour at all: a seat's independent
§9 execution pass** (recorded in a separate execution report)
**ran 11 of the 32 named §9 rows against a live PostgreSQL 17.7 instance, outside the nominal
command — 9 passed, 2 failed (J-05, and J-06's PUBLIC-inheritance half), both fixed in this fold
from the execution's own findings, not from re-reading.** Any claim that these tests pass must show
they **ran**, not that they were collected.

## 10. Named residuals

- **The public door stays unmeasured**, in exchange for its SELECT-only credential and its GRANT
  exclusion. **Visible in the data only as an absence — no `hint_read` row correlates with a given
  public-door read at all (H-15: `door` is `NOT NULL`, §6, so there is no row with a missing `door`
  value to find; v4's "visible... as an absent `door` value" described a state the schema makes
  impossible).**
- **`run_id`/`seat_id` are NULL on every row in this slice, both tiers.** Neither `client.py` (bus)
  nor `read_tools.py` (local) currently plumbs seat/run identity into a memory read. The columns
  and their now-partial indexes (G-10) exist for forward compatibility; per-seat/per-run
  breakdowns require identity plumbing this design does not add, and remain a Take 2 candidate.
- **Suppressed rate-limit rejections are not counted anywhere (G-08).** Bounding recording to one
  row per 60s window **per rejection class (H-08 — the bound now also covers query-too-long and
  missing-API-key, not only rate-limiting)** prevents INSERT amplification, but the number of calls
  suppressed within that window is not itself persisted — only `logger`-level visibility exists for
  it, if any is added. Accepted here because the liveness signal ("this seat is being throttled, or
  misconfigured, or sending oversized queries") survives on the one row per class that does get
  written; a precise suppressed-count would need its own aggregate row or column, which is more
  schema than this residual currently earns.
- **A genuine `store.retrieve` failure, after every rejection guard has passed, is recorded
  unconditionally on every call (H-08's deliberately separate third axis, §4).** This is not the
  amplification risk the per-class bound addresses — a runaway *caller* vs. a broken *database* —
  but an unbounded row count under a sustained database outage is itself a residual worth naming:
  this design accepts it rather than adding a fourth bound, because a database outage is already
  visible through every other consumer of that database, and bounding it risks hiding the one
  signal ("every call is now erroring") an operator needs during that outage.
- **Query screening beyond length.** Length is capped (and, on local, enforced by rejection);
  content is not classified. `store.py:307-312`'s `learn_proposal` withholding is adjacent but
  governs delivery, not persistence.
- **Cross-store local reads.** `local_read_policy.py:19-20` permits
  `ARB_MEMORY_LOCAL_ALLOW_CROSS_STORE=1`, under which the local reader's DSN points at a store
  other than `ARB_MEMORY_DSN`. Under that flag, local `hint_read` rows land in a **different**
  database than the bus tier's rows — "one table, both doors, joinable" (§2) holds only within a
  single store. This is a deliberate escape hatch (dev mirrors, read-only replicas), not a defect,
  but a deployment running it must know its local and bus `hint_read` rows do not join.
- **Two writers on `hint_read`.** Verified safe on the embedding-coherence axis and GRANT-enforced
  on the control-proves-only-its-path axis (F-14, CLOSED-1). Not itself a residual risk, kept here
  only as a pointer to where the argument lives (§4).
- **Hard-deleted hints leave unreconstructable rows.** Stated precisely in §1; not fixed by a
  snapshot column in this slice, deliberately, to avoid growing per-read write cost for a residual
  this narrow.
- **No relevance signal**, therefore no quality measurement. §1 states this rather than implying
  otherwise.
- **H-09's narrowed claim is no longer contested (round 5, 4/4), but the product question it defers
  (§11 Q7) is still open. H-11's accepted proxy remains contested (J-12) — the invariant now states
  the proxy correctly (J-11), but whether the proxy is the right long-term property is not
  resolved.** See §4's "Author choices requiring operator adjudication" for both, and §11 for the
  question each poses to the next panel.

## 11. Questions for the next panel

1. Is 30 days the right retention default, or does the higher-volume local tier need a shorter
   window than the bus tier?
2. Does `query_truncated` plus a 2000-char cap (bus) / reject-over-cap (local) adequately address
   the screening concern, or is storing seat-authored text at all the wrong call — should it be
   hashed instead?
3. `ARB_HINT_READ_RETENTION_DAYS` purges the very telemetry §3 forbids using for deletion
   decisions. Does the retention window create an audit gap, where a hint-deletion decision cites
   "never served" and, by the time anyone checks, the supporting or refuting `hint_read` rows have
   already been purged? **Round 5 (J-04): two of three addressing seats file this at P1; the third
   calls it "a free win left on the table two rounds running." All three propose the identical
   one-line §3 remedy — any evidence artifact citing served-hint statistics must snapshot the
   supporting rows or aggregate, and the window bounds, at the time of the claim, including any
   period already purged — but §3's rule is co-signed at MUST strength, so adding to it is a
   constitution-layer call this draft does not make unilaterally.**
4. Should local-tier reads carry seat identity — a new env var plumbed through
   `LOCAL_MEMORY_MCP_ENV_KEYS` → `LocalReadSettings` → the row — so `hint_read_seat_idx` becomes
   meaningful on the local tier? Deliberately deferred here as its own scope-growing change; a
   Take 2 candidate, and one that should also settle whether the bus tier gets equivalent plumbing
   through `client.py` at the same time.
5. Is the local reader's `INSERT`-without-`SELECT` grant the right shape long-term, or will a
   future legitimate consumer (e.g., a per-seat "what have I searched for" tool) need read-back,
   requiring a narrower row-level policy instead of a blanket table grant?
6. G-08's bound ("one row per 60s rate-limit window per process") relies on one `ReadMemoryTools`
   instance mapping to one seat. If a future deployment shape puts more than one seat behind a
   single local-MCP process, this bound silently stops being a per-seat bound — worth a named
   assumption check before that shape is built, not after.
7. **H-09 — settled as recorded, round 5 (§4).** The only open item is the product question: should
   the record-intent XADD get a bounded non-blocking handoff (a producer thread/queue between the
   reply and the XADD), or does keeping the narrowed claim as the long-term posture fully discharge
   it? Not a spec defect either way.
8. **H-11 — contested, J-12, unresolved (§4).** Should the local COMMIT guard check
   `conn.info.transaction_status == IDLE` directly, or is the `autocommit` proxy — now stated in the
   invariant's own text (J-11) — sufficient long-term? Landing the direct check requires extending
   both `FakeConn` fixtures, which neither currently models.
9. **New, from round 5's executed §9 pass.** G-10's partial-index check now asserts
   `pg_indexes.indexdef LIKE '%WHERE (run_id IS NOT NULL)%'` (J-05, fixed this fold). The same
   execution found the catalog form, `pg_index.indpred IS NOT NULL`, discriminates identically and
   does not depend on `pg_get_indexdef`'s text formatting — the exact brittleness that produced J-05.
   Should the next fold switch to the catalog form for robustness, or is the parenthesised `LIKE`
   (matching §9's stated "predicate check" framing) good enough now that it is correct?

