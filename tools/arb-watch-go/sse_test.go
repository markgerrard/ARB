package main

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"
	"time"
)

func TestIsResumableID(t *testing.T) {
	tests := map[string]bool{
		"1782-0":       true,
		"e=5-0;t=9-0":  true,
		"e=-;t=9-0":    true,
		"e=5-0;t=-":    true,
		"e=-;t=-":      false,
		"backfill-3":   false,
		"stale-x":      false,
		"":             false,
		"not-a-cursor": false,
	}
	for input, want := range tests {
		if got := isResumableID(input); got != want {
			t.Fatalf("isResumableID(%q)=%v want %v", input, got, want)
		}
	}
}

func TestFetchOrchestrators(t *testing.T) {
	var gotAuth string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		if r.URL.Path != "/orchestrators" {
			t.Fatalf("path=%s", r.URL.Path)
		}
		_, _ = fmt.Fprint(w, `{"orchestrators":["o1","o2"]}`)
	}))
	defer server.Close()

	got, err := fetchOrchestrators(server.URL, "token-1")
	if err != nil {
		t.Fatal(err)
	}
	if gotAuth != "Bearer token-1" {
		t.Fatalf("auth=%q", gotAuth)
	}
	if !reflect.DeepEqual(got, []string{"o1", "o2"}) {
		t.Fatalf("orchestrators=%#v", got)
	}
}

func TestFetchSeatsHistoryParsesResponseAndNullCursor(t *testing.T) {
	var gotPath, gotAuth string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		gotAuth = r.Header.Get("Authorization")
		_, _ = fmt.Fprint(w, `{"seats":[{"task_id":"t1"}],"next_cursor":null,"has_more":false}`)
	}))
	defer server.Close()

	got, err := fetchSeatsHistory(server.URL, "token-1", "orch-1", "")
	if err != nil {
		t.Fatal(err)
	}
	if gotPath != "/orchestrators/orch-1/seats/history" {
		t.Fatalf("path=%s", gotPath)
	}
	if gotAuth != "Bearer token-1" {
		t.Fatalf("auth=%q", gotAuth)
	}
	if got.HasMore || got.NextCursor != nil || len(got.Seats) != 1 || got.Seats[0]["task_id"] != "t1" {
		t.Fatalf("response=%#v", got)
	}
}

func TestFetchSeatsHistoryPassesCursorAsQueryParam(t *testing.T) {
	var gotQuery string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotQuery = r.URL.RawQuery
		_, _ = fmt.Fprint(w, `{"seats":[],"next_cursor":null,"has_more":false}`)
	}))
	defer server.Close()

	if _, err := fetchSeatsHistory(server.URL, "token-1", "orch-1", "abc123"); err != nil {
		t.Fatal(err)
	}
	if gotQuery != "cursor=abc123" {
		t.Fatalf("query=%q", gotQuery)
	}
}

func TestStreamSSEParsesFramesHeadersAndPartialTail(t *testing.T) {
	requests := 0
	var gotAuth, gotLastID string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		gotAuth = r.Header.Get("Authorization")
		gotLastID = r.Header.Get("Last-Event-ID")
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = fmt.Fprint(w, ": ping\n\n")
		_, _ = fmt.Fprint(w, "id: 1-0\nevent: seat_appear\ndata: {\"task_id\":\"t1\"}\n\n")
		_, _ = fmt.Fprint(w, "id: backfill-1\nevent: backfill\ndata: {\"source\":\"transcript\",\"task_id\":\"t1\"}\n\n")
		_, _ = fmt.Fprint(w, "id: 2-0\nevent: ignored\ndata: {")
	}))
	defer server.Close()

	ch := make(chan Frame, 4)
	streamSSE(context.Background(), server.URL, "token-1", "1-0", ch)
	var frames []Frame
	for frame := range ch {
		frames = append(frames, frame)
	}

	if requests != 1 {
		t.Fatalf("requests=%d", requests)
	}
	if gotAuth != "Bearer token-1" || gotLastID != "1-0" {
		t.Fatalf("headers auth=%q last=%q", gotAuth, gotLastID)
	}
	if len(frames) != 2 {
		t.Fatalf("frames=%#v", frames)
	}
	if frames[0].ID != "1-0" || frames[0].Event != "seat_appear" || frames[0].Data["task_id"] != "t1" {
		t.Fatalf("first frame=%#v", frames[0])
	}
	if frames[1].ID != "backfill-1" || frames[1].Event != "backfill" || frames[1].Data["source"] != "transcript" {
		t.Fatalf("second frame=%#v", frames[1])
	}
}

func TestStreamSSEDoesNotSendSyntheticLastID(t *testing.T) {
	var gotLastID string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotLastID = r.Header.Get("Last-Event-ID")
		_, _ = fmt.Fprint(w, "id: 1-0\nevent: event\ndata: {\"ok\":true}\n\n")
	}))
	defer server.Close()

	ch := make(chan Frame, 1)
	streamSSE(context.Background(), server.URL, "token-1", "backfill-1", ch)
	if gotLastID != "" {
		t.Fatalf("synthetic last id was sent: %q", gotLastID)
	}
}

func TestStreamSSECancelReturnsAndClosesChannel(t *testing.T) {
	released := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		if flusher, ok := w.(http.Flusher); ok {
			_, _ = fmt.Fprint(w, "id: 1-0\nevent: event\ndata: {\"task_id\":\"t1\"}\n\n")
			flusher.Flush()
		}
		<-r.Context().Done()
		close(released)
	}))
	defer server.Close()

	ctx, cancel := context.WithCancel(context.Background())
	ch := make(chan Frame)
	done := make(chan struct{})
	go func() {
		streamSSE(ctx, server.URL, "token-1", "", ch)
		close(done)
	}()
	select {
	case frame := <-ch:
		if frame.ID != "1-0" {
			t.Fatalf("first frame=%#v", frame)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("first frame was not delivered")
	}
	cancel()

	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("streamSSE did not return after cancel")
	}
	select {
	case <-released:
	case <-time.After(2 * time.Second):
		t.Fatal("server request context was not cancelled")
	}
}
