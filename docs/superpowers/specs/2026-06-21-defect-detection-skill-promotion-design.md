# Defect-detection skill promotion — held-axis hunts as operative panel procedures [design]

**Status:** DESIGN (for the design panel). Promotes the `docs/defect-classes/` corpus from *reference* into
*operative doctrine* a panel runs. **This is the one slice where the artifact under construction IS the
reviewing instrument** — so its gate order is inverted (§1). Corpus: `docs/defect-classes/`.

## 0. Scope — IS / IS NOT
**IS:** turn the two highest-value detection moves into **procedures a review panel executes** (not facts it
reads), wired into the review-discipline skill (`skills/bridge-protocol` and/or the `skills/diagnose` panel
briefs); **plus an eval** (positives + negatives) that is the **primary gate** for this slice.
**IS NOT:** not a rewrite of every corpus entry into a skill; not new gate *machinery* beyond what the hunts
need; not auto-remediation — the hunts *flag*, a human/panel adjudicates.

## 1. The circularity, and why the eval is the primary gate (the load-bearing decision)
Every prior slice, the diagnosis skill/panel was the *independent* check on the work. **Here the work IS the
skill.** A flaw in the new detection moves cannot be caught by a panel using those very moves to review — the
hollow-deny-proof problem, applied to the reviewer itself ("the reviewer is the work"). Resolution: **invert
the gate order.**
- **Primary gate = the EVAL.** The four session findings are **fixed historical facts** (the pre-fix code
  states exist in git) — the skill either flags the held-axis class on each or it doesn't, and that judgment
  does **not** depend on the skill being correct. This is the one check that can't be circular.
- **Secondary gate = the panel**, run **after** the eval, with eval results in front of it. The panel's
  question becomes *"do we trust this eval, and is the skill sound beyond what the eval covers?"* — not *"is
  this skill any good,"* which the skill is too entangled to answer cleanly.
- **Done = the eval passes (recall AND precision, §4) AND the panel confirms** — in that order.

## 2. The home — where the hunts live
The hunts attach to the review path the panel already runs (briefs/gate), as a **standing checklist of
procedures** the reviewer executes against the diff under review. Proposed home (panel to confirm): a
`defect-hunts` section the `skills/diagnose` brief composer emits into every review brief, backed by the
`skills/bridge-protocol/gate` standing checks. Exact wiring is a §8 open; the *form* (procedures, not prose)
is fixed (§3).

## 3. The two detection moves — as PROCEDURES the panel RUNS (not descriptions it reads)
A skill that says "here are defect classes" is reference; the promotion is operative only if it changes what
the reviewer *does*. Each hunt is a concrete procedure with a decision:

- **H1 — vary-the-config / grep-the-constant (config drift across call sites).**
  *Procedure:* for each constant the diff makes (or could make) configurable (e.g. a module global read from
  env), **grep every reader of that constant** and assert they **co-move**; and for each configurable
  property, confirm **a test sets it OFF its default and asserts the dependent behaviour**. *Flag if:* a
  reader was left at the hardcoded value while a sibling became configurable (the `audit.PREFIX=""` shape), OR
  no test varies the property off-default.
- **H2 — run-the-path (environmental assumption never violated).**
  *Procedure:* enumerate the environmental assumptions the code makes (a dep is importable, a process is up,
  a real workdir, single-process, on-default config) and for **each**, identify **the run that violates it**
  (a lean env, real process timing, a real seat workdir, two concurrent processes). *Flag if:* a load-bearing
  assumption has no run that violates it — i.e., the primary path was only ever exercised under the held
  condition.

These two are the substance; the run-the-path / vary-config distinction is correct and load-bearing (they
have *different* hunts — one is a static grep, one is a missing run).

## 4. The eval — the primary gate (recall AND precision)
Built on the `tools/eval/` harness (scenarios + fixtures). Two halves; **both** required.

### 4a. Positives — must-FLAG (recall). The four session findings, reconstructed from git (non-circular):
| Case | Pre-fix state (git anchor) | Hunt that must fire |
|---|---|---|
| audit-prefix | `audit.py PREFIX=""` while `bus.py` env-configurable (pre-`113fc89`) | H1 |
| seat-import | `run.py` top-level psycopg/mcp; client not import-light (pre-`ac502ae`) | H2 |
| boot-race | sentinel written before group exists (pre-`5e46276`) | H2 |
| redis-per-seat | seat runs bare python3 lacking redis (pre-`5e46276`) | H2 |
The skill, run **blind** against each pre-fix state, must flag the held-axis class. Recall = 4/4.

