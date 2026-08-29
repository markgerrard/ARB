# Skills — the orchestrator's entry point

The bridge is driven by a **warm orchestrator**: a Claude Code session that stands the seats up,
publishes briefs, dispatches, monitors, runs the review panel, verifies against git, and
integrates. These skills are what that session loads. Install the one below and a session picks
up the operational guide the moment a bridge-shaped task appears — no one has to remember to read
a document.

Skills here are the Claude Code kind: a directory with a `SKILL.md` whose frontmatter carries a
`name` and a `description`, and the description is the trigger. Some of these directories also
ship executable Python (`diagnose/`, `diagnose-steer/`, `bridge-protocol/gate/`,
`defect_hunts/`); those are importable packages under `skills.` as well as skills, and the test
suite drives them directly.

## Install

```bash
mkdir -p ~/.claude/skills
ln -s <bridge-clone>/skills/using-agent-bridge ~/.claude/skills/using-agent-bridge
```

Symlink rather than copy: `git pull` in the clone then keeps the skill content fresh on every
host. Copy only if you cannot symlink — that hard-fixes the skill at install time.

```bash
cp -r <bridge-clone>/skills/using-agent-bridge ~/.claude/skills/
```

The skill body is deliberately generic across hosts. Host-specific state — which bus you point
at, which agent IDs are registered, where the env file lives — belongs in per-host operator
memory, not in the skill. Updating the skill is `git pull` in this clone.

Harnesses other than Claude Code do not auto-load skills; they open
[`using-agent-bridge/SKILL.md`](using-agent-bridge/SKILL.md) directly. That is why orientation
and doc routing live in [`../AGENTS.md`](../AGENTS.md) rather than only in a skill.

## The skills

| Skill | What it is for | Triggers on |
|---|---|---|
| [`using-agent-bridge`](using-agent-bridge/SKILL.md) | The operational guide: dispatch recipe, worktree-by-default rule, monitoring without burning tokens, health checks, notify-inbox routing, managed-bus topology, panel composition and audit wiring, failure-shape table. Start here. | `agent-dispatch`, `agent-bridge-ping`, seat IDs, `agent_scratch:` keys, `sender-rejected`, `envelope-invalid`, `bridge busy`, panel votes, audit closure — or any request that another model should review or implement something |
| [`bridge-protocol`](bridge-protocol/SKILL.md) | Running a change to *this repo* through the declared build pipeline (design → panel → spec → panel → plan → panel → TDD build → tri-review → merge-gate) and evaluating its executable merge gate. Owns `bridge-protocol/gate/gate.py` and the JSON contract artifacts beside it. | A Bridge change that must be gated before merge; producing or validating `phase_input.json` / `gate_result.json`; choosing a phase `correctness_basis`; a gate that blocked a merge |
| [`defect_hunts`](defect_hunts/) | Not a `SKILL.md` — an importable package of executable defect hunts (config drift, assumption graduation) and their eval runner. The doctrine in `docs/defect-classes/` made runnable; exercised from `tests/defect_hunts/`. | Imported by the gates and tests, not loaded conversationally |
| [`diagnose`](diagnose/SKILL.md) | Read-only tri-seat root-cause diagnosis behind a neutral contamination boundary, driven by a failing test. Assigns blind candidates deterministically and evaluates the `run_record` against the frozen `_diagnose_common` validator. Applies no code changes and accepts no steer. | A failure whose cause should be established by an independent blind panel rather than by its author |
| [`diagnose-steer`](diagnose-steer/SKILL.md) | The declared-steer variant, with steer-specific validation. **The live decorrelated 3-model panel and isolated scribe dispatch are not wired yet** — runs are machine-marked `panel_executed: false`, `verified: false`, `harness_only: true`, and must not be read as verified diagnoses. | A diagnosis that must carry an explicit declared steer |
| [`autonomous-mode`](autonomous-mode/SKILL.md) | Hands-off delivery after spec approval: the panel implements, resolves its own design forks, logs decisions, and either merges (reversible work) or stages on a branch (irreversible work). You audit an already-executed run the next morning; there is no human in the loop mid-run. | "autonomous mode", "ship this overnight", "run unattended", "I'll review in the morning" |
| [`using-arb-learn`](using-arb-learn/SKILL.md) | The ARB Learn intake gate for external techniques and workflow lessons — propose, evaluate, resolve, promote. Dispatches audited bridge seats, so it depends on `using-agent-bridge`. | `/learn`, or asking ARB to evaluate or adopt an external technique |
| [`handoff`](handoff/SKILL.md) | Write a handoff document so a fresh session can continue work after context is cleared — or pick one up and resume. | "handoff", "about to clear context", "pick this up in a new session" |

