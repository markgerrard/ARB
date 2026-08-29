# ARB Disagreement Corpus

> **⚠ READ FIRST — what this file is (reconciled after DC-002).** This is **Instrument-2-LITE**:
> **calibration / agreement data on *legible* defects.** It is **NOT** the decorrelation oracle it was
> first conceived as, and it **cannot, by itself, answer the cold-Opus-voting question** — see
> § ⚠ STRUCTURAL LIMITATION below, which is authoritative. Any earlier "the decorrelation measure" /
> "answers the voting question" framing in this header is **superseded** by that section. The
> seat-keep/drop question needs an *external* oracle (escaped defects / prod incidents) — see
> `docs/escaped-defect-journal.md` and the eval-design v3 decision.

> **Purpose:** Records **real seat splits on real reviews** — cases where seats disagreed on a genuine
> artifact and adjudication eventually resolved who was right. Useful for characterizing seat
> *calibration* and *agreement patterns* on legible defects (real value — DC-001 is exactly that). Not
> a decorrelation oracle (⚠).
>
> **Why this and not a harvested-defect corpus:** a harvested-from-fix-commits defect is one that was
> *eventually caught and fixed* — still the legible set, one step out. The decorrelation-revealing
> event is a seat catching (or mis-flagging) something on a real review that other seats didn't. Those
> events are *splits*, and they already exist in ARB's review history. This file harvests them.
> **(Caveat per DC-002: a whole-panel miss is not a disagreement, so this corpus is still
> "legible-to-at-least-one-seat" — see ⚠.)**
>
> **The data this feeds:** per seat, by defect class — lone-correct rate (caught a real defect others
> missed) vs lone-wrong rate (flagged a non-defect others correctly didn't, or mis-severitied). **Read
> these as calibration/agreement descriptors, NOT as a decorrelation/seat-value metric** — lone-correct
> rate is the confounder the eval-design panel rejected (P0-1: it scores a high-value *confirmer* seat
> ~0), and it is legibility-bounded (⚠). **Both directions count** — this instrument is not built to
> vindicate any seat, including cold-Opus.
>
> **Adjudication source matters:** record HOW the split was resolved (config check, production data,
> later catch, human call). A split resolved by ground-truth (config/prod/test) is a strong row; a
> split resolved by orchestrator opinion is weak and flagged as such.

---

## Schema (per entry)

- **ID** — sequential
- **Date** — review date
- **Artifact** — what was under review (diff/spec/skill/etc.)
- **Split** — which seats said what
- **Adjudication** — how it was resolved + by what authority (ground-truth > opinion)
- **Outcome** — per seat: lone-correct / lone-wrong / with-consensus / overcall / undercall
- **Class** — defect class (posture-oracle taxonomy + correctness/perf/logic/severity-calibration)
- **Signal strength** — strong (ground-truth adjudication) / weak (opinion adjudication)
- **Note** — the characterizable pattern, if any

---

## Entries

### DC-001 — 2026-06-16 — project-a dev..main session-cookie middleware review

- **Artifact:** `git diff dev..main` — `ExpireBroadScopeSessionCookie` middleware
  (the `.example.com` duplicate-cookie / 419 fix) + bootstrap registration, a design
  doc, lockfile bump.
- **Split:**
  - **agy (Gemini-family adjunct):** FIX_BEFORE_MERGE, **P0** — "unconditionally sets
    an expired `.example.com` cookie → if `SESSION_DOMAIN=.example.com`, deletes
    the live session every request, logs everyone out."
  - **codex (GPT-5.5, code-path quorum):** SHIP, no material findings.
- **Adjudication:** **Ground-truth (config check).** Orchestrator verified
  `SESSION_DOMAIN` is unset in both dev and prod → live cookie is host-scoped, so the
  `.example.com`-scoped expiry targets a *different* cookie and cannot clobber the
  live session. agy's P0 is a non-firing hypothetical ("if it's ever configured to
  .example.com"), which it isn't. codex independently reached the same cookie-identity
  conclusion. Resolved **SHIP**.
- **Outcome:**
  - **agy → lone-wrong (severity overcall on a conditional).** Hard P0 label on a
    latent/non-firing condition. The underlying observation (the coupling exists) is
    *valid* and worth a defensive guard, but the severity was wrong: latent P2, not P0.
  - **codex → with-consensus/correct.** Stopped at the firing analysis; correct.
- **Class:** severity-calibration (specifically: conditional/latent-defect overcall).
- **Signal strength:** **strong** — adjudicated by config ground-truth, not opinion.
- **Note:** Characterizable pattern — agy (Gemini-family) labels latent/conditional
  defects at their *worst-case-if-configured* severity rather than their
  *as-configured* severity. Orchestrator named it independently: "thorough but
  soft-calibrated on severity." This is the **pi-soft-label / calibration gap** the
  `using-agent-bridge` skill already documents, showing up live.
  **Calibration read:** this row counts *against* agy on the severity-calibration axis —
  its lone-flag here was burden, not catch. Notably cuts against a non-Claude seat, which
  is evidence the instrument isn't just rewarding the Claude family. (Caveat: agy's
  *observation* was real; only its *severity* was wrong — so the honest scoring is "valid
  finding, miscalibrated severity," not "false positive." Track those as distinct: a
  miscalibrated-real-finding is less negative than a pure false flag.)

