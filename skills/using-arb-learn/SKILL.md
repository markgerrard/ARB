---
name: using-arb-learn
description: Run the ARB Learn intake gate for external techniques and workflow lessons. Use when the user says `/learn`, asks ARB to learn or evaluate something, or wants to propose, evaluate, resolve, or promote an `arb-learn` proposal. Before every evaluation, use the host's structured question tool to ask which model panel to include.
---

# Using ARB Learn

Operate from the AgentRedisBridge repository. Read `skills/using-agent-bridge/SKILL.md` before any evaluation because Learn dispatches audited bridge seats.

## Propose

Put the external technique in a source file, then run:

```sh
scripts/arb-learn propose <source-file> --target arb
```

Use `--target skill` or `--target project` only when the user's intended destination requires it. Report the minted `learn-*` id.

## Select the evaluation panel

Before every `evaluate` or `evaluate --retry`, use the host's structured question tool. Do not reuse a prior run's selection or silently accept the CLI default.

Ask: **Which model panel should evaluate this proposal?**

- **Full five (Recommended):** Codex GPT-5.6 Sol at high effort, agy, GLM-5.2, Fable 5 through Agent SDK, and Grok 4.5.
- **Core three:** Codex GPT-5.6 Sol at high effort, agy, and GLM-5.2.
- **Custom:** let the user provide any combination of `codex-sol`, `agy`, `glm`, `fable`, and `grok`.

Prefer one multi-select question when the host supports it. With an exclusive-choice question tool, offer Full/Core/Custom and use a follow-up structured question for Custom. If the host has no structured question tool, stop before evaluation and ask the same selection question directly.

Map the answer exactly:

```sh
# Full
scripts/arb-learn evaluate <learn-id> --panel full

# Core
scripts/arb-learn evaluate <learn-id> --panel core

# Custom
scripts/arb-learn evaluate <learn-id> --seat codex-sol --seat fable --seat grok
```

Seat mapping for health checks:

| Alias | Engine | Target |
|---|---|---|
| `codex-sol` | `codex` | `codex-bridge-dev-example` |
| `agy` | `agy-print` | `agy-bridge-dev` |
| `glm` | `pi-sdk` | `pi-sdk-bridge-dev-glm` |
| `fable` | `agent-sdk` | `asdk-agentredisbridge-dev-fable5` |
| `grok` | `grok-acp` | `grok-agentredisbridge-dev-grok45` |

Before evaluation, perform a read-only health check for every selected target through the
Python dispatcher, which owns the supported `--check` path:

```sh
FROM_AGENT_ID=claude-bridge-dev \
BRANCH=dev \
AGENT_ENV_FILE=<repo-env-file> \
scripts/agent-dispatch --engine <engine> --target-id <target> --check
```

Those sender and branch values intentionally match the environment used internally by
`arb-learn` evaluation; preflighting as the ambient orchestrator could pass a different
sender policy than the real dispatch.

Do not use `scripts/dispatch-dev --check`; its Go-wrapper argument parser does not support
that flag. If any selected seat is unavailable, report it and stop; do not persist a
predictable `eval-error` merely because the fleet is down.

The CLI fails closed: any substantive `REJECT` rejects; otherwise any invalid, failed, or timed-out selected seat yields `eval-error`; otherwise `NEEDS-MARK` yields `needs-mark`; only unanimous clean `WORTH-BUILDING` verdicts yield `eval-approved`.

## Resolve and promote

Resolve `needs-mark` only from an explicit user decision:

```sh
scripts/arb-learn resolve <learn-id> approve --reason "<reason>"
scripts/arb-learn resolve <learn-id> reject --reason "<reason>"
```

Promote only after `eval-approved` and explicit user authorization:

```sh
scripts/arb-learn promote <learn-id>
```

Promotion emits an inert build brief; it does not authorize implementation.
