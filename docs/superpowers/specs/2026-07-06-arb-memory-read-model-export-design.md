# ARB Memory read-model export — one-way markdown vault (design)

> Status: design, **round 2 panel-confirmed 2026-07-06 — ready for plan** (3-seat independent
> panel both rounds: codex + agy-print + cold-Opus). Round 1: no P0/P1 on the core
> architecture — all three independently verified the guardrail coverage, schema claims, and
> grants-reuse soundness against real source, and 2-of-3 explicitly confirmed the mac-mini cron
> placement does not violate the architecture doc's write/single-writer doctrine. Eight P1/P2
> findings (2 cross-validated by independent reviewers via different code paths) were addressed
> in a revision — see the "Round 1 panel finding, addressed" callouts at each fix site. Round 2
> (targeted confirmation on the revision): codex and agy both `approve`/severity-none; cold-Opus
> `approve`/P2 with two small non-blocking notes, both folded in (see "Round 2 finding,
> addressed" callout and the Round 2 record at the end). No open findings remain. Filed from
> `docs/BACKLOG.md` → "ARB Memory read-model export — one-way markdown vault for human
> browsing" (concept converged 2026-07-05 via `ARB-2026-07-OBSVAULT-R1`, a 4-pattern adoption
> review of `claude-obsidian`: 3-of-4 seats — cold-Opus, agy, codex — recommended ADAPT;
> pi-GLM's REJECT rested on a premise verified false post-panel: `src/arb_memory/visibility.py`
> serves live seat/task activity, not memory content, so it does not already fill this need).
> The open design questions BACKLOG.md listed are answered below, grounded in the real schema
> and existing grants code, not re-derived from first principles.

## Problem

No non-LLM-mediated way exists today to browse ARB Memory's accumulated artefacts (specs,
decisions, handoffs, lessons — anything written verbatim to the `artefacts` table). The only
access path is `memory_search`/`memory_get`/`memory_recent`
(`src/arb_memory/mcp/read_tools.py`), all of which require an LLM client in the loop to
formulate a query. SSH+psql direct access is already discouraged for casual reads (memory
`arb-memory-read-via-mcp`: prefer the MCP tools, they're fresher than psql and don't require
a live DB session). A human wanting to `grep` "what has ARB decided about X" has no path that
doesn't involve either opening an LLM client or hand-rolling a `psql` query.

## Non-negotiable guardrails (converged 2026-07-05, hard preconditions — not design choices)

1. Exporter runs as a **read-scoped DB principal with no write grant** to Memory/Files —
   verified via a grant audit at deploy time, not assumed.
2. The vault host holds **no ARB-write credential** at all.
3. **Never** wire an `/autoresearch`-style ingest tool (or any future importer) at the vault
   path — the one change that would flip this from a P2-advisory read-model export to a real
   Memory-plane trust-boundary issue requiring re-panel.
4. A **standing test** asserting the exporter's grants stay read-only, so privilege drift is
   caught, not assumed away.

## What already exists that this design reuses (grounded in source, 2026-07-06)