### DC-002 — 2026-06-16 — eval-suite-design v2 panel review

- **Artifact:** `docs/eval-suite-design.md` (v2) — the redesigned eval suite, specifically
  Instrument 2 (the disagreement corpus *this file is*).
- **Split:**
  - **codex:** REJECT — confirmed Instrument 1 confounds closed; flagged residuals.
  - **agy:** BLOCK_MERGE (REJECT) — thorough, but per prior pattern, label-heavy.
  - **cold-opus:** FIX_BEFORE_BUILD with the structural catch the other two did **not**
    name cleanly: that all three corpus reframes (seeded → harvested → disagreement) hit
    the *same wall* — a catch-record is definitionally blind to whole-panel misses — and
    that v2's own §4 argument against the harvested corpus ("excludes the never-caught
    set") **applies verbatim to the disagreement corpus**, because a whole-panel miss is
    not a disagreement. v2 fixed the seeding half and left the never-caught exclusion
    fully intact. "Fixed the wrong half."
- **Adjudication:** **Strong (logical/structural).** The contradiction is internal and
  checkable: v2's discriminating argument against its own predecessor defeats its own
  instrument. Not opinion — the design's text refutes itself once the invariant is named.
  codex and agy circled the confirmer-confound (P0-1 reincarnated) but **cold-opus alone**
  named the deeper invariant (every internal corpus is "legible-to-X"; the never-caught
  set is outside every X).
- **Outcome:**
  - **cold-opus → lone-correct (structural/foundational).** The catch the panel's whole
    justification rests on: a defect (here, a design flaw) that the deep seat caught and
    the others missed. **This is the first row bearing on the cold-Opus-voting question,
    and it answers in cold-Opus's favour** — on a real artifact, cold-Opus was the lone
    catcher of the load-bearing flaw.
  - **codex → with-consensus/correct.** Caught the confirmer-confound (P0-1), missed the
    deeper invariant. Correct but not deepest.
  - **agy → with-consensus, label-heavy.** Same direction, less differentiated; consistent
    with DC-001's calibration pattern (thorough, hard labels, not the sharpest cut).
- **Class:** judgment / structural-construct-validity (the *illegible* class — exactly the
  class the seeded corpus cannot contain, which is why this row could only arise on a real
  review, not a benchmark).