`_diagnose_common/` is shared machinery for the two diagnose skills — canonicalisation, clock,
collation, git-blob access, neutral validators, and the `run_record` JSON schema. It is
deliberately frozen: the validator is what makes a diagnosis checkable by something other than
its author.

## Role profiles (`roles/`)

Skills are what the **orchestrator** loads. Role profiles are what a **seat** carries: a
system-prompt addition attached at seat level via `BRIDGE_ROLE_PROFILE_FILE`, so every request
that bridge handles is answered in that posture. Wiring and engine-by-engine delivery are in
[`../src/agent_redis_bridge/README.md`](../src/agent_redis_bridge/README.md) § "Role profiles".

| Profile | Posture |
|---|---|
| [`../roles/lead.md`](../roles/lead.md) | Coordination lead — a persistent seat that sequences cross-host work, assigns it, verifies it, and owns the decisions. Decide and own it; do not hedge. |
| [`../roles/team-seat.md`](../roles/team-seat.md) | Team-member seat — a persistent peer taking direction from a lead, executing and reporting with evidence, owning its slice. |
| [`../roles/reviewer.md`](../roles/reviewer.md) | Adversarial reviewer in a multi-model panel. Opens with a verdict label on its own line (`SHIP` / `SHIP_WITH_NITS` / `FIX_BEFORE_MERGE`), expecting to be cross-checked by 2–4 independent reviewers. |
| [`../roles/validator.md`](../roles/validator.md) | Validator in a gate-first build — authors the executable acceptance gate before any builder sees the task, and modifies nothing but the gate path. |
| [`../roles/judgment-oracle.md`](../roles/judgment-oracle.md) | The decorrelated judgment tier of the autonomous-mode posture oracle: a deliberately non-quorum adjunct that judges the diff itself rather than echoing the panel. |

## Which layer reads which file

Four files carry instructions, and the split is the point — worker lifecycle rules are noise to
an orchestrator's reviewers, and orchestration doctrine is noise to a dispatched worker.

| File | Read by | Carries |
|---|---|---|
| [`../AGENTS.md`](../AGENTS.md) | **Everyone**, including dispatched workers. Four layers, most general first. | Universal agent discipline (verify-by-outcome, source-before-analysis, claims-vs-evidence, scope restraint), a generic coding layer, this repo's specifics, and the bridge-worker role layer. Its first two layers mirror `docs/fragments/agents-base.md` and `docs/fragments/coding.md` verbatim — edit the fragment first, then sync. |
| [`../CLAUDE.md`](../CLAUDE.md) | The **orchestrating** Claude Code session, and the cold-Opus subagents it spawns to review. Never workers. | The orchestrator-role layer only: acceptance rules pinned into always-loaded context, who owns dispatch versus integration, the constitution-layer boundary, protected-file overwrite gate, review hygiene. |
| `SKILL.md` (here) | A Claude Code session, auto-loaded on trigger. | Operational how-to. Not doctrine — doctrine that must fire at acceptance-shaped moments is duplicated into `CLAUDE.md` on purpose, because a skill that never triggers injects nothing. |
| `../roles/*.md` | A **seat**, at spawn. | Posture for that seat's whole lifetime, delivered by the bridge as a role profile. |

`GEMINI.md` is a symlink to `AGENTS.md`, not an independent document.

## See also

- [`../README.md`](../README.md) — what ARB is and the six-step loop these skills serve.
- [`../docs/pipeline-operating-manual.md`](../docs/pipeline-operating-manual.md) — the end-to-end
  multi-agent workflow the skills execute (workflow shapes, the light path, arc closure).
- [`../docs/orchestrator-patterns.md`](../docs/orchestrator-patterns.md) — parallel dispatch and
  zero-poll completion monitoring patterns.
- [`../docs/multi-model-consensus.md`](../docs/multi-model-consensus.md) — review hygiene: why
  independent reviewers must not be able to read each other's reports mid-phase.
- [`../docs/quorum-decision-taxonomy.md`](../docs/quorum-decision-taxonomy.md) — the closed
  taxonomy of peer-quorum outcomes and override discipline.
- [`../docs/README.md`](../docs/README.md) — how the rest of `docs/` is organised.
