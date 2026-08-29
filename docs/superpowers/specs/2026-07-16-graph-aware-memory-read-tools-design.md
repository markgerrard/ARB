# Graph-aware ARB Memory read tools — memory_related / memory_references (design, r4)

**Date:** 2026-07-16 · **Author:** warm Fable (orchestrating session) · **Workflow:** A
**Backlog filing:** `docs/BACKLOG.md` § "Graph-aware ARB Memory read tools" (filed 2026-07-06)
**Revision:** r4 — folds design-review panel r0–r3 findings
(runs `panel-graph-read-tools-design-r0-20260716T113657Z-df4835`,
`panel-graph-read-tools-design-r1-20260716T114750Z-e6cefa`,
`panel-graph-read-tools-design-r2-20260716T115606Z-15ff8b`, and
`panel-graph-read-tools-design-r3-20260716T120301Z-2bf4c3`, all verdict block). Fold
changelogs at the end of this document.

## Context and goal

The vault graph exporter (`src/arb_memory/vault_export.py`) already computes two edge types
over the ARB Memory corpus and materializes them as markdown footers:

- **E1 — explicit references:** artefact A's body textually cites artefact B's id
  (backtick-quoted or bare-token match; `_reference_targets`, `vault_export.py:69-80`).
- **E2 — similarity:** min-pairwise pgvector cosine distance between A's and B's hint
  embeddings, top-k under a threshold (`_related_artefacts`, `vault_export.py:128-154`).

Only vault-synced consumers can walk those edges today. Every MCP consumer (claude.ai,
pi-sdk seats, agent-sdk seats, the local stdio door) is blind to them. This slice adds two
read-only MCP tools so recall becomes graph-aware: search finds an entry point, then the
model walks edges instead of composing more searches.

**Non-goals:** no new tables, no precomputed edge store, no new DB privileges, no write
surface, no changes to the exporter's rendered output, no psql access for seats
(per `arb-memory-read-via-mcp` and structural containment).

## Tool surface

### `memory_related(artefact_id, version=None, k=5, threshold=0.35) -> list[dict]`

E2 edges. Returns up to `k` artefacts whose min-pairwise hint distance to the subject is
`<= threshold`, ordered nearest-first (ties broken by artefact_id ascending). Each item:
`{"artefact_id": str, "version": int, "distance": float}` — `version` is the related
artefact's latest version, so the caller can `memory_get` it directly.

- `version=None` (default) resolves to the subject's latest version; an explicit `version`
  queries from that historical version's hint embeddings against the **latest** corpus
  (Mark's call 2026-07-16: optional version param, default latest).
- **Subject-hint mode semantics (r3 — pure caller-intent modes, replacing r2's
  DB-derived flag):** the write path soft-deletes older versions' `kind=artefact_index`
  hints on every new indexed write (`store.py:151-168`), so a live-hints-only query would
  return `[]` for almost every historical version — silently, indistinguishable from "no
  edges". `graph.related_artefacts` takes `subject_hints: 'live' | 'as_written'`, and the
  value is a **pure function of caller intent, never of database state** (r2's flag was
  computed from a "version < latest?" probe in one autocommit statement while the query
  ran in another — a panel-r2 P1 TOCTOU: a concurrent version publish between the two
  statements silently emptied an explicit-version call):
  - `version=None` (default MCP calls) → `'live'`: the `mine` CTE keeps today's exact
    predicate (`deleted_at IS NULL`), and the subject's latest version is resolved
    **inside the same SQL statement** (the `mine` CTE joins the `latest` CTE), so
    default calls are single-snapshot too.
  - explicit `version` (MCP calls) → `'as_written'`: the `mine` CTE drops the
    `deleted_at` filter — the as-written view of that version's hints, retired or
    otherwise soft-deleted included, used only as the query vector and never exposed.
    This applies whether or not the named version happens to be latest: "explicit
    version" always means the as-written view, which is coherent, documented, and free
    of any snapshot-dependent decision.
  - the exporter always passes `'live'` with its fetched latest version — today's exact
    predicate, so exporter behavior is unchanged **by construction**.
  The corpus side (`others`) always keeps `deleted_at IS NULL` (live latest hints only).
  Tool docstrings on both doors must state the two modes.
- `k` capped to 1..20; `threshold` validated to `0 < threshold <= 2.0` (cosine distance
  range). Defaults mirror the exporter (`k=5`, `threshold=0.35`,
  `VaultExportSettings.similarity_threshold`).
- Uses stored hint embeddings only — **no embed() call, no OPENAI_API_KEY dependency**
  (unlike `memory_search`).
- Subject with no hints **under the selected mode's predicate** returns `[]` (that is the
  "node exists, no edges" signal; a missing node raises — see Errors). The empty check and
  the edge query share one predicate and one statement — re-deriving "has hints?" with a
  different predicate would re-introduce the silent-empty trap (r2 panel, advisory).

