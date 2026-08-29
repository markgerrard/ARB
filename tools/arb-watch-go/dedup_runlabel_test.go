package main

import (
	"strings"
	"testing"
)

// The run_id<->task_id dual-tee can register one seat under two task_ids sharing the same
// (seat_id, run_id) — the phantom duplicate row. It must collapse to one.
func TestVisibleSeatsCollapsesPhantomDuplicate(t *testing.T) {
	m, _, _ := testModel()
	m.view = "seats"
	m.seatOrder = []string{"task-real", "task-phantom"}
	m.seats = map[string]map[string]any{
		"task-real":    {"task_id": "task-real", "seat_id": "agy-bridge-dev", "run_id": "d6132bfd", "state": "done", "last_event_ts": "2026-06-29T20:00:00+00:00"},
		"task-phantom": {"task_id": "task-phantom", "seat_id": "agy-bridge-dev", "run_id": "d6132bfd", "state": "done", "last_event_ts": "2026-06-29T19:59:00+00:00"},
	}
	n := 0
	for _, id := range m.visibleSeats() {
		if getString(m.seats[id], "seat_id") == "agy-bridge-dev" {
			n++
		}
	}
	if n != 1 {
		t.Fatalf("phantom not collapsed: %d agy rows, want 1", n)
	}
}

// Same seat, DIFFERENT run_ids = real parallel/sequential work — must NOT collapse.
func TestVisibleSeatsKeepsDistinctRunsOnOneSeat(t *testing.T) {
	m, _, _ := testModel()
	m.view = "seats"
	m.seatOrder = []string{"t1", "t2"}
	m.seats = map[string]map[string]any{
		"t1": {"task_id": "t1", "seat_id": "codex-1", "run_id": "run-a", "state": "running"},
		"t2": {"task_id": "t2", "seat_id": "codex-1", "run_id": "run-b", "state": "running"},
	}
	if got := m.visibleSeats(); len(got) != 2 {
		t.Fatalf("distinct runs collapsed: %v want 2", got)
	}
}

// Tagged run: show the meaningful label AND a short task handle.
func TestRunDisplayLabelAndShortTask(t *testing.T) {
	got := runDisplay(map[string]any{"run_id": "arb-files-impl", "task_id": "d6132bfd-4c2c-4f8a"})
	if !strings.Contains(got, "arb-files-impl") || !strings.Contains(got, "d6132bfd") {
		t.Fatalf("runDisplay tagged = %q, want label + short task", got)
	}
	if strings.Contains(got, "4c2c") {
		t.Fatalf("runDisplay should shorten the task id to 8 chars: %q", got)
	}
}

// Un-tagged: run_id == task_id (the GUID fallback) — show the short id once, no doubling.
func TestRunDisplayUntaggedShowsShortOnce(t *testing.T) {
	guid := "d6132bfd-4c2c-4f8a-b3a5"
	got := runDisplay(map[string]any{"run_id": guid, "task_id": guid})
	if strings.Count(got, "d6132bfd") != 1 || strings.Contains(got, " · ") {
		t.Fatalf("runDisplay untagged = %q, want a single short id and no separator", got)
	}
}

// GO-4 (panel, 2026-07-08): a phantom dual-tee duplicate and a genuine sequential
// retry are INDISTINGUISHABLE by (seat_id, run_id) — both are two distinct task_ids
// sharing one run label. Collapsing phantoms therefore necessarily collapses a
// same-label retry too; the roster shows one row per (seat, run) and the recency
// sort makes it the LATEST attempt. This is intentional (making the key task-aware
// would defeat the phantom collapse). The older attempt's transcript still lives in
// the store; only the live roster row is deduped. This test pins that contract.
func TestVisibleSeatsSameRunLabelRetryShowsLatestAttemptOnly(t *testing.T) {
	m, _, _ := testModel()
	m.view = "seats"
	// Two attempts of the same tagged run on one seat, distinct task_ids.
	m.seatOrder = []string{"attempt-old", "attempt-new"}
	m.seats = map[string]map[string]any{
		"attempt-old": {"task_id": "attempt-old", "seat_id": "codex-1", "run_id": "arb-files-impl", "state": "failed", "last_event_ts": "2026-07-08T10:00:00+00:00"},
		"attempt-new": {"task_id": "attempt-new", "seat_id": "codex-1", "run_id": "arb-files-impl", "state": "running", "last_event_ts": "2026-07-08T10:05:00+00:00"},
	}
	got := m.visibleSeats()
	if len(got) != 1 {
		t.Fatalf("same-label retry not collapsed to one row: %v", got)
	}
	if got[0] != "attempt-new" {
		t.Fatalf("collapsed row should be the LATEST attempt, got %q", got[0])
	}
}
