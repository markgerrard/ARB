# Implementor routing — which model gets which task

This is the **routing rulebook** for picking an implementor model. Keep
decisions out of vibes by checking this doc *before* dispatching. If a task
falls between rungs, escalate up (the cost of a wrong edit is always greater
than the cost of a stronger model).

The bridge enforces nothing here — routing is the **caller's** job. This doc
codifies the policy so every future caller picks the same way.

## Implementor ladder

Three rungs, narrowest to broadest. Default down; escalate up by trigger.

### 1. `qwen3-coder-next` — cheap deterministic worker (default for bounded work)

**Seat:** `pi-sdk-<project>-dev-qcn-w` (default), with `pi-<project>-dev-qcn-worker`
(pi-rpc engine) as fallback. Both seats run qwen3-coder-next via OpenRouter
and produced 10/10 on the same dev.project-f gate; pi-sdk is the documented
default because it harvests canonical finalText from `agent_end.messages`,
distinguishes abort/error via `AssistantMessage.stopReason` (no exception-string
matching), uses a single tool-event shape (no camelCase/snake_case dedup), and
drops ~280 LOC of accumulated quirk-handling. See
[`qwen-worker-seats.md`](qwen-worker-seats.md) "pi-sdk vs pi-rpc seats".
**Strengths:** exact-spec adherence, no silent normalisation, no unsolicited
helper extraction, fast, very cheap (~$0.11/$0.80 per Mtok).
**Sourced from:** 5-spec deterministic probe (88/88) + 10-task real-Laravel
gate against dev.project-f (pi-rpc 10/10 2026-06-06; pi-sdk 10/10 2026-06-07
on the same task set, 2 byte-identical diffs / 8 functionally-equivalent /
1 passing-but-clumsy that tripped the Composer-2.5 escalation rule).

Use it when **all** of these apply:

- The desired change shape is **explicit**: signature, body, placement, file path.
- The task is **bounded** to a single file or a tightly-scoped multi-file set.
- The brief can list "do NOT X" prohibitions for any rule the model might
  otherwise "improve" past.
- No design discovery is required — the brief tells the model exactly what
  to write, not what to figure out.

Examples that fit: add a query scope to a model; add a new migration with a
named column type; add a missing assertion to a test; extract a constant; add
a typed property; create a new test class with a single explicit assertion;
create a new migration that matches a defined schema.

### 2. `Composer 2.5` — broader implementation fallback

