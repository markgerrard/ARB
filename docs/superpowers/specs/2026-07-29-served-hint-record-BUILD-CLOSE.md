# Served-hint record — BUILD CLOSE

**Date:** 2026-07-29. **Branch:** `feat/served-hint-record-impl`, tip `2aa2512e`.
**Base:** `origin/dev` @ `69197ba6`. **Not merged** — the merge decision is the operator's
(BUILD-CHARTER §4) and is untouched here.

The build phase opened when Stop Condition B confirmed and the review loop was closed. This document
closes it against the charter's nine definition-of-done items. Every claim below is backed by a
command run in the closing session; where something is *not* proven, it says so.

---

## Definition of done — item by item

| # | Item | Verdict | Evidence |
|---|---|---|---|
| 1 | Schema: three tables, re-appliable | **MET** | `test_hint_read_schema.py`; mutation: stripping `ON DELETE CASCADE` reddens the cascade test |
| 2 | Grants wired into `run_grants()`, reader INSERT survives ordering | **MET** | `test_hint_read_grants.py`; mutation: reverting the PUBLIC revoke to the guide's 2 tables reddens both deny-proofs |
| 3 | Bus tier: producer → parser → sink | **MET** | S4a `test_hint_read_producer.py`, S4b `test_hint_read_consumer.py` |
| 4 | Local tier: `_record_local_read`, atomic, autocommit-guarded | **MET** | `test_hint_read_local_recorder.py`; mutation: neutering `conn.transaction()` reddens the atomicity test |
| 5 | `run_hint_read_purge()` + CLI | **MET** | `test_hint_read_retention.py`; mutation: purging dead-letters reddens the non-goal test |
| 6 | §9 executes green against a real database | **MET** | `…-S9-COVERAGE.md`: 32 rows, **17 COVERED / 15 ADDED / 0 NOT-APPLICABLE**. All 39 cited test ids verified to exist against pytest's 819 collected ids — the cited-but-not-real set is empty |
| 7 | Producer contract pinned by **capture**, not reconstruction | **MET** | `test_producer_bytes_through_parser_into_sink` runs the real producer through a spy, asserts an XADD occurred, and feeds the captured dict into parse → sink. Read and confirmed to build no wire fixture by hand |
| 8 | Zero skips | **MET** | `pytest -k hint_read -rs` → **74 passed, 0 skipped** |
| 9 | No regressions | **MET, with a qualification recorded below** | Final suite: **8 failed, 1023 passed, 1 skipped**. All 8 reproduce at base code in isolation |

## Item 9 — the qualification, stated in full

**No failure is caused by this slice's code.** All eight reproduce at base commit `a91a6408` with
zero `hint_read` tables present (verified by reverting `src/arb_memory/` and `tests/arb_memory/` in a
venv-bearing worktree and re-running).

**But the observable suite outcome moved from 5 failures to 8, and that is not nothing.** Five are
the long-standing baseline (3× MCP-role provisioning, 1× missing Playwright browser, 1× SSE
revalidation). The other three are `test_lane_writer.py`:

- `test_seat_a_and_b_bound_isolation_matrix`
- `test_unbound_role_refuses_function_calls`
- `test_retire_bound_consumer_predicate_mutation_is_detectable`

These **fail at base code when run in isolation** and *passed* inside the full suite at S4b.
`pytest-randomly` is not installed, so ordering is deterministic: adding test files whose names sort
before `test_lane_writer.py` changed what runs beforehand and unmasked a pre-existing
order-dependence. Failure mode: `arm_lease_lane` raises *"query returned more than one row"*.

**Deliberately not chased.** An order-dependent test in the lane-writer subsystem is unrelated to
served-hint recording, and chasing it is the harness-refinement trap this charter exists to forbid.
It is recorded here as a real, proven finding for whoever owns that subsystem — not closed, not
minimised.

## Decisions taken during the build

| | Decision | Authority |
|---|---|---|
| D-1 | §3's MUST rule gains its snapshot obligation (J-04) | delegated |
| D-2 | Keyed HMAC by default; raw query text behind explicit opt-in | delegated |
| D-3 | `store.retrieve` emits `withheld` — client-visible in the `memory_search` MCP response | operator: "go with your recs" |
| D-4 | The bus wire carries the same query columns the database does | operator: "go with your recs" |

## The finding that most justifies having stopped the review loop

`store.retrieve` computed `withhold` and never returned it; the string `withheld` appeared nowhere in
`store.py`. The frozen guide listed this as **CLOSED (F-09)** — *"withheld returned from
store.retrieve"* — and its own recorder called `hit["withheld"]`, which would `KeyError` against real
output.

Six review rounds across four seats read past it. The implementor hit it in one pass, because
writing code forces contact with a real signature in a way reading does not. It is worse than an
open defect: it was a cross-slice claim **ticked off as verified**.

Closed by D-3 in S4a.

## Rules this build discovered (all now in the charter)

1. **Every denial assertion must seed the privilege it denies.** Found by mutation: the local-reader
   "cannot SELECT" test passed vacuously because a prior function had already revoked everything.
   Deleting the revoke left the suite green — the J-05 vacuity class, reproduced inside its remedy.
2. **An `ImportError` RED is near-zero evidence.** It proves the module didn't load, not that any
   assertion discriminates. The proof burden shifts to mutation testing.