- **Signal strength:** **strong** — logical self-contradiction, adjudicable without opinion.
- **Note:** This is the row that matters for the voting-seat decision, and it is itself a
  live instance of the thing it's about: cold-Opus's value is catching the illegible
  structural defect no one seeded and the other seats didn't reach. **Bootstrap-contamination
  caveat (cold-opus's own P1-3, applied honestly to this very row):** this split was
  adjudicated in-quorum, in the session arguing about cold-Opus, so it is *correlated
  evidence*, not clean. It counts, but it cannot be the *sole* basis for keeping cold-Opus —
  logging it as such would be the exact self-referential curation cold-opus flagged. Recorded
  as a real but contaminated row.

### DC-003 — 2026-06-16 — eval dispatch-pipeline gate (F6: distinct-instance loophole)

- **Artifact:** `feat/eval-dispatch-pipeline @ 3925670` — the built Instrument 1 dispatch
  pipeline (a real code artifact under execution-primary gate review; NOT a self-referential
  discussion).
- **Split:**
  - **cold-opus (gate):** lone-flagged that `class_level_ok` counts seed **rows**, not
    **distinct** instances — so 5 seeds at the *same* location clear `I_min=5` and earn a
    class-level PASS. The P0-A guard's own row-count loophole. Demonstrated by running.
  - **codex (gate):** REJECT on other findings (F1 NDJSON etc.); did **not** flag F6.
  - **agy (gate):** FIX_BEFORE_MERGE on other findings; did **not** flag F6.
- **Adjudication:** **strong (execution).** Reproduced: dup-location seeds → class-level PASS;
  the fix (count distinct `(file,line/symbol)`) verified to flip it to INSTANCE-LEVEL. Not opinion.
- **Outcome:** **cold-opus → lone-correct (structural).** codex/agy → missed this class
  (under-reached, not contradicted).
- **Class:** judgment / structural-construct-validity (illegible — a *variant* of a known bug
  class that neither the human's P0-A flag nor the schema nor the other two seats anticipated).
- **Signal strength:** **strong.**
- **Note:** **CLEAN row — contrast DC-002.** Adjudicated by execution on a real built artifact, in
  a code review **not** about cold-Opus's seat value → **not bootstrap-contaminated**. This is the
  cleanest lone-correct structural row to date: a deep seat catching the adjacent thing nobody
  specified, on a real review — the illegible-catch value the disagreement-corpus saga concluded
  only shows up on real reviews.

### DC-004 — 2026-06-18 — agent-sdk mutation-probe delta review (held-out oracle filesystem-reachable)

- **Artifact:** `feat/agent-sdk-probe` delta — the anti-false-PASS verifier (`verifier.py`) of the
  agent-sdk mutation probe, under tri-model delta code review (real code artifact, executed).
- **Split:**
  - **agy:** lone-flagged **P0** — the held-out oracle (`held_out/cases.py`) lives on disk and the
    candidate impl runs in an **unsandboxed** subprocess, so a malicious impl can `rglob` for the
    answer key, read the expected outputs, print a spoofed result, and `os._exit(0)` before the driver
    compares → **false-PASS**; the "held-out" oracle isn't actually held-out. **Demonstrated by
    construction** (wrote the exploit; `verify()` returned PASS).
  - **cold-opus:** SHIP_WITH_NITS — *"could not construct a false-PASS."* Reasoned (correctly) that
    expected is never in argv/stdin/cwd/env; **did not check filesystem reachability**. Missed the P0.
  - **codex:** FIX_BEFORE_MERGE, but on a **different** finding (the `_is_artifact` substring P1); rated
    the marker-spoof a P2 "not demonstrated." Did not flag the filesystem-read P0.
- **Adjudication:** **strong (execution).** agy's exploit verified to PASS pre-fix; the structural fix
  (oracle ships only `sha256(expected)` + fresh inputs → answer absent from the reachable namespace by
  construction) verified to flip the same exploit to FAIL (regression-tested), while the legitimate path
  still PASSes (M3/Kimi/GLM live).
- **Outcome:** **agy → lone-correct (structural).** cold-opus → under-reached (enumerated-surface
  reasoning, missed the filesystem window). codex → caught a real adjacent P1, missed this P0.
- **Class:** judgment / structural — **answer-key reachability** (same un-cheatable-by-construction
  class as the floor-suite confinement: a verifier whose answer key is filesystem-reachable by the
  process under test is un-cheated-by-luck, not un-cheatable-by-construction).
- **Signal strength:** **strong** (execution / ground-truth — the exploit was run and observed to PASS).
- **Note:** **MIRROR of DC-002/DC-003**, where cold-opus was the lone deep-catcher. Here the *non-Claude*
  seat (agy) is lone-correct **against** the deep Claude seat (cold-opus) — direct evidence the panel's
  decorrelation is **seat-agnostic**, not a cold-opus-vindication instrument (the "cuts both ways"
  property the corpus exists to test). cold-opus closed the doors it enumerated; agy checked the window.

---

### DC-005 — 2026-06-18 — agent-sdk engine IMPLEMENTATION tri-panel review

- **Artifact:** `feat/agent-sdk-engine` build (the AgentSdkEngine + bridge wiring), tri-model
  implementation review against the panel-hardened spec/plan. Safety core (can_use_tool gate
  fail-closed, run_turn silent-death) was unanimously confirmed sound; the splits were on functional
  defects, not the safety the prior panels protected.
- **Split (two independent disagreements in one review):**
  - **agy:** lone-flagged **P0 "secret leak in scrubbing logic"** — claimed `scrub(text, secrets,
    var_names)` only redacts the env-var *names*, never their *values*, so vendor keys leak in
    cleartext. **FALSE POSITIVE.** Source-verified: `self.key` (agent_sdk.py:55) IS the resolved key
    *value* (injected into the env at L84), and every call passes `secrets=[self.key]` — the actual
    value is redacted; the var-name redaction is bonus. agy reasoned from the function signature
    without checking the call site.
  - **codex:** lone-correct **P1** — `assert_serveable()` hard-requires a `Write` *denial*
    (`saw_write_denied`), but the engine's whole purpose is a `Write`-in-ceiling mutation seat, for
    which the trusted startup probe *allows* Write → `assert_serveable` raises → **the mutation seat
    can never start** (Task 8 fails at launch). cold-opus and agy each exercised only a Read-only
    ceiling and missed it. Source-confirmed (`decide()` denies out-of-ceiling first, so the negative
    probe must use an out-of-ceiling tool, not hardcoded Write).
  - **cold-opus:** SHIP_WITH_NITS — caught the real `isolated_env` prefix-leak (with consensus), but
    rated the continuation/resume gap P2 ("in-process works") when the bridge's first-class resume
    protocol is in fact unwired (thread_id clobbered to None), and missed codex's startup-blocker.
  - **consensus (not a disagreement):** all three caught the `isolated_env` enumerated-vs-prefix leak.
- **Adjudication:** **execution-grade.** Both contested claims were first traced to specific lines,
  then confirmed by the fix dispatch: codex's `test_startup_guard_allows_mutation_ceiling_with_write`
  fails on the pre-fix code (proving a `Write`-in-ceiling seat could not start) and passes after
  `e07f29d`; agy's "scrub leaks values" P0 is falsified by `self.key` being the resolved value passed
  as `secrets=[...]` (the redaction test stays green, no scrub change was needed). Suite 360 OK post-fix.
- **Outcome:** **agy → lone-WRONG (false positive, P0 security claim).** **codex → lone-correct
  (P1 functional blocker).** cold-opus → under-reached on two functional gaps (Read-only test bias).
- **Class:** correctness / functional-blocker (codex) + false-positive (agy). The false-positive is
  the more-negative bucket per Open-methodology-note #1 (flagged a non-defect, not merely
  miscalibrated severity).
- **Signal strength:** moderate→strong (source-adjudicated now; execution-adjudicated after the fix
  test runs).
- **Note:** **Cuts-both-ways within one session.** The same non-Claude seat (agy) that was
  lone-correct in **DC-004** is lone-WRONG here, while **codex** — 0 lone-correct across DC-001/002 —
  lands its first lone-correct. Direct evidence that no single seat (Claude or non-Claude, deep or
  cheap) is reliable alone: the decorrelation value is the *panel*, and the orchestrator's
  source-verification of every contested claim is the load-bearing adjudicator — not any seat's
  verdict prose. agy's dramatic "P0 secret leak" headline was the thing most likely to be waved
  through on authority; reading the call site killed it.

---

## Running tallies (update as entries accrue)

> Too few entries to mean anything yet (N=1–2). Tallies become meaningful at N≥~20 per
> seat per class, per the eval-design panel's CI findings — **and even then are calibration/
> agreement reads, not seat-drop evidence (⚠).** Do NOT draw seat-drop conclusions from this
> file — same discipline as the seeded suite's "no seat-drop verdict" rule.

| Seat | Class | lone-correct | lone-wrong | miscalibrated-real | notes |
|---|---|---|---|---|---|
| agy | severity-calibration / structural / false-positive | 1 | 1 | 1 | lone-correct DC-004 (structural); lone-WRONG DC-005 (P0 false positive); overcall DC-001 — **cuts both ways** |
| codex | correctness | 1 | 0 | 0 | lone-correct DC-005 (mutation-seat startup blocker); with-consensus DC-001, DC-002 |
| cold-opus | judgment/structural | 2 (1 clean) | 0 | 0 | DC-002 (contaminated) + DC-003 (CLEAN, F6); under-reached (not lone-wrong) DC-004, DC-005 |
| opus-4.8 (asdk seat) | correctness / severity-calibration | 0 | 2 | 1 | DC-007 lone-wrong ×2 (missed prod-wiring P0; falsely *cleared* door blast radius). Undercall-not-blind: its `.trash/` P2 predicted both fast-follow fixes. **Row provisional — run was gate-failed by orchestrator contamination** |
| opus-5 (asdk seat) | correctness / production-vs-test-wiring | 2 | 0 | 0 | DC-007 lone-correct ×2 from static reading with no execution; same P0 found on both harnesses (model-attributable, not tool-attributable); plus one net-new P1 no prior review holds |

> **\*DC-002 caveat:** cold-opus's DC-002 lone-correct is real but in-quorum/self-referential
> (split adjudicated in the session arguing about cold-opus) — counts but contaminated. **DC-003 is
> the first CLEAN lone-correct structural row** (execution-adjudicated on a real built artifact, not a
> session about the seat). Still not a seat-drop basis (⚠ — calibration/agreement data, not a
> decorrelation oracle), but it is the first uncontaminated data point bearing on cold-Opus's
> illegible-catch value.

### DC-006 — 2026-06-19 — diagnose-live-panel full build review (recompute anchor: contamination-P0 vs correctness-P1)

- **Artifact:** `git diff dev..feat/diagnose-live-panel-batch1` — the live-panel gate recompute reads
  observable content from `repo_root` (caller-supplied path), never `git show {repo_sha}:`.
- **Split (SAME finding, different severity-framing):**
  - **cold-Opus:** LIVE-PANEL-NEEDS-CHANGES, **P0 — contamination.** An orchestrator pointing `repo_root`
    at a doctored tree makes the recompute self-agree on the content channel; the regress §1 claims to
    terminate is reopened. Also found a 2nd P0 (submission authenticity via `phase_input` = forgeable) that
    GLM did not flag.
  - **GLM:** LIVE-PANEL-NEEDS-CHANGES (narrow), **P1 — correctness.** Same `repo_root`-not-`repo_sha`
    finding, framed as "`repo_sha` is decorative → legitimate verified runs false-block when the repo
    advances," not as a contamination hole; accepted the (fixture-based) forge control as proving authenticity.
  - **agy:** no verdict — timed out mid-analysis (process gap, not a clean seat).
- **Adjudication:** **cold-Opus's framing (P0 contamination).** For a *contamination-resistance* skill, an
  un-anchored input channel IS a contamination hole, not merely a correctness break — and cold-Opus was
  deeper (it traced the `phase_input` provenance GLM's control-acceptance missed). The *fix* is identical
  under both framings (`git show {repo_sha}:` for content). Orchestrator + user (Mark) confirmed P0;
  submission-authenticity narrowed honestly (deferred to #6) since no forgery-resistant ledger exists yet.
- **Outcome:** Both seats correct on the *finding*; **severity depended on the threat-lens** (security vs
  correctness). Lesson: for a security-property artifact, take the security framing — the same observation
  is a P0 under the property the artifact exists to provide. cold-Opus lone-deeper on P0-2 (provenance
  trace), GLM lone-narrower on severity. Defect-classes: `orchestrator-supplied-state-forgeable`,
  `control-proves-only-its-path` (see orchestrator-patterns Pattern F).

---

### DC-007 — 2026-07-24 — ARB Files MCP impl, cold re-review: Opus 4.8 vs Opus 5 on one harness

- **Artifact:** <workspace> `986a11a` (branch point `12274a3`; 11 commits, 27 files, +1697/-2) — the ARB
  Files MCP implementation exactly as reviewed on 2026-06-29, re-dispatched cold on the *original*
  brief (`docs/superpowers/reviews/2026-06-29-arb-files-impl-review-brief.md`). Base chosen so the
  known defect is still live: `0b28399^2` already contains the fix and would have been the wrong base.
- **Split:** Same brief, same commit, same `asdk` oneshot seat type — **model is the only variable**.
  Both seats were blocked from executing anything (`Bash outside ceiling`), so neither could run tests.
  - `asdk-bridge-dev-opus48` (Opus 4.8): **APPROVE_WITH_NITS** — 0 P0, 0 P1, 5 P2. Marked R2 and R3 ✅
    on test evidence; affirmatively cleared "door cannot break memory door ✓".
  - `asdk-bridge-dev-opus5` (Opus 5): **REQUEST_CHANGES** — 1 P0, 2 P1, 8 P2.
  - Third arm (different harness, for the confound): in-session Opus 5 subagent *with* execution —
    same P0, ran the suite (65 passed).
  - Two concrete splits:
    - **A — audit plane:** 4.8 says the delete/overwrite audit is correctly implemented and tested.
      Opus 5 says **P0: never wired in production**.
    - **B — door blast radius:** 4.8 says the memory door is safe because "top-level import is only
      `door_wire` (imports just `logging`)". Opus 5 flags an unguarded module-scope import.
- **Adjudication:** **Ground-truth (code, independently re-verified by the orchestrator).**
  - A: `door_wire.py:18` and `run.py:14` both construct `FilesStore(settings)` with no sink;
    `store.py:28` defaults `audit_sink=None`; `store.py:59-61` emits only `if self.audit_sink is not
    None`. The audit half is live **only** under test injection. Corroborated twice over: the original
    2026-06-29 cold-Opus review found the same defect as its sole P1, and it was fixed by `cbf6561`
    ("wire prod audit sink") *before* merge.
  - B: `src/arb_memory/mcp/server.py:8` is an unguarded module-scope
    `from arb_files.mcp.door_wire import register_file_tools`. 4.8's premise is true but answers the
    wrong question — `door_wire` being light does not help if the `arb_files` package is absent or
    broken; `server.py` then fails at import and takes the memory door down before any try/except can
    fail soft. That is precisely the outcome R7 exists to prevent.
- **Outcome:**
  - **Opus 4.8 — lone-wrong ×2.** Missed a ground-truth P0-class defect that both the June panel and
    Opus 5 found (A), and affirmatively *cleared* a real risk rather than merely omitting it (B). A
    false clearance is worse than silence: it spends reviewer authority on a wrong answer.
  - **Opus 5 — lone-correct ×2**, from static reading only. Found the same P0 on both harnesses (with
    and without execution), which is the control: the catch is **model-attributable, not
    tool-attributable**.
  - **Not a split, recorded for the escaped-defect angle:** Opus 5 (seat) produced a net-new P1 no
    prior review holds — `presign_put` contains zero trash/audit/copy calls, and `force=True` merely
    omits `IfNoneMatch`, so a presigned force PUT overwrites the live object with no recovery copy and
    no audit event, bypassing the delete-safety plane. Verified; unreported by June, by 4.8, and by
    the in-session Opus 5 arm.
  - **4.8 was not blind, it undercalled.** Its `.trash/` P2 independently rediscovered *both*
    fast-follow fixes (`04ef100` reserve trash namespace, `f4e7efa` etag in trash keys) without
    sight of them. Severity-calibration miss, not a detection miss.
- **Class:** correctness / **production-vs-test-wiring** — the mechanism exists and is exercised, but
  only the test path constructs it (`fixture-supplies-what-code-lacks` family). Split B adds
  **false-negative clearance** (a seat asserting a property it did not actually check).
- **Signal strength:** **strong** — adjudicated by code ground truth, re-verified independently by the
  orchestrator, and corroborated by an independent prior finding plus its fix commit.
- **Note:** ⚠ **Provisional row — the 4.8 arm is process-invalid.** Its dispatch was gate-failed
  (`completion.state=dirty_uncommitted`) because the orchestrator edited the seat's workdir
  (`/Users/<user>/<workspace>`) *mid-flight* while standing up the opus-5 seat. The review content is
  unaffected — the subject was a pinned separate worktree — but the run must be repeated on a clean
  tree before this row is cited as anything but provisional. The gate behaved correctly; the
  orchestrator did not. Separately: **N=1, one run per arm.** Per the standing rule (do not average a
  seat into a single label prior — qwen38max returned P2 and P1 on the same brief hours apart), this
  is a smoke test, not calibration data, and must not move any seat's prior on its own.

---

## ⚠ STRUCTURAL LIMITATION OF THIS FILE (authoritative — added after DC-002)

DC-002's central finding is **about this corpus itself.** cold-opus proved that *every*
internal catch-record — seeded, harvested, or disagreement — is blind to whole-panel
misses by construction. A defect the *whole* panel missed never becomes a disagreement,
so it never enters this file. **This corpus is therefore "defects legible to at least one
seat," and the never-caught set — the exact thing that justifies a deep seat — is
structurally absent from it.**

Consequence: **this file cannot, by itself, answer the cold-Opus-voting question.** It can
characterize seat *calibration* and *agreement patterns* on legible defects (real, useful —
DC-001 is exactly that), but it cannot measure the illegible-catch value that is cold-Opus's
actual justification. Treat the running tallies as **calibration/agreement data, not
seat-keep/drop evidence.** The seat-keep/drop question needs an *external* oracle (escaped
defects / prod incidents observed independently of the panel) — see
`docs/escaped-defect-journal.md` and the eval-design v3 decision. This file is Instrument
2-lite: real, bounded, and explicitly not the decorrelation oracle it was first conceived as.

---

## Open methodology notes

1. **Distinguish lone-wrong from miscalibrated-real.** DC-001 shows the split: agy's
   *finding* was real, only its *severity* was wrong. A pure false-positive (flagged a
   non-defect) is more negative than a real-finding-wrong-severity. The instrument needs
   both buckets or it'll over-penalize thorough-but-soft seats.
2. **Adjudication authority must be recorded.** Ground-truth-resolved splits (config,
   prod data, test, later confirmed catch) are strong rows; orchestrator-opinion-resolved
   splits are weak and must be flagged, or the corpus inherits the orchestrator's blind
   spots (the same correlation problem as a same-family matcher).
3. **This corpus does NOT answer the cold-Opus-voting question** (⚠) — neither does the
   seeded suite. It characterizes calibration/agreement on *legible* defects. The voting
   question needs the external escaped-defect oracle. Keep this corpus and the seeded suite
   barred from each other, and keep **both** barred from seat-drop verdicts.
4. **Selection bias to watch:** splits only get logged when someone notices and
   adjudicates them. Silent splits (seats disagreed, nobody resolved it) are invisible
   here — the same legibility problem one layer up. Note it; don't pretend the corpus is
   complete.
