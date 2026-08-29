package main

import (
	"encoding/json"
	"fmt"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/charmbracelet/bubbles/viewport"
	tea "github.com/charmbracelet/bubbletea"
)

type streamCall struct {
	which  string
	url    string
	lastID string
}

// testModel injects a fake startStream that records calls and, like realStartStream,
// sets the per-stream url/gen/cancel so the model behaves consistently in tests.
func testModel() (*model, *[]streamCall, map[string]bool) {
	calls := []streamCall{}
	cancelled := map[string]bool{}
	m := &model{
		baseURL:      "http://visibility",
		token:        "token-1",
		view:         "orchestrators",
		seats:        map[string]map[string]any{},
		vp:           viewport.New(80, 20),
		orch:         streamState{backoff: baseBackoff},
		seat:         streamState{backoff: baseBackoff},
		seatSource:   "live",
		statusFilter: filterAll,
		agentFilter:  filterAll,
		focus:        focusSeats,
	}
	m.startStream = func(m *model, which, url, lastID string) tea.Cmd {
		calls = append(calls, streamCall{which: which, url: url, lastID: lastID})
		s := m.stream(which)
		s.url = url
		s.gen++
		w := which
		s.cancel = func() { cancelled[w] = true }
		return nil
	}
	return m, &calls, cancelled
}

func TestModelOrchestratorSelectionStartsSeatListStream(t *testing.T) {
	m, calls, _ := testModel()
	next, _ := m.Update(orchestratorsMsg([]string{"o1", "o2"}))
	m = next.(*model)
	if len(m.orchestrators) != 2 || m.orchestrators[0] != "o1" {
		t.Fatalf("orchestrators=%#v", m.orchestrators)
	}

	next, _ = m.Update(tea.KeyMsg{Type: tea.KeyEnter})
	m = next.(*model)
	if m.view != "seats" || m.orchestrator != "o1" {
		t.Fatalf("view/orchestrator mismatch: %#v", m)
	}
	if len(*calls) != 1 || (*calls)[0].which != streamOrch || (*calls)[0].url != "http://visibility/sse/orchestrator/o1" {
		t.Fatalf("stream calls=%#v", *calls)
	}
}

func TestModelOrchFrameUpsertsSeatAndDropsStaleGeneration(t *testing.T) {
	m, _, _ := testModel()
	m.view = "seats"
	m.orch.gen = 2

	next, _ := m.Update(frameMsg{which: streamOrch, gen: 1, f: Frame{ID: "1-0", Event: "seat_appear", Data: map[string]any{"task_id": "stale", "seat_id": "old", "state": "running"}}})
	m = next.(*model)
	if len(m.seats) != 0 {
		t.Fatalf("stale frame mutated seats: %#v", m.seats)
	}

	next, _ = m.Update(frameMsg{which: streamOrch, gen: 2, f: Frame{ID: "2-0", Event: "seat_appear", Data: map[string]any{
		"task_id": "t1", "seat_id": "codex-1", "state": "running", "run_id": "run-1", "last_event_ts": "2026-06-26T10:00:00+00:00",
	}}})
	m = next.(*model)
	if len(m.seatOrder) != 1 || m.seatOrder[0] != "t1" || m.seats["t1"]["seat_id"] != "codex-1" {
		t.Fatalf("seat state mismatch order=%#v seats=%#v", m.seatOrder, m.seats)
	}
	if m.orch.lastID != "2-0" || m.orch.backoff != baseBackoff {
		t.Fatalf("lastID/backoff mismatch last=%q backoff=%s", m.orch.lastID, m.orch.backoff)
	}
}

func TestToggleHistoryModeFetchesPageOneAndParksLiveFrames(t *testing.T) {
	m, calls, _ := testModel()
	m.view = "seats"
	m.orchestrator = "orch-1"
	m.seatOrder = []string{"live-seat"}
	m.seats = map[string]map[string]any{"live-seat": {"task_id": "live-seat", "seat_id": "codex-1"}}
	m.orch.gen = 5

	next, cmd := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'h'}})
	m = next.(*model)
	if m.seatSource != "history" || !m.historyLoading {
		t.Fatalf("seatSource=%q historyLoading=%v", m.seatSource, m.historyLoading)
	}
	if cmd == nil {
		t.Fatal("expected a fetch command")
	}
	if len(*calls) != 0 {
		t.Fatalf("unexpected stream calls: %#v", *calls)
	}

	next, _ = m.Update(frameMsg{which: streamOrch, gen: 5, f: Frame{ID: "9-0", Event: "seat_appear",
		Data: map[string]any{"task_id": "should-not-appear", "seat_id": "codex-2"}}})
	m = next.(*model)
	if _, exists := m.seats["should-not-appear"]; exists {
		t.Fatalf("frameMsg mutated m.seats while parked: %#v", m.seats)
	}
	if len(m.seatOrder) != 1 || m.seatOrder[0] != "live-seat" {
		t.Fatalf("original live seat was disturbed: %#v", m.seatOrder)
	}
}

func TestExistingLiveFrameUpsertStillWorksWithSeatSourceDefaulted(t *testing.T) {
	m := newModel("http://visibility", "token-1", "")
	if m.seatSource != "live" {
		t.Fatalf("newModel() default seatSource=%q, want %q", m.seatSource, "live")
	}
}

func TestToggleHistoryModeBackToLiveClearsAndRestartsStream(t *testing.T) {
	m, calls, _ := testModel()
	m.view = "seats"
	m.orchestrator = "orch-1"
	m.seatSource = "history"
	m.historyCursor = "some-cursor"
	m.historyHasMore = true
	m.historyLoading = true
	m.historyGen = 3
	m.seatOrder = []string{"hist-seat"}
	m.seats = map[string]map[string]any{"hist-seat": {"task_id": "hist-seat"}}
	priorOrchGen := m.orch.gen

	next, _ := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'h'}})
	m = next.(*model)

	if m.seatSource != "live" {
		t.Fatalf("seatSource=%q, want live", m.seatSource)
	}
	if len(m.seats) != 0 || len(m.seatOrder) != 0 {
		t.Fatalf("history rows not cleared: seats=%#v order=%#v", m.seats, m.seatOrder)
	}
	if m.historyCursor != "" || m.historyHasMore || m.historyLoading {
		t.Fatalf("history fields not reset: cursor=%q hasMore=%v loading=%v",
			m.historyCursor, m.historyHasMore, m.historyLoading)
	}
	if len(*calls) != 1 || (*calls)[0].which != streamOrch {
		t.Fatalf("expected a fresh orch stream restart, calls=%#v", *calls)
	}
	if m.orch.gen == priorOrchGen {
		t.Fatal("orch stream gen did not advance")
	}
}

