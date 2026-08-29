| Override | Why it's needed |
|---|---|
| `FROM_AGENT_ID` | The bridge's `--sender-policy` only trusts specific agent IDs. The shell helper's default may be a legacy value, so supply the real ID. |
| `BRANCH` | The bridge rejects empty branches as `envelope-invalid invalid-branch`. In detached HEAD, `git branch --show-current` returns `""`. |
| `AGENT_ENV_FILE` | Points helper scripts at the correct Redis and project settings for this worktree. |
| `--target-id` | Overrides legacy or inferred target names; use the actual registered agent ID. |
| `--timeout` | Default is 1800 seconds. Use 5400+ for substantial review or implementation tasks. |
| `--turn-timeout` | Optional ceiling for one task engine turn, not total multi-turn dispatch duration. Trusted senders may request above or below the seat default, up to its `--turn-timeout-max`; keep client `--timeout` above it. |
