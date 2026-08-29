package main

import (
	"testing"
	"time"
)

func TestReduceSeatTransitionsAndCopiesPrev(t *testing.T) {
	// A recent timestamp so the "running" transitions aren't masked by staleness
	// (a running seat older than staleGrace reduces to "stale" — pinned separately below).
	recent := time.Now().UTC().Format(time.RFC3339)
	prev := map[string]any{"task_id": "t1", "state": "done", "model": "gpt-5"}
	got := reduceSeat(prev, map[string]any{
		"task_id":      "t1",
		"seat_id":      "codex-1",
		"run_id":       "run-1",
		"orchestrator": "orch-1",
		"event_type":   "task_started",
		"sent_at":      recent,
	})
	if prev["state"] != "done" {
		t.Fatalf("reduceSeat mutated prev: %#v", prev)
	}
	if got["state"] != "running" || got["last_event"] != "task_started" || got["last_event_ts"] != recent {
		t.Fatalf("started state mismatch: %#v", got)
	}
	if got["seat_id"] != "codex-1" || got["run_id"] != "run-1" || got["orchestrator"] != "orch-1" || got["model"] != "gpt-5" {
		t.Fatalf("carried fields mismatch: %#v", got)
	}

	continuing := reduceSeat(got, map[string]any{"event_type": "task_continuing", "sent_at": recent})
	if continuing["state"] != "running" || continuing["last_event"] != "task_continuing" {
		t.Fatalf("continuing mismatch: %#v", continuing)
	}

	done := reduceSeat(got, map[string]any{"event_type": "task_finished", "data": map[string]any{"ok": true}})
	if done["state"] != "done" {
		t.Fatalf("done mismatch: %#v", done)
	}

	failed := reduceSeat(got, map[string]any{"event_type": "task_finished", "data": map[string]any{"ok": false}})
	if failed["state"] != "failed" {
		t.Fatalf("failed mismatch: %#v", failed)
	}

	voted := reduceSeat(got, map[string]any{"event_type": "vote", "data": map[string]any{"stance": "approve"}})
	if voted["state"] != "voted" || voted["voted"] != true || voted["stance"] != "approve" {
		t.Fatalf("vote mismatch: %#v", voted)
	}

	terminalVote := reduceSeat(done, map[string]any{"event_type": "vote", "data": map[string]any{"stance": "reject"}})
	if terminalVote["state"] != "done" || terminalVote["stance"] != "reject" {
		t.Fatalf("terminal vote mismatch: %#v", terminalVote)
	}

	// Stale via the tick path (empty entry refresh).
	stale := reduceSeat(map[string]any{
		"task_id":       "t-old",
		"state":         "running",
		"last_event_ts": "2000-01-01T00:00:00+00:00",
	}, map[string]any{})
	if stale["state"] != "stale" {
		t.Fatalf("stale mismatch: %#v", stale)
	}

	// Stale via the event path: a task_started whose timestamp is already past staleGrace
	// reduces straight to "stale" (this is what the old fixed-timestamp fixture tripped once
	// wall-clock moved past it — the running transition is only valid for a recent event).
	staleStart := reduceSeat(map[string]any{"task_id": "t1"}, map[string]any{
		"event_type": "task_started",
		"sent_at":    "2000-01-01T00:00:00+00:00",
	})
	if staleStart["state"] != "stale" {
		t.Fatalf("stale-on-start mismatch: %#v", staleStart)
	}
}

func TestReduceSeatCarriesStalledAtAndClearsWhenAbsent(t *testing.T) {
	recent := time.Now().UTC().Format(time.RFC3339)
	stalled := reduceSeat(map[string]any{}, map[string]any{
		"task_id":      "t1",
		"seat_id":      "codex-1",
		"event_type":   "stall_detected",
		"sent_at":      recent,
		"stalled_at":   "2026-07-07T12:00:00+00:00",
		"orchestrator": "orch-1",
	})
	if stalled["state"] != nil {
		t.Fatalf("stall event must not force terminal state: %#v", stalled)
	}
	if stalled["stalled_at"] != "2026-07-07T12:00:00+00:00" {
		t.Fatalf("missing stalled_at: %#v", stalled)
	}

	cleared := reduceSeat(stalled, map[string]any{
		"task_id":    "t1",
		"seat_id":    "codex-1",
		"event_type": "command_output",
		"sent_at":    recent,
	})
	if _, exists := cleared["stalled_at"]; exists {
		t.Fatalf("stalled_at survived absent-field refresh: %#v", cleared)
	}
}