func TestHistoryStaleGenResponseIsDroppedOnToggleBackToLive(t *testing.T) {
	m, _, _ := testModel()
	m.view = "seats"
	m.orchestrator = "orch-1"
	m.seatSource = "history"
	m.historyGen = 1
	m.historyLoading = true
	staleGen := m.historyGen

	next, _ := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'h'}})
	m = next.(*model)
	if m.historyGen == staleGen {
		t.Fatal("historyGen did not bump on the history->live toggle")
	}

	next, _ = m.Update(historyPageMsg{gen: staleGen, seats: []map[string]any{{"task_id": "late"}}, append: true})
	m = next.(*model)
	if _, exists := m.seats["late"]; exists {
		t.Fatal("stale history page mutated the freshly-rebuilt live map")
	}
}

func TestHistoryStaleGenResponseIsDroppedAfterOrchestratorSwitch(t *testing.T) {
	m, _, _ := testModel()
	m.view = "seats"
	m.orchestrator = "orch-a"
	m.seatSource = "history"
	m.historyGen = 7
	staleGen := m.historyGen

	m.enterSeats("orch-b")
	if m.historyGen == staleGen {
		t.Fatal("historyGen did not bump on orchestrator switch")
	}

	next, _ := m.Update(historyPageMsg{gen: staleGen, seats: []map[string]any{{"task_id": "late-a"}}})
	m = next.(*model)
	if _, exists := m.seats["late-a"]; exists {
		t.Fatal("stale history page from previous orchestrator mutated the new view")
	}
}

func TestHistoryFirstPageReplacesWholesale(t *testing.T) {
	m, _, _ := testModel()
	m.seatOrder = []string{"old-live-seat"}
	m.seats = map[string]map[string]any{"old-live-seat": {"task_id": "old-live-seat"}}

	m.replaceSeatsWithHistory([]map[string]any{
		{"task_id": "h1", "seat_id": "codex-1", "state": "done"},
		{"task_id": "h2", "seat_id": "codex-2", "state": "incomplete"},
	})

	if _, exists := m.seats["old-live-seat"]; exists {
		t.Fatal("old live entry survived a wholesale replace")
	}
	if !reflect.DeepEqual(m.seatOrder, []string{"h1", "h2"}) {
		t.Fatalf("seatOrder=%#v, want [h1 h2] preserving server order", m.seatOrder)
	}
}

func TestHistoryAppendPageDedupsOnTaskID(t *testing.T) {
	m, _, _ := testModel()
	m.replaceSeatsWithHistory([]map[string]any{
		{"task_id": "h1", "seat_id": "codex-1", "state": "done"},
	})
	m.appendHistoryPage([]map[string]any{
		{"task_id": "h1", "seat_id": "codex-1", "state": "done"},
		{"task_id": "h2", "seat_id": "codex-2", "state": "incomplete"},
	})
	if !reflect.DeepEqual(m.seatOrder, []string{"h1", "h2"}) {
		t.Fatalf("seatOrder=%#v, want [h1 h2] (no duplicate h1)", m.seatOrder)
	}
	if len(m.seats) != 2 {
		t.Fatalf("seats=%#v, want 2 entries", m.seats)
	}
}

func TestHistoryRowRendersWithNoSpecialCasing(t *testing.T) {
	m, _, _ := testModel()
	m.view = "seats"
	m.width = 160
	raw := `{"task_id":"h1","run_id":"r1","seat_id":"codex-1","orchestrator":"orch-1","state":"incomplete","last_event":"task_started","last_event_ts":"2026-01-01T00:00:00+00:00"}`
	var seat map[string]any
	if err := json.Unmarshal([]byte(raw), &seat); err != nil {
		t.Fatal(err)
	}
	m.replaceSeatsWithHistory([]map[string]any{seat})
	out := m.renderSeatTable()
	if !strings.Contains(out, "codex-1") {
		t.Fatalf("rendered table missing history row:\n%s", out)
	}
}

func TestSeatTableRendersStallMarkerWhenStalledAtPresent(t *testing.T) {
	m, _, _ := testModel()
	m.view = "seats"
	m.width = 160
	m.height = 30
	m.seatOrder = []string{"t1"}
	m.seats = map[string]map[string]any{
		"t1": {
			"task_id":       "t1",
			"seat_id":       "codex-1",
			"state":         "running",
			"last_event_ts": "2026-07-07T12:00:00+00:00",
			"stalled_at":    "2026-07-07T12:00:00+00:00",
		},
	}

	out := m.renderSeatTable()

	if !strings.Contains(out, "STALL") {
		t.Fatalf("rendered table missing stall marker:\n%s", out)
	}
}

func TestSeatTableOmitsStallMarkerWhenStalledAtAbsent(t *testing.T) {
	m, _, _ := testModel()
	m.view = "seats"
	m.width = 160
	m.height = 30
	m.seatOrder = []string{"t1"}
	m.seats = map[string]map[string]any{
		"t1": {
			"task_id":       "t1",
			"seat_id":       "codex-1",
			"state":         "running",
			"last_event_ts": "2026-07-07T12:00:00+00:00",
		},
	}

	out := m.renderSeatTable()

	if strings.Contains(out, "STALL") {
		t.Fatalf("rendered table should not show stall marker:\n%s", out)
	}
}

