# Design — diagnose live-panel dispatch: orchestrator-forwarded (corrects #7's self-dispatch deviation) [#13]

**Status:** SPEC (spec-panel folded; ready for the plan). Re-architects `run_panel`'s dispatch to match spec
§0 ("skill-authors-and-seals → orchestrator FORWARDS opaque sealed envelopes"). Implements task #13.

**Design-panel verdict (2026-06-19):** cold-Opus + GLM + agy, all **DESIGN-HOLES**, converged. Core
restructure (orchestrator-forwarded, skill never self-dispatches, seal+recompute preserved) judged sound.
Folded: answered-model recorded-not-checked → §4a; attested-vs-enforced over-claim → §2/§6; certifier starve
crash → §3; "goes live" mocked → §5. Roster decided with user: `open=minimax-m3`, `scribe=claude-haiku`.

**Spec-panel verdict (2026-06-19):** cold-Opus (stake-free) + agy + M3, all **SPEC-HOLES**, converged. Three
P0s folded: **(P0-1)** the scribe is load-bearing — its submission folds into the certifier+collation
post-briefs (`briefs.py:50-64`) and the certifier can follow `bus_reply_ref` to read its text, so the
attested channel can influence the verdict content (M3: holds at the *gate* level, leaks at the *certifier*
level). Fix = §4b: **exclude the scribe from the verdict basis BY CONSTRUCTION** (out of post-briefs + out of
the certifier-candidate list → no `bus_reply_ref` path); keep it as a recorded audit/cross-check artifact.
**(P0-2)** the spec built green and errored live — §5 argv omitted the positional `<task>`; §2 prose said
`--engine agy` but the real engine is `agy-print`; only the M3 seat was pinned. Fix = pin all three seats'
`--check`-verified `{engine, target_id, role}` + the positional task. **(P0-3)** §4a was unbuildable — the
bus reply carries no answering-model field (the recorded value is the engine token `pi-sdk`, not the roster
id), and `fake_dispatcher` records `answered-codex` yet PASSES clean today. Fix = source the answering model
from the seat's **bus registration (`AGENT_MODEL`)**, not the orchestrator's claim; the §4a deny-proof must
FAIL against today's `fake_dispatcher`. Plus a found-during-fold over-claim: read-only is infra-enforced only
on M3 (codex/agy are mutation-capable general seats) — §6 corrected; soundness rests on the immutable
git-blob recompute, not the ceiling.