func TestAgentOf(t *testing.T) {
	tests := map[string]string{
		"codex-ff-demo":     "codex",
		"agy-bridge-dev":    "agy",
		"pi-glm":            "pi",
		"cold-opus-1":       "opus",
		"claude-bridge-dev": "claude",
		"custom-seat":       "custom",
		"":                  "?",
	}
	for input, want := range tests {
		if got := agentOf(input); got != want {
			t.Fatalf("agentOf(%q)=%q want %q", input, got, want)
		}
	}
}

func TestFormatCommand(t *testing.T) {
	tests := map[string]string{
		"/bin/zsh -lc 'python3 fibonacci.py'": "Bash(python3 fibonacci.py)",
		"/bin/bash -c \"echo hi\"":            "Bash(echo hi)",
		"python3 x":                           "python3 x",
		"Read":                                "Read",
	}
	for input, want := range tests {
		if got := formatCommand(input); got != want {
			t.Fatalf("formatCommand(%q)=%q want %q", input, got, want)
		}
	}
}

func TestRenderTranscriptLineGoldenBranches(t *testing.T) {
	tests := []struct {
		name string
		ev   map[string]any
		want string
	}{
		{"model_text", map[string]any{"kind": "model_text", "content": " hi "}, "⏺ hi"},
		{"thinking", map[string]any{"kind": "model_thinking", "content": "pondering"}, "⏺ pondering"},
		{"thinking_empty", map[string]any{"kind": "model_thinking"}, "⏺ thinking"},
		{"command_started", map[string]any{"kind": "command_started", "tool_name": "/bin/zsh -lc 'ls'"}, "⏺ Bash(ls)"},
		{"tool_call_content", map[string]any{"kind": "tool_call", "content": "Read"}, "⏺ Read"},
		{"command_output", map[string]any{"kind": "command_output", "content": "a\nb\n"}, "  ⎿ a\n    b"},
		{"apply_patch", map[string]any{"tool_name": "apply_patch", "meta": map[string]any{"file": "f.py", "added": 3, "removed": 1}}, "⏺ Update(f.py) +3/-1"},
		{"apply_patch_content", map[string]any{"tool_name": "apply_patch", "content": "+x", "meta": map[string]any{"file": "f.py", "added": 3, "removed": 1}}, "⏺ Update(f.py) +3/-1\n  ⎿ +x"},
		{"unknown", map[string]any{"kind": "weird", "content": "z"}, "⏺ z"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := renderTranscriptLine(tt.ev); got != tt.want {
				t.Fatalf("renderTranscriptLine=%q want %q", got, tt.want)
			}
		})
	}
}

func TestAgeHelpersAndLifecycleNoise(t *testing.T) {
	if got := ageLabel(5); got != "5s" {
		t.Fatalf("ageLabel(5)=%q", got)
	}
	if got := ageLabel(90); got != "1m" {
		t.Fatalf("ageLabel(90)=%q", got)
	}
	if !isLifecycleNoise(map[string]any{"source": "eval"}) {
		t.Fatalf("eval should be lifecycle noise")
	}
	if isLifecycleNoise(map[string]any{"source": "transcript"}) {
		t.Fatalf("transcript should not be lifecycle noise")
	}
	if ageStyle(5, "running").GetFaint() != true {
		t.Fatalf("fresh running age should be faint")
	}
	if ageStyle(95, "running").GetBold() != true {
		t.Fatalf("old running age should be bold")
	}
	if ageStyle(95, "done").GetFaint() != true {
		t.Fatalf("terminal age should be faint")
	}
}
