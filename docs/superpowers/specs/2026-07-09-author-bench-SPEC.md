# Author bench — SPEC (build-ready)

**Design:** `docs/superpowers/specs/2026-07-09-author-bench-design.md` (v2.1, certified
`panel-capsuitecdesign-r4-20260709T012126Z-bc0d9a`, zero P0/P1 — the header records rounds 1–4).
This SPEC translates that design into named files, function seams, pinned wire shapes, and a
test map. It does not redesign. Where the design left a human fork (Open questions) the SPEC
parameterizes it with the design's default and names the fork; genuine gaps go in **§ Escalations**
(none block the build).

All new paths are under `tools/authorbench/` unless noted. Line references name the seam, not a
frozen offset (verified 2026-07-09). Design section tags (`D1`–`D6`, `V1`–`V9`) are carried inline.

---

## 0. Walls — restated verbatim, binding on every increment

The design's five hard walls (design §Constraints); a change that violates one is a build error,
not a review nit:

1. **Report-only.** No output field feeds trust, quorum, routing, or the rotation default
   automatically. The report is evidence attached to the human's ask-once rotation decision
   (`SKILL.md:550-558`), nothing else.
2. **No composite "best author" verdict, no ranked output.** Per-dimension evidence is the human's
   input; the reporter refuses to emit `winner|rank|total|composite|score_sum` and any cross-seat
   sort key (§8 wall, coded).
3. **Blinding is mandatory.** Judges never see author identity; identity-bearing surface is stripped
   mechanically, not by convention (§4 normalizer + denylist).
4. **Reproducible from stored artefacts.** A run's mechanical results are exactly re-derivable from
   what was stored; the judged phase re-runs as a new, separately-stored judging pass (append-only,
   evidence-store no-silent-drop).
5. **Cost-shaped for one candidate.** The unit of use is ONE candidate against a small frozen
   baseline, not a fleet sweep. No fleet-sweep mode in v1.

Transposed from the reviewer-side wall verbatim in spirit (`eval-suite-design.md:57-70`): **no
ranked leaderboard; mechanical output separation in a distinct namespace; the reporter refuses to
emit any ranking field.** The reader-convertibility residual (a dimension×brief grid remains
convertible into a ranking by a motivated human) stays NAMED in every report's construct-validity
block, never silently dropped.

---

## 1. Modules & files

New Python package `tools/authorbench/authorbench/` (mirrors `tools/eval/arb_eval/` layout; pure
helpers + subprocess glue + `main()`, `main()`-level tests mandatory):

| file | purpose | key public surface |
|---|---|---|
| `bundle.py` | D1 frozen brief-bundle assembly, hashing, author-visible subset | `freeze(src, *, corpus_version) -> BundleManifest`; `load(bundle_dir) -> Bundle`; `author_visible_subset(bundle) -> dict`; `bundle_hashes(bundle) -> dict` |
| `factkey.py` | D1 `fact_key.yaml` schema + V9 self-validator | `load_factkey(path) -> FactKey`; `validate(factkey, export_dir) -> list[FactKeyError]` |
| `normalize.py` | D3 blinding normalizer + denylist scan + quarantine/redaction | `normalize(raw, *, version) -> str`; `denylist_scan(text) -> list[Hit]`; `redact_once(text, hits) -> tuple[str, list[EditLog]]`; `stage_for_judges(draft, staging_dir) -> StagingResult` |
| `jail.py` | D2 per-role jail-profile manifests + canary token classes | `author_profile(bundle, base_sha) -> JailProfile`; `judge_profile(export, draft, factkey, rubric) -> JailProfile`; `canary_tokens(role, run) -> TokenSet`; `assert_manifest(profile, role)` |
| `rubric.py` | D3 rubric anchors R1–R6 + mechanical checkers | `RUBRIC` (frozen anchors); `check_r1(draft, factkey, export) -> R1Result`; `count_r4(draft) -> R4Result`; `r6_context(factkey) -> R6Context` (INPUT prep only — R6 outcomes are JUDGED, r2 codex+GLM P1) |
| `judge.py` | D3 judge dispatch, packet assembly, per-seat parse | `judge_packet(role_inputs) -> dict`; `dispatch_judges(panel, packet) -> list[JudgeReply]`; `parse_anchors(reply) -> dict[dim, Anchor]` |
| `score.py` | D4 cell computation (median anchor + mechanical cap) | `cell(dim, judge_anchors, mech) -> Verdict`; `MET`/`PARTIAL`/`NOT_MET` |
| `report.py` | D4 per-seat report + coded wall | `render_seat_report(seat, grid, detail) -> str`; `guard(obj)`; `assert_no_rank_fields(obj)` |
| `ledger.py` | D1 exposure ledger + V6 burn rule | `record(run, seat_lineage, brief, prior_exposure)`; `check_burn(seat_lineage, brief, prior_exposure, *, override) -> BurnDecision` |
| `store.py` | D6 append-only NDJSON run store + ARB Memory keying | `Recorder(run_dir)`; `write(event)`; `memory_key(...)`; `rederive_mechanical(run_dir) -> dict` |
| `run.py` | D2/D5 orchestration (author + judge turns, baseline pairing) | `author_turn(...)`; `judge_phase(...)`; `run_bench(candidate, corpus, *, baseline) -> RunResult` |
| `cli.py` | CLI surface (§ CLI below) | `main(argv)`; subparsers `freeze [--artefact-commit <sha>|--base-sha <sha>]|factkey-validate|author|judge|score|report|run|ledger`; `freeze` takes `--artefact-commit <sha>` (the commit that added the historical artifact; `base_sha` = its first parent) or explicit `--base-sha` — the round-1 cold-Opus P2 CLI gap |