3. **Briefs must require a FULL-suite run**, with each failure owned or named pre-existing *with the
   commit that proves it*. S2's "incomplete: none" was truthful and still misleading, because the
   brief only asked for its own file. A report scoped narrower than the claim it implies misleads
   however honest the seat.

Each was a **brief** defect, not a seat defect — the instruction was narrower than the principle.

## Honest limits of this close

- **Nothing is deployed and no live database was migrated.** Charter §4 and the guide's settled scope
  stop this branch at code + schema + tests.
- **`test_mcp_role_*` remain red** for MCP-role provisioning. Out of scope, proven pre-existing,
  explicitly not chased.
- **The §9 coverage map is a map.** Its test ids were verified to exist and the suite runs green, but
  "row N is adequately covered by test X" is a judgement the map asserts; only the cited tests were
  independently mutation-checked, not all 32 rows.

---

## Out-of-tree consumer audit — D-3's `withheld` key (2026-07-29)

Closes the limit struck above. DoD item 9 ran `tests/arb_memory/` only, so every consumer and every
test outside that directory was unexamined. Both were checked.

### The consumer set is closed at three call sites

`grep -rn "\.retrieve(" --include='*.py'` over the whole repo returns three in `src/`:

| Consumer | How it takes the element | Effect of the new outer key |
|---|---|---|
| `bus.py:262` | reads `hit["withheld"]` at `bus.py:297` | in-slice; **requires** the key |
| `mcp/read_tools.py` — `ReadMemoryTools.memory_search` | `_json_safe_search_hit` → `out = dict(hit)` | passes through to local MCP clients — D-3's intended effect |
| `mcp/tools.py:244` (public door) | returned raw, no reshaping | passes through to public MCP clients — the client-visible change D-3 accepted |

**`arb-eval` is not a consumer at all.** `tools/eval/arb_eval/` contains zero references to
`retrieve` or `memory_search`. Its single `withheld` occurrence (`pipeline.py:928`) is an unrelated
log string about suppressed matcher rows. The gap named in the close was empty.

**The one genuine out-of-tree data consumer is `learn_intake.memory_search`**
(`src/agent_redis_bridge/learn_intake.py:343`). It ships an inline script to `arb-prod` over ssh
that calls `retrieve` and **projects** each row to `{artefact_id, text}`, reading only `r["hint"]`.
An added outer key is inert to a projection. *Limit: this is a static argument — the path was not
executed against production.*

Not consumers, checked and excluded: `arb_files/mcp/door_wire.py` (docstring mentions only) and
`engines/agent_sdk.py` / `agent_sdk_mediation.py` (the string `mcp__arb-memory-local__memory_search`
appears in tool-name allowlists; neither parses a response body). No search-hit-shaped golden files
exist — `tests/golden/` and `tests/fixtures/` match neither `repo_pointer` nor `"hint"`.

### The MCP output schema permits extra keys — verified against the live server, not assumed

`tools/pi-sdk-host/fixtures/arb-memory-tools.json` carries no `outputSchema`, which would suggest no
output contract exists. **That fixture is stale.** FastMCP does derive one. Dumped verbatim from a
real `build_local_server(...)` via `list_tools()`, `memory_search`'s `outputSchema` in full:

```json
{"properties": {"result": {"items": {"additionalProperties": true, "type": "object"}, "title": "Result", "type": "array"}}, "required": ["result"], "title": "memory_searchOutput", "type": "object"}
```

The load-bearing part is the nested element schema — `outputSchema["properties"]["result"]["items"]`
— which is `{"additionalProperties": true, "type": "object"}`.

`additionalProperties: true` — the extra key is explicitly permitted. **This check could have
failed:** `false` there would have made every `memory_search` response fail validation. Recorded
because the reasoning that skipped it (trusting the fixture) was wrong even though the verdict held.

### The out-of-tree suite, against a control

Run discriminates: the loaded `store.retrieve` was confirmed to contain `"withheld": withhold`
before the run, and the base checkout's `store.py` contains the string zero times.

```
pytest tests/ --ignore=tests/arb_memory --ignore=tests/arb_messages -q
  impl  06e60eeb : 5 failed, 2397 passed, 12 skipped, 14 subtests passed  (261.58s)
  base  a91a6408 : 5 failed, 2397 passed, 12 skipped, 14 subtests passed  (274.81s)
```

Identical, and the same five ids both sides — `test_agent_dispatch_audit_panel.py` (×4) and
`test_agent_dispatch_run_id.py::test_run_id_appears_in_dry_run_envelope`. **Pre-existing; this slice
causes zero out-of-tree regressions.**

**`tests/arb_messages` (6 modules) could not be collected** — `KeyError: 'ARB_MESSAGES_TEST_DSN'` at
import, needing a separate PostgreSQL on :5599. Pre-existing and already recorded in an earlier
implementation report. Those modules
reference neither `retrieve` nor `memory_search`, so they are excluded on evidence, not convenience.

### Incidental, deliberately not chased

The `pi-sdk-host` tool fixture has drifted from what FastMCP now emits (no `outputSchema` key at
all). Real, out of scope, and chasing it is the anti-recursion guard's territory.
- ~~**`arb-eval` and other consumers of `store.retrieve` were not audited** for the new `withheld`
  key.~~ **CLOSED 2026-07-29** by the audit recorded in the next section.
