# Claude Tail Hooks

The Claude-layer visibility service reads warm-session records from the same registry configured by:

- `ARB_CLAUDE_TAIL_REGISTRY_PATH`, or
- Redis hash `${AGENT_REDIS_PREFIX:-agent_scratch:}${ARB_CLAUDE_TAIL_REGISTRY_KEY:-claude:registry}`.

The hook record shape is:

```json
{
  "session_id": "<claude session id>",
  "transcript_path": "<session jsonl path>",
  "seat_id": "claude-<project>-<workspace>",
  "run_id": "<claude session id>"
}
```

Example `settings.json` snippet:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "PYTHONPATH=/Users/<user>/<workspace>/.claude/worktrees/claude-layer-vis/src /Users/<user>/<workspace>/.claude/worktrees/claude-layer-vis/scripts/claude-tail-hooks/session_start.py"
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "PYTHONPATH=/Users/<user>/<workspace>/.claude/worktrees/claude-layer-vis/src /Users/<user>/<workspace>/.claude/worktrees/claude-layer-vis/scripts/claude-tail-hooks/session_end.py"
          }
        ]
      }
    ]
  }
}
```

Cold-Opus reviewer transcripts must be visible under `ARB_CLAUDE_TAIL_COLD_DIR`, defaulting to `~/.claude/tasks`. The `session_start` hook mirrors any `.output` paths supplied in `cold_output_paths` into that directory by symlink, falling back to copy if symlinks are unavailable.

When the warm orchestrator spawns a cold-Opus reviewer, the first user message must include:

```text
[ARB_RUN:<run> ARB_SEAT:<seat> ARB_ORCH:<warm seat_id>]
```

The tailer reads that marker from the first user line so `cold_identity()` groups the cold reviewer under the same run and warm orchestrator.

The cold reviewer should also **end its final message with a completion marker on its own line**:

```text
[ARB_SEAT_DONE]
```

The tailer detects this in the cold seat's output and finishes the seat promptly and accurately — the symmetric counterpart to the start marker. This is the primary "done" signal; if the marker is ever missing (the subagent didn't comply, crashed, or was killed) the daemon falls back to a resumable idle-finish (a cold seat quiet past `ARB_CLAUDE_TAIL_IDLE_FINISH_SECS` is marked done, but un-sticks and resumes if its output grows again). The marker is honored only for cold seats and only in assistant text, so a warm orchestrator quoting it never finishes itself.

## Container Sidecar

`deploy/claude-tail/` includes a small read-only sidecar image for the Claude-tail daemon. It
installs the bridge package and runs:

```sh
python -m agent_redis_bridge.claude_tail
```

The container reads Claude Code transcript files from bind mounts but never writes to the host
Claude directory. Tail offsets and the warm-session registry live in Redis, so no writable
filesystem mount is required.

## Build

```sh
cd <repo-root>
docker build -f deploy/claude-tail/Dockerfile -t arb-claude-tail:dev .
docker run --rm arb-claude-tail:dev python -c "import agent_redis_bridge.claude_tail"
```

## Run With Compose

Set the host Claude path and UID explicitly. The mount target intentionally matches the host path
because the host-side `SessionStart` hook records host-absolute transcript paths.

```sh
CLAUDE_HOME="$HOME/.claude" \
ARB_CLAUDE_COLD_SRC="/private/tmp/claude-$(id -u)" \
AGENT_UID="$(id -u)" \
AGENT_REDIS_URL="redis://host.docker.internal:6379/0" \
ARB_LIVE_REDIS_URL="redis://host.docker.internal:6379/0" \
ARB_TRACE_REDIS_URL="redis://host.docker.internal:6379/0" \
docker compose -f deploy/claude-tail/docker-compose.yml up -d --build
```

Compose mounts:

| Host path | Container path | Mode | Purpose |
|---|---|---|---|
| `CLAUDE_HOME` | `CLAUDE_HOME` | read-only | Claude Code project/session transcripts and the `tasks/` cold-output mirror |
| `ARB_CLAUDE_COLD_SRC` | `ARB_CLAUDE_COLD_SRC` | read-only | Real source root for cold `.output` files when `${CLAUDE_HOME}/tasks` contains symlinks |

The same-path mount is required: the hook registry stores host-absolute `transcript_path` values,
and cold-output symlinks may also point at host-absolute paths. Mounting `~/.claude` at `/claude`
would make those paths unreadable inside the container.

Containerized cold tailing requires each cold `.output` file's real source path to be reachable
in the container. The host hook mirrors cold outputs into `${CLAUDE_HOME}/tasks`; when that mirror
is a symlink to Claude's temp root, mount the source root at the identical absolute path with
`ARB_CLAUDE_COLD_SRC`. On macOS the usual root is `/private/tmp/claude-$(id -u)`. Warm sessions
and cold outputs stored directly under `${CLAUDE_HOME}/tasks` still use the `CLAUDE_HOME` mount.

The service runs as the host UID via `user: "${AGENT_UID:?set AGENT_UID with $(id -u)}"` so read
permissions match the host user. It does not need a writable data volume because `OffsetStore`
commits offsets to the agent Redis bus.

## Bus Environment

Required in normal operation:

- `AGENT_REDIS_URL` or the `AGENT_REDIS_HOST` / `AGENT_REDIS_PORT` / `AGENT_REDIS_DB` family:
  warm-session registry plus offset state.
- `AGENT_REDIS_PREFIX`: defaults to `agent_scratch:`.
- `ARB_LIVE_REDIS_URL`: `events:live` output.
- `ARB_TRACE_REDIS_URL`: `arbmem:trace` output.

Required for containerized cold-Opus tailing when cold mirrors are symlinks:

- `ARB_CLAUDE_COLD_SRC`: same-path read-only mount for the symlink target root, usually
  `/private/tmp/claude-$(id -u)` on macOS.

Useful daemon knobs:

- `ARB_CLAUDE_TAIL_COLD_DIR`: defaults to `${CLAUDE_HOME}/tasks` in the compose file.
- `ARB_CLAUDE_TAIL_INTERVAL_SECS`: polling interval, default `1.0`.
- `ARB_CLAUDE_TAIL_MAX_AGE_SECS`: cold transcript discovery recency window, default `21600`.
- `ARB_CLAUDE_TAIL_IDLE_FINISH_SECS`: idle finish threshold, default `300`.

For Docker Desktop local Redis, `host.docker.internal` points back at the host. On Linux hosts,
configure the Redis URLs for the reachable host/container network address.

### Linux fd-probe limitation

The finality probe fails closed when any `/proc/<pid>/fd` directory or target fd metadata cannot
be read. That protects the target's deny-proof, but on a multi-user Linux host an unrelated
unreadable PID can make a cold terminal turn never earn and eventually reach the abandon path.
The implementation intentionally does not skip such PIDs: inability to prove that an unreadable
process cannot hold the target inode is not safe evidence of quiescence. macOS `lsof` is the
production path; Linux deployments should use a same-user environment with readable `/proc`
entries or treat this residual as an operational limitation.