- `scripts/authorbench` — thin entrypoint (wiki/`arb-learn` style: `exec python3 -m authorbench.cli "$@"`).
- Confinement (new, reuses the Instrument-1 substrate image `arb-eval-seat:latest`):
  - `tools/authorbench/confinement/confined-authorbench.sh` — per-role runner (`author|judge`),
    authorbench jail profile (§ D2 below).
  - `tools/authorbench/confinement/authorbench-canary.sh` — per-role canary (token classes per role).
- Corpus (frozen bundles): `tools/authorbench/corpus/v1.0/<brief-id>/` (committed; drafts + run
  artefacts are gitignored working data — `tools/authorbench/runs/.gitignore`,
  `tools/authorbench/corpus/**/drafts/.gitignore`).
- Tests: `tools/authorbench/tests/test_bundle.py`, `…test_factkey.py`, `…test_normalize.py`,
  `…test_jail.py`, `…test_rubric.py`, `…test_score_report.py`, `…test_ledger.py`,
  `…test_reproducibility.py`, `…test_hermetic_suite.py`.
- **No changes** to `tools/eval/arb_eval/*` (Instrument 1) — separate namespace by wall #2; the two
  benches share only the container image and the confinement canary *pattern*, not code that could
  cross-emit.

---

## 2. Data shapes (pinned)

### 2.1 Brief bundle — frozen directory `corpus/<corpus-version>/<brief-id>/` (D1)

```
corpus/v1.0/AB-D1/
  bundle.yaml        # manifest (below)
  brief.md           # author-visible: the authoring brief, verbatim or reconstructed
  steer/             # author-visible: byte-identical steer blocks (D2 Fable-author steering)
    give-the-reason.md   scope-restraint.md   grounded-claims.md   no-reasoning-extraction.md
  fact_key.yaml      # JUDGE + mechanical-checker ONLY — NEVER in the author-visible subset (V2d)
```

`bundle.yaml` (all fields pinned; the manifest is hashed into the run artefact):

```yaml
id: AB-D1
stage: design                 # enum: design | spec | plan  (closed)
corpus_version: v1.0
source: "arb-watch-history design brief"
provenance: reconstructed(v1) # enum: verbatim | reconstructed(v<N>)   (V2/D1 AB-D1 caveat)
base_sha: 9de2850             # pre-outcome SHA the brief was authored against
length_budget: 300            # soft line cap (int); house exemplars run ~200–350
outcome_globs:                # v1.6 pin (r5 codex P1-2 - the v1.5 prose declared this field but
                              # the pinned schema omitted it; matches the shipped implementation):
                              # paths embodying THIS brief's canonical outcome, V9-validated
                              # >=1 match at HEAD and 0 at base_sha per glob
- docs/superpowers/**/2026-07-03-arb-watch-history-*
normalizer_version: 1
factkey_version: 1
steer_blocks: [give-the-reason, scope-restraint, grounded-claims, no-reasoning-extraction]
hashes:                       # sha256 of each frozen file; comparability-by-construction anchor
  brief_md: <hex>
  fact_key_yaml: <hex>
  steer: {give-the-reason: <hex>, ...}
```

**Corpus v1.0 (pinned; `base_sha`/provenance per design D1 table):**

| id | stage | source | base_sha | provenance |
|---|---|---|---|---|
| AB-D1 | design | arb-watch-history design brief (no committed original) | `9de2850` | `reconstructed(v1)` |
| AB-S1 | spec | `2026-07-03-arb-visibility-web-spec-authoring-brief.md` | parent of committed spec (resolved at `freeze`) | `verbatim` |
| AB-P1 | plan | `2026-07-03-arb-visibility-web-controls-plan-authoring-brief.md` | parent of committed plan (resolved at `freeze`) | `verbatim` |

**AB-S1/AB-P1 are the corpus anchors** (design D1); AB-D1 claims carry the reconstruction caveat and
AB-D1 is swappable for another verbatim-briefed design task without touching machinery. **Open fork
(design OQ3):** confirm AB-S1/AB-P1 fact-keys are rich enough to freeze, or swap for richer-keyed
candidates — parameterized: the corpus table is data, not code; `freeze` resolves `base_sha` for a
verbatim brief as `git rev-parse <committed-artefact-commit>^` and records it.

**Author-visible subset (`bundle.author_visible_subset`, V2d — LOAD-BEARING):** exactly
`{brief.md, length_budget (from manifest), steer/*}`. **NEVER `fact_key.yaml`** and never any file
matching the fact-key schema (round-2 cold-Opus P0 — the key is the R1/R6 answer sheet). The key
goes only to judges (§ D3 packet) and the mechanical checkers, post-authoring.

### 2.2 `fact_key.yaml` schema (D1 / V9)

```yaml
version: 1
brief_id: AB-D1
base_sha: 9de2850
facts:                         # historical hinge facts, verified at base_sha
  - id: F1
    claim: "ok IS in EVAL_ALLOWLIST"
    file: src/agent_redis_bridge/eval_tee.py
    line: 10       # REAL at 9de2850 (round-1 GLM+cold-Opus P2: the earlier exemplar cited nonexistent files and failed its own V9)
    pattern: "EVAL_ALLOWLIST"  # substring/regex that MUST match at file:line at base_sha
traps:                         # known historical traps (the process caught)
  - id: T1
    name: dead-vote-branch
    precondition: "a vote path that bypasses push_task_event/eval_event_raw"  # semantic, NOT code shape
    file: src/agent_redis_bridge/bridge.py
    symbol: _emit_vote         # REAL at 9de2850:2072 — named file/symbol MUST exist at base_sha (checkable)
    historical_catch: "<panel-report-not-included-in-this-copy>:34"
```

