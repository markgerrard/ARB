# Served-hint record — BUILD CHARTER

**Status:** adopted 2026-07-29 under explicit operator instruction (Mark: *"proceed to implementation
… with grok tdd as implementor"*, and *"give the build phase a definition of done before it starts"*).
This document closes the review loop and opens the build phase. It is the **exit criterion** the fold
loop never had.

**Supersedes as the active plan:** `…-PARTITION-PLAN.md` (moot — it existed to sustain folding),
the 1400-line ceiling, and the checkpointing driver. All three were improvements *to the loop*.

---

## 0. Why the loop stopped, in one line

Stop Condition B fired on its own pre-registered test (`…-CONVERGENCE.md`; round-6 record). The
underlying reason is sharper than "diminishing returns": **the defect class shifted from design
errors to implementation errors around round 3, and reading is the wrong instrument for the second
kind.** Rounds 1–3 paid — they produced the RECEIPT INVARIANT, which no test run would have
surfaced. Rounds 4–6 found a deleted function, a dict nested one level off, a SQL predicate matching
nothing, and a timestamp assigned after its use — every one a thing a test run catches in seconds.

**The measured proof, recorded here because it is the whole argument.** Six rounds of panels reported
`4 skipped` from `tests/arb_memory/test_schema.py` and each correctly noted a skip is not a pass.
The tests were never unrunnable. They were gated on `ARB_MEMORY_DSN`, which nobody set, next to a
PostgreSQL 17.7 instance that was running the entire time. With the DSN set:

```
$ ARB_MEMORY_DSN="postgresql:///arb_memory_test" .venv/bin/python -m pytest tests/arb_memory/test_schema.py -q
4 passed in 0.35s
```

Twenty seat-hours of prose review versus 0.35 seconds. That is the controlled comparison.

---

## 1. DEFINITION OF DONE — the build phase exits when all eight hold

The build is **done**, not "ready for another look", when:

1. **Schema.** `hint_read`, `hint_read_hit`, `hint_read_deadletter` exist in
   `src/arb_memory/schema.sql`, apply cleanly under the `scratch` fixture, and survive the standing
   **two-apply re-appliability** test.
2. **Grants.** The local-reader and local-writer grant functions exist in
   `src/arb_memory/mcp/grants.py` and are wired into `run_grants()` (`src/arb_memory/run.py:313`)
   **in an order where the reader's INSERT survives** — `apply_local_reader_grants` runs at
   `run.py:369`, before the hint-read writer grants.
3. **Bus tier.** `handle_read_request` builds the wire event and XADDs it; `_parse_hint_read_event`
   parses it; `HintReadSink.write` persists parent + hits.
4. **Local tier.** `_record_local_read` writes parent + hits atomically on an autocommit
   connection, with the G-02 precondition guard.
5. **Retention.** `run_hint_read_purge()` exists, following the established
   `run_eval_purge` / `run_transcript_purge` pattern (`run.py:67`, `run.py:84`).
6. **§9 runs green against a real database.** Every §9 check the spec specifies is *executed*, with
   `ARB_MEMORY_DSN` set, and passes. §9's 32 checks were 21-not-runnable **only because the code did
   not exist**; once it exists there is no such excuse. This is the hard gate.
7. **The producer contract is pinned by capture, not reconstruction.** At least one test calls
   `handle_read_request` with a spy Redis client, captures the actual argument passed to `xadd`, and
   feeds *those bytes* through `_parse_hint_read_event` → `HintReadSink.write`. A test that
   hand-builds the wire dict does not satisfy this item (that is the K-02 defect: a fixture and a
   producer that drift while each looks internally correct).
8. **No new skips.** The hint-read test run reports **0 skipped**. A skip is not a pass — six rounds
   of seats said so; item 8 makes it structural.
9. **No regressions.** The full `tests/arb_memory/` suite is run, and every failure is either fixed
   or **named as pre-existing with the commit that proves it**. See the reporting contract below.

When 1–9 hold, **the slice is done and the build phase closes.** Not "done pending review".

### Per-increment reporting contract (added 2026-07-29, after S2)

**Every increment brief MUST require a full-suite run**, not just the increment's own test file, and
the reply MUST classify each failure as either *owned* (this increment caused it; fix it) or
*pre-existing* (**cite the commit at which it also fails**).

**Why this is a brief defect, not a seat defect.** In S2 the seat reported *"incomplete in scope:
none"* — truthfully, because the brief only asked it to run its own file. The wider run then found a
failure the seat's report was structurally incapable of seeing. A report scoped narrower than the
claim it implies will mislead every time, however honest the seat. Fixing it upstream turns the
orchestrator's verification into **confirmation rather than discovery**, which is both cheaper and
what actually scales.

**Known-red baseline** (do not mistake these for regressions; see
`…-ENVIRONMENT-TRAPS.md`): `test_eval_grants.py::test_mcp_role_has_no_audit_or_eval_access` fails at
base commit `a91a6408` with zero `hint_read` tables present. It is an MCP-role provisioning gap,
out of scope, and must not be chased.

### Every denial assertion MUST seed the privilege it claims to deny

Found by mutation testing during S2 acceptance, and it is the highest-value rule in this document.

`test_local_reader_can_insert_hint_read_but_not_select` asserted — by catalog lookup *and* by a real
`SET ROLE` + `pytest.raises(InsufficientPrivilege)` — that the local reader cannot `SELECT`. Both
assertions passed **vacuously**: `apply_local_reader_grants` had already revoked ALL on those tables,
so `SELECT` was absent whether or not the writer function's `REVOKE SELECT` existed. Deleting that
revoke left the whole suite green.

This is the **same vacuity class as J-05** — a check that cannot fail on the defect it names — and it
survived a brief that explicitly mandated seeded deny-proofs, because that instruction was scoped to
the `PUBLIC` case only. So state it generally:

> **A test asserting that some principal LACKS a privilege must first GRANT that privilege, then
> apply the code under test, then assert it is gone.** Absence you did not create is not absence you
> proved. This applies to `PUBLIC` grants, per-role grants, and sequence grants alike.

Verified discriminating before adoption: seeding `GRANT SELECT ON hint_read, hint_read_hit TO <role>`
yields `has_table_privilege = True` before `apply_hint_read_local_writer_grants` and `False` after.

### On grading evidence, not categories

An `ImportError` RED technically satisfies "the test failed first" and a box-ticking check passes it.
It is **near-zero evidence**: it proves the module did not load, not that any assertion can detect
anything. When a RED is of that shape, the proof burden shifts entirely to **mutation testing** —
break the behaviour deliberately and confirm the specific test that names it goes red. S2's grants
suite was accepted on that basis, not on its RED.

### The anti-recursion guard

No work on the test harness, the fixtures, or the tooling beyond what a numbered DoD item above
requires. The fold loop got stuck because nothing defined its exit; TDD does not inherit that
open-endedness. **If a future session finds itself refining this harness for its own sake, that is
the fold loop reincarnated in new clothes — stop and re-read this section.**

---

## 2. The build guide is v5, frozen — and it is a guide, not a deliverable

**Base:** `c769e379`, `docs/superpowers/specs/2026-07-27-served-hint-record-design.md`, 1256 lines.

**Not v6.** v6 has a verified correctness break: `def _record_local_read` is defined **0 times** and
referenced **11 times** (v5: 1 and 10). Confirmed against the bytes, not taken from the panel:

```
$ git show c769e379:<spec> | grep -c "def _record_local_read"          -> 1
$ git show feat/served-hint-record:<spec> | grep -c "def _record_local_read" -> 0
```

v5's `_record_local_read` body is intact at **v5:486–514** and is the reference implementation for
DoD item 4.

**The spec's job ends when the code passes its tests.** From that moment the code is the source of
truth.

### Errata, not re-opening

Implementation **will** find spec errors — that is the point of building. Log each one in
`docs/superpowers/specs/2026-07-29-served-hint-record-ERRATA.md` as a dated line: what the spec says,
what the code does, why the code is right. **Do not re-open, re-fold, or re-panel the design
document.** Two errata are already known, found while writing this charter:

- **E-01.** The spec's code citations have drifted from the source. `drain_pending` is at
  `bus.py:308` (spec cites 287–298); `ReadLoop._handle_entry` is at `bus.py:367` (spec cites
  354–359). **Verify every anchor against the file; do not trust a cited line number.**
- **E-02.** K-06 confirmed against source: `grants.py:34-48`'s tuple does **not** contain
  `hint_read` / `hint_read_hit`. The spec's "already in that tuple" is false. This design *adds*
  them.

---

## 3. The two parked decisions — decided here, under delegation

Both were parked for the operator and both were explicitly delegated
(*"read the recorded arguments, pick, write the choice down"*). Recorded as
**adopted-by-delegation**, not self-authored doctrine. Either is reversible in one line.

### D-1 — J-04: §3's MUST rule gets its snapshot obligation. **ADOPTED.**

All three seats that addressed J-04 proposed the *identical* one-line remedy; the only disagreement
was scope (whether the v6 fold should have closed it), never the defect. The defect is real and
structural: §3 forbids served-hint statistics from driving deletion without a human evidence
artifact, while §8 purges the supporting rows at 30 days — so a later *"never served in 90 days"*
claim is **unfalsifiable by construction**. Without the obligation the MUST rule is decorative,
because compliance cannot be checked after the purge.

> **Adopted text.** Any evidence artifact citing served-hint statistics MUST snapshot the supporting
> rows (or the aggregate) and the window bounds at the time of the claim, including any period
> already purged.

Carries the existing migration obligation: this belongs in `CLAUDE.md` when the feature lands, via
the protected-file merge discipline (read first, classify append/merge, **never** blind overwrite).

### D-2 — §11 Q2: keyed HMAC by default, raw text behind an explicit opt-in. **ADOPTED.**

The recorded positions were: *"a length cap is not screening"* — prefer omission or a **keyed** HMAC,
noting a **plain** hash is dictionary-attackable (codex, rounds 4 and 5, refined to "keyed HMAC plus
optional operator-controlled short-lived raw sampling") — against *"hashing would destroy query
observability"*. The third seat declined, calling it constitution-layer.

The refined position answers both concerns rather than splitting them, and the distinction the
objection missed is load-bearing: **a keyed HMAC is not dictionary-attackable** by an attacker
holding the database but not the key. The observability objection is answered by the sampling
switch — when you actually need to know *what was asked*, you turn raw capture on for a window.

> **Adopted shape.** Store `query_hmac` (HMAC-SHA256 over the **capped** query, key from
> `ARB_HINT_READ_QUERY_KEY`) as the default persistent form. `query_text` becomes **nullable** and is
> populated only when raw capture is explicitly enabled (`ARB_HINT_READ_QUERY_RAW=1`). Keep
> `query_truncated` — it still tells a reader the HMAC is over a truncated string.
>
> **Failure posture is unchanged and dominates:** §7's *"recording failure never fails a read"* still
> binds. If the key is unset, the read still succeeds and the row records with a NULL `query_hmac`,
> logged once. Recording never raises into the read path.

Equal queries still produce equal digests, so frequency and liveness telemetry — the actual Take-1
purpose — is fully preserved.

**Scope note.** This is one env var, one conditional, and one column against the v5 schema. It is
decided *now* precisely because it touches the schema, which TDD builds first; deciding it later
means a migration.

---

### D-3 — `store.retrieve` emits `withheld`. **ADOPTED** (operator: "go with your recs", 2026-07-29)

**The defect.** `store.retrieve` computes `withhold` (`store.py:311`) and never puts it in the
returned dict (`store.py:313`); the string `withheld` appears nowhere in that file. The frozen guide
nonetheless lists it under **CLOSED**: *"F-09 `withheld` returned from `store.retrieve` and in §2's
scope table"* (guide line 1236). The guide's own recorder does `hit["withheld"]`, which `KeyError`s
against real output. Six review rounds read past it; the implementor hit it in one pass, because
writing code forces contact with the real signature. It is the cross-slice-assertion class named in
`CLAUDE.md`, made worse by having been ticked off as closed.

**Adopted.** `store.retrieve` includes `withheld` in each returned element, from the `withhold` value
it already computes.

**Known, accepted consequence — this is a client-visible change.** `_json_safe_search_hit` does
`out = dict(hit)`, so the field flows into the `memory_search` MCP response. Accepted because F-09's
stated intent was exactly this, and because a client arguably *should* know an artefact was withheld.

**Rejected alternatives, recorded so they are not revisited.** (a) Computing `withheld` at the
recorder's call site duplicates the `learn_proposal` rule in a second place — the J-01 defect class
(divergent copies) this spec spent rounds fighting. (b) `hit.get("withheld", False)` looks like the
smallest diff and is the worst option: it would silently record `withheld=False` for everything,
making the column lie.

### D-4 — the bus wire carries the same query columns the database does. **ADOPTED** (same)

D-2 keeps raw query text out of the database by default. A wire element carrying raw text would put
it in a Redis stream instead — "transient" is not "unexposed", and it would leave the bus holding
more sensitive data than the store it feeds.

**Adopted.** `handle_read_request` derives its query columns from the **same `_query_columns` helper**
S3 placed at module level, and the wire element carries `query_hmac` and `query_truncated`, plus raw
`query_text` **only** when raw capture is explicitly enabled. One helper, one policy, both tiers —
so the two ends cannot drift, which is the J-01 failure mode again.

## 4. What this charter does NOT authorise

- **No round 7, and no re-fold of the design document.** Stop Condition B.
- **No merge to `dev` or `main`.** The merge decision is the operator's and is untouched here.
- **No deploy, no live-DB apply.** v5 §"Settled scope": the branch stops at code + schema + tests.
- **No re-litigating settled scope** — Take 1 only; bus + local-MCP tiers; public door excluded; the
  single-writer exemption and the RECEIPT INVARIANT are owner-approved and closed.
