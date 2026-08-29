<p align="center"><img src="assets/readme-header.png" width="640" alt="ARB — Agent Redis Bridge"></p>

# Agent Redis Bridge

ARB turns local coding agents — Codex, Grok, Kimi, MiniMax, opencode, pi, Claude Code and a
dozen more — into addressable peers on a Redis `agent_scratch:` bus, and adds the machinery
you need once more than one model is doing the work: engine pools, worktree isolation,
completion gating, audited review panels, and a Postgres-backed shared memory.

**It is not a tool a human runs from a shell.** The thing that runs it is an **orchestrator**:
usually a Claude Code session with [`skills/using-agent-bridge`](skills/using-agent-bridge/SKILL.md)
loaded, though any harness that can read `SKILL.md` and drive a shell will do — Codex, pi and
Grok Build orchestrators have all run it; Claude Code is where it lives day to day. The standard
seat is **Claude Code with Fable 5**: in day-to-day use since the Claude 5 release, Opus has
struggled a little with discipline — verifying before claiming, staying inside a brief — where
Fable holds it. That session
stands the seats up, publishes the brief, dispatches, monitors, runs the
review panel, verifies the result against git, and integrates. Humans set intent, co-sign
doctrine, and adjudicate disputes — they do not type `agent-dispatch` themselves. Every recipe
in this repo is written for that reader, and that is what [`skills/`](skills/README.md) exists
for.

## How a session uses it

1. **Load the skill.** `skills/using-agent-bridge` auto-triggers on bridge vocabulary once
   symlinked into `~/.claude/skills/`. It carries the dispatch recipe, the monitoring idioms,
   and the failure-shape table. → [`skills/README.md`](skills/README.md)
2. **Stand up the seats.** One long-lived daemon per engine × project × workspace, under a
   supervisor — `systemd --user` on Linux, `launchd` on macOS. `nohup` is not supervision.
   → [`systemd/README.md`](systemd/README.md), [`docs/macos-launchd-seats.md`](docs/macos-launchd-seats.md),
   [`docs/runbooks/agentbridge-seat-setup.md`](docs/runbooks/agentbridge-seat-setup.md)
3. **Publish the brief.** Briefs are artefacts, authored off-cockpit and stored before they are
   sent: `scripts/arb-memory-harness-publish` writes the brief to ARB Memory and returns a
   receipt (`artefact_id`, `version`). → [`src/arb_memory/README.md`](src/arb_memory/README.md),
   [`docs/pipeline-operating-manual.md`](docs/pipeline-operating-manual.md)
4. **Dispatch with the pre-minted quartet.** Enqueue goes through one seam,
   `dispatch_authority.publish_and_enqueue`; `scripts/dispatch-dev` and `scripts/agent-dispatch`
   are its front ends, and an ordinary request must carry
   `--artefact-id` / `--version` / `--receipt` / `--brief`. Free-form task strings were removed.
   → [`src/agent_redis_bridge/README.md`](src/agent_redis_bridge/README.md)
5. **Monitor without polling.** The dispatcher process *is* the wait; `LLEN` on an inbox is a
   trap (BLPOP consumes atomically), and on the self-hosted bus foreign reads are `NOPERM` by
   design. → [`skills/using-agent-bridge/SKILL.md`](skills/using-agent-bridge/SKILL.md) §
   "Monitoring without burning tokens", [`docs/orchestrator-patterns.md`](docs/orchestrator-patterns.md)
6. **Collect, verify, integrate.** **The reply is a claim; the commit is the evidence.** Read
   the SHA, the diff and the test run — not the worker's prose — then merge. Workers commit
   inside their own worktree; only the orchestrator integrates.
   → [`CLAUDE.md`](CLAUDE.md), [`docs/defect-classes/README.md`](docs/defect-classes/README.md)

## The standard process

Work runs as a **graph, not a line**, and every gate is a panel:

1. **Design / spec** — authored by the orchestrator or by another model (a cold subagent or a
   bridge seat). The author is recorded.
2. **Panel** — a multi-model review panel certifies it. **The author is non-certifying**: the
   author — or any seat from the author's model family — may sit on the panel, *cold*, with no
   context from the authoring pass, and contribute findings; its vote does not count toward the
   quorum. Certification comes from seats of a different lineage.
3. **Plan** — same shape: authored, then panelled with the author non-certifying.
4. **Implementation** — dispatched to a seat in its own worktree and branch; the seat commits
   there, never on the shared branch.
5. **Review panel** — same rule, now for the **implementer**: the seat (and lineage) that wrote
   the code reviews cold and non-certifying; a different lineage certifies. Findings are triaged
   P0/P1/P2; the loop goes back to whichever stage the finding belongs to and **repeats until
   there are zero P0s and zero P1s**. Only then does the orchestrator integrate.

An orchestrator can run **sub-orchestrators on separate tracks** in parallel — each with its own
branch and worktree, its own Redis database for the bus, and its own memory namespace — and the
seat adapters themselves run turns in parallel (`--max-parallel`, engine pools). Rules for who
authors, who certifies, and how votes are counted: [`docs/pipeline-operating-manual.md`](docs/pipeline-operating-manual.md),
[`docs/quorum-decision-taxonomy.md`](docs/quorum-decision-taxonomy.md),
[`docs/multi-model-consensus.md`](docs/multi-model-consensus.md). Isolation and parallelism:
[`docs/worktree-isolated-dispatch.md`](docs/worktree-isolated-dispatch.md),
[`docs/bridge-parallelism.md`](docs/bridge-parallelism.md),
[`docs/orchestrating-claude-peers.md`](docs/orchestrating-claude-peers.md).