**Trap semantics (round-1 agy P1-2, D3-R6):** `precondition` states the trap's *condition*, not its
historical code layout. R6 scores a trap AVOIDED if the draft's structure renders the precondition
moot — an alternative design that eliminates the precondition by construction scores avoided,
REWARDED not penalized.

### 2.3 Normalizer / denylist / quarantine-redaction pipeline (D3)

- **`normalize(raw, version)`** strips, in order: authorship/status headers; model self-references;
  seat ids (author-identity form); dates/session markers; run labels — then normalizes heading style
  and list markers. Versioned (`normalizer_version`); **raw + normalized both stored**.
- **`denylist_scan(text)`** runs post-normalization; matches **author-identity tells** only — the
  authoring seat's own name/model/lineage (NOT every seat-id-shaped string, since this repo's subject
  matter legitimately contains them). Returns `[Hit{token, class, span}]`.
- **`stage_for_judges(draft, staging_dir)`** — judges read ONLY from a staging dir that this function
  populates; a draft with hits is **not written there** (structural quarantine). On hit: apply
  `redact_once` (matched tokens → neutral placeholders, each edit appended to a `redaction_log`
  event), rescan; clean ⇒ stage; still-hot ⇒ **abort loud** naming the token class. Returns
  `StagingResult{staged: bool, quarantined: bool, aborted: bool, token_class}`.

### 2.4 Per-role jail-profile manifests + canary token classes (D2, V2e)

Both roles run in the Instrument-1 confinement image via `confined-authorbench.sh <role>`, with an
**authorbench profile**: fresh per-run `HOME` on **tmpfs**, minimal auth material copied **read-only**
(auth tokens only — no history/session/config state), **NO writable persistent engine volume** (no
`agy-home` reuse; contrast `confined-review.sh:42-47` which mounts the persistent `agy-home`).
**agy buildability (round-1 codex+agy P1 — `confined-review.sh:9` records that agy auth does NOT
transplant off the persistent volume on macOS):** codex's non-persistent auth is specified and
proven (`~/.codex/auth.json` + `config.toml` RO-copied into tmpfs HOME); agy has NO specified
non-persistent auth path, so **agy participation is CONDITIONAL on a named prerequisite spike**
(demonstrate one agy turn served with tmpfs HOME + RO-copied material and no persistent volume;
live smoke test recorded). Until that spike lands, agy is NOT in the v1 authorbench engine set.

| role | mounts asserted EXACTLY (`jail.author_profile` / `jail.judge_profile`) | canary token classes |
|---|---|---|
| **author** | `{workspace = git archive <base_sha> \| tar -x (no .git/refs/history), author-visible bundle subset, tmpfs HOME + RO auth}` | **outcome-token** + **author-identity-token** over every mount |
| **judge** | `{export = base_sha tree, normalized draft, fact_key.yaml, rubric, tmpfs HOME + RO auth}` | **author-identity-token only** (the judge legitimately mounts the fact-key, whose outcome citations an outcome-token canary would false-flag — r3 cold-Opus P2) |

**Token classes (`jail.canary_tokens`):**
- **outcome-token** — **the BUNDLE'S OWN `outcome_globs`** (SPEC v1.5 amendment, found LIVE by
  the C worker's Task-5 stop: generic panel-report-shaped globs false-flag OTHER briefs' panel
  records that legitimately exist at `base_sha` — e.g. the 2026-07-02 arb-secrets panel docs
  predate AB-D1's `9de2850`). Each bundle's `bundle.yaml` declares `outcome_globs`: the paths
  created AFTER its `base_sha` that embody THIS brief's canonical outcome (for AB-D1:
  `docs/superpowers/**/2026-07-03-arb-watch-history-*`). V9 validates: every `outcome_globs`
  entry matches ≥1 path in the repo at HEAD and ZERO paths at `base_sha` (self-proving, both
  polarities per glob). Plus the historical outcome content markers compiled from the fact-key
  traps' `historical_catch` files.
- **author-identity-token** — the authoring seat's name/model/lineage tells for THIS run
  (seat id, model version string, lineage label).

Canary asserts, per role: (1) the workspace/export is readable; (2) the role's forbidden token
class is ABSENT from the entire jail read-surface (image layers + every mount + `git cat-file
--batch-all-objects` where a `.git` is present — it is not, for the author workspace); (3) the
persistent `agy-home` volume is **absent** from both profiles. Any failure ⇒ Park (loud), before
any author/judge turn.

### 2.5 Rubric anchors R1–R6 (D3)

Frozen in `rubric.RUBRIC`; **0/1/2 per dimension**, every judged point requires a quoted evidence
line from the draft (presence/absence of specific behaviours — length alone earns nothing).

