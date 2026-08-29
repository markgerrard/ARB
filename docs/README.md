# How `docs/` is organised

Most of what is written down here is not reference material — it is **doctrine with an incident
behind it**. Read in this order.

## Start here

- [`defect-classes/`](defect-classes/README.md) — **the corpus, and the best thing in this
  directory.** Generalisable defect-detection *moves*, each derived from a dated incident and
  each carrying a detection question a reviewer actively asks. Start with the umbrella class,
  `fake-cheaper-than-real.md`. These are the rules the executable hunts in `skills/defect_hunts/`
  and the gates under `scripts/` enforce.
- [`architecture-overview.md`](architecture-overview.md) — whole-system orientation: the comms
  plane, the orchestration machinery, the memory/evidence plane, the workflow layer on top, the
  ops layer underneath, and a question→document map. `scripts/check-doc-drift` asserts that every
  top-level package under `src/` is named in it, so it cannot silently go stale again.
- [`pipeline-operating-manual.md`](pipeline-operating-manual.md) — **the end-to-end multi-agent
  workflow on top of the bridge**: the workflow shapes and when to confirm which at kickoff, the
  light path for record-adjudicated fixes, and arc closure. Read this when adopting the bridge
  into a real project workflow.

## The subdirectories

| Directory | What lives there |
|---|---|
| [`defect-classes/`](defect-classes/README.md) | The doctrine corpus (above). |
| [`decisions/`](decisions/) | Decision records: what was settled, what was deferred and the trigger that builds it. `arb-memory-architecture.md` is the largest; also the agent-SDK trusted-continuation rule, the threat-model recalibration, the judgment seat, and the Python 3.14 / coincurve constraint. |
| [`runbooks/`](runbooks/) | Symptom-named operational procedures — seat setup, fleet restart, channel corruption, the ARB Memory e2e proofs, bus registrar operations, exempt-seat machine users. If another seat would need it *during* an incident, it belongs here. |
| [`agent-memory-seeds/`](agent-memory-seeds/README.md) | The version-controlled source of truth for the machine-wide memory topics seeded into each agent family's local auto-memory store. One corpus, per-agent adapters; `scripts/check-seed-canon` keeps the copies honest. |
| [`superpowers/`](superpowers/) | **The design record** — the paper trail of how changes were built: `specs/`, `plans/`, `reviews/`, `briefs/`, `probes/`, `notes/`. It is the largest thing in the repo by file count and is history, not current instruction. Reach for it to answer "why is it like this", not "how do I do this". |
| [`fragments/`](fragments/) | Canonical text mirrored verbatim into several documents. `dispatch-recipe.md`, `env-overrides.md` and `failure-shapes.md` are injected between `<!-- fragment:name begin/end -->` markers; `agents-base.md` and `coding.md` are the shared layers of `AGENTS.md`. **Edit the fragment, never the copy** — `scripts/check-doc-drift` fails on any divergence. |
| [`upstream/`](upstream/), [`examples/`](examples/) | Third-party CLI failures written up against the adapter that hit them; sample config. |

## The index is generated and gated

[`index.json`](index.json) is the source of truth: one object per tracked document with `path`,
`purpose` (single line, ≤ 120 chars), `status` (`current` / `design` / `runbook` / `archive` /
`incident`) and `audience` (`worker` / `orchestrator` / `operator` / `any`).
[`INDEX.md`](INDEX.md) is rendered from it by `scripts/gen-doc-index` and must never be
hand-edited.

`scripts/check-doc-index` enforces both directions, and `tests/test_doc_index.py` runs it:

- **Every tracked markdown file under `docs/`, and every tracked markdown file at the repo root,
  must have an entry.** A new doc without one fails the gate. Collections
  (`docs/reviews`, `docs/superpowers/reviews|plans|specs`) are exempt per-file once the
  collection itself is indexed, and symlinked markdown (`GEMINI.md` → `AGENTS.md`) is excluded
  as an alias rather than a document.
- **`INDEX.md` must be fresh.** Add the entry, then run `scripts/gen-doc-index`.

READMEs under `src/` and `skills/` are deliberately *not* indexed — `tracked_markdown_paths()` in
`scripts/doc_index_lib.py` takes root-level files and `docs/` only, so an area README lives next
to the code it describes without needing a routing entry.

Two sibling gates run over the same files:

