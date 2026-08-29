# agent-sdk mutation probe

This probe checks whether `claude-agent-sdk` can drive MiniMax-M3, Kimi, and
GLM-5.2 through a real code mutation in a throwaway git repo. A PASS means only:

> `claude-agent-sdk` can drive that model to a genuine code mutation in a clean cwd.

It green-lights an `agent-sdk` engine build-spike, not a finished bridge engine.
The bridge build still has to prove worktree dispatch, completion-gate behavior,
progress/steer/interrupt/stop plumbing, and whether SDK tool execution can be
mediated by the bridge.

## Installed versions

- `claude-agent-sdk`: 0.2.104
- Claude Code CLI: 2.1.181
- `pytest`: 9.1.0

## Secrets

Keys live in the gitignored host env file:

```bash
set -a
source /Users/<user>/<workspace>/envs/agent-sdk-models-dev.env
set +a
```

The source and results must remain secret-free. Captured stdout/stderr and trace
excerpts are scrubbed by both key value and env-var name before printing or
persisting. Rotate these provider keys after testing if the source chat was not
private.

## Stage-0 spike

Run Stage 0 before any mutation matrix:

```bash
set -a; source /Users/<user>/<workspace>/envs/agent-sdk-models-dev.env; set +a
/Users/<user>/<workspace>/.venv/bin/python3 tools/agent-sdk-probe/spike.py | tee /tmp/agent-sdk-spike.out
```

Observed Stage-0 findings on 2026-06-18:

| model | endpoint | model id | auth shape | verdict |
|---|---|---|---|---|
| minimax-m3 | `https://api.minimax.io/anthropic` | `MiniMax-M3` | `ANTHROPIC_API_KEY` / x-api-key | PASS: `Read` tool used, content contained `forty-two` |
| kimi | `https://api.moonshot.ai/anthropic` | `kimi-for-coding` | `ANTHROPIC_API_KEY` / x-api-key | FAIL(endpoint/auth): timed out |
| glm-5.2 | `https://open.bigmodel.cn/api/anthropic` | `glm-5.2` | `ANTHROPIC_API_KEY` / x-api-key | FAIL(endpoint/auth): timed out |

M3 is the required gate for this probe and passed. Kimi/GLM remain recorded
endpoint/auth failures with the current placeholder endpoint/model settings.

## D1 tool-mediation observation

The SDK call runs tools inside the Claude Code subprocess. This probe observes
tool use through the SDK message stream and constrains it with `allowed_tools`,
but it does not prove that Agent Redis Bridge can mediate or permission-gate each
write before execution. The installed SDK exposes hook/can-use-tool surfaces that
an engine build-spike should investigate; absent that, OS/worktree confinement
remains the hard boundary for mutation safety.

## Unit tests

Run from the repo worktree:

```bash
cd tools/agent-sdk-probe
/Users/<user>/<workspace>/.venv/bin/python3 -m unittest discover -s tests -v
```

The offline suite covers model config, secret scrubbing, fixture determinism,
the anti-false-PASS verifier, and runner wiring with a mocked SDK mutation.

## Mutation probe

Task 8 is intentionally not run by implementation workers. After review, the
orchestrator can run:

```bash
set -a; source /Users/<user>/<workspace>/envs/agent-sdk-models-dev.env; set +a
/Users/<user>/<workspace>/.venv/bin/python3 tools/agent-sdk-probe/probe.py 2>&1 | tee /tmp/agent-sdk-probe-run.out
```

Then grep non-empty key values and env-var names across the results doc and
captured output before committing results.

## Provider routing notes (resolved 2026-06-18)

- **MiniMax-M3**: `https://api.minimax.io/anthropic`, `ANTHROPIC_API_KEY`, model `MiniMax-M3` (passed directly).
- **Kimi**: `https://api.kimi.com/coding/`, `ANTHROPIC_API_KEY`, model `kimi-for-coding` (passed directly).
- **GLM-5.2 (z.ai)**: `https://api.z.ai/api/anthropic`, `ANTHROPIC_AUTH_TOKEN`, and **tier-lane mapping** —
  the SDK uses its default model while `ANTHROPIC_DEFAULT_{OPUS,SONNET}_MODEL=glm-5.2[1m]` +
  `ANTHROPIC_DEFAULT_HAIKU_MODEL=glm-4.5-air` route it to GLM (matches z.ai's Claude Code config).
  Passing `model="glm-5.2"` directly is the WRONG shape and 529-loops.
- **529 caveat**: z.ai intermittently returns HTTP 529 ("overloaded") under load and the SDK retries
  10× then fails. This is a **provider-side capacity signal, independent of plan tier** — every LLM
  provider load-sheds with 529 under pressure (Anthropic itself 529s even on Max plans). A `FAIL` whose
  error mentions `529`/overloaded is **transient** (re-run), not a config error — verified by passes
  between overload windows. Applies to any model here, not just GLM.
