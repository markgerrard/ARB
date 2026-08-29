#!/usr/bin/env bash
# PreCompact hook: steer the compaction summarizer to preserve the orchestration
# state that is expensive or impossible to rediscover after a compact.
# Wire-up: see scripts/claude-hooks/README.md
cat <<'JSON'
{"hookSpecificOutput":{"hookEventName":"PreCompact","additionalContext":"CRITICAL - preserve VERBATIM in the summary (never paraphrase away): every bridge dispatch task-id (UUID) and its seat/target-id; every ARB run-id (RID / panel-* / impl-* labels); all branch names and worktree paths in play; all unpushed or unmerged commit SHAs and which branch they sit on; paths of brief/report files under /tmp and docs/superpowers/; background shell task ids still running; the current TaskList state (task numbers + status); any minted-but-unclosed audit runs (manifest emitted, verdict pending). Also state explicitly which steps are DONE vs IN-FLIGHT vs NOT-STARTED for the active workflow."}}
JSON