func TestHistoryPageMsgReplacesOnFirstPageAppendsOnSubsequent(t *testing.T) {
	m, _, _ := testModel()
	m.seatSource = "history"
	m.historyGen = 1
	m.historyLoading = true

	next, _ := m.Update(historyPageMsg{gen: 1, seats: []map[string]any{{"task_id": "h1"}}, nextCursor: "c1", hasMore: true, append: false})
	m = next.(*model)
	if m.historyLoading || m.historyCursor != "c1" || !m.historyHasMore {
		t.Fatalf("post-page-1 state wrong: loading=%v cursor=%q hasMore=%v", m.historyLoading, m.historyCursor, m.historyHasMore)
	}
	if !reflect.DeepEqual(m.seatOrder, []string{"h1"}) {
		t.Fatalf("seatOrder=%#v", m.seatOrder)
	}

	m.historyLoading = true
	next, _ = m.Update(historyPageMsg{gen: 1, seats: []map[string]any{{"task_id": "h2"}}, nextCursor: "", hasMore: false, append: true})
	m = next.(*model)
	if m.historyLoading || m.historyHasMore || m.historyCursor != "" {
		t.Fatalf("post-page-2 state wrong: loading=%v hasMore=%v cursor=%q", m.historyLoading, m.historyHasMore, m.historyCursor)
	}
	if !reflect.DeepEqual(m.seatOrder, []string{"h1", "h2"}) {
		t.Fatalf("seatOrder=%#v, want both pages", m.seatOrder)
	}
}

func TestHistoryStaleGenResponseIsDropped(t *testing.T) {
	m, _, _ := testModel()
	m.seatSource = "history"
	m.historyGen = 2
	m.seatOrder = []string{"kept"}
	m.seats = map[string]map[string]any{"kept": {"task_id": "kept"}}

	next, _ := m.Update(historyPageMsg{gen: 1, seats: []map[string]any{{"task_id": "stale"}}})
	m = next.(*model)
	if !reflect.DeepEqual(m.seatOrder, []string{"kept"}) {
		t.Fatalf("stale-gen page mutated state: %#v", m.seatOrder)
	}
}

func TestHistoryFetchFailureKeepsLoadedRowsAndClearsLoading(t *testing.T) {
	m, _, _ := testModel()
	m.seatSource = "history"
	m.historyGen = 1
	m.historyLoading = true
	m.seatOrder = []string{"already-loaded"}
	m.seats = map[string]map[string]any{"already-loaded": {"task_id": "already-loaded"}}

	next, _ := m.Update(historyErrMsg{gen: 1, err: fmt.Errorf("boom")})
	m = next.(*model)
	if m.historyLoading {
		t.Fatal("historyLoading not cleared after a fetch failure")
	}
	if m.status == "" {
		t.Fatal("m.status not set on fetch failure")
	}
	if !reflect.DeepEqual(m.seatOrder, []string{"already-loaded"}) {
		t.Fatalf("already-loaded rows were disturbed: %#v", m.seatOrder)
	}

	m.historyGen = 2
	m.historyLoading = true
	next, _ = m.Update(historyErrMsg{gen: 1, err: fmt.Errorf("late boom")})
	m = next.(*model)
	if !m.historyLoading {
		t.Fatal("stale-gen error incorrectly cleared historyLoading")
	}
}

func TestIncompleteStateRendersStyledAndIsFilterable(t *testing.T) {
	m, _, _ := testModel()
	m.view = "seats"
	m.width = 160
	m.seatOrder = []string{"h1"}
	m.seats = map[string]map[string]any{
		"h1": {"task_id": "h1", "seat_id": "codex-1", "state": "incomplete", "last_event_ts": "2026-01-01T00:00:00+00:00"},
	}
	rendered := m.renderSeatTable()
	if !strings.Contains(rendered, "incomplete") {
		t.Fatalf("state not rendered:\n%s", rendered)
	}
	if _, ok := stateColors["incomplete"]; !ok {
		t.Fatal("incomplete missing from stateColors")
	}

	found := false
	for _, s := range statusCycle {
		if s == "incomplete" {
			found = true
		}
	}
	if !found {
		t.Fatal("incomplete missing from statusCycle")
	}
	m.statusFilter = "incomplete"
	if got := m.visibleSeats(); len(got) != 1 || got[0] != "h1" {
		t.Fatalf("status filter did not narrow to the incomplete seat: %#v", got)
	}
}

func TestEffectiveStateNeverReclassifiesIncompleteAsStale(t *testing.T) {
	seat := map[string]any{
		"task_id": "h1", "state": "incomplete",
		"last_event_ts": "2020-01-01T00:00:00+00:00",
	}
	if got := effectiveState(seat); got != "incomplete" {
		t.Fatalf("effectiveState=%q, want incomplete (never stale)", got)
	}
}

func TestHistoryScrollTriggersNextPageAtListEnd(t *testing.T) {
	m, _, _ := testModel()
	m.view = "seats"
	m.seatSource = "history"
	m.historyGen = 4
	m.historyHasMore = true
	m.historyCursor = "cursor-abc"
	m.seatOrder = []string{"h1", "h2"}
	m.seats = map[string]map[string]any{
		"h1": {"task_id": "h1", "seat_id": "codex-1"},
		"h2": {"task_id": "h2", "seat_id": "codex-2"},
	}
	m.seatCursor = 1

	next, cmd := m.Update(tea.KeyMsg{Type: tea.KeyDown})
	m = next.(*model)
	if cmd == nil {
		t.Fatal("expected a batched command at the list end with more history available")
	}
	if !m.historyLoading {
		t.Fatal("historyLoading was not set when the next page was dispatched")
	}
}

func TestHistoryScrollAtEndWithNoMoreDoesNotFetch(t *testing.T) {
	m, _, _ := testModel()
	m.view = "seats"
	m.seatSource = "history"
	m.historyHasMore = false
	m.seatOrder = []string{"h1"}
	m.seats = map[string]map[string]any{"h1": {"task_id": "h1", "seat_id": "codex-1"}}
	m.seatCursor = 0

	m.Update(tea.KeyMsg{Type: tea.KeyDown})
	if m.historyLoading {
		t.Fatal("historyLoading should not be set — historyHasMore is false")
	}
}

func TestMaybeFetchNextHistoryPageNoOpWhenNotInHistoryModeOrAlreadyLoading(t *testing.T) {
	m, _, _ := testModel()
	m.seatSource = "live"
	if cmd := m.maybeFetchNextHistoryPage(); cmd != nil {
		t.Fatal("should be a no-op outside history mode")
	}
	m.seatSource = "history"
	m.historyHasMore = true
	m.historyLoading = true
	if cmd := m.maybeFetchNextHistoryPage(); cmd != nil {
		t.Fatal("should be a no-op while a fetch is already in flight")
	}
}