## 0. Why (the defect this corrects)
#7 merged a `run_panel` that **python-self-dispatched** each sealed brief via `subprocess
scripts/agent-dispatch --model … --ceiling … --work-dir …`. But agent-dispatch has **none of those flags**
(real interface: `--engine`/`--target-id`/`--role`), and **model is a seat-config property** (the seat's
launch-time `AGENT_MODEL`), not a per-dispatch flag. So every real dispatch errored → `bridge-unavailable`
→ fail-loud: **the live panel never dispatched.** It shipped green because *every* test monkeypatches
`run_panel` (`fake_run_panel`) — the 4th `fixture-masks-reality` of the session (the test mocked the very
function that was broken). The root error: self-dispatch was a **deviation from §0**, which says the
*orchestrator* forwards the sealed envelopes; the skill authors+seals, it does not pick the channel.

## 1. The corrected model — orchestrator forwards, skill never self-dispatches
- **Skill (Python):** authors + seals each brief (unchanged, contamination-proof); **validates** submissions;
  runs the recompute gate. It does NOT shell out to dispatch anything.
- **`run_panel(sealed_briefs, dispatch, work_dir)`** takes an **injected dispatcher**
  `dispatch: (SealedBrief) -> {"model": <model-that-answered>, "reply": str}` (raises/None on failure). It
  records a submission `{role, seat, model, seal, bus_reply_ref, bus_reply_sha256}` per brief; a failed seat
  → `incomplete-panel`; an unavailable channel → `bridge-unavailable`. The skill never picks the channel —
  the dispatcher *is* the orchestrator's forward-opaque executor.
- **The orchestrator (Claude Code) forwards** per SKILL.md: it constructs the dispatcher that routes each
  sealed brief to the channel its roster role declares.

## 2. Dispatch channels — all VOTING seats bridge-enforced; only the descriptive scribe is Agent-tool
The design-panel (cold-Opus + GLM + agy, all DESIGN-HOLES) found the original split put a *voting* role
(`open=claude-opus`) on the orchestrator-ATTESTED Agent-tool channel and over-claimed it "stronger than a
bridge turn." The enforcement gap is about **mechanical provability** (bridge leaves an independent bus
record + can enforce a read-only ceiling; Agent-tool leaves only the orchestrator's attestation), which
matters most for voting/decorrelation and least for the non-voting descriptive scribe. The cost-check
confirmed the agent-sdk engine authenticates every Claude model via a **metered** key + vendor base_url and
`isolated_env` wipes inherited `ANTHROPIC_*` creds (`agent_sdk_models.py:74-85`) — there is NO
subscription-Claude bridge path; the only free Claude path is the in-process Agent tool. Resolution:

- **All three VOTING seats → bridge (bus-recorded + vendor-decorrelated). Exact `--check`-verified argv
  (each ends with the positional `<task>` — P0-2):**
  - `blind=codex` (GPT) — `agent-dispatch --workspace dev --engine codex --target-id codex-bridge-dev --role reviewer <task>`
  - `alternative=agy` (Gemini) — `agent-dispatch --workspace dev --engine agy-print --target-id agy-bridge-dev --role reviewer <task>`
    (the engine token is **`agy-print`**, NOT `agy` — a literal `agy` errors at dispatch; verified against
    the agent-dispatch engine list).
  - `open=minimax-m3` (MiniMax) — the **already-running launchd judgment-oracle seat**:
    `agent-dispatch --workspace dev --engine pi-sdk --target-id pi-sdk-bridge-dev-minimax-m3 --role judgment-oracle <task>`
    (model `minimax/MiniMax-M3` is seat-config; NO `--model`). Bus: `agent-redis-bridge-dev.env` (db=12);
    trusted senders include `claude-bridge-dev` (the required `--from`/sender identity);
    `BRIDGE_MAX_PARALLEL=1` (serial — distinct seats run in parallel, each seat is one-at-a-time). Zero
    net-new integration: reuses the tested M3 oracle seat. **Read-only is infra-enforced here**
    (`ARB_REQUIRE_READONLY_TOOLS=read,grep,find,ls` + `roles/judgment-oracle.md`) — see §6 for the
    codex/agy asymmetry (their read-only ceiling is attested, not enforced; not load-bearing).
  - NONE of `--model`/`--ceiling`/`--work-dir` (the #7 flags) appears — model is seat-config.
- **Scribe (descriptive-only) → in-process Agent-tool cold-subagent:** `scribe=claude-haiku` → the
  orchestrator spawns a **fresh cold-context Agent subagent** (the cold-Opus machinery already in use —
  near-zero new integration, free on subscription), given the **sealed brief verbatim**. "Cold" = zero
  orchestrator history → §1.3 "clean isolated context" *by construction*. The skill's Python cannot call the
  Agent tool, which is *why* this is orchestrator-forwarded. Its isolation holds by construction; what it
  lacks vs a bridge seat is an **independent record** of that isolation — see §6 (honest-limit-named, NOT
  claimed equal to bridge). The scribe is the only attested channel, and it does not vote.
- This mix is coherent: "forward the opaque sealed envelope by whatever channel reaches that model," with
  every decorrelation-bearing voting seat on the infra-enforced channel.

## 3. Certifier model — DYNAMIC ≠ predicate-author (the §4 fix #13 also carries)
`author_post_briefs(constants, sealed_submissions, predicates)` selects the certifier brief's model
deterministically as a roster/available model whose model-id **≠ the predicate's `author_model`** (never
reciprocal). NOT the hardcoded `scribe.model` (which equalled `blind`=codex, violating §4). If `blind`
authored, the certifier ∉ {codex}.

**Selection robustness (design-panel P1 fix).** "Deterministic" must not mean "first in JSON list order" —
selection is by a **stable total order** (sorted model-id, then declared role priority) so a roster
re-ordering cannot silently change the certifier. **Starve case** (no roster model ≠ author available) is a
**fail-loud BLOCK** (`certifier-starved` / `insufficient-decorrelated-roster`) raised *inside* the panel
try/except and surfaced as a gate-block reason — NOT an uncaught `ValueError` that crashes `run_diagnose`
(the original `diagnose.py:95` defect). Fail-closed: cannot select a decorrelated certifier ⇒ refuse, never
silently reuse the author.

**Certifier candidates EXCLUDE the scribe (spec-panel P0-1 / §4b).** `_certifier_model` currently appends
`constants["scribe"]["model"]` to the candidate list (`briefs.py:103`). Remove it: the certifier is selected
from the **voting roster only** (codex/agy/M3). The scribe model must never be a certifier — it rides the
attested channel and is excluded from the verdict basis entirely (§4b).

## 4. Roster (`panel_constants.json`) — real reachable, decorrelated, with channel + verified ids
Each role declares its full dispatch identity (P0-2: all three seats pinned, not just M3). Values are
`--check`-verified against the live dev bus. The §4a anchor is `{engine, target_id}` (seat identity — the
registry publishes `tool`=engine, not model; model is fixed seat-config, so seat identity is the model
proxy). `vendor` is documentary (which decorrelated family the seat-config yields):
- `blind = {channel: bridge, engine: codex, target_id: codex-bridge-dev, role: reviewer, vendor: gpt}`
- `alternative = {channel: bridge, engine: agy-print, target_id: agy-bridge-dev, role: reviewer, vendor: gemini}`
- `open = {channel: bridge, engine: pi-sdk, target_id: pi-sdk-bridge-dev-minimax-m3, role: judgment-oracle, vendor: minimax}`
- `scribe = {channel: agent-tool, model: claude-haiku}` (descriptive-only, **non-voting, excluded from the verdict basis — §4b**)
- `sender = claude-bridge-dev` (a trusted sender of all three seats)

Distinct vendors/seats across the three voting seats (GPT / Gemini / MiniMax — decorrelated); certifier
dynamic ≠ author, scribe excluded (§3). The `cold-opus`-as-bridge-engine error is removed; the one Agent-tool
role is the non-voting scribe.

### 4a. The answered-model is LOAD-BEARING — a neutral-validator predicate (design-panel P0)
The original §5 claimed "a silent model-swap is detectable because the submission carries the answering
model." The panel proved that **false**: `run_panel` *records* `submission["model"]` but **no validator ever
compares it to the roster-declared model**, and `_canonical_submissions` launders the answered model into the
expected post-brief, making the record self-ratifying. An orchestrator that collapses three roles onto one
vendor produces a fully-consistent record and the gate PASSES — defeating decorrelation. **Recorded ≠
checked** — the next `fixture-masks-reality`. Fix: a neutral-validator predicate (gate-side, recompute-time)
- each role's `submission["model"]` **equals its roster-declared model** (after a fixed alias-normalization
  map, e.g. `minimax/MiniMax-M3` ≡ the roster id), else BLOCK `model-mismatch`;
- the three voting-seat models are **pairwise distinct** (decorrelation actually held), else BLOCK
  `decorrelation-collapsed`.
This predicate is recomputed from committed constants, not from caller-supplied state, so it cannot be
self-ratified. It is the load-bearing half of the "no silent model-swap" claim.

**Ground-truth source (spec-panel P0-3 — and a SECOND correction found verifying the fix).** The first fold
said "read the answering model from the seat's bus registration." Verified against the live bus: **FALSE** —
the registry hash (`agent_scratch:registry:<target_id>`) publishes `{tool (engine), project, path, pid,
registered_at}` but **no `AGENT_MODEL`**, and the persistent task record (`:status` hash) carries no
originating-seat field either. The only record of which seat answered is the **transient reply envelope**
(`{"from": "<seat>", "kind": "reply", ...}`) in the orchestrator's inbox, which is consumed on read. So:

- **What is checkable now (anchor = seat identity, model follows from seat-config).** Model is fixed
  seat-config (`AGENT_MODEL` set at the seat's launch), so the *seat* is the model proxy: dispatching to
  `pi-sdk-bridge-dev-minimax-m3` necessarily runs MiniMax-M3. The enforcement is each role's
  recorded originating seat (the reply envelope's `from`, persisted into the submission at dispatch) equals
  the roster `target_id`; with distinct roster `target_id`s, decorrelation follows from that per-role check.
- **Where it lives (review correction — NO separate predicate).** The seat-identity check is already
  enforced END-TO-END by the gate's existing `_panel_blocks` recompute (`neutral_validators.py:~120` —
  `expected_seat = roster[role]["target_id"]; submission.seat != expected_seat → unverified-without-panel`).
  The first build added a *separate* `_decorrelation_blocks` predicate that **duplicated** this (its wiring
  was removable with the suite green) and emitted a `model-mismatch` reason while never comparing a model.
  The review (cold-Opus + agy + M3 + codex-contributor) removed it. The model is **deliberately NOT checked
  from the run record**: model is a property of the *registered seat*, not a trustworthy record field —
  record-checking it would misstate the trust boundary (forgeable theater). Seat-identity (via the registered
  `target_id`, with the adversarial limit below) is the honest anchor.
- **Load-bearing proof:** an END-TO-END deny-proof asserts a complete verified record with one submission's
  `seat` ≠ its roster `target_id` blocks `unverified-without-panel` — it goes red if the `_panel_blocks`
  seat-check is weakened (the proof the first build lacked).
- **What is NOT enforced in #13 (honest limit — §6).** Because the bus does not persist `from→task`
  immutably, the persisted originating-seat is *transcribed by the dispatcher* (orchestrator-side). The check
  is therefore **ENFORCED against accident / a buggy dispatcher / misconfigured roster / role-collapse**, but
  only **ATTESTED against a malicious orchestrator** that fabricates `from`. Full adversarial enforcement
  needs a bridge-level immutable `from→task` ledger the gate can independently re-read — **deferred to #14**
  (sibling of #7's submission-authenticity → #6). Decision with user (2026-06-19): build the real check now,
  name the limit, backlog #14 — do not expand #13 into a protocol change.
- **Deny-proof bar (unchanged):** the §4a test must FAIL against today's `fake_dispatcher` (which records
  `answered-codex` / the engine token for every role) and pass only when the recorded originating seat is
  checked against the roster. If it passes against the current fake, the check is wrong.

### 4b. The scribe is EXCLUDED from the verdict basis — by construction (spec-panel P0-1)
The scribe rides the attested Agent-tool channel; the panel proved its submission re-enters the verdict via
the certifier+collation post-briefs (`briefs.py:50-64`) and the certifier can follow `bus_reply_ref` to read
its text. The attested channel must NOT be able to influence the verdict. Fix — sever **text-reachability**,
not merely gate-inclusion (M3's altitude point: the gate's meta-check was already safe; the leak is the
certifier *model* reading the scribe's reply):
- `author_post_briefs` filters the scribe out of `sealed_submissions` before authoring the certifier and
  collation post-briefs — so neither post-brief contains the scribe's `bus_reply_ref`/`seal`/`model`. There
  is **no path** from the certifier to the scribe's reply text.
- `_certifier_model` candidate list excludes the scribe model (§3).
- The scribe submission is **still recorded** in the run record as an independent **audit/cross-check
  artifact** ("did a clean describer see the same observables the skill extracted?") — its value is an
  out-of-band check on the skill's own extraction, NOT a verdict input. It carries no verdict-influence risk
  precisely because it is unreachable from the verdict basis.
- This is *by construction* (the dangerous path is not defined), not by intention (the certifier is not
  merely told not to read it). Verified by a test asserting the certifier/collation post-briefs contain no
  scribe submission and no scribe `bus_reply_ref`.

## 5. The real-dispatch test — the deny-proof for the whole arc
The 4th fixture-masks-reality was "the dispatch never ran and the test mocked the broken function." So the
closing test must, **by construction, NOT be a blanket `run_panel` mock**:
- **Argv validity (P0-2):** `bridge_dispatch` constructs an agent-dispatch argv using ONLY accepted flags
  (`--workspace`/`--engine`/`--target-id`/`--role`) **plus the required positional `<task>`**, and NONE of
  `--model`/`--ceiling`/`--work-dir`. Asserted against agent-dispatch's actual parser (`scripts/agent-dispatch`
  — positional `<task>` is required; `--engine` value must be in the engine list, so `agy-print` not `agy`).
  A test that FAILS against #7's code AND against an argv missing the positional task.
- **Answered-model is CHECKED against seat-published ground truth (§4a):** the deny-proof drives a dispatcher
  whose seat registry reports a model ≠ the roster's `expected_model` (the silent-swap / role-collapse
  adversary) and asserts the gate **BLOCKS** (`model-mismatch` / `decorrelation-collapsed`). Critically, the
  test must **FAIL against today's `fake_dispatcher`** (which records `answered-codex` and passes clean) —
  i.e. the check reads the registered `AGENT_MODEL`, not the recorded engine-token claim. A check that passed
  against the current fake is the wrong check.
- **Scribe-unreachable-from-verdict (§4b):** a test asserts the certifier and collation post-briefs contain
  no scribe submission and no scribe `bus_reply_ref` — the by-construction severing, not an intention.
- **"Goes live" is exercised by a non-blanket-mock contract test (design-panel P1).** The argv test alone
  pins only the flag bug; the live path stays fake-dispatcher everywhere = the same masking shape. So one
  test injects a dispatcher that is a **thin real-interface shim** (constructs the actual argv, asserts it
  against the real agent-dispatch parser, returns a canned reply) — proving `run_panel` drives the real
  contract, not that "some callable returned a dict." The orchestrator-forward boundary (Agent-tool / live
  bridge round-trip) remains out of unit scope and is named as such (operationally verified at build's
  live-run stage, not unit-mocked-green).
- **Must fail today, pass after** (deny-proof): the real-interface argv test fails on #7's `--model` code;
  the model-mismatch test fails on any build lacking the §4a predicate.

## 6. Contamination + threat model — honest-limit-named (NOT "identical to bridge")
The brief is skill-authored + sealed BEFORE the orchestrator forwards it (orchestrator can't shape it); the
gate recomputes it (unchanged). No new contamination surface in the *brief*; the dispatcher-injection touches
only the *channel*. But the two channels are **NOT equally provable**, and §6 must say so (the
`honest-limit-named > fake-guarantee-shipped` bar the design-panel invoked against the original over-claim):

- **Bridge voting seats (codex / agy / M3): the ENFORCED property is the independent bus record** — the bus
  logs the actual dispatch + reply (a forward the orchestrator did not author) and the seat publishes its own
  `AGENT_MODEL` (the §4a anchor). This holds for all three. **Read-only ceiling asymmetry (found during the
  fold, honestly named):** read-only is infra-enforced ONLY on M3 (`ARB_REQUIRE_READONLY_TOOLS` +
  `judgment-oracle`); `codex-bridge-dev` and `agy-bridge-dev` are mutation-capable general seats sharing the
  live checkout — their read-only ceiling is **attested** (via `--role`/brief framing), not enforced. This is
  NOT load-bearing for verdict soundness: the gate recomputes briefs against **immutable git blobs**
  (`git show {repo_sha}:path`, the #7 P0-1 fix), so a seat mutating the working tree cannot corrupt the
  verdict basis. The ceiling is defense-in-depth (review-hygiene/tidiness); see §7 for the worktree-isolation
  hardening that would make it enforced if desired.
- **Agent-tool scribe (Haiku): orchestrator-ATTESTED — and EXCLUDED from the verdict basis (§4b).** Its clean
  context holds *by construction* (fresh cold subagent, zero orchestrator history), but there is no
  independent bus artifact proving the forward was verbatim. The original "acceptable because it doesn't vote"
  was too narrow — the panel showed its text could reach the certifier. The actual guarantee is stronger and
  by-construction: **certifier-cannot-read-scribe** (§4b severs the `bus_reply_ref` path), so the attested
  channel is *unreachable from the verdict*. The scribe's residual role is an audit cross-check only. The
  attested forward-fidelity limit is in the same class as the §7b forged-submission limit (mechanical
  authentication deferred to #6), but it no longer touches the verdict.
- **Orchestrator-forwarded is attested, not enforced, in general** (design-panel unifying finding): the
  injected dispatcher puts the orchestrator in control of what the model actually receives; the gate checks
  orchestrator-*produced* records. The contamination-proof property therefore holds *for the brief* (sealed +
  recomputed) but the *forward fidelity* is enforced only on the bridge channel and attested on Agent-tool.
  §6 names this rather than claiming construction-level proof for both.
- **Which-model-answered is accident-enforced, adversary-attested (§4a limit).** The bus does not persist
  `from→task` immutably, so the originating seat the gate reads is transcribed by the orchestrator. #13's
  model-decorrelation check catches accidental drift / buggy dispatcher / role-collapse (and fails today's
  `fake_dispatcher`), but a malicious orchestrator can fabricate `from`. Adversarial enforcement is deferred
  to **#14** (a bridge-level immutable `from→task` ledger), the same honest-limit class as the §7b
  forged-submission limit (→ #6). Named, not hidden.

## 7. Pinned for the plan (spec-panel folded; no load-bearing opens)
- Resolved: the dispatch architecture (orchestrator-forwarded, injected dispatcher); the channel split (three
  VOTING seats on bridge — codex/agy/M3, vendor-decorrelated, bus-recorded; non-voting scribe on Agent-tool,
  excluded from the verdict basis §4b); certifier-dynamic + starve fail-loud + scribe-excluded candidates
  (§3); the seat-registration-anchored answered-model predicate (§4a); the deny-proofs incl. positional-task
  argv, model-mismatch-vs-seat-registration, scribe-unreachable, and the non-blanket goes-live contract test
  (§5); the read-only-ceiling honest asymmetry (§6).
- **SKILL.md dispatcher contract (pinned, not open):** the orchestrator constructs the dispatcher routing
  each sealed brief per `panel_constants.json`: `blind → --engine codex --target-id codex-bridge-dev`;
  `alternative → --engine agy-print --target-id agy-bridge-dev`; `open → --engine pi-sdk --target-id
  pi-sdk-bridge-dev-minimax-m3`; each `--workspace dev --role <role> <task>`, sender
  `claude-bridge-dev`; `scribe → in-process Agent-tool cold subagent`. The read-only ceiling is enforced on
  M3, attested on codex/agy (§6).
- **Worktree-isolation hardening (deferred unless elected):** `agent-dispatch --worktree` would run codex/agy
  in throwaway worktrees, making their read-only-ness moot (a mutation can't touch the live checkout). Not
  required for soundness (§6 git-blob anchor); a belt-and-suspenders option for the build if elected.
- **Pre-build operational note (not a design open):** all three voting seats verified live on the dev bus
  (`agent-redis-bridge-dev.env`, db=12) at design time via `agent-dispatch --check` — `codex-bridge-dev`,
  `agy-bridge-dev`, and `pi-sdk-bridge-dev-minimax-m3` all alive with fresh heartbeats. Registered
  target-ids verified against the live registry (the launchd label `pi-sdk-dev-minimax-m3` is NOT the
  registered id — the bus id is `pi-sdk-bridge-dev-minimax-m3`; using `--check` to read the real id
  is mandatory before pinning, exactly the guard #7 lacked). This is a runtime concern for the live-run
  stage, not a design decision.