| dim | measures | 0 | 1 | 2 | scoring |
|---|---|---|---|---|---|
| R1 hinge-fact verification | claims cited `file:line`, TRUE at `base_sha` | fabricated/uncited | some cited, gaps | all load-bearing claims cited+true | **mechanical** (all citations) + judge spot-read (5 sampled) |
| R2 constraint coverage | every brief constraint addressed | constraints dropped | partial | all addressed w/ quote | judged, anchored |
| R3 rejected alternatives | alternatives named, reasons engage real trade-offs | none | named, thin | named + real trade-off engaged | judged, anchored |
| R4 verification obligations | obligations falsifiable, adversarial where guarding | none/vacuous | present, weak red | delete-guard⇒red concrete | judged + **mechanical count of obligations carrying a concrete red condition** |
| R5 trade-off / unknown honesty | uncertainties named, no fabricated confidence | fabricated confidence | some named | uncertainties named + no overclaim | judged; cross-checked vs `fact_key.traps` |
| R6 trap avoidance | did the draft carry a known historical trap | carried, no note | carried, noted | avoided (precondition moot or explicitly handled) | judged **with fact-key in hand** — near-mechanical |

### 2.6 Per-seat report format (D4) — `report-<seat>.md`

- **Headline:** one file per seat; a `dimension × brief` grid of `MET / PARTIAL / NOT-MET` (from
  the **median judge anchor** (a cell where ALL judges errored renders UNKNOWN(judge_error), run flagged re-runnable — round-1 cold-Opus P2), R1 mechanical results able to **cap** the cell (R6's cap is OQ1, parameterized) — a draft whose
  citations fail the mechanical check cannot show R1 `MET` regardless of judge reads). Counts as
  **prose** ("7 verified of 9, 2 fabricated"), **never** percentages or slash-ratios, in the headline.
- **Detail:** per-judge anchors + evidence quotes; mechanical records (R1 citation table, R4 count,
  R6 trap table); length stats (raw line count next to every judged read); judge-disagreement per cell.
- **Construct-validity block (verbatim, standing):** names (a) the tree-only-access construct
  narrowing (R2–R5 may under-read history-archaeology diligence); (b) the reader-convertibility
  residual (grid convertible into a ranking — inherent, accepted, informs the same human as the
  rotation); (c) the AB-D1 reconstruction caveat when AB-D1 is in the grid; (d) same-family
  non-certifying flag when an Anthropic judge scored an Anthropic author.
- **Claim grammar (D5, hardcoded in the template):** supported = "seat S (model M, corpus v1.0, run
  R): on AB-D1 verified 7/9 key facts, avoided trap T1, carried trap T2 (quote), NOT-MET on R3
  (evidence)"; NOT supported = "S is a better author than T" / "S has improved" / any sentence about
  S unqualified by brief and run. All reads **indicative**, not capability measurements.
- **Cross-run comparability flag:** only the deterministic mechanical columns (R1 citation,
  R4 count) are marked cross-run-comparable at equal corpus+key+normalizer versions; R6 and
  all other judged columns are within-run only (r2 codex+GLM P1: R6 is judged).

### 2.7 NDJSON run events (D6) — append-only under `runs/<run-id>/events.ndjson`

Every event carries `{"event":<str>,"ts":<float>,"run_id":<str>}` and passes `report.guard` before
write (`store.Recorder.write`). Pinned event shapes:

```
{"event":"bundle_frozen","brief_id":<str>,"corpus_version":<str>,"base_sha":<sha>,
 "hashes":{...},"normalizer_version":<int>,"factkey_version":<int>}
{"event":"factkey_validated","brief_id":<str>,"ok":<bool>,"errors":[<str>,…]}
{"event":"author_dispatch","seat":<str>,"seat_lineage":<str>,"brief_id":<str>,
 "role":"author","profile_ok":<bool>,"prior_exposure":"none-known"|"possible(<reason>)"}
{"event":"author_draft","seat":<str>,"brief_id":<str>,"raw_sha256":<hex>,"line_count":<int>}
{"event":"blinding_scan","seat":<str>,"hits":[{"class":<str>,"span":[i,j]},…],"result":"clean"|"quarantined"}
{"event":"redaction_log","seat":<str>,"edits":[{"class":<str>,"from":<str>,"to":<str>}],"rescan":"clean"|"hot"}
{"event":"normalized","seat":<str>,"brief_id":<str>,"normalized_sha256":<hex>,"normalizer_version":<int>}
{"event":"mechanical_r1","seat":<str>,"brief_id":<str>,"cited":<int>,"true":<int>,"fabricated":<int>,
 "citations":[{"claim":<str>,"file":<str>,"line":<int>,"true":<bool>}]}
{"event":"mechanical_r4","seat":<str>,"brief_id":<str>,"obligations":<int>,"with_red_condition":<int>}
{"event":"factkey_r6_context","seat":<str>,"brief_id":<str>,"traps":[{"id":<str>,"precondition":<str>}]}  # round-1 cold-Opus+agy P1: R6 OUTCOMES are semantic (precondition-survival over prose) and CANNOT be byte-reproducible — the deterministic record is the INPUT context handed to judges; avoided/carried is a JUDGED outcome (median judge anchor, key in hand, per the certified design "judged, near-mechanical"), excluded from the V5 byte-for-byte set (which covers R1 citation checks, counts, staging records only). OQ1 (headline cap) stays parameterized.
{"event":"judge_dispatch","judge_seat":<str>,"draft_seat":"<blinded-handle>","brief_id":<str>,"role":"judge","profile_ok":<bool>}
{"event":"judge_anchor","judge_seat":<str>,"draft_handle":<str>,"brief_id":<str>,
 "anchors":{"R1":{"score":0|1|2,"quote":<str>},…},"length_note":<int>}
{"event":"exposure_ledger","seat_lineage":<str>,"brief_id":<str>,"prior_exposure":<str>,"override":<bool|null>}
{"event":"report_rendered","seat":<str>,"path":<str>,"wall_ok":<bool>}
```