func TestModelSeatSelectionAndTranscriptFiltering(t *testing.T) {
	m, calls, _ := testModel()
	m.view = "seats"
	m.seatOrder = []string{"t1"}
	m.seats["t1"] = map[string]any{"task_id": "t1", "seat_id": "codex-1", "state": "running"}

	next, _ := m.Update(tea.KeyMsg{Type: tea.KeyEnter})
	m = next.(*model)
	if m.selectedTask != "t1" {
		t.Fatalf("selected=%q", m.selectedTask)
	}
	if len(*calls) != 1 || (*calls)[0].which != streamSeat || (*calls)[0].url != "http://visibility/sse/seat/t1" {
		t.Fatalf("stream calls=%#v", *calls)
	}

	next, _ = m.Update(frameMsg{which: streamSeat, gen: m.seat.gen, f: Frame{ID: "e=1-0;t=2-0", Event: "event", Data: map[string]any{"source": "eval", "event_type": "task_started"}}})
	m = next.(*model)
	if len(m.transcript) != 0 {
		t.Fatalf("eval frame should be filtered: %#v", m.transcript)
	}

	next, _ = m.Update(frameMsg{which: streamSeat, gen: m.seat.gen, f: Frame{ID: "e=1-0;t=3-0", Event: "transcript", Data: map[string]any{"source": "transcript", "kind": "model_text", "content": "hi"}}})
	m = next.(*model)
	if len(m.transcript) != 1 || renderTranscriptLine(m.transcript[0]) != "⏺ hi" {
		t.Fatalf("transcript=%#v", m.transcript)
	}
	if m.seat.lastID != "e=1-0;t=3-0" {
		t.Fatalf("lastID=%q", m.seat.lastID)
	}
}

// TestSeatSelectionKeepsOrchestratorStreamLive is the dual-stream regression guard,
// mirroring Python test_selecting_seat_does_not_cancel_orchestrator_stream: selecting a
// seat must NOT cancel/restart the orchestrator stream, and orchestrator frames must keep
// updating the seat list while a seat is open.
func TestSeatSelectionKeepsOrchestratorStreamLive(t *testing.T) {
	m, calls, cancelled := testModel()
	m.view = "seats"
	m.startStream(m, streamOrch, "http://visibility/sse/orchestrator/o1", "") // orch stream running
	orchGen := m.orch.gen
	*calls = (*calls)[:0]
	m.seatOrder = []string{"t1"}
	m.seats["t1"] = map[string]any{"task_id": "t1", "seat_id": "codex-1", "state": "running"}

	next, _ := m.Update(tea.KeyMsg{Type: tea.KeyEnter}) // drill into the seat
	m = next.(*model)
	if cancelled[streamOrch] {
		t.Fatal("selecting a seat cancelled the orchestrator stream (regression)")
	}
	if m.orch.gen != orchGen {
		t.Fatalf("orchestrator stream was restarted (gen %d -> %d)", orchGen, m.orch.gen)
	}
	if len(*calls) != 1 || (*calls)[0].which != streamSeat {
		t.Fatalf("expected only a seat stream start, got %#v", *calls)
	}

	// An orchestrator frame still updates the seat list while the seat transcript is open.
	next, _ = m.Update(frameMsg{which: streamOrch, gen: m.orch.gen, f: Frame{Data: map[string]any{
		"task_id": "t2", "seat_id": "codex-2", "state": "running"}}})
	m = next.(*model)
	if _, ok := m.seats["t2"]; !ok {
		t.Fatal("orchestrator frame did not update the seat list while a seat was open (regression)")
	}
}

// A 4xx (auth / not-found) must stop the stream for good — Python breaks its reconnect
// loop on 4xx; Go marks the stream fatal so the close handler does not reconnect.
func TestModelFatalFrameSuppressesReconnect(t *testing.T) {
	m, calls, _ := testModel()
	m.view = "seats"
	m.startStream(m, streamSeat, "http://visibility/sse/seat/t1", "") // running
	*calls = (*calls)[:0]

	next, cmd := m.Update(frameMsg{which: streamSeat, gen: m.seat.gen, f: Frame{Event: streamFatalEvent, Data: map[string]any{"status": float64(403)}}})
	m = next.(*model)
	if !m.seat.fatal {
		t.Fatal("fatal frame did not mark the stream fatal")
	}
	if cmd != nil {
		t.Fatal("fatal frame should stop listening (no further cmd)")
	}
	if m.status == "" {
		t.Fatal("fatal frame should surface a status message")
	}

	// The channel close that follows must NOT trigger a reconnect.
	next, cmd = m.Update(streamClosedMsg{which: streamSeat, gen: m.seat.gen})
	m = next.(*model)
	if cmd != nil || len(*calls) != 0 {
		t.Fatalf("fatal stream reconnected: cmd=%v calls=%#v", cmd != nil, *calls)
	}
}

func TestCollapseOutputTruncatesToolOutputUnlessExpanded(t *testing.T) {
	longBody := strings.Repeat("line\n", 20) // 20 lines of command output
	output := map[string]any{"kind": "command_output", "content": longBody}
	rendered := styledTranscriptLine(output)

	collapsed := collapseOutput(output, rendered, false)
	if n := strings.Count(collapsed, "\n") + 1; n != maxCollapsedOutputLines+1 {
		t.Fatalf("collapsed line count = %d, want %d (kept + hint)", n, maxCollapsedOutputLines+1)
	}
	if !strings.Contains(collapsed, "e to expand") {
		t.Fatalf("collapsed output missing expand hint: %q", collapsed)
	}

	if full := collapseOutput(output, rendered, true); full != rendered {
		t.Fatal("expanded=true must not truncate")
	}

	// Model prose is never collapsed, even when long.
	prose := map[string]any{"kind": "model_text", "content": longBody}
	proseRendered := styledTranscriptLine(prose)
	if got := collapseOutput(prose, proseRendered, false); got != proseRendered {
		t.Fatal("model_text must never collapse")
	}

	// Short output is left intact.
	short := map[string]any{"kind": "command_output", "content": "ok\ndone"}
	shortRendered := styledTranscriptLine(short)
	if got := collapseOutput(short, shortRendered, false); got != shortRendered {
		t.Fatal("short output should not be truncated")
	}
}

