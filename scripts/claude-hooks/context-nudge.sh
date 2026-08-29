#!/usr/bin/env bash
# Stop hook: when the session transcript crosses size bands, block the stop once
# per band with an instruction to write a handoff and suggest /clear.
# Calibration 2026-07-06: heavy orchestration sessions run 20-60 MB of JSONL.
# Limitation: transcript size only grows (a /compact shrinks context, not the
# file), so bands only ratchet upward; the per-band sentinel keeps this to one
# nudge each. A /clear starts a new transcript, which resets the measure.
# Wire-up: see scripts/claude-hooks/README.md
set -eu
input=$(cat)
tp=$(printf '%s' "$input" | jq -r '.transcript_path // empty')
[ -n "$tp" ] && [ -f "$tp" ] || exit 0
size=$(stat -f%z "$tp" 2>/dev/null || stat -c%s "$tp" 2>/dev/null) || exit 0
THRESHOLD=$(( ${CLAUDE_CONTEXT_NUDGE_MB:-25} * 1024 * 1024 ))
BAND=$(( ${CLAUDE_CONTEXT_NUDGE_BAND_MB:-15} * 1024 * 1024 ))
[ "$size" -ge "$THRESHOLD" ] || exit 0
band=$(( (size - THRESHOLD) / BAND ))
sid=$(printf '%s' "$input" | jq -r '.session_id // "unknown"')
sentinel="/tmp/claude-handoff-nudge-${sid}-band${band}"
[ -e "$sentinel" ] && exit 0
touch "$sentinel"
mb=$(( size / 1024 / 1024 ))
printf '{"decision":"block","reason":"Context-size hook: this session transcript is ~%sMB, so every cache-missed wake-up re-reads a large context. Unless a handoff already covers the CURRENT state: invoke the handoff skill now, write it under .claude/handoffs/, then tell the user a handoff is ready and suggest /clear + resume from it. If work is mid-flight (a dispatch or subagent still running), note that in the handoff instead of stopping the work. Do not repeat this for the same state - one handoff per nudge."}\n' "$mb"
