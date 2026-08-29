# ARB Memory vault graph export — typed wikilink edges (design)

> Status: design, **round 2 panel-confirmed 2026-07-06 — ready for plan** (3-seat independent
> panel both rounds: codex + agy-print + cold-Opus). Round 1: nine findings, one P1 (the `\b`
> boundary false-positive class), no architectural objection — all addressed. Round 2
> (targeted confirmation): codex `approve`/none, agy `approve`/none (independently traced the
> new E1 rule closing all four failure modes simultaneously), cold-Opus `approve`/P2 with one
> fix-induced catch (sentence-final `.` suppression), folded in — see the round-2 callout in
> § E1 and the panel records at the end. Extends (does not replace) the
> 2-round panel-confirmed base design
> `docs/superpowers/specs/2026-07-06-arb-memory-read-model-export-design.md` and its shipped,
> prod-deployed implementation (`src/arb_memory/vault_export.py`, live-proven 2026-07-06 with
> 91 real artefacts). Motivated by a Quartz-viewer assessment panel (report not included in this
> copy; unanimous RECONSIDER): all three seats found the vault's graph value inert because the
> exporter emits no links and hash-suffixed filenames can't resolve name-based links. Mark's
> explicit steer (2026-07-06): the goal is the **graph** — ARB Memory already holds the
> semantic relationships (textual artefact references, pgvector similarity, review-cites-
> artefact structure); the exporter should emit them as wikilinks so the vault becomes a real
> map of how reviews, specs, and lessons relate. Viewer choice (Quartz vs Python-native) is a
> separate, later decision — links and backlinks render in any of them.

## Problem

Each exported vault file is an island. The relationships between artefacts exist in ARB Memory
but are not materialized in the export:

- Bodies reference other artefacts as plain text (e.g. ``see ARB Memory artefact
  `art-49c566cc076f374a` ``) — human-legible, not machine-traversable.
- Every artefact carries pgvector embeddings (via its linked `hints` rows) encoding semantic
  similarity to every other artefact — currently unused by the export entirely.