func TestSeatPaneFiltersNarrowDisplayAndNavigation(t *testing.T) {
	m, _, _ := testModel()
	m.view = "seats"
	m.seatOrder = []string{"t1", "t2", "t3"}
	m.seats = map[string]map[string]any{
		"t1": {"task_id": "t1", "seat_id": "codex-1", "state": "running"},
		"t2": {"task_id": "t2", "seat_id": "agy-1", "state": "done"},
		"t3": {"task_id": "t3", "seat_id": "codex-2", "state": "running"},
	}

	// No filter → all visible.
	if got := m.visibleSeats(); len(got) != 3 {
		t.Fatalf("unfiltered visibleSeats = %v, want 3", got)
	}

	// 's' cycles status all→running; only the two running seats remain, order preserved.
	next, _ := m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'s'}})
	m = next.(*model)
	if m.statusFilter != "running" {
		t.Fatalf("statusFilter=%q want running", m.statusFilter)
	}
	if got := m.visibleSeats(); len(got) != 2 || got[0] != "t1" || got[1] != "t3" {
		t.Fatalf("status-filtered visibleSeats = %v, want [t1 t3]", got)
	}

	// 'a' cycles agent all→agy (sorted: agy, codex). agy has only t2, which is 'done',
	// so combined with the running-status filter nothing matches.
	next, _ = m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'a'}})
	m = next.(*model)
	if m.agentFilter != "agy" {
		t.Fatalf("agentFilter=%q want agy", m.agentFilter)
	}
	if got := m.visibleSeats(); len(got) != 0 {
		t.Fatalf("running+agy visibleSeats = %v, want none", got)
	}
	if m.seatCursor != 0 { // clamped, never negative or out of range
		t.Fatalf("seatCursor=%d want clamped to 0", m.seatCursor)
	}

	// Cursor stays in range of the visible list after filtering.
	m.statusFilter, m.agentFilter = filterAll, "codex"
	if got := m.visibleSeats(); len(got) != 2 || got[0] != "t1" || got[1] != "t3" {
		t.Fatalf("agent=codex visibleSeats = %v, want [t1 t3]", got)
	}

	// statusCycle wraps back to "all".
	last := statusCycle[len(statusCycle)-1]
	if cycleNext(statusCycle, last) != filterAll {
		t.Fatalf("statusCycle did not wrap to all from %q", last)
	}
}

func TestParseRange(t *testing.T) {
	cases := []struct {
		in   string
		max  int
		a, b int
		ok   bool
	}{
		{"10-20", 30, 10, 20, true},
		{"12", 30, 12, 12, true},
		{"10-", 30, 10, 30, true},    // to end
		{"-5", 30, 1, 5, true},       // from start
		{"25-100", 30, 25, 30, true}, // clamp high to max
		{" 3 - 7 ", 30, 3, 7, true},  // tolerate spaces
		{"0", 30, 0, 0, false},       // below 1
		{"20-10", 30, 0, 0, false},   // inverted
		{"abc", 30, 0, 0, false},
		{"", 30, 0, 0, false},
		{"100", 30, 0, 0, false}, // single line past end
		{"5", 0, 0, 0, false},    // empty transcript
	}
	for _, c := range cases {
		a, b, ok := parseRange(c.in, c.max)
		if ok != c.ok || a != c.a || b != c.b {
			t.Fatalf("parseRange(%q,%d) = (%d,%d,%v), want (%d,%d,%v)", c.in, c.max, a, b, ok, c.a, c.b, c.ok)
		}
	}
}

func TestCopyModeNumbersAndCopiesVisualLines(t *testing.T) {
	m, _, _ := testModel()
	m.view = "seats"
	m.selectedTask = "t1"
	m.transcript = []map[string]any{
		{"kind": "model_text", "content": "alpha"},
		{"kind": "model_text", "content": "beta"},
		{"kind": "model_text", "content": "gamma"},
	}
	// Visual lines (blank separators between entries): 1 "⏺ alpha" 2 "" 3 "⏺ beta" 4 "" 5 "⏺ gamma".
	if lines := m.transcriptPlainLines(); len(lines) != 5 || lines[0] != "⏺ alpha" || lines[2] != "⏺ beta" || lines[4] != "⏺ gamma" {
		t.Fatalf("visual lines = %#v", lines)
	}

	m.enterCopyMode()
	if !m.copyMode {
		t.Fatal("ctrl+c did not enter copy mode")
	}
	if !strings.Contains(m.vp.View(), "│") {
		t.Fatal("numbered gutter missing from copy-mode viewport")
	}

	for _, r := range "3-5" {
		m.handleCopyKey(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{r}})
	}
	if m.copyInput != "3-5" {
		t.Fatalf("copyInput=%q", m.copyInput)
	}
	text, a, b, ok := m.selectedCopyText()
	if !ok || a != 3 || b != 5 || text != "⏺ beta\n\n⏺ gamma" {
		t.Fatalf("selection a=%d b=%d ok=%v text=%q", a, b, ok, text)
	}

	// Backspace edits the range; Esc cancels and leaves copy mode.
	m.handleCopyKey(tea.KeyMsg{Type: tea.KeyBackspace})
	if m.copyInput != "3-" {
		t.Fatalf("after backspace copyInput=%q", m.copyInput)
	}
	m.handleCopyKey(tea.KeyMsg{Type: tea.KeyEsc})
	if m.copyMode {
		t.Fatal("Esc did not exit copy mode")
	}

	// Ctrl+C with no transcript is a no-op (does not quit, does not enter copy mode).
	empty, _, _ := testModel()
	empty.view = "seats"
	empty.enterCopyMode()
	if empty.copyMode {
		t.Fatal("copy mode entered with no transcript")
	}
}