**Seat:** existing `cursor-acp-*` bridge instances (Cursor's ACP harness).
**Strengths:** broader implementation reasoning when the spec leaves room,
fast repo navigation, terminal/tool behaviour built for agentic coding,
larger-scope execution.

**Position note (2026-06-06 hardening):** Composer 2.5 is **no longer the
default for simple bounded edits**. The brain-repo bake-offs (see "Operational
evidence" below) showed qwen3-coder-next is consistently 1.3–1.9× faster on
bounded mechanical work *and* produces byte-identical diffs on most of them.
Composer's wins come from broader reasoning when it has room to make
implementation choices, not from raw speed on tight specs.

Use it when **any** of these are true:

- Larger implementation span: a plan that spans several files / modules.
- Apply a stated plan across multiple files — the shape is decided, but
  the count of edits is non-trivial AND qcn-w hasn't already done it cleanly.
- The task benefits from fast repo navigation or terminal use during the
  edit (e.g., tracing callers, running a sanity check before committing).
- qwen3-coder-next produced a passing-but-clumsy result that needs cleaner
  implementation style (e.g. the redundant lazy import on brain `q5 deep
  /health`).
- qwen3-coder-next failed review once and the failure mode wasn't a brief gap.
- Cursor/ACP-native tasks where its harness behaviour is an advantage.

Examples that fit: extend an existing feature across model + service +
controller + test, given a plan; perform a rename across N callers with
behaviour preservation; implement a multi-file pattern (e.g., add a new
column with its model cast + factory + migration + tests).

### 3. `Codex GPT-5.5` — frontier judgement implementor (production-risk work)

**Seat:** existing `codex-*` bridge instances.
**Strengths:** judgement, complex reasoning, computer-use, knowledge work,
research workflows — positioned by OpenAI as the recommended frontier model
for complex coding in the Codex harness.

Use it when **any** of these are true:

- Ambiguous refactor where the design is under-specified.
- Architectural judgement required (interface design, layering decisions).
- Touches **auth, billing, permissions, production data, shared-DB migrations**,
  or destructive operations.
- Hard debugging where the failure mode is non-obvious.
- Failure recovery: qwen and Composer disagreed, or both produced dirty/
  partial output.
- The cost of a wrong edit is much higher than the token cost.

This is the "don't be an idiot, this actually matters" seat.

### Off-ladder fallback: `qwen3.7-max` — Codex-unavailable substitute only

**Seat:** `pi-project-b-dev-qwen37max` (read-only) /
`pi-project-b-dev-qwen37max` worker variant if/when stood up.
**Position:** **not on the routing ladder**. The 88/88 synthetic-probe result
and the qwen-family heritage make it a reasonable "heavier qwen with 1M
context", but on the merits Composer 2.5 and Codex GPT-5.5 sit above it for
the rungs they cover.

Use **only** when:

- Codex GPT bridge is **unavailable** (down, queue-saturated, rate-limited)
  AND the task is one that would otherwise route to Codex.
- AND the task's risk profile permits a model substitution (not a stop-the-
  world auth/billing/migration that should *wait* for Codex to come back).

Default behaviour when Codex is unavailable is to **delay or block the
dispatch**, not silently substitute. The fallback is a deliberate choice the
orchestrator surfaces to the user, not an automatic failover.

Do **not** use qwen3.7-max as the next escalation step out of
qwen3-coder-next. Escalate to Composer 2.5, then Codex GPT-5.5. qwen3.7-max is
the bench player.

## The routing rule (hardened 2026-06-06)

**Start with `qwen3-coder-next` unless ANY of these are true:**

1. The task requires architectural interpretation.
2. The task spans multiple subsystems.
3. The task touches **auth, money, permissions, production data, destructive
   operations, or shared-DB migrations**.
4. The brief is ambiguous or incomplete.
5. The desired implementation style matters more than raw correctness.
6. qcn produced a passing-but-clumsy result on a prior attempt at this task.
7. qcn failed review once on this task.

Triggers 1–5 are *up-front* routing inputs (you know them before you dispatch).
Triggers 6–7 are *retry-after-feedback* inputs (you know them only after a
first attempt). Re-route accordingly:

- Trigger 1 → Codex GPT-5.5.
- Trigger 2 → Composer 2.5 (if mostly mechanical multi-file) or Codex (if also
  architectural).
- Trigger 3 → Codex GPT-5.5 + tri-model review.
- Trigger 4 → Codex GPT-5.5 (or pause and refine the brief first).
- Trigger 5 → Composer 2.5.
- Trigger 6 → Composer 2.5 to redo cleanly.
- Trigger 7 → next rung up, depending on failure shape.

If NONE of the triggers fire: stay on qcn-w. That is the cheap default the
operational evidence below justifies.

### Operational evidence behind this rule

5 head-to-head bake-offs against the brain repo on 2026-06-06 (single dispatch
per task per implementor, same brief, isolated worktrees):

| Task | qcn (s) | Composer (s) | Ratio | Outcome |
|---|---|---|---|---|
| `q3` tag length validator (1-line) | **7.18** | 13.82 | 1.93× | byte-identical |
| `q5` deep `/health` (2-file feature) | 9.82 | 17.70 | 1.80× | Composer cleaner — qcn added redundant lazy import |
| `q2` idempotency key (1-file feature) | **8.29** | 12.77 | 1.54× | functionally identical |
| `q1` systemd timer (2 new files) | **11.67** | 15.37 | 1.32× | byte-identical |
| `q8` pytest scaffold (4 new files) | 16.58 | 16.58 | **1.00×** | byte-identical; both passed 7/7 |
| **Totals** | **53.54s** | **76.24s** | **~1.42×** overall qcn-faster | |
| **Excluding the tie** | **36.96s** | **59.66s** | **~1.61×** | |

13 commits shipped to `origin/main` of the brain repo during the autonomous
session driving the bake-offs. All cherry-picked, pushed, deployed, and
verified live (pytest 7/7, `/health?deep=1` green, embedding latency logged
to journal). One qcn failure (q11 v1) was a self-recoverable spec-drift on
import handling, resolved by a tighter before/after brief on v2.

This is real operational evidence, not a synthetic benchmark.

## Routing flow

```
Apply the routing rule above. If qcn-w is the answer, dispatch.
If not, the trigger tells you which rung.
Pictorially:

Is the change shape explicit AND bounded?
├── yes → qwen3-coder-next
│         (and if it fails review or produces dirty output → escalate)
└── no
    │
    Is it a planned multi-file execution OR fast repo-nav helpful?
    ├── yes → Composer 2.5
    │         (and if it fails → escalate to Codex)
    └── no → Codex GPT-5.5
```

**Don't** treat the ladder as "try qwen, then composer, then codex" for every
failure. That wastes time. **Route by task shape up front**; escalate only when
a deterministic guard fails (tests, expected artifacts) or a reviewer rejects.

## Hard escalation triggers (out of `qwen3-coder-next`)

Use a higher rung if **any** trigger fires. These are the rules; don't argue
them ad-hoc per task.

| Trigger | Escalate to |
|---|---|
| Brief + worktree context approaches 262K (coder-next limit) | Composer 2.5 (262K → 1M tier) — only fall back to `qwen3.7-max` if Composer is unavailable |
| Crosses multiple files in a non-mechanical way (refactor, not edit) | Composer 2.5 |
| Model must infer architecture rather than execute a stated shape | Codex GPT-5.5 |
| Auth / billing / permissions / migrations on shared DB / destructive ops | Codex GPT-5.5 + tri-model review |
| Previous worker fails review or produces dirty/partial output | next rung up on the ladder |
| Reviewer flags "technically works but not the requested shape" | next rung up on the ladder |
| Brief required design discovery (e.g., "figure out the right interface") | Codex GPT-5.5 |
| Hard debugging — failure mode is non-obvious | Codex GPT-5.5 |

**qwen3.7-max is NEVER on this table as the primary destination.** It's only
the off-ladder fallback when Codex GPT bridge is unavailable for a task that
would otherwise route to Codex (see rung 3 above). If Codex is healthy, skip
qwen3.7-max entirely.

## How to actually call each model — bridge seats, not raw API

A meta-rule that cuts across both ladders: **dispatch through the bridge seat
that owns each model on this host, not by raw HTTP to the upstream provider.**

- "Codex GPT-5.5" means *GPT-5.5 inside the codex CLI harness*, reached via
  `--engine codex --target-id codex-<project>-<workspace>` on this host. It
  does NOT mean `openai/gpt-5.5` via OpenRouter — that's the same model
  without the harness, billed against the wrong account, and bypasses every
  envelope / policy / audit guarantee.
- "agy-print" / "Gemini-family" reviewer means the agy bridge seat
  (`--engine agy-print --target-id agy-<project>-<workspace>`), not a raw
  Gemini API call. (This is distinct from the `gemini-acp` engine, which
  drove the `gemini` CLI directly — that engine is deprecated as of
  2026-07-03 and `agent-dispatch` rejects `--engine gemini-acp`. `agy-print`
  is unaffected and remains the live Gemini-family route.)
- "Composer 2.5" means the cursor-acp seat, not a Cursor API call.

The only legitimate raw-API uses are a capability probe before wiring a new
seat, billing introspection (e.g. `GET /api/v1/credits` to verify routing),
or a genuinely one-shot need where no seat exists. For repeated work — any
panel, review, dispatch, or analysis — stand up the seat.

Full table of which seat hosts which model + the rationale lives in the
bridge skill (`skills/using-agent-bridge/SKILL.md`, "Hard rule: bridge seats
are the default" section).

## Reviewer ladder (separate from implementor ladder)

Reviewers are **independent seats** — never the same instance that implemented
the work. They exist to catch what the implementer's framing missed.

### 1. No heavy review

For low-risk qwen3-coder-next tasks where the **deterministic guards already
prove correctness**: tests stayed green, expected artifact present, diff
matches the brief. The deterministic guards are the review.

Tasks fitting rung 1 of the implementor ladder with all guards passing land
here.

### 2. Cold-Opus or GPT-5.5 review

For production-impacting work. Independent subagent / instance reads the
brief + diff + test output and verdicts pass/fail with a written reason. Used
on anything that goes to a shared branch or affects shared infrastructure.

The 10-task gate's review pattern (`/tmp/qwen-gate/review-bundle.md` style:
brief + response + diff per task → cold-Opus subagent returns per-task
verdicts) is the canonical shape.

### 3. Tri-model review

For migrations, auth, billing, permissions, anything with data-loss risk, or
anything that touches production behaviour. Use the canonical certify quorum
from [`agent-role-routing.md`](agent-role-routing.md): codex-contributor +
cold-Opus + agy-print + pi-GLM, with author-non-quorum and at most one Opus
seat certifying. Composer / `cursor-acp` is experimental and non-certifying
(verify its claims; it has fabricated a citation), and warm-Opus + cold-Opus
would put two Opus seats in one certifying quorum. Reviewers verdict
separately and the orchestrator **surfaces the divergence** to the user rather
than vote-counting.

See [`docs/multi-model-consensus.md`](multi-model-consensus.md) for the review
hygiene rules (no reviewer reads another's draft during the independent phase).

## What this is *not*

- This is not a rule that says "always use the cheapest model that fits".
  Cost is a tiebreaker, not the primary factor. Spec adherence and the
  failure-mode of the model dominate.
- This is not a license to skip reviews when qwen passes deterministic
  guards on a production-affecting change. Rung 1 of the reviewer ladder
  applies only to low-risk work.
- This is not a static list. As models change (Composer 3, the next Qwen, a
  Codex revision), re-run the gate, update the rungs.

## Re-gating cadence

Re-run the 10-task gate against a fresh codebase target whenever any of these
happen:

- A model on the ladder gets a new version (Codex 5.6, Composer 3, Qwen
  next-coder).
- Failure rate creeps on a deployed seat (track via dispatch ok-rate per
  seat over time).
- We add a 4th implementor.
- It's been > 6 months since the last gate.

Gate criteria + procedure: see [`qwen-worker-seats.md`](qwen-worker-seats.md)
"Gate result" section for the canonical pass criteria. Use a different
codebase target each time (avoid measurement-time contamination).
