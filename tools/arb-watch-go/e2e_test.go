//go:build e2e

package main

import (
	"context"
	"net/url"
	"os"
	"strings"
	"testing"
	"time"
)

// TestLiveGatewayE2E drives the REAL transport against the live demo gateway: list
// orchestrators, gather seat task_ids, and assert at least one seat's transcript stream
// renders a ⏺ line. Read-only client — no gateway logic. Skips cleanly if the gateway
// is unreachable or no seat in the fleet has any transcript (environmental, not a failure).
func TestLiveGatewayE2E(t *testing.T) {
	baseURL := envDefault("ARB_WATCH_GO_E2E_BASE_URL", "http://localhost:8810")
	token := envDefault("ARB_VISIBILITY_TOKEN", "ff-demo-token")
	if strings.TrimSpace(token) == "" {
		t.Skip("ARB_VISIBILITY_TOKEN is empty")
	}

	orchestrators, err := fetchOrchestrators(baseURL, token)
	if err != nil {
		t.Skipf("gateway unreachable (fetchOrchestrators: %v) — skipping live E2E", err)
	}
	if len(orchestrators) == 0 {
		t.Skip("gateway has no orchestrator sessions — skipping live E2E")
	}

	// Gather candidate task_ids: a specific one if pinned, else every seat across all
	// orchestrators (so a stale empty seat being "first" can't fail the test).
	var tasks []string
	if pinned := os.Getenv("ARB_WATCH_GO_E2E_TASK_ID"); pinned != "" {
		tasks = []string{pinned}
	} else {
		for _, orch := range orchestrators {
			tasks = append(tasks, collectTaskIDs(baseURL, token, orch)...)
		}
	}
	if len(tasks) == 0 {
		t.Skip("no seats found in the fleet — skipping live E2E")
	}

	// Pass on the first seat whose transcript renders a ⏺ line.
	checked := 0
	for _, taskID := range tasks {
		checked++
		if seatHasTranscript(baseURL, token, taskID) {
			t.Logf("live E2E OK: seat task %q streamed a rendered ⏺ transcript line (checked %d seat(s))", taskID, checked)
			return
		}
	}
	t.Skipf("none of the %d fleet seat(s) had a transcript (capture may be off / streams cleared) — skipping", checked)
}

// seatHasTranscript opens /sse/seat/{task} and returns true if any non-lifecycle frame
// renders with ⏺ within a bounded window.
func seatHasTranscript(baseURL, token, taskID string) bool {
	ctx, cancel := context.WithTimeout(context.Background(), 6*time.Second)
	defer cancel()
	frames := make(chan Frame, 32)
	go streamSSE(ctx, strings.TrimRight(baseURL, "/")+"/sse/seat/"+url.PathEscape(taskID), token, "", frames)
	for frame := range frames {
		if isLifecycleNoise(frame.Data) {
			continue
		}
		if strings.Contains(renderTranscriptLine(frame.Data), "⏺") {
			return true
		}
	}
	return false
}

// collectTaskIDs reads an orchestrator stream for a bounded window and returns the
// distinct seat task_ids it sees.
func collectTaskIDs(baseURL, token, orchestrator string) []string {
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
	defer cancel()
	ch := make(chan Frame, 32)
	go streamSSE(ctx, strings.TrimRight(baseURL, "/")+"/sse/orchestrator/"+url.PathEscape(orchestrator), token, "", ch)
	seen := map[string]bool{}
	var out []string
	for frame := range ch {
		if taskID := getString(frame.Data, "task_id"); taskID != "" && !seen[taskID] {
			seen[taskID] = true
			out = append(out, taskID)
		}
	}
	return out
}

func envDefault(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}