func TestPaneFocusRoutesArrowKeys(t *testing.T) {
	m, _, _ := testModel()
	m.view = "seats"
	m.seatOrder = []string{"t1", "t2"}
	m.seats = map[string]map[string]any{
		"t1": {"task_id": "t1", "seat_id": "codex-1", "state": "running"},
		"t2": {"task_id": "t2", "seat_id": "codex-2", "state": "running"},
	}
	m.vp = viewport.New(40, 3)

	// Default focus = seats: 'down' moves the seat cursor.
	next, _ := m.Update(tea.KeyMsg{Type: tea.KeyDown})
	m = next.(*model)
	if m.seatCursor != 1 || m.focus != focusSeats {
		t.Fatalf("seats focus: cursor=%d focus=%q", m.seatCursor, m.focus)
	}

	// Right arrow focuses the transcript (selecting the cursor seat first).
	next, _ = m.Update(tea.KeyMsg{Type: tea.KeyRight})
	m = next.(*model)
	if m.focus != focusTranscript || m.selectedTask == "" {
		t.Fatalf("right arrow: focus=%q selected=%q", m.focus, m.selectedTask)
	}

	// Load a tall transcript; with transcript focus, 'down' scrolls and leaves the cursor put.
	m.transcript = nil
	for i := 0; i < 20; i++ {
		m.transcript = append(m.transcript, map[string]any{"kind": "model_text", "content": "row"})
	}
	m.renderTranscript()
	m.vp.GotoTop()
	cursorBefore, y0 := m.seatCursor, m.vp.YOffset
	next, _ = m.Update(tea.KeyMsg{Type: tea.KeyDown})
	m = next.(*model)
	if m.seatCursor != cursorBefore {
		t.Fatal("down with transcript focus moved the seat cursor")
	}
	if m.vp.YOffset <= y0 {
		t.Fatalf("down with transcript focus did not scroll (y %d->%d)", y0, m.vp.YOffset)
	}

	// Shift+Down pages further than a single line.
	yLine := m.vp.YOffset
	next, _ = m.Update(tea.KeyMsg{Type: tea.KeyShiftDown})
	m = next.(*model)
	if m.vp.YOffset <= yLine {
		t.Fatalf("shift+down did not page (y %d->%d)", yLine, m.vp.YOffset)
	}

	// Left arrow returns focus to the seat list.
	next, _ = m.Update(tea.KeyMsg{Type: tea.KeyLeft})
	m = next.(*model)
	if m.focus != focusSeats {
		t.Fatalf("left arrow: focus=%q", m.focus)
	}

	// Fullscreen makes the transcript active regardless of stored focus.
	m.fullscreen = true
	if !m.transcriptActive() {
		t.Fatal("fullscreen should make the transcript active")
	}
}

func TestModelMenuStopsBothStreamsAndReconnectUsesLastID(t *testing.T) {
	m, calls, cancelled := testModel()
	m.view = "seats"
	m.orchestrator = "o1"
	m.orch.url = "http://visibility/sse/orchestrator/o1"
	m.orch.gen = 3
	m.orch.lastID = "e=5-0;t=9-0"
	m.orch.backoff = baseBackoff
	m.orch.cancel = func() { cancelled[streamOrch] = true }

	next, _ := m.Update(streamClosedMsg{which: streamOrch, gen: 2})
	m = next.(*model)
	if len(*calls) != 0 {
		t.Fatalf("stale stream close reconnected: %#v", *calls)
	}

	// Current-gen close GATES the reconnect behind the backoff (no immediate reconnect).
	next, cmd := m.Update(streamClosedMsg{which: streamOrch, gen: 3})
	m = next.(*model)
	if len(*calls) != 0 {
		t.Fatalf("streamClosed reconnected immediately (should gate behind backoff): %#v", *calls)
	}
	if cmd == nil {
		t.Fatal("streamClosed did not schedule a reconnect cmd")
	}
	if m.orch.backoff != time.Second {
		t.Fatalf("backoff=%s want 1s (doubled)", m.orch.backoff)
	}
	// The reconnect fires when the backoff tick lands, resuming from lastID.
	next, _ = m.Update(reconnectMsg{which: streamOrch, gen: 3})
	m = next.(*model)
	if len(*calls) != 1 || (*calls)[0].lastID != "e=5-0;t=9-0" || (*calls)[0].which != streamOrch {
		t.Fatalf("reconnect calls=%#v", *calls)
	}
	// A stale reconnect tick (stream switched since) is dropped.
	next, _ = m.Update(reconnectMsg{which: streamOrch, gen: 99})
	m = next.(*model)
	if len(*calls) != 1 {
		t.Fatalf("stale reconnect fired: %#v", *calls)
	}

	// 'm' returns to the fleet menu and stops BOTH streams.
	next, _ = m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{'m'}})
	m = next.(*model)
	if !cancelled[streamOrch] {
		t.Fatal("menu did not cancel the orchestrator stream")
	}
	if m.view != "orchestrators" || m.selectedTask != "" || len(m.seats) != 0 {
		t.Fatalf("menu reset mismatch: %#v", m)
	}
}

func TestVisibleSeatsOrderByMostRecentActivity(t *testing.T) {
	m, _, _ := testModel()
	m.view = "seats"
	// insertion order t1,t2,t3; activity makes t2 newest, t1 oldest, t3 has no timestamp.
	m.seatOrder = []string{"t1", "t2", "t3"}
	m.seats = map[string]map[string]any{
		"t1": {"task_id": "t1", "seat_id": "codex-1", "state": "running", "last_event_ts": "2026-06-26T10:00:00+00:00"},
		"t2": {"task_id": "t2", "seat_id": "codex-2", "state": "running", "last_event_ts": "2026-06-26T10:05:00+00:00"},
		"t3": {"task_id": "t3", "seat_id": "agy-1", "state": "running"},
	}
	got := m.visibleSeats()
	// most-recent first (t2 then t1), the no-timestamp seat (t3) last.
	if len(got) != 3 || got[0] != "t2" || got[1] != "t1" || got[2] != "t3" {
		t.Fatalf("activity order = %v, want [t2 t1 t3]", got)
	}
}