A vault with no edges gives a graph view of disconnected dots and empty backlinks everywhere
(the Quartz panel's decisive finding), and — more fundamentally — fails the actual goal: a
browsable map of accumulated ARB knowledge, independent of which app renders it.

## Empirical grounding (measured on the real prod corpus, 2026-07-06)

- **E1 (explicit references):** 59 of 91 exported files mention at least one other artefact by
  `art-<hex>` id in their body; 104 total mentions; 58 unique ids mentioned; **58/58 resolve**
  to an artefact present in the vault. The explicit link graph is real, dense, and
  fully resolvable — it just isn't emitted as links.
- **E2 (semantic similarity):** **91/91** latest-version artefacts have ≥1 live
  (`deleted_at IS NULL`) linked hint row carrying a `vector(1536)` embedding (180 hint rows
  total). Similarity edges can be computed for the whole corpus with pure SQL over columns the
  `arb_vault_export` role can already `SELECT` — no new embeddings are computed, so **no
  `OPENAI_API_KEY` dependency** and no privilege change.

## Edge sources

### E1 — explicit textual references (deterministic, high precision)

Scan each exported body for mentions of artefact ids **that exist in the current export set**,
under these rules (each rule addressed a specific round-1 panel finding — see callout below):

- **Boundaries are custom lookarounds, never `\b`**: leading
  `(?<![A-Za-z0-9._/-])`, trailing `(?![A-Za-z0-9_/-]|\.[A-Za-z0-9._/-])`. The lookarounds
  treat `-`, `_`, `.` and `/` as word constituents, so a shorter id can never match inside a
  longer hyphenated id, and ids embedded in URLs or file paths do not match (a deliberate
  precision-biased call: a path/URL mention is excluded rather than guessed at). The trailing
  side treats a `.` as a boundary-breaker **only when followed by another id character** —
  so `art-<hex>.md` (extension dot, part of a longer token) is excluded, but a bare id ending
  a sentence (`… see art-<hex>.`) still matches. Candidate ids are scanned longest-first as
  belt-and-braces.
  > **Round 2 panel finding, addressed (cold-Opus P2; agy independently observed the same
  > behavior):** round 1's fix added `.` to a symmetric lookahead class, which also suppressed
  > sentence-final bare mentions — the rule couldn't tell an extension-dot from a
  > sentence-dot. Refined as above per cold-Opus's concrete suggestion; a sentence-final
  > positive test is added to the testing plan. Note the round-1 "58/58 resolvable" figure was
  > measured with a looser scan than this rule; the plan-stage calibration should re-measure
  > E1 yield under the final rule.
- **`art-[0-9a-f]{16}` ids**: match bare or backtick-wrapped.
- **Underscore-containing named ids** (e.g. `project-a_overview`): match bare or
  backtick-wrapped — underscores don't occur in natural English prose, so bare matches are
  collision-safe.
- **All other named ids** (hyphen-only, e.g. `spec-a`, `mcp-auth`,
  `arb-pi-orchestration-lessons-2026-07-05`): match **only when backtick-wrapped**
  (`` `id` ``). No length floor — export-set membership plus the backtick intent signal is the
  guard. Bare hyphenated tokens never link, because a hyphenated English phrase
  ("design-review") is indistinguishable from a hyphenated id by shape alone at any length.
- A file never links to itself; each distinct target appears once regardless of mention count.

> **Round 1 panel findings, addressed:** (agy, P1, proven via live regex demo) the original
> "whole-word token match" implied `\b`, which treats `-`/`_` as boundaries — so
> `deploy-x` would falsely match inside `deploy-x-completed`; fixed with the lookaround class
> above. (agy, P2) the original ≥10-char floor silently dropped short valid ids like `spec-a`
> or `mcp-auth`; replaced by the backtick rule, which links them when cited with intent and
> has no floor. (agy, P2) URL/path-embedded ids matched under `\b`; the lookaround class now
> includes `/` and `.`, excluding them by construction. (cold-Opus, P2) the floor never
> protected against bare *hyphenated English phrases* ("design-review" is 13 chars); resolved
> by never bare-matching hyphen-only ids at all. The agy (no floor, trust export-set
> membership) and cold-Opus (backtick-restrict named ids) recommendations pulled in opposite
> directions; the rule above adopts both where they're each strongest: no floor for
> *backticked* mentions, backticks required where bare matching is genuinely unsafe.

### E2 — semantic similarity (pgvector, zero new privileges)

For each latest-version artefact, compute nearest neighbours over **other** artefacts' live
(`deleted_at IS NULL` — same filter as every other hint read path) linked-hint embeddings
using the existing pgvector cosine operator (`<=>`), taking the minimum pairwise distance
between the two artefacts' hint sets as the artefact-pair distance. Emit the top-k neighbours
(default **k=5**) subject to a maximum-distance threshold, exposed as an env-tunable
(`ARB_VAULT_EXPORT_SIMILARITY_THRESHOLD`) whose **default** is pinned at plan stage by a
calibration query against the real corpus. Because min-aggregation is single-linkage, an
artefact with many hints has more chances to score a close pair and can become a spurious hub —
so the calibration must report the **resulting edge-degree distribution** (per-artefact in/out
degree under the candidate threshold), not just the raw distance distribution, and pick the
cutoff on both. Ordering is fully deterministic: `(distance ASC, artefact_id ASC)`.

Properties: read-only over `hints`+`artefacts` (exactly the existing grant), no embedding
computation, deterministic given DB state, O(91 × k-NN) per nightly run — negligible.

> **Round 1 panel findings, addressed:** (agy, P2) soft-deleted hints were excluded from the
> tag path but the E2 query didn't state the same filter — now explicit above, with a test.
> (agy, suggestion) threshold now env-tunable with a calibrated default rather than
> hardcoded. (cold-Opus, P2) single-linkage hub bias named; calibration now required to
> validate edge-degree, not just distances. cold-Opus independently **verified** the
> privilege/no-OPENAI claim against `grants.py` and `schema.sql` (true: stored vectors
> compared server-side, no query embedding), and verified idempotency holds structurally
> because `export_vault` full-rewrites every file — the footer is recomputed, never appended.

### E3 — audit/panel citation structure: explicitly deferred (named non-goal)

Panel-vote `refs` live in `audit_events` payloads — a table the exporter role has **zero**
access to by deliberate, panel-approved design (base spec guardrail #1). Widening the
exporter's read scope requires re-panelling that guardrail, and the practical value is largely
already captured: reviews stored *as artefacts* cite their subject artefacts in body text,
which E1 picks up. Audit-derived edges are deferred until they justify a guardrail re-panel;
this spec does not touch grants.

## Rendering (viewer-agnostic)

1. **Frontmatter `aliases`:** every file gains `aliases: ["<artefact_id>"]` (JSON-escaped, same
   convention as the other caller-influenced fields). This is a **convenience layer, not the
   load-bearing link mechanism**: Quartz resolves `[[artefact-id]]` wikilinks through aliases
   (Frontmatter + AliasRedirects plugins), but Obsidian does *not* resolve a raw `[[alias]]`
   as a link destination — it only surfaces aliases in link autocomplete and unlinked-mention
   workflows, inserting `[[stem|alias]]` when selected. Every link this exporter **emits**
   therefore targets the exact filename stem (`[[<filename-stem>|<artefact_id>]]`), which
   resolves in every tool with no alias support needed; and any future content generator that
   writes artefact cross-references (e.g. the prospective ARB Wiki ingestion, see Pipeline
   context below) should either cite ids in backticks for E1 to materialize, or emit
   stem-targeted links itself — never bare `[[artefact-id]]`.
   > **Round 1 panel finding, addressed (codex, P2, verified against current Obsidian/Quartz
   > docs):** the original wording claimed both tools "resolve `[[artefact-id]]` through
   > aliases," which is false for Obsidian; reworded as above. The footer mechanism was never
   > affected (it always used stem-targeted links).
2. **Body stays verbatim** — preserves the base spec's panel-approved decision. No inline
   rewriting: mutating body text risks corrupting ids inside code fences/backticks, and inline
   links add little once backlinks exist.
3. **Generated footer**, clearly delimited below the body:

   ```markdown
   <!-- generated by vault_export: graph footer — do not edit; regenerated nightly -->

   ## References
   - [[<filename-stem>|<artefact_id>]]           (one per E1 target, id-sorted)

   ## Related
   - [[<filename-stem>|<artefact_id>]] (distance 0.31)   (E2 top-k, distance-sorted)
   ```

   Wikilinks target the exact filename stem (guaranteed resolution, no alias lookup needed)
   with the artefact id as display text. Either section is omitted when empty; an artefact
   with no edges at all gets no footer. The footer is always preceded by a blank line, emitted
   unconditionally — bodies are caller content and are not guaranteed to end with a newline
   (codex round-1 note). Graph/backlink engines (Quartz, Obsidian, or a Python viewer) count
   links anywhere in the file, so the footer carries the full graph.

## Idempotency & determinism

The footer is a pure function of DB state: E1 from body text + export-set membership, E2 from
stored embeddings with pinned `(distance, id)` ordering and pinned k/threshold. The existing
rerun-idempotent-except-`exported_at` test extends unchanged to cover footers.

## Testing plan (extends `tests/arb_memory/test_vault_export.py`)

- E1 positive: bare `art-<hex>` mention linked; backticked `art-<hex>` linked; bare
  underscore-id linked; **backticked short hyphen-only id** (`` `spec-a` ``) linked;
  **bare id ending a sentence** (`see art-<hex>.` followed by space/EOL) linked (round-2
  refinement); duplicate mentions deduplicated to one footer entry.
- E1 negative (each a round-1-identified failure mode): shorter id NOT matched inside a longer
  hyphenated id (`deploy-x` vs `deploy-x-completed` — the agy prefix-collision proof, as a
  fixture); id embedded in a URL or path NOT matched; **bare** hyphen-only id NOT matched
  (`design-review` as prose); mention of a non-export-set id NOT linked; self-mention NOT
  linked; id character-adjacent to other id-chars (`xart-<hex>`) NOT matched.
- E2: three artefacts with **explicit, hand-constructed 1536-dim vectors** passed directly to
  `upsert_hint` — an identical/near-identical pair (distance ≈ 0) and an orthogonal third
  (distance ≈ 1) — asserting near pair gets mutual Related entries, far one excluded by
  threshold, ordering pinned by `(distance, id)`. NOT `fake_embed`-derived strings, whose
  pairwise distances are hash accidents rather than chosen values (codex round-1 finding).
  Plus: a **multi-hint** artefact where only one hint pair is close (proves min-aggregation);
  and a **soft-deleted** hint whose embedding would create an edge — assert it does not
  (deleted_at filter in the E2 query, not just the tag path).
- Footer: byte-deterministic across reruns (except `exported_at` in frontmatter, as today);
  body text above the footer marker byte-identical to `content`, including when `content`
  lacks a trailing newline; `aliases` present and JSON-escaped; empty-graph artefact gets no
  footer at all.

## Pipeline context — ARB Wiki (openwiki-inspired ingestion, upstream, separate feature)

Mark's broader intent (2026-07-06): this graph exporter is the **output half** of a larger
pipeline. The prospective **input half** — "ARB Wiki" — takes inspiration (bits, not wholesale
adoption) from `langchain-ai/openwiki`, an LLM-agent CLI that generates and maintains a
markdown documentation wiki (`openwiki/` directory) from a repository's code. The ARB shape of
that idea: generate wiki-style pages for the repos ARB works in, **store the pages as ARB
Memory artefacts** through the existing single-writer path, and let *this* exporter emit them
into the vault — where E1 materializes their cross-references into the graph. Filed in
`docs/BACKLOG.md` ("ARB Wiki"); not designed here. Two boundary facts this spec already
settles for it: (a) generated pages should cite other artefacts by backticked id (or emit
stem-targeted `[[stem|id]]` links directly) — never bare `[[artefact-id]]`, per the Obsidian
alias finding; (b) ingestion happens repo → ARB Memory via the write path — **never** via the
vault directory, which stays one-way outbound per base-spec guardrail #3.

## Non-goals

- **Viewer selection** — Quartz vs Python-native viewer is decided separately; this feature
  makes the graph real for whichever is chosen.
- **Audit-derived edges (E3)** — deferred, see above; no grants change in this feature.
- **Inline body rewriting** — rejected, see Rendering.
- **Tag pages / tag-graph** — tags are already in frontmatter; viewers derive tag browsing
  natively without exporter work.

## Self-review notes

- Both edge sources are grounded in measurements taken on the real prod corpus this session,
  not assumed: E1's 58/58 resolvability and E2's 91/91 embedding coverage are quoted above
  with their measurement queries reproducible from the vault files and `hints`/`artefacts`.
- The one privilege-adjacent temptation (audit refs) is explicitly fenced off rather than
  silently absorbed — guardrail #1 of the base spec survives untouched.
- Placeholder scan: the two values deliberately left open (E2 distance threshold default;
  E1's exact lookaround regex spelling) are assigned to the plan stage with the decision
  procedure stated, not hand-waved.

## Round 1 panel record

Independent 3-seat panel (codex-bridge-dev, agy-bridge-dev, cold-Opus subagent), run
`panel-vault-graph-spec-20260706T175410Z-e9192a`. Stances: codex `needs-changes`/P2 (Obsidian
alias portability overstated; E2 tests need explicit vectors), agy `needs-changes`/P2 with one
**P1** (the `\b` word-boundary false-positive class, proven with a live regex demo; plus the
length-floor false-negative, URL/path collisions, soft-delete gap in E2, and test-plan gaps),
cold-Opus `approve`/P2 (hyphenated-English bare-match FP, single-linkage hub bias, three test
gaps — and independent **verification** that the privilege/no-OPENAI claim is true and that
idempotency holds structurally via full-rewrite). No seat objected to the architecture (edge
sources, footer rendering, E3 deferral, guardrail preservation). All nine findings addressed
inline via "Round 1 panel findings, addressed" callouts; where agy (drop the floor) and
cold-Opus (backtick-restrict) pulled in opposite directions on named-id matching, the
resolution adopts both where each is strongest — no floor for backticked mentions, backticks
required exactly where bare matching is unsafe.

## Round 2 panel record

Same three seats, targeted confirmation, run
`panel-vault-graph-spec-r2-20260706T180246Z-d03d1a`, `supersedes` round 1's run. Stances:
codex `approve`/none (re-verified the alias wording against current Obsidian/Quartz docs), agy
`approve`/none (independently traced the new E1 rule character-by-character: all four round-1
failure modes close simultaneously, none reopened; verified E2/alias/testing/pipeline-context
sections), cold-Opus `approve`/P2 (same conclusion on the four modes, plus one genuinely
fix-induced catch: the `.` added to the boundary class to close URL/path false-positives also
suppressed sentence-final bare mentions — refined per its concrete suggestion, see the round-2
callout in § E1; it also flagged that the round-1 "58/58 resolvable" was measured with a
looser scan, so plan-stage calibration re-measures E1 yield under the final rule). No new
findings otherwise; no P0/P1 in either round. Design is ready for the implementation plan.
