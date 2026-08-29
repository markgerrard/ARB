#!/usr/bin/env bash
# SessionStart hook: point a fresh session at the most recent handoff if one was
# written in the last 7 days. Hint only - does not inject the content.
# Wire-up: see scripts/claude-hooks/README.md
set -eu
dir="${CLAUDE_PROJECT_DIR:-$PWD}/.claude/handoffs"
latest=$(ls -t "$dir"/*.md 2>/dev/null | head -1) || exit 0
[ -n "$latest" ] || exit 0
now=$(date +%s)
mtime=$(stat -f%m "$latest" 2>/dev/null || stat -c%Y "$latest" 2>/dev/null) || exit 0
age_h=$(( (now - mtime) / 3600 ))
[ "$age_h" -le 168 ] || exit 0
printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"Most recent handoff: %s (written ~%sh ago). If this session resumes that work, Read it before doing anything else."}}\n' "$latest" "$age_h"