func TestVisibleSeatsIsolatesClaudeOrchestratorAfterRunSeats(t *testing.T) {
	m, _, _ := testModel()
	m.view = "seats"
	m.width = 160
	now := time.Now().UTC()
	m.seatOrder = []string{"warm", "old", "new", "cold"}
	m.seats = map[string]map[string]any{
		"warm": {"task_id": "warm", "seat_id": "claude-bridge-dev", "state": "running", "run_id": "run-orch", "last_event_ts": now.Format(time.RFC3339Nano)},
		"old":  {"task_id": "old", "seat_id": "codex-1", "state": "running", "run_id": "run-1", "last_event_ts": now.Add(-2 * time.Minute).Format(time.RFC3339Nano)},
		"new":  {"task_id": "new", "seat_id": "agy-1", "state": "running", "run_id": "run-1", "last_event_ts": now.Add(-1 * time.Minute).Format(time.RFC3339Nano)},
		"cold": {"task_id": "cold", "seat_id": "cold-opus-1", "state": "running", "run_id": "run-1", "last_event_ts": now.Add(-3 * time.Minute).Format(time.RFC3339Nano)},
	}

	got := m.visibleSeats()
	if len(got) != 4 || got[0] != "new" || got[1] != "old" || got[2] != "cold" || got[3] != "warm" {
		t.Fatalf("visibleSeats = %v, want [new old cold warm]", got)
	}

	rendered := m.renderSeatTable()
	divider := "── orchestrator ──"
	if !strings.Contains(rendered, divider) {
		t.Fatalf("rendered seat table missing orchestrator divider:\n%s", rendered)
	}
	if strings.Index(rendered, divider) > strings.Index(rendered, "claude-bridge-dev") {
		t.Fatalf("orchestrator divider must appear before warm claude row:\n%s", rendered)
	}
	if strings.Index(rendered, divider) < strings.Index(rendered, "cold-opus-1") {
		t.Fatalf("cold opus reviewer must remain above orchestrator divider:\n%s", rendered)
	}
}

func TestCursorFollowsSelectedSeatAcrossReorder(t *testing.T) {
	m, _, _ := testModel()
	m.view = "seats"
	m.seatOrder = []string{"t1", "t2"}
	m.seats = map[string]map[string]any{
		"t1": {"task_id": "t1", "seat_id": "codex-1", "state": "running", "last_event_ts": "2026-06-26T10:00:00+00:00"},
		"t2": {"task_id": "t2", "seat_id": "codex-2", "state": "running", "last_event_ts": "2026-06-26T10:01:00+00:00"},
	}
	// t2 is newer -> visibleSeats = [t2, t1]; the user has selected t1 (index 1).
	m.selectedTask = "t1"
	m.seatCursor = 1
	if got := m.visibleSeats(); got[1] != "t1" {
		t.Fatalf("precondition: t1 should be at index 1, got %v", got)
	}
	// t1 now gets the freshest activity -> reorders to the top.
	m.seats["t1"]["last_event_ts"] = "2026-06-26T10:02:00+00:00"
	m.syncCursorToSelection()
	if got := m.visibleSeats(); got[0] != "t1" {
		t.Fatalf("t1 should be newest at top, got %v", got)
	}
	if m.seatCursor != 0 {
		t.Fatalf("cursor should follow selected t1 to index 0, got %d", m.seatCursor)
	}
}

func TestSyncCursorNoSelectionIsNoop(t *testing.T) {
	m, _, _ := testModel()
	m.view = "seats"
	m.seatOrder = []string{"t1", "t2"}
	m.seats = map[string]map[string]any{
		"t1": {"task_id": "t1", "seat_id": "codex-1", "state": "running"},
		"t2": {"task_id": "t2", "seat_id": "codex-2", "state": "running"},
	}
	m.selectedTask = ""
	m.seatCursor = 1
	m.syncCursorToSelection() // nothing selected -> cursor untouched
	if m.seatCursor != 1 {
		t.Fatalf("no-selection sync must not move cursor, got %d", m.seatCursor)
	}
}

func TestCopyCountsWrappedLineAsOneLogicalLine(t *testing.T) {
	m, _, _ := testModel()
	m.vp.Width = 20 // narrow: the entry would wrap on display, but copy must count it as 1
	m.transcript = []map[string]any{
		{"kind": "model_text", "content": "alpha beta gamma delta epsilon zeta eta theta"},
	}
	if plain := m.transcriptPlainLines(); len(plain) != 1 {
		t.Fatalf("a wrapped entry must count as 1 logical line for copy, got %#v", plain)
	}
	if n := len(m.transcriptStyledLines()); n != 1 {
		t.Fatalf("styled copy lines must also be 1 (alignment), got %d", n)
	}
}

func TestCopyRangeExtractsCleanLogicalLines(t *testing.T) {
	m, _, _ := testModel()
	m.vp.Width = 20
	m.selectedTask = "t1"
	m.transcript = []map[string]any{
		{"kind": "model_text", "content": "alpha beta gamma delta epsilon zeta"}, // would wrap on display
		{"kind": "model_text", "content": "second"},
	}
	// visual lines: 1="⏺ alpha…" 2="" 3="⏺ second"  (logical, unwrapped → clean copy)
	plain := m.transcriptPlainLines()
	if plain[0] != "⏺ alpha beta gamma delta epsilon zeta" {
		t.Fatalf("logical line 1 must be the unwrapped full text, got %q", plain[0])
	}
	m.copyInput = "1-1"
	if got, _, _, ok := m.selectedCopyText(); !ok || got != "⏺ alpha beta gamma delta epsilon zeta" {
		t.Fatalf("copy of line 1 must be the clean unwrapped line, got %q", got)
	}
}

func TestDisplayWrapsLongLineToWidth(t *testing.T) {
	lines := strings.Split(wrapTo("alpha beta gamma delta epsilon zeta eta", 20), "\n")
	if len(lines) < 2 {
		t.Fatalf("display should wrap a long line, got %#v", lines)
	}
	for _, ln := range lines {
		if w := len([]rune(ln)); w > 20 {
			t.Fatalf("wrapped display line exceeds width 20: %q (%d)", ln, w)
		}
	}
}