### `memory_references(artefact_id, version=None) -> dict`

E1 edges, both directions. Returns
`{"references": list[str], "referenced_by": list[str]}` (sorted artefact ids).

- `references` (outgoing): ids cited in the subject's body — the specified version's body,
  default latest — matched against the id set of all latest artefacts, using the exporter's
  exact patterns (backtick-quoted id, or bare token for `art-…`/underscore ids with the
  lead/trail guards).
- **NULL-content subject (r1, panel finding):** a subject whose text `content` is NULL
  (binary-only artefact — schema allows it, `schema.sql:3-7`) yields `references: []`.
  Treat the body as `""` for the outgoing scan; do NOT reuse the exporter's `_body()`
  placeholder text (it could false-match). Backlink candidates with NULL content are
  likewise skipped.
- `referenced_by` (backlinks): latest-version artefacts whose body cites the subject's id.
  Backlinks are id-based and version-agnostic — always computed over the latest corpus
  regardless of the `version` argument. **Both doors' docstrings must state that `version`
  affects only the outgoing direction** (r1, panel finding: models will otherwise assume
  symmetry).
- Computed on demand, O(corpus) per call — trivially cheap at the current ~100-artefact
  corpus. **Backlink prefilter (r1, replaces LIKE):** resolve latest versions FIRST
  (`DISTINCT ON` CTE), then filter with an exact substring test —
  `strpos(content, %s) > 0` — then confirm in Python with the authoritative
  `reference_targets` patterns. `strpos` has no pattern semantics at all, so it is a pure
  over-approximation for every legal id (ids are unconstrained `text`; `LIKE` was rejected
  because backslash in an id **under-matches** — PostgreSQL's default LIKE escape — and an
  under-matching prefilter discards rows the Python confirmation never sees). The
  latest-first ordering matters: filtering before `DISTINCT ON` would surface an *old*
  version that matches when the latest doesn't.

### Errors (both tools)

Unknown `artefact_id`, or explicit `version` that doesn't exist (checked by a real row
probe against `artefacts`) → raise `ValueError("artefact not found")` (fail-loud;
deliberately unlike `memory_get`'s `None`-return, because a graph query on a missing node
is a caller error the model should see and correct, not a silent empty result it might read
as "no edges"). Out-of-range `k`/`threshold` → `ValueError`.

## Architecture — shared edge logic in `src/arb_memory/graph.py`, thin methods on BOTH tool classes

Approach chosen (2026-07-16, Approach A of three; C-duplicate rejected as drift-guaranteed,
B-store.py rejected as mixing regex-over-text semantics into a pure data-access module).

