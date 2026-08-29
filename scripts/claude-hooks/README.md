# Claude Code lifecycle hooks — context-cost management

Three hooks that keep long orchestration sessions cheap to resume. Written 2026-07-06 after
measuring that heavy warm-orchestrator sessions accumulate 20–60 MB of transcript, making every
prompt-cache miss (any wake-up >5 min after the last activity) a full-price context re-read.

| Script | Event | What it does |
|---|---|---|
| `precompact-preserve.sh` | `PreCompact` | Injects summarizer instructions so compaction preserves dispatch task-ids, run-ids, branches, unpushed SHAs, worktree paths, TaskList state, and unclosed audit runs verbatim. |
| `context-nudge.sh` | `Stop` | Past a transcript-size threshold (default 25 MB, then every +15 MB), blocks the stop ONCE per band and instructs the session to write a handoff and suggest `/clear`. Tunable via `CLAUDE_CONTEXT_NUDGE_MB` / `CLAUDE_CONTEXT_NUDGE_BAND_MB`. |
| `handoff-hint.sh` | `SessionStart` | Points a fresh session at the newest `.claude/handoffs/*.md` (if <7 days old). Hint only — no content injected. |

Known limitation: `context-nudge.sh` measures the transcript file, which only grows — a `/compact`
shrinks context but not the file, so treat post-compact nudges as advisory. A `/clear` starts a new
transcript and resets the measure.

## Wiring (per-operator, `.claude/settings.local.json` — do not commit personal settings)

Merge into the `hooks` object (paths assume this clone; adjust):

```json
{
  "hooks": {
    "PreCompact": [
      { "hooks": [ { "type": "command", "command": "<clone>/scripts/claude-hooks/precompact-preserve.sh", "timeout": 10 } ] }
    ],
    "Stop": [
      { "hooks": [ { "type": "command", "command": "<clone>/scripts/claude-hooks/context-nudge.sh", "timeout": 15 } ] }
    ],
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "<clone>/scripts/claude-hooks/handoff-hint.sh", "timeout": 10 } ] }
    ]
  }
}
```

If a `SessionStart` (or other) entry already exists — e.g. the claude-tail observability hooks —
append inside the existing `hooks` array rather than adding a second top-level entry.

Requires `jq` on PATH. Pipe-test any change before trusting it:

```bash
echo '{"transcript_path":"<a big .jsonl>","session_id":"pipetest"}' | scripts/claude-hooks/context-nudge.sh
echo '{}' | scripts/claude-hooks/precompact-preserve.sh | jq .
echo '{}' | scripts/claude-hooks/handoff-hint.sh
```