func TestRerenderTranscriptHonorsCopyMode(t *testing.T) {
	m, _, _ := testModel()
	m.selectedTask = "t1"
	m.vp.Width = 20
	m.vp.Height = 10
	m.transcript = []map[string]any{
		{"kind": "model_text", "content": "alpha beta gamma delta epsilon zeta eta"},
	}
	m.copyMode = false
	m.rerenderTranscript()
	if strings.Contains(m.vp.View(), "│") {
		t.Fatalf("normal mode must not show the line-number gutter")
	}
	m.copyMode = true
	m.rerenderTranscript()
	if !strings.Contains(m.vp.View(), "1│") {
		t.Fatalf("copy mode must show the numbered gutter after rerender")
	}
}

// TestSeatScrollWindowFollowsCursor pins the left-pane scroll: a seat list longer than the
// pane height windows to keep the cursor visible (no overflow), and renderSeatTable shows the
// more-above/below affordance instead of every seat.
func TestSeatScrollWindowFollowsCursor(t *testing.T) {
	m, _, _ := testModel()
	m.view = "seats"
	m.vp.Height = 5 // seatViewportRows() -> 3
	m.seatOrder = nil
	m.seats = map[string]map[string]any{}
	for i := 0; i < 10; i++ {
		id := fmt.Sprintf("t%d", i)
		m.seatOrder = append(m.seatOrder, id)
		m.seats[id] = map[string]any{"task_id": id, "seat_id": fmt.Sprintf("codex-%d", i), "state": "running"}
	}
	rows := m.seatViewportRows()
	if rows != 3 {
		t.Fatalf("seatViewportRows=%d want 3", rows)
	}
	// Cursor at the top: window starts at 0.
	m.seatCursor = 0
	m.clampSeatScroll()
	if m.seatScroll != 0 {
		t.Fatalf("seatScroll=%d want 0 with cursor at top", m.seatScroll)
	}
	// Cursor past the window bottom: scroll so the cursor is the last visible row.
	m.seatCursor = 5
	m.clampSeatScroll()
	if m.seatScroll != 3 { // 5 - 3 + 1
		t.Fatalf("seatScroll=%d want 3 with cursor at row 5", m.seatScroll)
	}
	if m.seatCursor < m.seatScroll || m.seatCursor >= m.seatScroll+rows {
		t.Fatalf("cursor %d not in window [%d,%d)", m.seatCursor, m.seatScroll, m.seatScroll+rows)
	}
	// Scrolling back up brings the window with it.
	m.seatCursor = 1
	m.clampSeatScroll()
	if m.seatScroll != 1 {
		t.Fatalf("seatScroll=%d want 1 after moving cursor up to row 1", m.seatScroll)
	}
	// renderSeatTable shows the off-window affordance, not all 10 seats.
	out := m.renderSeatTable()
	if !strings.Contains(out, "more") {
		t.Fatalf("expected a 'more' scroll affordance in:\n%s", out)
	}
	if n := strings.Count(out, "codex-"); n > rows {
		t.Fatalf("rendered %d seat rows, want <= %d (windowed)", n, rows)
	}
}

// TestSeatTableFixedHeightAcrossScroll pins the wonky-filter-bar fix: with a full list the seat
// table must render the SAME number of lines whether the cursor is at the top, middle, or bottom,
// so the pane (and the filter bar above it) can't jump as the ↑/↓ affordances come and go.
func TestSeatTableFixedHeightAcrossScroll(t *testing.T) {
	m, _, _ := testModel()
	m.view = "seats"
	m.seatSource = "history"
	m.width = 160
	m.vp.Height = 24
	m.seatOrder = nil
	m.seats = map[string]map[string]any{}
	for i := 0; i < 40; i++ {
		id := fmt.Sprintf("t%02d", i)
		m.seatOrder = append(m.seatOrder, id)
		m.seats[id] = map[string]any{"task_id": id, "seat_id": fmt.Sprintf("codex-%02d", i), "state": "done", "last_event_ts": "2026-06-01T00:00:00+00:00"}
	}
	lineCount := func() int { return strings.Count(m.renderSeatTable(), "\n") + 1 }
	m.seatCursor = 0
	m.clampSeatScroll()
	top := lineCount()
	m.seatCursor = 20
	m.clampSeatScroll()
	mid := lineCount()
	m.seatCursor = 39
	m.clampSeatScroll()
	bottom := lineCount()
	if top != mid || mid != bottom {
		t.Fatalf("seat table height must be constant across scroll; top=%d mid=%d bottom=%d", top, mid, bottom)
	}
}

// TestHistorySeatOlderThanADayShowsDate pins the age-column change: a history seat older than 24h
// shows its dd/mm date (a "768h" relative age is useless), while live mode keeps the relative age.
func TestHistorySeatOlderThanADayShowsDate(t *testing.T) {
	m, _, _ := testModel()
	m.view = "seats"
	m.width = 160
	m.vp.Height = 24
	m.seatOrder = []string{"h1"}
	m.seats = map[string]map[string]any{
		"h1": {"task_id": "h1", "seat_id": "codex-1", "state": "done", "last_event_ts": "2026-06-01T00:00:00+00:00"},
	}
	m.seatSource = "history"
	if out := m.renderSeatTable(); !strings.Contains(out, "01/06") {
		t.Fatalf("history seat older than a day should show dd/mm (01/06):\n%s", out)
	}
	m.seatSource = "live"
	if out := m.renderSeatTable(); strings.Contains(out, "01/06") {
		t.Fatalf("live mode should keep the relative age, not a dd/mm date:\n%s", out)
	}
}

func TestRealStartStreamResetsBackoffForFreshTarget(t *testing.T) {
	// GO-3 (panel-confirmed): backoff was reset only in stop() or on a received
	// frame; enterSeat/enterSeats start a new stream without stop(), so up to
	// maxBackoff of stale backoff from the previous seat/orchestrator carried
	// into the new target's reconnect path.
	m := &model{token: "t", orch: streamState{backoff: maxBackoff}, seat: streamState{backoff: maxBackoff}}

	// Unreachable URL: the streamSSE goroutine fails in the background; the
	// backoff reset under test happens synchronously in realStartStream.
	_ = realStartStream(m, streamOrch, "http://127.0.0.1:0/nope", "")
	if m.orch.backoff != baseBackoff {
		t.Fatalf("orch backoff not reset on fresh stream: %s", m.orch.backoff)
	}
	if m.orch.cancel != nil {
		m.orch.cancel()
	}
}