## The layers

| Layer | Where | What it is |
|---|---|---|
| **Comms plane** | [`src/agent_redis_bridge/`](src/agent_redis_bridge/README.md) | The bridge daemon and its operator surface: envelope protocol, inbox/BLPOP dispatch, run-id discipline, sender policy, worktree isolation, completion gating, safety knobs. |
| **Engine adapters** | [`src/agent_redis_bridge/engines/`](src/agent_redis_bridge/engines/README.md) | Seventeen adapters behind one `base.py` contract, on a shared ACP transport. Support tiers — certifying / experimental / retired — are declared in `support_tiers.py` and asserted by tests. |
| **ARB Memory + sibling planes** | [`src/arb_memory/`](src/arb_memory/README.md) | Postgres-backed artefact and hint store: single-writer, visibility model, grants, audit, MCP server. Plus `arb_messages`, `arb_files`, `arb_secrets`, `arb_email`, `arb_registration`. |
| **Orchestration & doctrine** | [`skills/`](skills/README.md), `roles/`, [`docs/`](docs/README.md) | What turns the bus into a dev team: skills the orchestrator loads, role profiles seats carry, workflow shapes, quorum rules, and the constitution layer in [`CLAUDE.md`](CLAUDE.md) / [`AGENTS.md`](AGENTS.md). |
| **Enforcement** | `tests/`, `scripts/*gate*`, [`.githooks/pre-push`](.githooks/pre-push) | The doctrine made executable: defect hunts, AST guards, engine parity and support-tier checks, doc-index and fragment-drift gates, and a suite gate that treats skips as a set. |

Start with [`docs/architecture-overview.md`](docs/architecture-overview.md) for the whole-system
model and a question→document map, then [`docs/defect-classes/README.md`](docs/defect-classes/README.md)
for the reasoning behind the gates.

## Authentication

ARB drives agents through their official local CLIs and SDKs. You authenticate with each vendor
through that vendor's own flow — `claude` / `codex` / `pi login`, or an API key in the seat's
environment. Guarantees, enforced in code:

- **ARB implements no vendor login** and never extracts credentials from a vendor's credential store
  (`~/.codex/auth.json`, `~/.pi/agent/auth.json`, the keychain). Confined seats receive an
  operator-exported API key, not a copied session.
- **Subscription seats use `CLAUDE_CODE_OAUTH_TOKEN`** — a token *you* mint with `claude setup-token`,
  Anthropic's documented route for running the Agent SDK on a subscription. ARB only ever receives it
  through the seat's environment; it never obtains one itself.
- **Operator-supplied tokens are scrubbed** from every transcript and stored session
  (`ScrubbedSessionStore` in `engines/agent_sdk.py`), and a subscription seat refuses to start if any
  shadow `ANTHROPIC_*` key is present alongside it.
- **The no-credential-store rule is a test**, not a grep:
  `tests/test_no_vendor_credential_store_reads.py` fails if code under `src/`, `scripts/`, `tools/`,
  `pi-extensions/` or `.pi/` references a vendor credential file or keychain lookup.
- **No pooling, proxying or reselling** of subscription usage: one seat is one operator's own
  authentication with one vendor.
- **API keys are recommended** for CI, servers and shared deployments.
- **Compatibility depends on each vendor's current terms and supported interfaces**; this is a
  description of what the code does, not a legal opinion.

## Status and limits

- **Solo-built, AI-assisted, small scale.** One operator, single- to double-digit seats. There is no load
  or latency data from sustained operation. The review panels documented here are models
  reviewing models with one human adjudicating.
- **CI runs the doctrine gate.** [`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs the defect hunts, AST guards, engine parity, doc-index and redaction checks on every push — under two minutes on a fresh clone. The full suite (Postgres, Redis, tmux; ~10 minutes) is `scripts/suite-gate`, run locally; `.githooks/pre-push` is the opt-in local gate. See [`src/agent_redis_bridge/README.md`](src/agent_redis_bridge/README.md) § "Pre-push gate" for the measurement behind that split.
- **Deployment docs describe a fleet that is not this repo.** `deploy/` and the runbooks under
  `docs/runbooks/` carry a real topology's shape; the inventory in them is not this checkout's.
- **Some engines are barely exercised.** Only three adapters are `certifying`
  (`support_tiers.py`); the rest are experimental or retired, and the table says which.
- **Python ≥ 3.11.** On 3.14 the Nostr/BIP-340 path skips with a stated reason — see
  [`docs/decisions/python-314-coincurve-daemon-hosts.md`](docs/decisions/python-314-coincurve-daemon-hosts.md).

## Licence

Apache-2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

## See also

- [`docs/architecture-overview.md`](docs/architecture-overview.md) — whole-system orientation.
  Read this first if you are new to the repo.
- [`docs/INDEX.md`](docs/INDEX.md) — the generated routing table for every tracked document
  (source of truth: `docs/index.json`; see [`docs/README.md`](docs/README.md)).
- [`SPEC.md`](SPEC.md) — envelope and protocol specification.