Author drafts are keyed by a **blinded handle** in every judge-visible event (`draft_handle` is a
per-run opaque token, unbindable to the seat without the run's private map, stored outside the judge
mounts) — so even the event stream a judge could theoretically see never carries the author label.

### 2.8 Exposure-ledger format (D1) — `runs/<run-id>/exposure.ndjson`

One row per (seat-lineage, brief) authoring event: `{"seat_lineage":<str>,"brief_id":<str>,
"prior_exposure":"none-known"|"possible(<reason-class>)","authored_at":<float>,"override":<str|null>}`.
`prior_exposure` reason classes are a **closed enum**: `none-known | possible(public-mirror) |
possible(fork-known) | possible(<other>)`. **Burn policy (V6, D1):** `possible(public-mirror)` and
`possible(fork-known)` **REFUSE** the run without an explicit recorded `override`; any other
`possible(<other>)` flags the artefact and proceeds; the warm-orchestrator lineage and any
ARB-Memory/repo-history-granted lineage are hard-burned (refuse).

### 2.9 CLI surface (`tools/authorbench` / `scripts/authorbench`)

```
authorbench freeze <src-brief> --stage <design|spec|plan> --corpus-version v1.0 [--base-sha <sha>]
    # assemble+hash a frozen bundle into corpus/<cv>/<id>/; resolves base_sha for verbatim briefs
authorbench factkey-validate --brief <id> [--corpus-version v1.0]     # V9: validate key vs base_sha export
authorbench author --candidate <seat> --brief <id> [--corpus-version v1.0] [--override <reason>]
    # V6 burn-check → author turn in the jailed author profile → raw draft stored
authorbench judge --run <run-id> [--panel codex,pi-glm,m3]           # blind judge phase; default panel has M3 while agy is spike-locked (§2.4); pass --panel codex,agy,pi-glm only when the agy auth spike has landed
authorbench score --run <run-id>                                     # mechanical checks + cell computation
authorbench report --run <run-id>                                    # per-seat report files (coded wall)
authorbench run --candidate <seat> --corpus-version v1.0 [--panel …] [--baseline-seat <seat>]
    # freeze-check → author → judge → score → report (one candidate, whole corpus)
authorbench ledger --run <run-id>                                    # print/inspect the exposure ledger
```

Default judge panel v1 (agy CONDITIONAL per §2.4): `codex` (jailed) + `pi-glm` (bare-API packet) +
**`minimax-M3` (bare-API packet)** as the third family until the agy auth spike lands (M3 is
Instrument-1's normalizer — a different bench, no quorum collision; flagged in run metadata); agy
replaces M3 when its non-persistent auth is proven. PINNED registered target-ids (the `/learn`/wiki
lesson — engine labels are not seat ids): `codex→codex-bridge-dev`,
`pi-glm→pi-sdk-bridge-dev-glm`, `m3→pi-sdk-bridge-dev-minimax-m3`, (`agy→agy-bridge-dev` when
unlocked). When an Anthropic seat is among the authors, any Anthropic judge is admissible but
**flagged non-certifying** in report metadata.

---

## 3. Mechanics — per-module contracts (carrying D1–D6)

### 3.1 `bundle.freeze` / `bundle.load` (D1)

`freeze` copies `brief.md` + steer blocks + `fact_key.yaml` into `corpus/<cv>/<id>/`, resolves
`base_sha` (given, or `git rev-parse <committed-artefact-commit>^` for a verbatim brief), records
the bundle's `outcome_globs` (v1.6: a required freeze input, validated per V9's two-polarity glob
check before the bundle is written), computes
every file's sha256 into `bundle.yaml:hashes`, and refuses to overwrite an existing frozen bundle
(comparability-by-construction: a frozen bundle is immutable; a change mints a new corpus version).
`load` returns a `Bundle` with `author_visible_subset()` and `judge_inputs()` (which DOES include the
fact-key) as **separate accessors** — the author path never calls `judge_inputs`.

### 3.2 `run.author_turn` (D2)

1. `ledger.check_burn(...)` → refuse (loud) on a hard-burned or unresolved-`possible` lineage without
   `--override`.
2. Build the **author jail profile** (`jail.author_profile`): workspace = `git archive <base_sha> |
   tar -x` into the run's author dir; mount the author-visible subset ONLY. `jail.assert_manifest`
   checks the mount set == the exact author set and that `fact_key.yaml` (and any key-schema file) is
   ABSENT (V2d).
3. Run the canary (`authorbench-canary.sh author`): outcome + author-identity token classes over
   every mount; Park on any hit.
4. Dispatch the candidate author turn via `confined-authorbench.sh author` — **no ARB Memory MCP, no
   bridge inbox, no network beyond the model API** (`jail` asserts no MCP configured, V2c). The
   dispatch brief is self-contained and carries the steer blocks byte-identical to the frozen bundle.
5. Store the raw draft (`author_draft` event); authors in the same run never share a workspace and
   drafts are written outside any shared checkout until all finish (review-hygiene, applied to authoring).

Bare-API authors (pi-GLM-class) and Anthropic-family authors are **out of v1 scope** (named): the
former has no filesystem for authoring; the latter **requires the containerized Claude Code seat
first** (design D2 prerequisite — a parameter, NOT built here). v1 benchable authors = jail-runnable
engines (codex, agy).