### 4b. Negatives — must-STAY-QUIET (precision). **The harder, more valuable artifact.**
**A skill that flags everything catches the four and is useless** — a smoke alarm that fires on toast buries
real findings in noise and the panel learns to ignore it (itself the indiscriminate-green defect this corpus
names). So the eval needs negatives the skill must **not** fire on:
- **configurable properties whose readers correctly co-move** — real repo cases where a constant is read by
  several sites and all move together (H1 must stay quiet);
- **environmental assumptions that are actually safe / paths genuinely covered** — real repo code where the
  held axis genuinely doesn't matter or a real run already exercises it (H2 must stay quiet).
- **Sourced from REAL repo code, not toy examples** — so the skill is tested against the actual texture of the
  codebase. **Sourcing the negatives is itself panel-challenged**: the design panel must adjudicate *"are
  these negatives actually safe, or did we just assume they were?"* — guarding against an eval rigged toward
  passing. Precision = 0 false-fires on the negative set.

## 5. Sequencing (because of §1)
`design → design-panel → build (the hunts + the eval positives/negatives) → RUN THE EVAL (primary gate:
recall 4/4 + precision 0-false-fire) → code-panel CONFIRMS (eval results in front of it)`. The eval breaks the
circularity; the panel confirms what the eval can't reach (soundness beyond the eval's coverage).

## 6. Tests + review
- The eval harness run is the merge gate (§4/§5). Unit tests for each hunt procedure (given a crafted diff →
  the right flag/no-flag). The hunts' own deny-proofs inject-revert-verified (a hunt that can't fail to flag
  its positive is hollow).
- Review: design panel on this spec, then build, then the eval (primary), then the code panel (confirm).

## 8. Opens for the design panel
- **O1 (home/wiring):** is `skills/diagnose` briefs + `skills/bridge-protocol/gate` the right home, or does an
  operative hunt need to live as executable check code (e.g. an H1 grep-the-constant linter) the panel runs,
  not just brief text a reviewer is told to perform?
- **O2 (procedure vs prose):** how do we *guarantee* the build produces procedures the panel runs, not prose
  it reads — is H1 mechanizable as a static check (grep readers + co-movement assertion) and H2 as a checklist
  the brief forces an explicit answer to?
- **O3 (negatives):** are the proposed negatives actually safe? (panel-challenged — the load-bearing precision
  guard). How many negatives are enough to trust precision?
- **O4 (eval as gate):** is reconstructing the 4 positives from pre-fix git state the right non-circular
  anchor, and is "blind" run achievable (the skill must not be told which class to look for)?
- **O5 (scope):** only H1+H2 now, or do other corpus classes (fixture-supplies, framework-face) also promote
  in this slice — or is two-done-well the right cut?

---

## 9. v2 — folded the 4/5 design panel (cold-Opus + agy + codex + M3; GLM timed out). All DESIGN-HOLES.

The panel reshaped the slice. The two biggest were structural (wrong eval instrument; wrong home); the rest
hardened the eval into something that can't be gamed.

- **HOME — the review/diff path, NOT `skills/diagnose` (agy P0, the architectural catch).** `diagnose` is
  **failure-triggered** (it runs on a failing test) — but **held-axis bugs PASS the suite**. Wiring the hunts
  there means they never fire. They live on the **code-review path** (every review, where the held-axis bug is
  in the *diff*): an **executable H1 check** + an **H2 forced-answer section** the review brief
  (`skills/diagnose/briefs.py` composer for *review* briefs, and/or `skills/bridge-protocol/gate` standing
  checks) emits and the gate enforces.
- **EVAL — a purpose-built DETERMINISTIC check-runner, NOT `tools/eval/arb_eval` (cold-Opus P0, the
  load-bearing fix).** `arb_eval` is a *statistical LLM-panel capability-floor* instrument with a **closed
  taxonomy that raises on an unknown class** — it cannot run a deterministic grep/AST check. Build a small
  dedicated runner: for each **sealed scenario**, run the H1 AST check (+ present the H2 schema), capture the
  skill's **pinned structured output**, assert flag/no-flag. The whole circularity-break depends on this gate
  being real and runnable.
