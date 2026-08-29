# dsh-acp live gate — first real dispatch to a DeepSeek Harness seat

This is the acceptance gate for the `dsh-acp` engine: a real `agent-dispatch`
round trip to a DeepSeek Harness seat, answered by the model, billed on the
opencode gateway. Keep the work small — the point is the round trip, not the task.

## Assumptions

```json
{
  "items": [
    {
      "statement": "The dsh-acp seat dsh-bridge-dev is registered and its inbox consumer is alive. Observed from the ORCHESTRATOR's vantage (agent-bridge-ping returned heartbeat=alive consumer=alive at 2026-08-17T21:38Z), not the seat's, and not published as an artefact — so it is assumed here, not demonstrated.",
      "status": "assumed",
      "vantage": "bridge-dev-mac"
    },
    {
      "statement": "The runtime reaches deepseek-v4-pro through the opencode gateway at https://opencode.ai/zen/go/v1. A hand-run ACP probe got a model reply with stopReason=end_turn on 2026-08-17, but that was a standalone process, not this seat, so it does not carry to the seat's vantage.",
      "status": "assumed",
      "vantage": "bridge-dev-mac"
    },
    {
      "statement": "The seat's bash and filesystem tools are confined to its workspace by DSH_PERMISSION_MODE=workspace-write. Set at spawn in the seat env file and never exercised — the probe ran a tool-free turn.",
      "status": "assumed",
      "vantage": "bridge-dev-mac"
    }
  ]
}
```

## Task

Answer all four, briefly and factually. Do not modify any file.

1. State the model you are running as. Your system prompt names it.
2. Run `pwd` and report the working directory.
3. Run `ls` and report how many entries are in that directory.
4. Read the first line of `README.md` in that directory and quote it exactly.

## Why these four

They exercise a different layer each, so a partial failure is legible rather
than a single opaque "it didn't work":

- (1) proves the model route resolved — the composition booted with a model and
  the gateway answered.
- (2) proves the ACP session's `cwd` reached the tools.
- (3) proves the bash tool executes at all under the sandbox policy.
- (4) proves the filesystem tool reads under the same policy.

## Reply format

Plain prose, four numbered answers. No file writes, no commits. If any step is
refused or errors, say which one and quote the error verbatim rather than
working around it — a refusal here is the finding.