**Door reality (r1 — corrects r0's false premise):** the two doors use two independent tool
classes. The local stdio door builds `ReadMemoryTools` (`local_server.py:11`,
`read_tools.py:22`); the connector OAuth door builds `MemoryTools`
(`server.py:276-283`, `tools.py:72`) — no shared base, no delegation. Any new tool must
therefore land as methods on **both** classes. The single-implementation guarantee lives
one level down, in `graph.py`.

New module `src/arb_memory/graph.py` owns both edge definitions:

- `ART_ID_RE`, reference lead/trail guard patterns — **moved** from `vault_export.py:57-59`.
- `reference_targets(body, artefact_id, export_ids) -> list[str]` — **moved** verbatim from
  `vault_export.py::_reference_targets`.
- `related_artefacts(conn, artefact_id, version, *, k, threshold, subject_hints='live') -> list[tuple[str, int, float]]`
  — **moved** from `vault_export.py::_related_artefacts` with the version-carrying
  extension. `subject_hints` is set from caller intent only (see mode semantics above).
  **Full query invariant (r1+r2+r3, panel findings — every piece moves together):**
  - `others` CTE additionally selects `l.version` (threaded from the `latest` CTE) and
    always keeps `deleted_at IS NULL`;
  - `mine` CTE: in `'live'` mode keeps `deleted_at IS NULL` (today's exact predicate) and,
    for `version=None` callers, resolves the subject's latest version via the `latest`
    CTE **within this same statement** (single snapshot — no separate resolution step
    feeding the query); in `'as_written'` mode filters only on
    `(artefact_id, artefact_version)`;
  - projection `SELECT o.aid, o.version, MIN(o.embedding <=> m.embedding) AS dist`;
  - `GROUP BY o.aid, o.version` (one version per aid by construction);
  - `HAVING MIN(o.embedding <=> m.embedding) <= %(threshold)s` — retained unchanged (r2:
    stated explicitly so "every piece" reads as exhaustive);
  - `ORDER BY dist ASC, o.aid ASC` — **named alias, never a positional ordinal** (the r0
    text inherited `ORDER BY 2`, which silently re-sorts by version once a column is
    inserted);
  - `LIMIT %(k)s` — retained unchanged;
  - row mapping `[(row[0], int(row[1]), float(row[2])) for row in rows]`;
  - return field order documented as `(artefact_id, version, distance)`.
- `references(conn, artefact_id, version) -> dict` — **new**: outgoing scan of the subject
  body (NULL → `""`) + backlink scan per the latest-CTE-then-`strpos` recipe above, both
  confirmed via `reference_targets` patterns.
- `artefact_exists(conn, artefact_id, version=None) -> bool` — **new** helper for the
  fail-loud check ONLY. It never resolves `None` to a number; for `version=None` it
  probes "any row with this artefact_id", for explicit versions the exact row. It gates
  raise-vs-proceed, never the mode and never the query's version argument.
- `latest_version(conn, artefact_id) -> int | None` — **new** helper used by
  `memory_references` ONLY (its outgoing scan must fetch one concrete body row). It is
  NOT used by `memory_related` on any path (r4, panel finding: a helper-resolved version
  fed into the `'live'` query re-opens the two-statement default-call race —
  `memory_related` passes the caller's `version` argument through to
  `related_artefacts` verbatim, `None` included).
- **Shared glue helpers (r2, anti-drift; r3-adjusted):** param validation (`k`/`threshold`
  range checks), the fail-loud existence probe, and mode selection (`version is None` →
  `'live'`, explicit → `'as_written'` — caller-intent only, no DB probe) live as pure
  functions in `graph.py`, called by BOTH tool classes — the classes duplicate only their
  door-native concerns (rate-limit keying, output shaping), so the semantics cannot drift
  between doors. Latest-version resolution for the query itself happens inside the query
  statement (see invariant), not in a helper.

`vault_export.py` imports `reference_targets` and `related_artefacts` from `graph` (its own
private copies and pattern constants deleted); its `_footer` unpacking adapts to the
3-tuple (`for t, _v, d in related`) and its **rendered output must be byte-identical**.
With `subject_hints` defaulting to `'live'` the exporter runs today's exact subject predicate, so
byte-identity holds by construction — AND it remains a named test obligation (the paired
counter-case test below), not an assumption.

**Tool-class glue (both classes, same shape):**

- `ReadMemoryTools` (`read_tools.py`) gains async `memory_related` / `memory_references`,
  per-tool recipes (r4 — single-valued, `None`-preserving):
  - `memory_related`: validate params → `artefact_exists` fail-loud probe (never resolves
    `None`) → `_check_graph_allowed()` (process-global bucket, mirroring
    `_check_search_allowed` at `read_tools.py:44-50`) → call
    `graph.related_artefacts(conn, artefact_id, version, ...)` **passing the caller's
    `version` argument verbatim — `None` stays `None`** (mode and latest-resolution both
    live inside `graph.py`/the statement) → shape JSON-safe output.
  - `memory_references`: validate → `artefact_exists` probe → rate limit →
    `latest_version` only here, to pick the concrete body row when `version=None` → call
    `graph.references` → shape output.
- `MemoryTools` (`tools.py`) gains the same two methods with the **connector door's
  idioms**: `access_token` keyword, per-access-token graph bucket mirroring
  `_check_search_allowed` at `tools.py:92-100` (a process-global bucket there would let one
  token consume every client's quota — r1, panel finding).

## Rate limiting

Graph queries get their own bucket, separate from search, on **each** class:

- `LocalReadSettings.graph_rate_per_min: int = 30` (`read_tools.py`) — process-global
  window, same pattern as local search.
- `Settings.graph_rate_per_min: int = 30` (`mcp/config.py`) — per-access-token windows,
  same pattern as connector search. One shared bucket covers both graph tools per scope
  (token or process).

Rationale: the pgvector cross-join and the corpus scan are cheap today but unmetered; they
get the same guard search got, with each door keeping its native keying model.

## Registration — both doors at once

- **Local stdio door** (`mcp/local_server.py`): two `server.add_tool(...)` lines binding
  the `ReadMemoryTools` methods.
- **Connector OAuth door** (`mcp/server.py:404-408` region): thin async wrappers calling
  `tools.memory_related` / `tools.memory_references` on the `MemoryTools` instance, with
  docstrings that become the tool descriptions (existing pattern), two `add_tool` lines.
- **Scope:** no new scope, no reconnect ceremony. The connector door gates the whole server
  with `required_scopes=["memory.read"]` (`server.py:354`). The
  `chatgpt-connector-scope-grant` DCR ceremony applies only to NEW scopes — read tools ride
  the existing grant.
- **Grants:** both roles already hold SELECT on the only two tables the graph queries touch
  — connector role at `mcp/grants.py:77-91`, local/vault reader role at `mcp/grants.py:6-27`.
  No grant change.

## Testing

TDD (luna@high implements). **Test-tier honesty (r1, panel finding):** the SQL-backed
tests are NOT hermetic — the suite's DB fixture silently skips without a live PostgreSQL
DSN, and a green run with every SQL assertion skipped is the vacuously-green shape. The
plan must define two named gates:

1. **No-DB unit gate** — pure matcher (`reference_targets` cases: backtick, bare-token
   guards, self-exclusion, NULL/empty body) and tool-class wrapper tests (param validation,
   rate-limit bucket independence and per-token keying, JSON shaping, ValueError
   contracts, and **sentinel preservation** (r4): a default `memory_related` call on EACH
   class reaches `graph.related_artefacts` with `version is None` — pins that no wrapper
   pre-resolves the version and re-opens the two-statement race). Hermetic, always runs.
2. **PostgreSQL-backed gate** — runs with the canonical dev/test `ARB_MEMORY_DSN` and
   **fails if the named SQL tests skip** (assert expected pass/skip counts). Covers:
   - `related_artefacts` ordering adversarially: ≥2 candidates whose latest-version order
     is the REVERSE of their distance order (would catch a positional-ORDER-BY regression
     that membership-only tests pass), plus an equal-distance aid tie-break;
   - subject-hint modes, PAIRED assertions (r2, r3): (a) v2 write retires v1's index
     hint; `memory_related(version=1)` returns non-empty via the retired-hint subject
     embedding (`'as_written'`); (b) counter-case: a soft-deleted hint on the LATEST
     subject version yields NO edge on a DEFAULT call (`version=None`, `'live'`) and
     leaves the exported footer byte-identical to current behavior; (c) mode boundary:
     an EXPLICIT `version` equal to latest with a soft-deleted hint DOES return the edge
     (`'as_written'` applies to explicit calls regardless of recency — pins the
     caller-intent rule); corpus side excludes deleted/old hints in both modes;
   - backlink prefilter: backslash-containing id (the `strpos` soundness case), `%`/`_`
     ids, NULL-content candidates, and latest-version precedence (old version matches,
     latest doesn't → no backlink);
   - exporter equivalence: `test_vault_export.py` passes unchanged AND a byte-identical
     footer assertion for a fixture with references + related entries.
3. Registration tests on both doors: `test_local_server.py` (local) and
   `tests/arb_memory/test_mcp_tools.py` (connector `MemoryTools` methods incl. per-token
   graph rate limiting) — the connector class was untested in r0's plan (panel finding).

**e2e gate (arc-final):** live call of both tools through at least the local door against
the real dev DB, plus a connector-door smoke (per `live-verification-catches-cli-glue`).

## Deployment notes

- pi-sdk/agent-sdk seat wrappers need `tools/pi-sdk-host/install.sh` re-run after deploy
  (per memory `pi-sdk-mcp-client-coverage`).
- Connector door: deploy the service; NO client reconnect required (no new scope — see
  Registration above).
- Exporter runs under a different role/process than the MCP doors; the hoist to `graph.py`
  changes imports only. No grants change (citations above).

## Process — review regime for this arc (Mark, 2026-07-16)

Every stage artifact (this design/spec, the implementation plan, the implementation) goes to
a four-seat panel — **grok, agy-print, codex sol@high, cold-Opus subagent** — looping
remediation until **0 P0 and 0 P1**. Implementation is **luna@high, TDD**. After
implementation review passes, the **e2e test gate** closes the arc. Review hygiene: seats
write reports outside the repo during independent phases (CLAUDE.md § review hygiene).

## Fold changelog — r0 → r1

Panel run `panel-graph-read-tools-design-r0-20260716T113657Z-df4835` (agy block/P0,
grok block/P0, codex-sol block/P1, cold-opus needs-changes/P1). Every finding's hinge claim
was re-verified against the code before folding. Dispositions:

| # | Finding (seats) | Disposition in r1 |
|---|---|---|
| 1 | Dual-door class premise false — connector uses `MemoryTools`, not `ReadMemoryTools` (agy P0, grok P0, codex P1-1) | Architecture rewritten: `graph.py` single logic, thin methods on BOTH classes; connector keeps per-token idioms |
| 2 | `Settings.graph_rate_per_min` missing; connector limiter keying (agy F2, grok F2, codex P1-1) | Rate-limiting section: field on both settings classes; per-token buckets on connector |
| 3 | 3-tuple SQL: positional `ORDER BY 2` re-sorts by version; `GROUP BY` missing; `_footer` unpack (agy F3, grok F3, codex P1-2) | Full query invariant specified (named ORDER BY, GROUP BY aid+version, row mapping, footer unpack, byte-identical footer test); adversarial ordering test mandated |
| 4 | Historical version always `[]` — older index hints soft-deleted (agy F4, cold-opus P1-1, codex P2-1) | Semantic decision folded: `mine` CTE drops `deleted_at IS NULL` (subject side only); caveat documented; DB test mandated. **Flag for Mark:** this refines his optional-version call to make it deliver; alternative (document-as-live-hints-only) rejected as a silent-empty trap |
| 5 | NULL-content subject crashes outgoing scan (grok F4, cold-opus P1-2) | Tool surface: NULL body → `""`, `references: []`; explicit non-use of exporter placeholder |
| 6 | `LIKE` prefilter under-matches on backslash ids (codex P1-3; supersedes the `%`/`_` escaping P2s from agy/grok) | Prefilter replaced with `strpos(content, %s) > 0`; backslash test mandated |
| 7 | SQL tests skip silently without DSN — vacuously-green gate (codex P1-4) | Testing rewritten: two named gates; PostgreSQL gate fails on skip |
| 8 | `test_mcp_tools.py` (connector class) absent from test plan (agy F5) | Added as named obligation |
| 9 | Backlink `LIKE`-before-`DISTINCT ON` returns stale versions (agy F6) | Latest-CTE-first recipe specified |
| 10 | Scope citation off-by-one; false "write-side per-tool scope" side-claim (cold-opus P2, codex P2-2) | Citation corrected to `server.py:354`; side-claim removed; grants cited (`grants.py:77-91`, `:6-27`) |
| 11 | Version-asymmetry docstring requirement (grok F5) | Tool surface: docstring obligation on both doors |

r0 claims that survived review unchanged: fail-loud `ValueError` (endorsed by all seats),
stored-embeddings-only (no embed call), on-demand computation, k/threshold caps, no new
scope/grants, both-doors-at-once.

## Fold changelog — r1 → r2

Panel run `panel-graph-read-tools-design-r1-20260716T114750Z-e6cefa` (agy approve/none,
cold-opus approve/P2, grok needs-changes/P2, codex-sol block/P1). All seats confirmed folds
1–3 and 5–11 resolved. Dispositions:

| # | Finding (seats) | Disposition in r2 |
|---|---|---|
| 12 | r1's unconditional `mine`-CTE `deleted_at` drop widens latest/exporter behavior, contradicting the byte-identity invariant — concrete counterexample: soft-deleted latest-version subject hint creates an edge/footer today's exporter doesn't render (codex-sol P1; same texture flagged by grok P2-1) | Historical semantics re-scoped to a flag-gated mode: `include_retired_subject_hints=False` default keeps today's exact predicate (exporter + default calls); True only when explicit `version` < latest. Paired PostgreSQL assertions mandated (historical non-empty + latest-deleted counter-case with byte-identical footer) |
| 13 | Invariant list omitted `HAVING`/`LIMIT` — "every piece" read as exhaustive (grok P2-2) | Both clauses stated explicitly in the invariant list, retained unchanged |
| 14 | Dual-class glue (validation/version-resolution) can drift between doors (grok P2-3) | Shared pure helpers in `graph.py`; classes duplicate only door-native rate-limit keying and output shaping |
| 15 | Caveat text cited a manual-delete surface that doesn't exist in the codebase (cold-opus P2) | Caveat rewritten around the real auto-retire path; historical mode described as retired-or-otherwise-soft-deleted hints, query-side only |

## Fold changelog — r2 → r3

Panel run `panel-graph-read-tools-design-r2-20260716T115606Z-15ff8b` (agy approve/none,
grok approve/none, cold-opus approve/P2-advisory, codex-sol block/P1). Three seats
confirmed folds 12–15 resolved. Dispositions:

| # | Finding (seats) | Disposition in r3 |
|---|---|---|
| 16 | TOCTOU: r2's flag was derived from a "version < latest?" probe in one autocommit statement while the query ran in another — a concurrent version publish between them silently empties an explicit-version call (codex-sol P1; both read doors verified `autocommit=True`, `read_tools.py:31-38`, `config.py:93-95`) | Mode is now a pure function of caller intent (`version=None` → `'live'`, explicit → `'as_written'`) — no DB-derived decision exists to go stale. Default calls resolve latest INSIDE the query statement (single snapshot). Explicit version==latest now means the as-written view (documented, mode-boundary test (c) pins it). Exporter passes `'live'` explicitly — today's exact predicate by construction |
| 17 | Empty-signal wording could let an implementer re-derive "has hints?" with a different predicate than the query (cold-opus P2 advisory) | Tool-surface text now requires the empty check and edge query to share one predicate and one statement |

## Fold changelog — r3 → r4

Panel run `panel-graph-read-tools-design-r3-20260716T120301Z-2bf4c3` (agy approve/none,
grok approve/P2, cold-opus approve/P2, codex-sol block/P1). All seats confirmed folds
16–17 close the r2 TOCTOU. Dispositions:

| # | Finding (seats) | Disposition in r4 |
|---|---|---|
| 18 | Contradictory implementation instructions survived the r3 fold: `latest_version` helper "for `version=None` resolution" + generic "resolve/verify version" glue wording vs the in-statement rule — the helper-first reading erases the `None` sentinel and restores a two-statement DEFAULT-call race (codex-sol P1) | Recipes made single-valued: `memory_related` passes the caller's `version` verbatim (`None` stays `None`); new `artefact_exists` probe never resolves `None` and gates only raise-vs-proceed; `latest_version` scoped to `memory_references` only; per-tool glue recipes replace the generic wording; sentinel-preservation unit test mandated on both classes |
| 19 | Stale r2 wording: "With the flag defaulting to False", title still "(design, r2)" (cold-opus P2, grok P2) | Both fixed; full-document sweep for flag-era wording done |