### 3.3 `normalize.stage_for_judges` (D3) — quarantine is structural

Judges read from `runs/<run-id>/staging/<draft_handle>/` which ONLY `stage_for_judges` populates.
Flow: `normalize` → `denylist_scan` → clean ⇒ write to staging; hit ⇒ `redact_once` + rescan ⇒
clean ⇒ write; still-hot ⇒ raise `BlindingAbort(token_class)` and write NOTHING to staging (the
draft is provably unreachable by any judge). Both polarities exercised (V1): a seeded marker must
quarantine; a fixture draft *about* seat ids (legitimate subject matter) must pass clean.

### 3.4 `run.judge_phase` (D3, D5)

- Judges score **candidate + FROZEN baseline** drafts fresh in the same sitting (D5 baseline: baseline
  drafts are authored ONCE per corpus version by the current rotation-default model, cold+jailed,
  stored in the run bundle namespace; refreshed only on rotation-default or corpus-version change).
- Each judge runs in its role profile: filesystem judges (codex, agy) in the jail with
  `{export, draft, fact-key, rubric}` mounted; the bare-API judge (pi-GLM) gets the same set inlined
  as a packet (`judge.judge_packet`) — no filesystem to leak through. A judge is **never** dispatched
  into the live checkout (V2/D3 — the live repo holds the committed drafts + bake-off records).
- Judge dispatches follow review hygiene: independent phase, per-judge replies captured
  synchronously, reports outside any shared surface until all land (`multi-model-consensus.md:114-119`).
- `parse_anchors` extracts `{dim: (score, quote)}` per judge; a judge returning no parseable anchor
  for a dim ⇒ that judge is `judge-error` for that cell (recorded, never silently dropped).

### 3.5 `rubric` mechanical checkers (D3)

- **`check_r1`** — every `file:line` citation in the draft resolved against the `base_sha` export;
  a citation whose file/line is absent or whose text does not match ⇒ `fabricated`. Emits
  `mechanical_r1` with per-citation truth. (This is V3's citation checker.)
- **`count_r4`** — counts verification obligations carrying a concrete red condition (regex over the
  draft's obligation blocks for a "delete/remove … ⇒ red"-shaped clause).
- **`r6_context`** — assembles each `fact_key.traps[]` precondition into the judge packet
  (deterministic INPUT prep, recorded as `factkey_r6_context`). The avoided|carried
  DETERMINATION is a judged outcome: each judge tests precondition-survival over the draft
  with the key in hand (semantic — r2 codex+GLM P1: a prose draft cannot be
  deterministically precondition-checked; the earlier `check_r6` mechanical contract is
  DELETED, not renamed).

### 3.6 `score.cell` + `report` (D4)

`cell(dim, judge_anchors, mech)` = median of the judge anchors mapped `0→NOT-MET, 1→PARTIAL,
2→MET`, then **mechanical cap**: R1 cell cannot exceed the mechanical citation result (R6 has
NO mechanical cap — its outcomes are judged; whether R6's JUDGED consensus caps the headline
is OQ1, parameterized). `render_seat_report` emits `report-<seat>.md` and calls
`report.guard` + `assert_no_rank_fields` over the assembled object before writing; a refused field
name (`winner|rank|total|composite|score_sum` or any cross-seat sort key) ⇒ `WallBreach`. Output
namespace is the bench's own (`runs/` + ARB Memory `author-bench/*`), mechanically separate from
Instrument 1.

### 3.7 `store` reproducibility + ARB Memory keying (D6)

- Append-only NDJSON per run is authoritative. `rederive_mechanical(run_dir)` re-runs the mechanical
  phase from the stored bundle + raw drafts and MUST reproduce the `mechanical_*` events
  byte-for-byte (V5). A judged re-run lands under a NEW judging-run id and never overwrites
  (append-only, evidence-store no-silent-drop).
- ARB Memory artefact per run keyed `author-bench / <author-seat> / <author-model-version> /
  <corpus-version> / <run-id>`, containing/referencing: bundle hashes, `base_sha`, raw + normalized
  drafts, normalizer + fact-key versions, judge set + judge model versions, per-judge raw outputs,
  mechanical records, exposure-ledger entries, and the report. The memory artefact is a
  pointer-carrying summary; the NDJSON + report files are the authoritative measurement source.

### 3.8 Trigger (D6) — manual only

`authorbench run`/`author` is invoked by a human on a model-change event (new candidate author model,
rotation-default version bump, or the human revisiting the rotation). No watcher, no automation —
consistent with the calibration SPEC's manual, report-only posture.

---

## 4. Verification obligations → named tests (V1–V9)

Design tags are carried; each test names its red condition. **Hermetic (CI) vs live** is stated
per test: **CI has no engines and no docker** (standing scar) — any test that launches the container
or a real engine is **live/local-only, NOT CI**. Manifest/argv/parse assertions that do NOT launch
docker are hermetic.

### V1 — blinding red-green + quarantine (`tests/test_normalize.py`) — HERMETIC
- `test_normalizer_strips_identity_markers` — a fixture with model names, seat ids, status headers,
  session labels normalizes to a marker-free draft; `denylist_scan` then returns clean.
- `test_seeded_marker_quarantines_unreachably` — seed one author-identity marker past the normalizer
  ⇒ `stage_for_judges` raises/quarantines and the staging dir is **provably empty** (no judge-readable
  file). **Red condition:** delete the scan/staging gate ⇒ the draft reaches staging ⇒ test red
  (deny-proofs-need-adversarial-verification).
- `test_redaction_pass_then_rescan` — a single-marker draft → `redact_once` → rescan clean → staged;
  `redaction_log` records the edit.
- `test_still_hot_after_redaction_aborts_loud` — a draft whose marker resists one redaction ⇒
  `BlindingAbort` naming the token class; nothing staged.
- `test_over_fire_draft_about_seat_ids_passes_clean` — a fixture draft whose *content* discusses seat
  ids (legitimate subject matter) passes clean (over-fire polarity).

### V2 — isolation proof, both polarities (`tests/test_jail.py`)
- **(a) in-tree, HERMETIC** `test_archive_export_excludes_outcome_globs` — `git archive 9de2850 | tar`
  contains no path matching **the AB-D1 bundle's `outcome_globs`** (detector green; v1.5 amendment —
  the generic-glob form false-flagged pre-existing unrelated panel docs); **the same detector on an
  export at `33a09a6` MUST find them** (detector not vacuous — 33a09a6 is the bake-off record commit).
- **(b) filesystem jail, LIVE (docker, no engine)** `test_absolute_path_read_blocked_in_jail` — from
  inside the author jail, a scripted read of the canonical outcome's ABSOLUTE host path fails (no such
  mount); **the same attempt run unjailed on the host SUCCEEDS** — proving the jail, not the cwd, is
  the boundary. Red condition: run the read without the jail ⇒ succeeds ⇒ the "blocked" assertion red.
