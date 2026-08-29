# Scope — Instrument 1 live-dispatch increment

> Goal: make `arb-eval run --scenario X --normalizer anthropic:MiniMax-M3` actually dispatch the panel to
> review a real subject diff and produce a floor report **end-to-end live** — replacing the `MockDispatcher`
> the whole pipeline was built+gated against. The off-quorum normalizer (P-1, M3) is done; this is the
> *seat-side* dispatch that feeds it. Three pieces + one forced decision.

## Piece A — real `BridgeDispatcher` (the load-bearing build)

Today `BridgeDispatcher.dispatch(task)` shells `agent-dispatch --engine <seat> <json-task>` — but the task
is `{"kind":"review","seat":...,"seed_id":...,"repeat":...}` with **no code, no diff, no instructions**, and
the invocation omits the bridge params. A seat receiving that has nothing to review. Build the real thing:

1. **Review-prompt construction from the subject.** The task must carry what the seat reviews: compute
   `git diff <base>..<head>` of the subject repo, and build an **evidence-first review brief** asking the
   seat to emit findings in a form the segmenter handles — each finding with `file:line`, a class hint, and
   a one-line statement. (Couples to `segment_reply`, already hardened for bundles/no-op; pick a finding
   format — one-per-line or a JSON list — and make the brief request it explicitly.)
2. **Correct bridge invocation.** `agent-dispatch --engine <engine> --target-id <seat-target-id>
   --timeout N "<brief>"` with `FROM_AGENT_ID`, `BRANCH`, `AGENT_ENV_FILE` env (per `using-agent-bridge`).
   Needs a **seat → (engine, target-id)** mapping: derive from the panel entry (`harness`→engine,
   `seat`→`<engine>-agentredisbridge-dev`) or add `target_id` to the panel schema. Decide one.
3. **Reply parsing.** `agent-dispatch` returns a JSON envelope (`{"result":..,"ok":..,"completion":..}`);
   extract `result` as the seat's review text (the current stub returns raw stdout). On `ok:false` / non-zero
   exit / timeout → record a **dispatch-failure event and route to detection-miss** (fail-loud, never silent).
4. **Background/parallelism.** Per-`(seat × seed × repeat)` dispatch is the unit; the live run is many
   dispatches. Reuse the bridge's parallelism (or bounded sequential for the first run); record each in NDJSON.

## Access patterns (MEASUREMENT CORRECTNESS — the rule that gates validity)

A reviewing seat's strength in ARB is that it **investigates** — harness + tools (Read/Grep/Bash) against a
**real checkout**, verifying against ground truth (DC-001: the catch came from checking `SESSION_DOMAIN` in
config, not reasoning from the diff). A seat handed **diff text** is a crippled version of itself; measuring
that and calling it the seat's floor capability is measuring the **wrong construct** (snippet-review ≠ the
tool-equipped review the floor is meant to predict), and it **understates every reviewing seat**. Therefore:

- **Reviewers (codex, agy, cold-opus) → harness + tools + real checkout.** The dispatcher must point the
  seat at a real fixture checkout in its working dir and instruct investigation (done: `_review_prompt` now
  references `git -C <repo> diff base..head` and says "investigate with your tools, verify against ground
  truth, do NOT reason from the diff alone"). The fixture MUST be a genuine repo the seats can Read/Grep/Bash.
- **Normalizer (M3) → bare API, no tools.** Correct *because* it doesn't review — it transforms a finding the
  seat already produced; tools would be contamination (it must not form its own opinion of the code). The two
  patterns are role-correct, not an inconsistency.

## Piece B — cold-opus: in-process subagent ONLY (rejected: bare-API on correctness grounds)

`cold-opus` is an in-process `Agent` subagent. The earlier (iii) "reach Claude via bare API as a floor seat"
is now **rejected — on correctness, not just CLI-convenience:** a harness-less API Claude can't reach the
repo, so it would be the *crippled* cold-opus measured as cold-opus's floor (the access-pattern rule above).
So cold-opus stays the subagent (which has its tools). **Consequence:** a floor run that *includes* cold-opus
must be **orchestrator-driven** (a session that can spawn subagents), not a standalone `arb-eval` CLI.

- **First run (decision i, unchanged):** bridge seats only — **codex + agy, each with a real fixture
  checkout** — + M3 normalizer. cold-opus deferred to an orchestrator-driven run.
- **Appraisal scope (note):** a faithful *reviewing* seat needs a harness+tools+checkout — i.e. a bridge
  engine, or Claude-via-subagent. A model reachable *only* by bare API can't be a faithful reviewer seat;
  bare-API is the normalizer's pattern, not a reviewer's. (So "AnthropicDispatcher as a reviewer path" is
  off the table; it was only ever right for the normalizer, which M3 already is.)

## Piece B (original) — the FORCED decision: cold-opus is not dispatchable by a standalone harness

The example panel includes `cold-opus`, an **in-process `Agent` subagent** — but `arb-eval` is a standalone
Python CLI; it **cannot spawn Claude Code Agent subagents** (that's the orchestrator's capability, not a
CLI's). So option "add a subagent path" is **infeasible** and rejected. The real choices:

- **(i) Drop cold-opus from *floor-measurement* panels.** The floor suite measures bridge-dispatchable
  seats (codex, agy, + other bridge engines); cold-opus stays a *gate-review* seat (Stage 5), not a
  floor-capability seat. Simplest; smallest first live run.
- **(iii) Generalize the AnthropicNormalizer pattern into an `AnthropicDispatcher`** — reach Claude (and any
  Anthropic-compatible model) **directly via the API as a floor seat**, exactly as the normalizer reaches
  M3. Makes the *whole* panel API-dispatchable (codex/agy via bridge, Claude/others via direct API),
  consistent and general — but more build. **(This is the principled end-state; the floor suite's cross-role
  appraisal wants all seats reachable.)**

**Recommendation:** **(i) for the first live run** (codex + agy, both already up), **(iii) as the fast-follow**
so cold-opus/Claude and any Anthropic-compatible model can be a floor seat via the same direct-API pattern
that resolved P-1. **This fork is yours** — it changes which seats the first live numbers cover.

## Piece C — minimal real fixture

A tiny repo with a `base..head` diff containing **one** known seeded defect (e.g. secrets-in-logs: a head
commit adding `log.info(request.headers["Authorization"])`). Scenario records `repo`/`base`/`head` + the
seed (`file:line:symbol:class`). One seed → **instance-level** result by construction (the P0-A runtime
check fires — the thing you flagged to watch on the first live run). Full ≥5-distinct-seeds/class corpus
stays **P-3**.

## Acceptance criteria (the first live run passes when)
- `arb-eval run --scenario mini --normalizer anthropic:MiniMax-M3` dispatches the live panel (codex[+agy])
  to review the real fixture diff, gets real findings, **M3 normalizes them at temp=0**, the matcher runs,
  and a floor report is produced — **labeled instance-level** (1 seed < I_min), `GOLD_UNADJUDICATED` flagged.
- NDJSON is authoritative (verdict derived from the events); the wall holds on all emitted artifacts.
- A dispatch failure (seat error/timeout) is recorded and routed, not silently dropped.

## Stays parked / out of scope
- **P-2** gold-set adjudication (blind disjoint raters) — floor verdicts remain `GOLD_UNADJUDICATED`.
- **P-3** full fixture corpus (≥5 distinct seeds/class) — this builds only the minimal one.
- **(iii)** the `AnthropicDispatcher` generalization — recommended fast-follow, not the first run (unless you pick it now).

## Risks
- **Cost/time:** real codex/agy dispatches + M3 calls per trial; bound repeats low for the first run.
- **Format coupling:** the seat finding-format must match what `segment_reply` + the M3 normalizer expect —
  the review brief must request it explicitly, or findings get mis-segmented (a real source of matcher noise).
- **Same treatment:** this is a code increment → built TDD where possible, **execution-primary paneled**
  (the dispatcher's real behavior is exactly the kind of thing that passes a read and fails a run), branch-only.
