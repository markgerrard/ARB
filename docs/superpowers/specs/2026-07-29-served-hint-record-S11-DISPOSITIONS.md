# Served-hint record — §11 open-question dispositions

**Date:** 2026-07-29. **Branch:** `feat/served-hint-record-impl` (merged to `dev`).
**Status: ADOPTED 2026-07-29 by Mark** ("adopt the five §11 questions"). All five recommendations
below are now decisions of record; the reasoning under each is preserved as the basis for the
decision, not as an open argument. Reopening any of them needs the operator, not a new panel.

*Before adoption, this document recorded them as recommendations only, on the basis that:* §11 questions are product/constitution-layer, and
unlike Q2 and Q3 — which the operator *explicitly delegated*, and which the charter records as
adopted D-2 and D-1 (`…-BUILD-CHARTER.md` §3) — these five were never delegated. (The charter
labels D-2 "§11 Q2" explicitly but records D-1 under its defect-hunt id **J-04**, never "Q3";
the D-1↔Q3 mapping here is by subject matter — J-04 adopts exactly the snapshot obligation that
closes Q3's audit gap — not by the charter's own label.) Nothing here has
been adopted, and none of it changed code.

(An earlier revision cited "BUILD-CHARTER §4" as reserving these questions. It does not: §4's four
bullets are no round 7 / no re-fold, no merge, no deploy, no re-litigating settled scope
(`…-BUILD-CHARTER.md:244-250`). The corrected basis is non-delegation, above.)

**What this document is for.** The §11 questions were answered across three review rounds, but the
positions are scattered through four seats' reports in three DECISION-RECORDs. This collects each
question's recorded positions, checks them against the code **as built** (rather than as the frozen
guide describes it — its citations have known drift, E-01), and puts one recommendation on each.

**Scope.** Q2 and Q3 are already closed as D-2 and D-1 (BUILD-CHARTER §3). Q8/H-11 was closed
2026-07-29 on evidence — see the ERRATA entry and commit `ba0a2117`. The five below are what
remained.

**Method note.** Authored inline by the warm session rather than through a FABA author round. The
permitted case: `CLAUDE.md` § Workflow C keeps inline authoring available for **code-grounded**
design, and every claim here is a file:line assertion verified in the authoring session. No content
was carried from the review transcripts unverified.

---

## Summary

| Q | Subject | Decision (adopted 2026-07-29) | Code change |
|---|---|---|---|
| 1 | 30-day retention default | **Keep 30, one knob** — precondition now met with a measured number | none |
| 4 | Seat identity on local reads | **Defer**, plumb both doors together in Take 2 | none |
| 5 | Local reader INSERT-without-SELECT | **Keep as built** | none |
| 6 | G-08's one-seat-per-process bound | **Closed as spec consistency; add nothing** — Q4 is its prerequisite | none |
| 7 | H-09 non-blocking XADD handoff | **Do not build it** | none |

Four of five are "keep as built / defer". Only Q1 carries information the panel did not have.

---

## Q1 — Is 30 days the right retention default, or does the local tier need a shorter window?

**Recorded positions.** `codex` (rounds 4 and 5): *"an unverified starting value, not an
evidence-backed default… ship only with volume/capacity metrics and a sizing calculation; separate
per-door defaults are premature."* `asdk-opus5`: *"ship 30, revisit with data"* — a second knob
before the first row is written is premature. `agy-print` (round 4): acceptable for Take 1, add a
local var in Take 2 if needed. `grok-acp`: residual, non-blocking. The v5 record calls this
**near-convergence**; no seat argued for a shorter local window on evidence, only on the untested
intuition that the local tier is higher-volume.

**Verified against source.** `ARB_HINT_READ_RETENTION_DAYS` defaults to **30**
(`src/arb_memory/run.py:104`), directly beside `ARB_EVAL_RETENTION_DAYS` (`run.py:70`) and
`ARB_TRANSCRIPT_RETENTION_DAYS` (`run.py:87`), **both also 30**. So 30 is not an arbitrary pick —
it is the standing convention across all three retention lanes, and a per-tier value would make
`hint_read` the only asymmetric one.

**The sizing calculation `codex` made a precondition — now measured, not estimated.** Against the
actual DDL (`schema.sql:593-623`: both tables and all five indexes), 20,000 parent rows at k=8
(160,000 hit rows), local-tier column shape (`run_id`/`seat_id`/`query_text` NULL, `query_hmac` 64
hex chars):

```
hint_read      (table + indexes):   4,669,440 bytes
hint_read_hit  (table + indexes):  23,322,624 bytes
TOTAL PER READ                  :       1,399.6 bytes
```

The local ceiling is knowable because the limiter is a fixed per-process rate:
`search_rate_per_min = 30` (`read_tools.py`, `LocalReadSettings.search_rate_per_min`). One **saturated** local process over a 30-day
window is therefore:

```
reads     :  1,296,000
hit rows  : 10,368,000
disk      :       1.81 GB
```

Read that ceiling honestly: saturation means 30 searches per minute sustained around the clock for
a month. At a realistic 1,000 reads/month the same arithmetic gives **~1.4 MB**. The ceiling is the
number that matters for a capacity decision, and it is not alarming.

*Reproducing it:* create the two tables and five indexes from `schema.sql` in a throwaway schema,
`COPY` N parents plus N×8 hits in the local column shape, `ANALYZE`, then read
`pg_total_relation_size` for both tables and divide by N. No production data involved.

*Tolerance, measured:* an independent reviewer reproduced this method at N=60,000 and got
**1.97 GB** against the 1.81 GB above (1.09×), and 1.52 MB against "~1.4 MB". The figure is
extrapolated from a sample, so page-overhead amortisation shifts it by roughly ±10% with N. Treat
it as an order-of-magnitude capacity number, which is the precision this decision needs — not a
budget line.

**DECISION (adopted 2026-07-29) — keep 30, one knob.** The precondition `codex` set is met and the number does not
justify a second default. There is also a coupling worth naming: with Q4 deferred nothing populates
`seat_id`, so a per-tier knob would be tuning a volume you cannot attribute to any seat. If the
window is ever revisited, do it after seat identity lands, not before.

## Q4 — Should local-tier reads carry seat identity?

**Recorded positions.** `codex`: defer, and plumb **both** doors together in Take 2 — a local-only
identity *"would imply a completeness bus rows do not have."* `agy-print`: deferring is correct.
`grok-acp`: residual. `asdk-opus5`: not separately adjudicated. No seat argued for landing it now.

**Verified against source.** The column exists — `seat_id text` (`schema.sql:598`) — with a partial
index `hint_read_seat_idx ON hint_read (seat_id) WHERE seat_id IS NOT NULL` (`schema.sql:611`).
**Neither tier populates it.** The local INSERT's column list omits it entirely
(`read_tools.py`, the `INSERT INTO hint_read` column list in `_record_local_read`), and the bus states it outright: *"run_id / seat_id omitted — not on the
current wire request; consumer stores NULL"* (`bus.py:317`).

**Measured cost of deferring.** Because the index is partial on `IS NOT NULL` and the column is
uniformly NULL, `hint_read_seat_idx` occupies **8,192 bytes** — a single empty page. (`run_idx`, in
the same state, likewise 8,192.)

**DECISION (adopted 2026-07-29) — defer to Take 2.** Deferring costs 8 KB, so cost is not the argument either
way; `codex`'s reason is the load-bearing one. A `seat_id` populated on one door of two makes
`hint_read_seat_idx` look authoritative while covering half the traffic, and a half-complete index
is worse than an empty one: the empty one cannot mislead a query. Land both doors together or
neither.

## Q5 — Is the local reader's INSERT-without-SELECT the right long-term shape?

**Recorded positions.** `codex`: *"correct for this slice. Do not pre-authorize a hypothetical
read-back feature."* `agy-print`: *"correct for security isolation."* `grok-acp`: residual.
Consistent across rounds 4 and 5; no seat dissented.

**Verified against source.** `apply_hint_read_local_writer_grants` (`grants.py:153-164`) grants
INSERT on `hint_read`/`hint_read_hit` to the local-MCP reader role and then revokes
SELECT/UPDATE/DELETE from that same role. Its docstring shows the separation is **load-bearing, not
stylistic**: it is deliberately not folded into `apply_local_reader_grants`, which `run.py` also
applies to `vault_export_role` — folding it there would create a third `hint_read` writer (G-03).

