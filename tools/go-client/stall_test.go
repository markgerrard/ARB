package main

import (
	"bytes"
	"testing"
)

func TestMaybePrintStallLineExactlyOncePerEpisode(t *testing.T) {
	var stderr bytes.Buffer
	state := stallPrintState{}
	status := map[string]string{
		"task_id":    "task-1",
		"seat_id":    "codex-bridge-dev",
		"stalled_at": "2026-07-07T12:00:00+00:00",
	}

	maybePrintStallLine(&stderr, &state, status)
	maybePrintStallLine(&stderr, &state, status)

	want := "[stall] task task-1 seat codex-bridge-dev: no progress since 2026-07-07T12:00:00+00:00\n"
	if got := stderr.String(); got != want {
		t.Fatalf("stderr=%q want %q", got, want)
	}

	status["stalled_at"] = ""
	maybePrintStallLine(&stderr, &state, status)
	status["stalled_at"] = "2026-07-07T12:15:00+00:00"
	maybePrintStallLine(&stderr, &state, status)

	want += "[stall] task task-1 seat codex-bridge-dev: no progress since 2026-07-07T12:15:00+00:00\n"
	if got := stderr.String(); got != want {
		t.Fatalf("stderr after rearm=%q want %q", got, want)
	}
}

func TestFailedPollDoesNotClearOrDuplicateStall(t *testing.T) {
	// GO-1 (panel-confirmed): a transient poll error returned an empty map,
	// indistinguishable from "cleared", which reset lastPrinted and re-printed
	// the SAME stalled_at on the next successful poll — a duplicate line for one
	// still-ongoing episode.
	var stderr bytes.Buffer
	state := stallPrintState{}
	stalled := map[string]string{
		"task_id":    "task-1",
		"seat_id":    "codex-bridge-dev",
		"stalled_at": "2026-07-07T12:00:00+00:00",
	}

	maybePrintStallLineOK(&stderr, &state, stalled, true)     // prints once
	maybePrintStallLineOK(&stderr, &state, nil, false)        // poll FAILED — must not reset
	maybePrintStallLineOK(&stderr, &state, stalled, true)     // same episode, must stay silent

	want := "[stall] task task-1 seat codex-bridge-dev: no progress since 2026-07-07T12:00:00+00:00\n"
	if got := stderr.String(); got != want {
		t.Fatalf("stderr=%q want exactly one line %q", got, want)
	}
}

func TestMaybePrintStallLineSilentWhenNeverStalled(t *testing.T) {
	var stderr bytes.Buffer
	state := stallPrintState{}

	maybePrintStallLine(&stderr, &state, map[string]string{"task_id": "task-1"})
	maybePrintStallLine(&stderr, &state, map[string]string{"task_id": "task-1", "stalled_at": ""})

	if got := stderr.String(); got != "" {
		t.Fatalf("stderr=%q want empty", got)
	}
}
