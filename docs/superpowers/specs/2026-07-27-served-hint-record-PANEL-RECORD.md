# Served-hint record — panel record

**Companion to `2026-07-27-served-hint-record-design.md`. Not the design.**

This file holds the *record of the argument about* the served-hint design: what each round
settled, what is closed, and what changed between versions. The spec describes the system; this
describes the rounds. They are separated deliberately.

**Why the split (2026-07-28, after v5).** Process history is the one category of spec content that
grows by construction — every fold adds a changelog row and a closed-finding line, and nothing ever
removes one. v5 reached 1256 lines / 97KB, +40% lines over v4 in a single round, and the panel has
to read the whole document to review it. Moving this content out does not make the spec much
smaller on its own (these sections were 6,130 bytes, ~6%), but it removes the *mechanism* by which
the spec grows every round regardless of whether the design changed.

**For reviewers.** The `Settled scope` and `CLOSED` sections below carry the same authority they
had inside the spec. Re-raising an item marked settled here wastes the round. If you believe a
settled item is genuinely wrong, say so once, in one sentence, marked `OUT-OF-SCOPE-CHALLENGE`.

**For fold authors.** Changelog entries and newly-closed findings go **here**, appended, never into
the spec. The spec carries a one-line pointer to this file and nothing more.

**Per-round decision records** (the full findings, per-seat reports and verdicts) live separately,
one directory per round. This file is the running summary, not a replacement for those.

---

## What changed, v4 → v5