- **Schema** (`src/arb_memory/schema.sql`): `artefacts(artefact_id, version, content,
  content_bytes, content_mime, repo_pointer, content_hash, source, author, created_at)`,
  primary key `(artefact_id, version)` — **artefacts are immutable and versioned, never
  deleted** (no `deleted_at` column, unlike `hints`, which does have one). `hints` is the
  semantic index *over* artefacts (`src/arb_memory/store.py` §"Schema — two tables, two
  retrieval contracts", `docs/decisions/arb-memory-architecture.md` §2): a hint row optionally
  points at `(artefact_id, artefact_version)` and carries `metadata jsonb` (which holds
  `tags`).
- **No separate "decision" concept.** `docs/decisions/arb-memory-architecture.md` and the
  schema confirm decisions are simply artefacts whose content happens to be a decision record
  (mirroring this very repo's own `docs/decisions/*.md` convention) — not a distinct table or
  `source` enum value. This resolves BACKLOG's "one file per artefact vs. per topic" question:
  **one file per artefact_id** (there is no topic grouping to speak of at the data-model
  level; hints' `tags` become frontmatter metadata on that one file, not a second grouping
  axis that would duplicate content across files).
- **A read-only-role grants pattern already exists and is already tested**:
  `src/arb_memory/mcp/grants.py::apply_local_reader_grants(conn, role)` grants `SELECT` only
  on `hints`+`artefacts`, explicitly revokes `INSERT/UPDATE/DELETE` on both, and revokes all
  access to `mcp_auth` and every audit/eval/transcript table. This is **exactly** the shape
  guardrail #1 asks for — the exporter needs precisely this privilege shape (read artefacts +
  hints, nothing else). `tests/arb_memory/test_local_reader_grants.py` shows the established
  test pattern for guardrail #4: spin up a scratch role, apply the grants function, assert
  `has_table_privilege`/`has_schema_privilege` per table.
  > **Round 1 panel finding, addressed (cold-Opus, P2, confidence 85):** `apply_local_reader_grants`
  > contains no `REVOKE ... FROM PUBLIC` (unlike `apply_visibility_grants`, which does) — an
  > earlier draft of this design incorrectly claimed the new grants test would "assert `PUBLIC`
  > has nothing," mirroring `test_visibility_grants.py`'s pattern. That assertion tests the
  > schema baseline, not the function under test, and would mislead a builder. Fixed in the
  > Testing plan below: the new test mirrors `test_local_reader_grants.py` (which makes no
  > PUBLIC claim), not `test_visibility_grants.py`.
  > **Round 1 panel finding, addressed (agy, P1):** `apply_local_reader_grants` has no
  > `to_regclass` existence guards on the audit/eval/transcript tables it revokes from (unlike
  > `apply_visibility_grants`), so it would raise `UndefinedTable` against a schema missing
  > those tables. This is a pre-existing property of the reused function, not introduced by
  > this design — it does not affect prod (fully migrated) or the new grants test (which, like
  > `test_local_reader_grants.py` today, must run against a fully-migrated fixture, not a bare
  > schema). Noted here for whoever eventually hardens `grants.py` itself; out of scope for
  > this design to fix.
- **A working remote-read precedent.** Memory `arb-memory-read-via-mcp` records that a local
  read MCP already holds a live read path to the **prod** Postgres instance (fresher than ad
  hoc `psql`) — read-only direct-to-Postgres access from outside the MCP host process is
  already an established, sanctioned pattern, not a new network path this design has to
  justify from scratch.

## Architecture

**One new role, reusing the existing grants function — not a new one.** Create a role (e.g.
`arb_vault_export`) via `CREATE ROLE`, then call the *existing*
`apply_local_reader_grants(conn, "arb_vault_export")` — no new function in `grants.py`. This
directly answers BACKLOG's "shared with or kept separate from the existing MCP read-only
role" question: **separate principal, identical privilege shape.** Separate principal because
the exporter and the MCP local reader are different trust domains with different exposure
(the exporter's credential sits on whatever host runs the cron job; the MCP reader's
credential sits inside the always-on MCP host process) — revoking one must never revoke the
other, and a leaked exporter credential should not double as a leaked MCP-reader credential.
Identical shape because they need exactly the same two tables at exactly the same privilege
level; duplicating `apply_local_reader_grants` under a new name would be pure duplication with
no behavioral difference, and the guardrail-4 grant test can then follow
`test_local_reader_grants.py`'s established pattern almost verbatim (swap the role name).

> **Round 1 panel finding, addressed (agy P1 + cold-Opus P2, independently convergent):** the
> existing `grants` CLI (`src/arb_memory/run.py::run_grants`, driven by
> `ARB_MEMORY_LOCAL_READER_ROLE`) has no path to apply or re-audit grants for a *second* role.
> Guardrail #1 requires the exporter's grants be "verified via a grant audit at deploy time,"
> so this is a required plan-stage task, not an optional follow-up: extend `run_grants()` to
> also read a new `ARB_VAULT_EXPORT_ROLE` env var and call `apply_local_reader_grants` for it
> (same function, second role name, same CLI invocation point) — see Testing plan item 1 and
> the plan this design feeds.

**One new module, `src/arb_memory/vault_export.py`**, plus a thin CLI wrapper
`scripts/arb-memory-vault-export` (mirroring the existing `scripts/` — thin-wrapper /
`src/`-does-the-work convention used throughout this repo, e.g. `scripts/arb-audit-emit` →
`arb_memory.audit`).

`vault_export.py` responsibilities:

1. Connect using a **dedicated read-only DSN** (own env var, `ARB_VAULT_EXPORT_DSN`), read
   directly from the process environment (`os.environ["ARB_VAULT_EXPORT_DSN"]`) — the exporter
   process never holds any credential beyond this one role.
   > **Round 1 plan-review finding, addressed (codex, P2):** an earlier draft of this line said
   > the DSN is "resolved the same `env-file > process-env` way every other DSN in this repo is
   > resolved," which a fresh reader (correctly) took to mean the *Python code itself* must
   > implement layered env-file-then-process-env resolution, the way
   > `agent_redis_bridge/bridge.py`'s `resolve_audit_redis`/`resolve_eval_redis` do. That's not
   > what happens anywhere in `arb_memory`'s own code, and it shouldn't for this exporter either:
   > every `run_*` function in `src/arb_memory/run.py` (e.g. `_memory_conn`'s
   > `os.environ["ARB_MEMORY_DSN"]`) reads directly from the process environment, because the
   > env-file layering already happened one level up — `deploy/docker-compose.yml`'s
   > `environment: { ARB_MEMORY_DSN: ${ARB_MEMORY_DSN} }` blocks pull values from
   > docker-compose's own `.env` file into the container's process environment *before* Python
   > starts; a systemd-deployed job would use `EnvironmentFile=` the same way. `bridge.py`'s
   > layered resolution is a different package solving a different problem (one bridge daemon
   > process may be pointed at an arbitrary `--env-file` path, supporting multiple bridge
   > instances per host) — it does not generalize to `arb_memory`'s single-DSN-per-service
   > shape. Reworded above to match what "the same way every other DSN in this repo is resolved"
   > actually means inside `arb_memory`: direct process-environment read.
2. **Query the latest version of every artefact**: `SELECT DISTINCT ON (artefact_id) ... ORDER
   BY artefact_id, version DESC` — one row per `artefact_id`, its highest `version`. This uses
   the same `DISTINCT ON` idiom `store.py` already relies on (the leftmost `ORDER BY` column
   must match the `DISTINCT ON` expression), but is a different query from
   `store.recent_artefacts` (which orders by `created_at DESC` first, for global recency, not
   per-group latest-version) — the two answer different questions and are not expected to
   share an ordering beyond the shared idiom.
   > **Round 1 panel finding, addressed (agy, P2):** an earlier draft claimed this query
   > "mirrors `store.recent_artefacts`'s ordering idiom," which overstated the similarity
   > (different leftmost `ORDER BY` column, different purpose). Reworded above; the SQL itself
   > was already correct (confirmed independently by codex and cold-Opus).
3. For each artefact, look up hint(s) with a matching `(artefact_id, artefact_version)` **and
   `deleted_at IS NULL`** — every hint read path in `store.py` (`search_hints`'s three queries)
   applies this filter, so the exporter must too, or a soft-deleted hint's tags/description
   would leak into the vault as if still current — to pull `metadata->tags` and a one-line
   description. This is the only place `hints` is read; there is no independent hint-only
   export (a hint with no linked, non-deleted artefact-hint carries nothing citable outside
   ARB Memory's own search). **Multiple linked hints are resolved deterministically:**
   tags are the union of every linked hint's `metadata->tags`, deduplicated and sorted
   alphabetically; the description line comes from the linked hint with the lowest `id` (the
   first one ever written for that artefact version). Both rules exist purely for run-to-run
   determinism, not semantic significance — the schema places no uniqueness constraint on the
   `(artefact_id, artefact_version)` pairing on the `hints` side, so an artefact can legitimately
   have several linked hints.
   > **Round 1 panel finding, addressed (cold-Opus, P2, confidence 75):** an earlier draft said
   > "look up any hint(s)... to pull tags and the hint's text," with no rule for which hint wins
   > when more than one links to the same artefact version — nondeterministic across reruns.
   > Fixed above with an explicit union/lowest-id rule.
   > **Round 2 finding, addressed (cold-Opus, P2):** the round-1 fix specified the tie-break
   > rule but omitted the `deleted_at IS NULL` filter every other hint read path in `store.py`
   > enforces — fixed above.
4. Write one markdown file per artefact: `<vault_root>/<slug(artefact_id)>-<idhash>.md`, where
   `slug()` strips anything that isn't `[A-Za-z0-9._-]` (defensive against a pathological
   `artefact_id` containing `/` or `..` — artefact IDs are caller-chosen strings with no format
   constraint enforced at write time today, confirmed by tracing `store.py`'s write path: it
   inserts the caller-supplied `artefact_id` with no format validation, so the exporter must
   not trust it as an already-safe path component) and `idhash` is the first 8 hex characters
   of `sha256(artefact_id)`. The hash suffix — not the slug alone — is what the exporter treats
   as the collision-free identity; `slug` exists only to keep the filename human-legible.
   **Fail loud on the (cryptographically negligible but checked) case of two distinct
   `artefact_id`s producing the same `idhash`:** the exporter tracks an in-run
   `idhash -> artefact_id` map and raises rather than silently overwriting if a second,
   different `artefact_id` maps to an already-seen `idhash`.
   > **Round 1 panel finding, addressed (codex P2 high-confidence + cold-Opus P2 confidence 78,
   > independently convergent):** the original `slug(artefact_id)`-only filename (stripping,
   > not replacing, disallowed characters) is not injective — `docs/foo.md` and `docsfoo.md`
   > both stripped to `docsfoo.md`, and codex traced an existing design doc
   > (`docs/superpowers/specs/2026-06-20-arb-memory-phase0-store-design.md:256-258`) describing
   > exactly this path-like `artefact_id` shape for repo-mirror artefacts, so the collision risk
   > is real, not just theoretical, even though cold-Opus separately noted real observed
   > artefact IDs today (`art-<hex>`) don't trigger it. Fixed above with a hash-suffixed,
   > fail-loud-on-collision filename scheme.
5. **Frontmatter carries provenance**: `artefact_id`, `version`, `source`, `author`,
   `created_at`, `content_hash`, `tags` (from step 3, `[]` if none), `exported_at` (the export
   run's own timestamp — lets a human tell a stale vault from a fresh one without checking
   cron logs). **`exported_at` is excluded from the idempotency comparison in the Testing
   plan** (see below) since it varies every run by design.
   > **Round 1 panel finding, addressed (cold-Opus, P2, confidence 90):** an earlier draft's
   > Testing plan asserted a rerun produces "byte-identical output," which is impossible while
   > `exported_at` is in the frontmatter (it changes every run) — a self-contradiction that
   > would ship a failing or silently-weakened test. Fixed in the Testing plan below: the
   > idempotency assertion explicitly excludes the `exported_at` line.
6. **Body is `content`** (the text column) verbatim. If `content` is `NULL` and
   `content_bytes` is set (a binary artefact — rare; every artefact seen in this repo's own
   history is text), write a one-line placeholder (`_binary artefact, content_mime: {mime},
   {len(content_bytes)} bytes — not rendered_`) instead of attempting to decode it — the vault
   is for human markdown browsing, not a binary blob store.
7. **Full-rewrite every run, not incremental sync.** Given the architecture doc's own §2 note
   that "hints/artefacts are small" (the fast-growing data is audit/eval, explicitly excluded
   from this exporter's scope), a full nightly rewrite is simpler than diffing and carries no
   meaningful cost. This also sidesteps any incremental-sync bug class (partial writes,
   deleted-then-recreated artefact_ids, clock-skew-based "what changed since last run" logic)
   entirely.
8. **No deletion/GC logic needed.** Artefacts are never hard-deleted (confirmed: no
   `deleted_at` column on `artefacts`, unlike `hints`) — so every artefact_id the exporter has
   ever seen still exists in the source table on every run; a full-rewrite naturally produces
   the exact current set with no stale-file accumulation to reason about.
9. **Idempotent and safe to re-run**: writing is a plain overwrite per file (no temp-file
   dance needed — a crash mid-run leaves some files stale until the next run, which is
   acceptable for a nightly informational export, not a transactional system).

**Cadence and where it runs: nightly, via cron on the MCP-host box — not a new container, and
not the mac-mini.** `docs/decisions/arb-memory-architecture.md` §6's own doctrine is "match
supervision to lifecycle": ephemeral + periodic → cheap systemd/nohup/cron, not a persistent
container (containers are reserved for the three stateless singletons — MCP host, memory
consumer, audit consumer). This exporter is ephemeral (runs, writes, exits) and periodic
(nightly), so a plain cron job is the architecturally-consistent supervision choice either
way — the open question round 1 settled was *which box runs it*, not *how it's supervised*.
The MCP-host box, not the mac-mini: it already holds the highest-trust credentials in the
topology (the memory-consumer's write DSN), is always-on by design (unlike a laptop that
sleeps/closes overnight), and adding one more read-only credential to an already-hardened,
always-on box is a smaller blast-radius increase than placing a new credential on a personal
laptop. Mark browses the vault via the SSH access already used for prod ops
(`arb-memory-prod-deploy` memory), or a scheduled `rsync`/`scp` pull to the mac-mini if local
grep access is preferred — either way, the export job itself runs where the credential is
safest, not where the browsing happens.
> **Round 1 panel finding, addressed (agy, P2 — placement).** The original mac-mini placement
> drew a real operational objection: a developer laptop is frequently asleep or closed
> overnight (unreliable nightly cron), and putting `ARB_VAULT_EXPORT_DSN` on it needlessly
> expands the credential footprint to a personal workstation. **Not** an architectural
> violation — codex and cold-Opus independently confirmed a mac-mini cron would not break the
> arch doc's write/single-writer doctrine (§7 is scoped to the write/embedding path; read-only
> direct-to-Postgres is already the sanctioned pattern per §5 and the live local-read-MCP
> precedent) — but the practical reliability/credential-footprint argument stands on its own
> merits regardless of architectural permissibility, so the primary recommendation moves to
> the MCP-host box.

## Non-goals (explicit, to keep the scope guardrail #3 protects)

- **Not a replacement for ARB Visibility.** `visibility.py` stays exactly as-is; it serves a
  different need (live seat/task activity, not memory content) and this design does not touch
  it.
- **Not two-way.** The vault is read-only output; nothing ever reads the vault directory back
  into anything ARB-write-capable. Guardrail #3 makes this explicit: wiring an importer at the
  vault path is the one change serious enough to require a re-panel, not a normal iteration.
- **Not a hint-content browser.** Hints without a linked artefact produce nothing in the
  vault — they are ARB's internal semantic-search index, not human-facing documents. This
  keeps the export surface to exactly what guardrail #1's read-scope needs to cover.
- **Not incremental, not diffed, not versioned-file-per-version.** Only the latest version of
  each artefact is rendered as the primary (and only) file for that `artefact_id` — older
  versions remain queryable via `memory_get(artefact_id, version)` as today; the vault is a
  browse surface for current state, not a full history browser.

## Testing plan

1. **`tests/arb_memory/test_vault_export_grants.py`** — mirrors
   `test_local_reader_grants.py`'s established shape (not `test_visibility_grants.py` — see
   the round-1 finding noted above `apply_local_reader_grants` never touches `PUBLIC`, so a
   `PUBLIC`-empty assertion would test the schema baseline, not this function): spin up a
   scratch role, call `apply_local_reader_grants(conn, role)`, assert `SELECT`-only on
   `artefacts`+`hints`, assert zero privileges on `mcp_auth`.*, `audit_events`,
   `eval_event_raw`, `eval_deadletter`, `transcript_io`, `transcript_deadletter`. Also covers
   the deploy-time wiring finding above: a companion test drives `python -m arb_memory grants`
   with `ARB_VAULT_EXPORT_ROLE` set (alongside the existing
   `ARB_MEMORY_LOCAL_READER_ROLE` test) and asserts both roles end up correctly grant-scoped —
   proving the CLI path applies the new role, not just that the grants function *could*.
2. **`tests/arb_memory/test_vault_export.py`** — exercises `vault_export.py` against a fixture
   DB (same `scratch` fixture convention used by the grants tests): multiple artefacts at
   different versions (assert only latest version's content is rendered), an artefact with one
   linked hint carrying tags (assert tags appear in frontmatter), an artefact with **multiple**
   linked hints with different tags (assert the deduplicated-sorted-union rule from
   Architecture step 3), an artefact with no linked hint (assert `tags: []`), a binary artefact
   (`content` NULL, `content_bytes` set — assert the placeholder line, not a decode attempt),
   two distinct `artefact_id`s that strip to the same slug (assert both are exported under
   distinct hash-suffixed filenames, not one silently overwriting the other), an `artefact_id`
   containing a path-hostile character (assert the slug function neutralizes it and no file
   escapes `vault_root`), and a full-rerun-is-idempotent case (run twice, assert every
   frontmatter field and the body are byte-identical **except** `exported_at`, which is
   asserted present and non-empty in both runs but not compared for equality).
3. Do **not** unit-test the cron wiring itself — mirrors how this repo tests `scripts/*`
   wrappers elsewhere (thin wrapper, tested via the module it calls into).

## Open design questions from BACKLOG.md — resolved above, listed here for traceability

- Markdown export schema (per-artefact vs. per-topic) → **per-artefact** (§ "What already
  exists", no topic concept in the schema).
- Frontmatter/provenance → **yes**, listed fields in Architecture step 5.
- Cadence → **nightly**, plain cron on the MCP-host box (§ Cadence and where it runs;
  round-1 panel moved this off the mac-mini for reliability/credential-footprint reasons).
- Shared vs. separate read-only DB role → **separate role, same grants function** (§
  Architecture, first paragraph).

## Self-review notes

- **Guardrail coverage:** all four non-negotiable guardrails from the converged panel concept
  map onto a concrete mechanism above (role/grants reuse → #1, now including the deploy-time
  CLI wiring the round-1 panel caught missing; DSN-only credential, no other secret on the
  vault host → #2; explicit Non-goals section + no code path reading the vault → #3;
  `test_vault_export_grants.py` → #4). None are deferred to "later."
- **Every claim about the current system is sourced from a real file read this session**
  (`schema.sql`, `store.py`, `grants.py`, `read_tools.py`, `run.py`,
  `docs/decisions/arb-memory-architecture.md`, `test_local_reader_grants.py`), not asserted
  from memory or plausibility — the WE2 lesson this very BACKLOG item's status block
  references (pi-GLM's false premise about `visibility.py` tipping a verdict) is the failure
  mode this spec is trying not to repeat.
- **Placeholder scan:** no TBD/TODO. Role name, module path, script path, table names, and
  test file names are all concrete and unambiguous enough for a plan to reference directly.

## Round 1 panel record

Independent 3-seat panel (codex-bridge-dev, agy-bridge-dev, cold-Opus subagent), run
`panel-vault-export-spec-20260706T113531Z-2b966e`. Stances: codex `needs-changes`/P2 (slug
collision), agy `approve`/P1 (CLI grants gap + grants.py existence-check risk, both addressed
or triaged above), cold-Opus `approve`/P2 (five findings, all addressed above). No P0 from any
seat; no reviewer objected to the core architecture (guardrail coverage, grants-reuse
soundness, or "no architecture violation" on placement). All eight distinct findings across
the three reports are addressed inline above via "Round 1 panel finding, addressed" callouts,
except the pre-existing `apply_local_reader_grants` existence-check gap (agy, P1), which is
explicitly triaged as out-of-scope for this design (a latent property of an already-shipped,
already-tested function, not something this design introduces or worsens — noted precisely
this way, not as "fixed," to avoid overstating the disposition; a round-2 nit flagged the
status header's looser wording, corrected there too).

## Round 2 panel record

Same three seats, targeted confirmation pass, run
`panel-vault-export-spec-r2-20260706T114718Z-34062e`, `supersedes` round 1's run. Stances:
codex `approve`/none ("round 2 fixes confirmed"), agy `approve`/none (independently re-derived
the collision-freedom, determinism, idempotency, and citation fixes from source rather than
trusting the round-1 writeup), cold-Opus `approve`/P2 (two non-blocking notes: the `hints`
lookup needed the same `deleted_at IS NULL` filter every other read path in `store.py`
enforces — now added above — and the status header overstated one triaged-out-of-scope item
as "fixed" — reworded above). No new findings from codex or agy; no P0/P1 from any seat in
either round. Design is ready for the implementation plan.

## Post-plan wording fix (from the plan's own round-1 panel review)

The plan built from this spec (`docs/superpowers/plans/2026-07-06-arb-memory-read-model-export.md`)
went through its own independent 3-seat panel. One finding traced back to an imprecise sentence
in *this* document (Architecture step 1's DSN-resolution wording) rather than to the plan
itself — fixed inline above, see the "Round 1 plan-review finding, addressed" callout at that
step. No other spec content changed as a result of the plan review.