- `scripts/check-doc-recipes` — every documented `agent-dispatch` / `dispatch-dev` invocation in a
  fenced block must carry `--run-id`, `--adhoc`, `--check` or `--dry-run-envelope` (or sit under a
  `<!-- doc-recipes: allow-bare -->` marker), and a deprecated `--engine gemini-acp` must be
  marked as deprecated within three lines.
- `scripts/check-doc-drift` — the fragment mirrors above, plus the `src/` package coverage
  assertion on `architecture-overview.md`.

## The document map

Where the whole-system links used to live, by question.

**Adopting the bridge into a workflow**

- [`pipeline-operating-manual.md`](pipeline-operating-manual.md) — the workflow layer on top of
  the dispatch patterns: two workflow shapes (lightweight single-reviewer; rigorous
  parallel-review plus a cold-Opus final), confirmed at project kickoff.
- [`phase-workflow.template.md`](phase-workflow.template.md) — stub to copy into a new project as
  `docs/phase-workflow.md`. Records the per-phase workflow choice, the cold-Opus dispatch
  mechanism, and the revision audit trail, so a fresh session reading the project knows what
  cadence each phase runs at.
- [`orchestrator-patterns.md`](orchestrator-patterns.md) — field-tested patterns for parallel
  dispatch and zero-poll completion monitoring: git-worktree-per-task, the bare-foreground
  dispatch antipattern and its fix, the dual-reviewer pattern, recurring-gotcha briefing. Read
  before driving the bridge from an orchestrator.
- [`worktree-isolated-dispatch.md`](worktree-isolated-dispatch.md) — the isolation mechanism the
  patterns above assume.

**Routing and panels**

- [`agent-role-routing.md`](agent-role-routing.md) — which engine to dispatch for which job, and
  the bake-off evidence behind the default routing, plus the completion-stack recipe that
  guarantees commits.
- [`implementor-routing.md`](implementor-routing.md) — the implementor-side counterpart.
- [`multi-model-consensus.md`](multi-model-consensus.md) — review hygiene: reviewers in an
  *independent* phase must not be able to read each other's reports, and why (bridge engines and
  in-session subagents share a checkout).
- [`quorum-decision-taxonomy.md`](quorum-decision-taxonomy.md) — the closed taxonomy of
  peer-quorum outcomes plus override discipline: six outcome states, and five mandatory fields on
  any override (voter / auditor / named doubts / chosen safer action / deferred follow-up).

**Claude-to-Claude coordination**

- [`claude-peer-coordination.md`](claude-peer-coordination.md) — two Claude Code sessions talking
  over the bus with no bridge daemon and no engine pool, including managed-bus day-one gotchas
  (DB discovery, BLPOP idle timeout, background-task monitoring).
- [`orchestrating-claude-peers.md`](orchestrating-claude-peers.md) — the field lessons from doing
  it for real.

**Mechanism and operations**

- [`bridge-parallelism.md`](bridge-parallelism.md) — engine pool design, `--max-parallel`, control
  envelope semantics.
- [`self-hosted-bus.md`](self-hosted-bus.md) — per-identity ACLs, `NOPERM`-by-design reads, and
  the panel-close failure they produce when an audit-emitter grant is missing.
- [`macos-launchd-seats.md`](macos-launchd-seats.md) — the launchd recipe for durable seats on
  macOS.
- [`measurement-principles.md`](measurement-principles.md), [`gotcha-lint.md`](gotcha-lint.md),
  [`evidence-first-remediation.md`](evidence-first-remediation.md) — the measurement and
  remediation discipline the gates encode.

Protocol and code-level references live with the code: [`../SPEC.md`](../SPEC.md) for the
envelope contract, [`../src/agent_redis_bridge/README.md`](../src/agent_redis_bridge/README.md)
for the daemon and its operator surface,
[`../src/agent_redis_bridge/engines/README.md`](../src/agent_redis_bridge/engines/README.md) for
the adapters, [`../src/arb_memory/README.md`](../src/arb_memory/README.md) for the memory and
sibling planes, and [`../skills/README.md`](../skills/README.md) for what an orchestrator loads.
`scripts/agent-inbox-watcher` is the reference inbox listener. Per-host channel design notes —
which bus is live, which seats are registered — stay in operator memory rather than in the repo,
by the same rule that keeps host state out of the skill.