- **(c) author-env assembly, HERMETIC** `test_author_env_has_no_arb_memory_and_no_network` — the
  assembled author profile asserts no ARB Memory MCP configured and no network beyond the model API;
  judge staging contains only `{export, draft, key, rubric}`.
- **(d) author-mount exclusion, HERMETIC** `test_author_mounts_exclude_factkey` — `jail.author_profile`
  mount set contains NO `fact_key.yaml` and no file matching the key schema; **planting the key in the
  author staging dir ⇒ `jail.assert_manifest` fails loud** (round-2 cold-Opus P0 deny-proof).
- **(e) per-role jail-profile proof** — manifest shape HERMETIC, canary-red LIVE:
  - `test_author_manifest_exactly` / `test_judge_manifest_exactly` (HERMETIC) — AUTHOR mounts asserted
    exactly `{workspace, author-visible bundle, tmpfs HOME + RO auth}`; JUDGE mounts asserted exactly
    `{export, draft, fact-key, rubric, tmpfs HOME}`; the `agy-home` persistent volume asserted ABSENT
    from both.
  - `test_canary_reds_on_seeded_token_per_role` (LIVE, docker) — seed an **outcome** token into an
    author mount ⇒ author canary red; seed an **author-identity** token into a judge mount ⇒ judge
    canary red; seeding an outcome token into a judge mount does NOT false-flag (the fact-key's outcome
    citations are legitimate for the judge — r3 P2). Red condition: remove the canary ⇒ seeded token
    goes undetected ⇒ test red.

### V3 — citation checker red-green (`tests/test_rubric.py`) — HERMETIC
`test_fabricated_citation_flagged` — a fixture draft with one fabricated `file:line` ⇒ flagged as a
mechanical R1 failure; an all-true fixture passes. Red condition: remove `check_r1` ⇒ fabrication
unflagged ⇒ test red.

### V4 — report wall (`tests/test_score_report.py`) — HERMETIC
`test_reporter_refuses_rank_fields` — attempting to emit `winner`/`composite`/`score_sum`/a cross-seat
sort key ⇒ `WallBreach`; `test_headline_counts_not_rates` — headline cells carry prose counts, never
percentages/slash-ratios; `test_r1_mechanical_caps_cell` — a draft with failing citations cannot show
R1 `MET` even with `2` judge anchors. Mirrors Instrument 1's report wall.

### V5 — reproducibility (`tests/test_reproducibility.py`) — HERMETIC
`test_mechanical_rederives_byte_for_byte` — from a stored run bundle, `rederive_mechanical` reproduces
the `mechanical_*` NDJSON records byte-for-byte; `test_judged_rerun_new_id_original_untouched` — a
judged re-run lands under a new judging-run id and the original is unmodified.

### V6 — exposure ledger (`tests/test_ledger.py`) — HERMETIC
`test_burned_pair_refuses_without_override` — authoring a hard-burned (warm-orchestrator or
memory-granted) lineage, or a `possible(public-mirror|fork-known)` lineage, refuses without an
explicit recorded `--override`; `test_possible_other_flags_and_proceeds` — a `possible(<other>)`
lineage flags the artefact and proceeds; `test_ledger_records_every_authoring_event` — every
(lineage, brief) pair appears in `exposure.ndjson`.

### V7 — hermeticity (`tests/test_hermetic_suite.py`) — HERMETIC (the gate)
V1, V2(a/c/d/e-manifest), V3, V4, V5, V6, V9 run in CI on fixture drafts with **zero live engines and
no docker**. `test_no_docker_no_engine_imports_in_hermetic_paths` asserts the hermetic suite never
shells to `docker`/an engine binary. The docker-requiring proofs — V2(b), V2(e-canary), V8 — are
explicitly marked live and skipped-with-asserted-reason in CI (no vacuous green).