| Finding | Disposition | Fix location | What changed |
|---|---|---|---|
| H-01 | Folded (Tier 1) | §6 Grants, §9 | `apply_hint_read_consumer_grants` now grants `USAGE` on `hint_read_deadletter_id_seq` (mirrors `apply_eval_grants`'s existing `eval_deadletter_id_seq` grant, `grants.py:307-312`); §9's grant test replaced with a real `SET ROLE` dead-letter INSERT |
| H-02 | Folded (Tier 1) | §4 Local + Bus tiers | `_record_local_read` and the new `HintReadSink.write` both now read `hit["hint"]["id"]`, `hit["hint"].get("vector_distance"/"lexical_rank")`, `hit["withheld"]` — the actual `store.retrieve` nesting, not the flat shape v4 assumed |
| H-03 | Folded (headline structural fix) | §9 (largely rewritten) | Every test named as a tripwire for a specific finding now exercises the behaviour it labels; new test asserts a successful `memory_search` produces one `hint_read` row with non-NULL `hint_id` and `vector_distance` |
| H-04 | Folded (Tier 1) | §4 Bus tier, §6 Schema | `stream_entry_id` now stores the fully-qualified, prefixed key (the same string fed into `read_id`'s `uuid5`) on both `hint_read` and `hint_read_deadletter`, closing the cross-prefix collision without changing either table's `UNIQUE (stream_entry_id)` constraint shape (unchanged shape needed by the generic dead-letter canary) |
| H-05 | Folded (Tier 1) | §4 Local tier | Entire recording decision — `should_record`, the call, the timestamp update, the log — moved inside one `try/except Exception`; `__init__` gains `_last_rejection_receipt_at`; `read_tools.py` gains a module-level `logger` |
| H-06 | Folded (Tier 3) | §6 Grants | `hint_read_deadletter` added to both `apply_local_reader_grants`'s and `apply_mcp_grants`'s enumerated per-role REVOKE lists |
| H-07 | Folded (Tier 3) | §4 Local tier | `_record_local_read`'s INSERT now binds `door` as `%s` instead of hardcoding `'local'` |
| H-08 | Folded (Tier 2) | §4 Local tier, §6, §9 | The 60-second bound now covers all three pre-`retrieve` rejection classes (query-too-long, missing-key, rate-limited), independently, not only the rate-limit one |
| H-09 | **CONTESTED — recorded, minimum remediation applied** | §4 Bus tier + new "Author choices" subsection | Claim narrowed to what it actually protects (the already-sent reply, not this `ReadLoop`'s next request's throughput); both positions recorded with evidence; flagged for operator adjudication |
| H-10 | Folded (Tier 2) | §4 Bus tier | The bus sink's parent+hits write is now explicit SQL (`HintReadSink.write`) — `ON CONFLICT (read_id) DO NOTHING RETURNING read_id`; no row ⇒ skip hits, return `"duplicate"`; §9 gains a redelivery-does-not-dead-letter test |
| H-11 | **CONTESTED — recorded, minimum remediation applied** | §4 Local tier + new "Author choices" subsection | Invariant text now states `autocommit` is the accepted, verified proxy for `IDLE` and why, rather than switching the guard to a direct `transaction_status` check; both positions recorded; flagged for operator adjudication |
| H-12 | Folded (Tier 3) | §4 Bus tier, §8 | `run_hint_reads()` / `run_hint_read_purge()` specified and wired to `python -m arb_memory hint-reads` / `hint-read-purge`, mirroring `run_eval()`'s wiring exactly |
| H-13 | Folded (Tier 3) | §9 | G-10's index test replaced with a `pg_indexes.indexdef` predicate assertion, not a row-count assertion that needs `pageinspect` to be trustworthy |
| H-14 | Folded (Tier 3) | §7 | Heading corrected: local *does* write to the database; ISOLATION means the read's outcome never depends on it, not that no database work happens |
| H-15 | Folded (Tier 3) | §10 | "Absent `door` value" corrected — `door` is `NOT NULL`, so no row is written for the public door at all; there is no in-table marker to find |

**The invariant itself is untouched by this fold, deliberately.** All three reporting seats
credited the RECEIPT INVARIANT restructure as genuine, not decorative, and no seat could construct
a commit-or-isolation failure from v4's transaction mechanics. v4's new defects sit on **data-shape**
(H-02, H-04) and **guard-boundary** (H-05, H-08) axes the invariant's two clauses were never written
to cover. This draft extends the invariant's **test coverage** onto those axes (§9); the invariant's
own two clauses, in §4, are reproduced from v4 without change.

---

## CLOSED — verified fixed in v3, not reopened in v4

- **F-01** commit contract restated as fresh-connection-per-entry (verified against
  `eval.py:494-496` + `eval.py:369-371`).
- **F-03** deterministic `read_id` + dual `ON CONFLICT` is genuinely idempotent under PEL
  redelivery. G-09 (v4) fixes only the namespace/collision axis; the redelivery mechanics are
  unchanged and not reopened.
- **F-08** loss posture corrected to commit-then-ack.
- **F-09** `withheld` returned from `store.retrieve` and in §2's scope table.
- **F-13** `door` rename, cross-store case named.
- **F-15** citation fixed; the two-apply re-appliability test is the standing proof.
- **F-07 local `served_at`** — `DEFAULT now()` at an inline insert **is** serve time on the
  production autocommit path. The only exposure was via G-02's unenforced invariant, now closed.
- **CLOSED-1** single-writer exemption (F-14 completes the GRANT argument, extended in v4 to name
  the two new dedicated grant functions).
- **CLOSED-2** `ON DELETE CASCADE` safety.
- **CLOSED-3** no FK on `hint_id`, `hint_read_local` fallback deleted.
- All round-2 CLOSED items (single-writer exemption, CASCADE safety, dropped FK, §8a unaffected)
  remain closed.

## Settled scope — do NOT re-litigate

- Take 1 only. Coverage = bus + local-MCP tier; public door excluded.
- Branch stops at code + schema + tests. No deploy, no live-DB apply.
- §3's no-deletion-from-usage-stats rule is **co-signed at MUST (Mark, 2026-07-27)**. Keep the
  rule, its rationale, and the CLAUDE.md migration obligation.
- The §4 single-writer exemption was resolved in round 2 and must not be re-opened.
- The structural directive itself (RECEIPT INVARIANT, §4) is owner-approved and is the shape of
  this fold, not an open question for the next panel.

---

## Round 6 (v5 → v6) — appended from the v6 fold


**This section is not part of `2026-07-27-served-hint-record-design.md`. It is the content the
standing fold instructions ask the round-6 author to append to
`2026-07-27-served-hint-record-PANEL-RECORD.md`, under a new heading for this round.** A FABA author
round may not write to the repository directly, so this content travels with the design draft for
the orchestrator to apply to that file separately, verbatim, rather than being folded into Part 1
above. Every row cites the line(s) in **this artefact's Part 1** that carry the change.

## What changed, v5 → v6

| Finding | Disposition | Fix location (this draft) | What changed |
|---|---|---|---|
| J-01 | Folded | §4 Bus tier — field table `hits` row, new `_parse_hint_read_event`, `HintReadSink.write` | Bus wire `hits` stated as flat, not nested; new parser renames `query`→`query_text`, JSON-decodes `hits`, coerces bool/int fields; `HintReadSink.write`'s hit loop reads `hit["hint_id"]`/`hit["withheld"]` flat instead of `inner = hit["hint"]`; the false eval-parity claim corrected to note the parse step it previously omitted |
| J-02 | Folded | §4 Local tier — `memory_search` except block | `self._last_rejection_receipt_at[rejection_class] = time.monotonic()` moved above the `_record_local_read` call, inside the same inner `try`, so it advances on attempt not success |
| J-03 | Folded | §9 — new test row after the H-02 headline row, plus a blanket rule | New bus-tier row mirrors the local H-02 tripwire, built from `_parse_hint_read_event`'s parsed output; a blanket rule requires every other `HintReadSink.write`-exercising row to use the same parsed, flat event shape, not a hand-built nested one |
| J-05 | Folded, executed | §9 — G-10/H-13 index row | `indexdef LIKE '%WHERE run_id IS NOT NULL%'` → `'%WHERE (run_id IS NOT NULL)%'` (and the `seat_id` equivalent); citation to the live-PostgreSQL execution added to the row itself |
| J-06 | Folded, executed | §6 grants prose + PUBLIC-revoke code; §9 row 1127 split | `hint_read_deadletter` added to both isolating functions' `REVOKE ALL ... FROM PUBLIC` line (closes the executed PUBLIC-inheritance gap); the door-role and local-reader clauses of the old combined test row split into two non-contradictory rows |
| J-07 | Folded | §4 Local tier — code block | `_cap(query, max_chars) -> tuple[str, bool]` defined; previously called, undefined, since v4 |
| J-08 | Folded | §4 Local tier — code block + two comments | `SearchRateLimitExceeded` class deleted (replaced in the same slot by the `_cap` definition, J-07); the comment at the `_check_search_allowed()` call and the "neither one is..." sentence both corrected to describe the `rejection_class` marker instead of a class nothing raises |
| J-09 | Folded | §6 grants prose | Corrected to state `apply_local_reader_grants` uses a tuple loop and `apply_mcp_grants` uses discrete grouped statements, not "both... enumerated tuple" |
| J-10 | Folded | §4 Bus tier XADD; §7 bullet | XADD wrapped in `try/except Exception: logger.exception(...)`; §7's claim corrected to drop the unimplemented "counter incremented" half |
| J-11 | Folded | §4 — new blockquote immediately under the RECEIPT INVARIANT box | One-paragraph proxy note added under the box stating `autocommit` is the accepted, verified proxy for `IDLE` and why — the change-table claim v5 made about the invariant text is now true |
| J-12 (H-11) | Not resolved — operator adjudication | §4 "Author choices" | Unchanged 1-block/2-accept split; proxy-vs-direct-check question restated with the J-11 fix noted as not itself an answer to J-12 |
| J-04 | Not resolved — flagged, not authored | §11 Q3 | Question sharpened with round-5's near-unanimous proposed fix named; the fix itself not added to §3, since §3 is co-signed at MUST strength |
| H-09 | Reframed from CONTESTED to accepted | §4 "Author choices," §10, §11 Q7 | Round-5 4/4 acceptance recorded; remaining item reframed as the §11 Q7 product question, not a spec defect |

**H-09/H-11 note.** H-09's status changes this round (contested → accepted, 4/4); H-11/J-12 does not
(still 1 block / 2 accept, with J-11's procedural fix applied but the substantive question
untouched).

## Round 5 findings — closure status entering v6

- **J-01, J-02, J-03, J-05, J-06, J-07, J-08, J-09, J-10, J-11** — folded in this draft (v6); see the
  table above for the specific fix location and text.
- **J-04** — not closed. Flagged at §11 Q3 for operator adjudication (MUST-strength rule addition,
  constitution-layer, not an author's call).
- **J-12** — not closed. Contested; both positions recorded in §4 "Author choices"; flagged for
  operator adjudication.
- **§11 Q2** (query text hashing vs. raw) — untouched, unresolved, carried from round 4.

## Settled scope — do NOT re-litigate (unchanged, reaffirmed this round)

No seat raised an out-of-scope challenge in round 5. The existing settled-scope list (Take 1 only,
bus + local-MCP coverage, no deploy, §3's MUST-strength no-deletion rule, the §4 single-writer
exemption, and the RECEIPT INVARIANT structure) stands unchanged into v6. `asdk-opus5` explicitly
named size/structure among the things it did not re-open in round 5, which bears on the committed
v6 relocation (executed in this draft) and the v7 §4-compression plan (still deferred).