**DECISION (adopted 2026-07-29) — keep as built.** The hypothetical future consumer (a per-seat "what have I
searched for" tool) wants a row-level policy scoped to the seat's own rows, which is a different
design from a blanket table SELECT. Pre-authorizing now buys that feature nothing and widens the
door in the meantime.

## Q6 — G-08's bound assumes one seat per process

**Recorded positions.** The v5 record states **"Q6 is closed by convergence"**: it was round 4's
`asdk-opus5` finding that §4 stated the topology as fact while §11.6 called it an assumption, and
two seats independently verified in round 5 that the two now agree. `codex` (round 4) had wanted
more than consistency — *"an explicit seat key in limiter state or a startup assertion"*;
`agy-print` suggested naming it in `local_memory_mcp.py`.

**Verified against source.** The limiter state is per-**instance**: `self._search_hits`, filtered
and appended in `ReadMemoryTools._check_search_allowed`, and the G-08 suppression map
`self._last_rejection_receipt_at` (set in `ReadMemoryTools.__init__`), both hang off `ReadMemoryTools`. So the
bound is genuinely per-process. Under a shared-process topology it silently stops being a per-seat
bound — exactly as §11.6 says. **No assertion or seat key exists in code.**

**DECISION (adopted 2026-07-29) — the spec-consistency half is closed; add nothing now.** The startup assertion
`codex` wanted needs a seat key to assert *about*, and that is Q4, which is deferred. Sequencing
them the other way round would mean asserting a property of an identity the system does not yet
carry. If seat identity lands in Take 2, the topology assertion lands with it, in the same change.
§11.6 asked for "a named assumption check before that shape is built" — it is named, in the spec
and now here.

**One claim this document cannot make.** An earlier revision closed this section with "and no
shared-process deployment exists." That is an assertion about live fleet topology, and nothing in
this repo can establish it — the bytes prove only that limiter state hangs off one
`ReadMemoryTools` instance (`ReadMemoryTools.__init__`'s `_search_hits` /
`_last_rejection_receipt_at`, consumed by `_check_search_allowed`). It is withdrawn as evidence and
restated as what it is: **an assumption, uncited and unverified here.** If the recommendation is
ever challenged, the thing to produce is a dated fleet inventory or a runtime probe showing one
seat per local-MCP process — not this document.

## Q7 / H-09 — should the record-intent XADD get a bounded non-blocking handoff?

**Recorded positions.** All four seats accept the narrowed claim, and the movement matters: `codex`
**raised** H-09 at P1 in round 4 and by round 5 concluded *"H-09's narrower claim is internally
consistent… the handoff is an operator product call, not a fold defect. **Do not block on H-09
itself.**"* `grok-acp`: accept, do not block. `agy-print`: properly handled. `asdk-opus5`: recorded,
not resolved. The v5 record marks the obligation 4/4 and leaves the product question open as Q7.

**Verified against source — the narrowing is true as built.** In `handle_read_request` the reply is
delivered first (`redis.lpush(reply, …)` at `bus.py:277`, `redis.expire` at `bus.py:278`), and the
record-intent XADD comes *after* it, inside its own try/except (`bus.py:319-324`). So the reply is
genuinely protected. What is **not** protected is this `ReadLoop`'s *next* request, because the XADD
runs synchronously on the loop thread. That is precisely §4's narrowed claim, and it holds against
the code.

**DECISION (adopted 2026-07-29) — do not build the handoff.** It is unanimous among the seats after the raising
seat's own movement, and the claim the narrowing rests on is verified rather than asserted. A
producer thread or queue between reply and XADD buys latency on the loop's next request at the cost
of a new concurrency surface and an unbounded-queue question, on a tier the panel put at roughly one
read per panel. Revisit if and when loop latency is *observed* to matter — which requires the
telemetry this slice exists to build. Building the mitigation before the measurement is the same
inversion §11 Q1 was criticised for.

---

## What would change these

- **Q1** — a measured production volume materially above the ~1,000 reads/month assumed here, or a
  shared database where 1.81 GB per saturated process is not affordable.
- **Q4/Q6** — a deployment shape that puts more than one seat behind one local-MCP process. That
  single change reopens both together, which is why they should move together.
- **Q5** — an actual read-back consumer being specified, at which point the design question is
  row-level policy, not widening this grant.
- **Q7** — observed `ReadLoop` latency attributable to the record XADD.

---

## Adoption record (2026-07-29)

**Adopted by Mark**, in his own words ("adopt the five §11 questions"), after the served-hint slice
merged to `dev` and after a two-round pre-merge review panel that returned zero P0 and zero P1.

**No code changed on adoption.** All five decisions are *keep as built* or *defer*, which is why
adoption is a record act rather than an implementation one. The slice as merged already embodies
every one of them.

### Standing obligations these decisions create

1. **Q4 and Q6 move together, or not at all.** Q6's topology assertion needs a seat key to assert
   *about*, and that key is Q4. If seat identity lands in Take 2, the one-seat-per-process assertion
   lands in the **same change**. Landing either alone re-opens the other as a defect: seat identity
   without the assertion gives a half-populated index that reads as authoritative; the assertion
   without seat identity has nothing to assert.
2. **Q1's revisit is gated on Q4, not on the calendar.** Revisit the 30-day window only if measured
   volume runs materially above the ~1,000 reads/month assumed here, or a shared database makes
   1.81 GB per saturated process per window unaffordable — and revisit it *after* seat identity
   lands, because until `seat_id` is populated a per-tier knob tunes a volume nothing can attribute.
3. **Q5 re-opens only when a real read-back consumer is specified**, and the design question then is
   a row-level policy scoped to a seat's own rows — not widening the existing table grant.
4. **Q7 re-opens only on observed `ReadLoop` latency** attributable to the record-intent XADD, which
   requires the telemetry this slice exists to produce.

### What does not re-open them

**Another review panel does not.** These are now operator decisions of record. A panel may surface
new evidence that the operator chooses to act on — that is the mechanism in obligation 1–4 above —
but a seat disagreeing with an adopted decision is an input, not a reversal. The five were carried
through three review rounds as open questions precisely so they could be closed once, deliberately,
by the person entitled to close them.