- **POSITIVES — count independent CLUSTERS, not raw findings (cold-Opus P0).** boot-race + redis-per-seat
  share fix commit `5e46276` → correlated; it's **~3 independent positives**, not 4/4. Recall bar = flag every
  independent cluster; result is **class-seed / merge-floor validation, NOT broad certification** (codex);
  add held-out cases as they accrue.
- **NEGATIVE MANIFEST — pinned PRE-BUILD, named, with a min count + 0-false-fire budget (agy+codex+M3 P0).**
  Not "panel-challenged after" (that lets the build pick easy negatives once it's seen the hunt). The manifest,
  fixed before the hunt is built:
  - **Clean co-moving negatives (must STAY QUIET):** `ARB_MEMORY_PREFIX` (`bus.py:14`+`audit.py:20`, co-move,
    tested — the same locus that is the *positive* pre-`113fc89`); `BRIDGE_NOTIFY_INBOX` (routing tests,
    readers co-move).
  - **TRAP negatives (must FLAG — the discrimination test):** `BRIDGE_ROLE_PROFILE_FILE` — *looks* like a
    co-moving config but its readers **diverge** (`bridge.py:149` reads the env-file; engines read only
    `os.environ`) — *assumed-safe, not safe*. A skill that calls this a negative is pattern-matching
    "two readers = safe," not detecting. These prove precision AND discrimination.
  - **Suspects — RESOLVE EACH BEFORE MERGE (an explicit gate item, not a loose end):** `ARB_MEMORY_DSN`,
    `ARB_MEMORY_REDIS_URL` (agy), `BRIDGE_MAX_PARALLEL` (codex — *"parser-safe but maybe not
    runtime-concurrency-safe"*). Each closes as **either a verified clean negative (readers shown to co-move)
    OR a logged finding** — never an assumption. `BRIDGE_MAX_PARALLEL` is the pointed one: a parallelism
    constant that's safe in every test and could bite under real concurrency is *the held-axis shape itself*.
    **If it is a real held-axis bug, the skill found its own class's instance during its own construction** —
    shipping the skill with that first live catch unresolved would reproduce the very defect it detects. So the
    suspect list is adjudicated to closure (negative or finding) as a merge precondition; the discrimination
    the skill is *about* is exercised, live, on its own design inputs.
- **SEALED + CLEAN BLIND (agy+codex+M3 P0).** Scenarios present **only the pre-fix git diff/context** — no
  defect-class names, no filenames that leak the class — AND a **clean worktree** checkout of the pre-fix SHA
  (no later CHANGELOG/corpus/session notes visible), or the "blind" is contaminated. The skill must not be told
  which class to hunt.
- **PIN THE SKILL'S OUTPUT FORMAT (M3 P0).** The hunt emits a structured verdict per check
  (`{constant|assumption, FLAG|CLEAR, evidence}`) so the eval recognizes a flag deterministically — else the
  eval false-negatives without the skill being wrong.
- **H1 = AST kernel; H2 = forced-answer schema (cold-Opus+codex+M3 P1) — COMMIT to the split.** H1 co-movement
  is semantic (the `prefix=PREFIX` default-arg capture); the static kernel is **AST: a literal default where a
  sibling reader is env-derived** — only over constants the diff *changes*, not "could make configurable"
  (which is unbounded → reviewer memory). H2 has no static kernel → a brief schema requiring
  `{assumption, violating_run, evidence}` **or** `FLAG` per assumption, **missing fields fail the gate.**
- **PIN the hunts' own inject-revert semantics (M3 P0):** "remove the defect" for a hunt deny-proof must be the
  minimal, specified reversion (so a builder can't define a narrow remove that hides it). A hunt that can't
  fail to flag its positive is hollow.

**Sequencing (unchanged, now concrete):** build (H1 AST check + H2 brief schema + the deterministic runner +
the pinned negative manifest) → **RUN THE EVAL** (recall: every independent cluster + the trap-negatives
flagged; precision: 0 false-fire on clean negatives) → **code-panel CONFIRMS** with eval results in front of
it. O1–O4 close here; O5 = H1+H2 only (breadth deferred, with a one-line roadmap note).