### V9 — fact-key self-validation (`tests/test_factkey.py`) — HERMETIC + CI on frozen corpus
`test_factkey_validates_against_base_sha` — `factkey.validate` runs each `fact_key.yaml` against its
`base_sha` export: every `facts[].file:line` must exist and match its `pattern`; every
`traps[].precondition` must be checkable (its named `file`/`symbol` exists at `base_sha`). **Plus the
v1.6 glob polarity contract: every `bundle.outcome_globs` entry matches ≥1 path at HEAD and 0 paths
at `base_sha`** (empirically verified for AB-D1: 31 at HEAD, 0 at 9de2850). A key that
fails validation **blocks the run**. Runs in CI over the frozen corpus (uses `git archive`, available
in CI; no docker). Red condition: corrupt a key's line number ⇒ validation fails ⇒ run blocked.

**Full suite gate:** `python3 -m pytest tools/authorbench` green is required; the hermetic subset is
the CI gate.

---

## 5. V8 — live gate runbook (REQUIRED; docker + real engines; NOT CI)

Per live-verification-catches-cli-glue, one real end-to-end run before v1 is called done. Candidate +
baseline for this gate = **jail-runnable engines (codex or agy class)** — the Anthropic-author
question explicitly waits on the claude-container (design D2; **the claude-container is a NAMED
PREREQUISITE, a parameter, NOT built by this SPEC**).

1. **Prereq:** container image `arb-eval-seat:latest` built (`tools/eval/confinement/build.sh`); codex
   authed into the authorbench profile (RO auth, tmpfs HOME); agy ONLY if its auth spike (§2.4) has
   landed — V8 runs with the M3 fallback judge otherwise.
2. **Freeze + validate:** `authorbench freeze` the AB-D1 bundle (already committed under `corpus/v1.0/`);
   `authorbench factkey-validate --brief AB-D1` → green (V9).
3. **Baseline (one-time):** author the frozen baseline draft for AB-D1 with the current
   rotation-default model, cold+jailed, stored in the run bundle namespace.
4. **Author:** `authorbench author --candidate <codex|agy> --brief AB-D1` → canary green (author
   profile), draft produced in the jailed export, `author_draft` stored.
5. **Blind:** blinding scan green (or quarantine→redact→clean); normalized draft staged.
6. **Judge:** `authorbench judge --run <run-id> --panel codex,pi-glm,m3` (agy replaces m3 only
   when its auth spike has landed — the gate must not select a known-conditional judge, r2
   codex P1) → each judge canary green (judge profile / bare-API packet), anchored scores
   with quotes returned, per-judge replies captured.
7. **Score + report:** `authorbench score` then `authorbench report` → per-seat reports render inside
   the coded wall; headline counts (not rates); construct-validity block present.
8. **Store + re-derive:** run artefact stored (NDJSON + ARB Memory `author-bench/*` key);
   `store.rederive_mechanical` reproduces the mechanical records byte-for-byte.
9. **Record cost** into design D6's envelope (measured $ + wall-clock). CHANGELOG.md entry (what AND
   why) per repo discipline.

**Negative control (part of V8):** plant an outcome token into the author mount ⇒ author canary Parks;
plant the fact-key into the author staging dir ⇒ prep fails loud (V2d live). Both must go red or the
guards are hollow.

---

## 6. Rejected alternatives (carried from design §Rejected, binding — none to be introduced)

Head-to-head comparative judging with a carried-forward winner; pairwise-preference LLM judging;
seeded-defect briefs; a synthetic brief corpus in v1; worktree isolation for authors; judging by the
live panel inside the normal pipeline; automatic trigger on model-version detection / auto-feed to the
rotation default; building judge-bias measurement into the bench.

---

## 7. Non-goals (carried from design §Non-goals)

Reviewer floor + implementor generation floors (Designs A/B, separate namespaces); any
trust/quorum/routing/rotation-default change from bench output; benching the warm inline orchestrator
(structurally unbenchable — cold-subagent-same-model is the honest proxy, gap named); judge-calibration
mechanisms (redirected to the existing live loop); synthetic briefs / corpus-growth policy beyond v1.0
/ paneling bench drafts through the real pipeline; statistical capability claims at any N reachable by
this corpus; model-training-contamination detection (named per-run as `prior_exposure`, not solved).

---

## 8. Escalations

**None block the build.** Items surfaced for a reviewer to check (specification choices resolving
design under-determination, not reopened decisions):

1. **Blinded draft handle (§2.7).** The design mandates blinding but does not name the mechanism that
   keeps the author label out of the *event stream* a judge could see. The SPEC pins a per-run opaque
   `draft_handle` with the handle→seat map stored outside every judge mount. Additive; strengthens
   wall #3, weakens nothing.
2. **`base_sha` resolution for verbatim anchors (§2.1, design OQ3).** AB-S1/AB-P1 `base_sha` = parent
   of the committed artefact's commit, resolved at `freeze`. This is the design's stated intent
   ("parent of the committed spec/plan") made mechanical; the corpus table stays data (swappable per
   the open fork), so no decision is foreclosed.
3. **Design open forks left parameterized (not resolved):** OQ1 (whether R6's JUDGED consensus
   caps the headline cell — the SPEC default is NO cap; R6 has no mechanical result to cap with
   since R6 outcomes are judged, r2/r3 panels); OQ3 (AB-S1/AB-P1
   fact-key richness — corpus table is data); OQ4 (claude-container ordering — a prerequisite
   parameter, v1 gates on codex/agy). Each default is a runnable value; changing it is a
   config/data change, not a redesign.
