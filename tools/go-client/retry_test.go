package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
	"time"
)

// scriptedConn fakes the bus for the retry loop: an RPUSH to the TARGET inbox
// records the envelope id and enqueues the scripted reply for that attempt; an
// RPUSH to the sender's own inbox is a re-queue (kept, re-served). BLPOP serves
// the queue instantly.
type scriptedConn struct {
	replyFor   func(attempt int, envID string) string
	dispatches []string // envelope ids seen on the target inbox, in order
	queue      []string
}

func (s *scriptedConn) Do(args ...string) (interface{}, error) {
	switch args[0] {
	case "RPUSH":
		key, raw := args[1], args[2]
		if strings.Contains(key, ":agent:codex-test:inbox") {
			var env struct {
				ID string `json:"id"`
			}
			_ = json.Unmarshal([]byte(raw), &env)
			s.dispatches = append(s.dispatches, env.ID)
			s.queue = append(s.queue, s.replyFor(len(s.dispatches), env.ID))
			return int64(1), nil
		}
		// sender-inbox re-queue (orphan path) — keep it cycling
		s.queue = append(s.queue, raw)
		return int64(1), nil
	case "LLEN":
		return int64(0), nil
	}
	return nil, nil
}

func (s *scriptedConn) DoBlocking(blockSeconds int, args ...string) (interface{}, error) {
	if len(s.queue) == 0 {
		return nil, nil
	}
	raw := s.queue[0]
	s.queue = s.queue[1:]
	return []interface{}{"inbox", raw}, nil
}

func notOkReply(envID, errStr string) string {
	return `{"kind":"reply","in_reply_to":"` + envID + `","payload":{"ok":false,"result":"","error":"` + errStr + `"}}`
}

func okReply(envID string) string {
	return `{"kind":"reply","in_reply_to":"` + envID + `","payload":{"ok":true,"result":"done"}}`
}

const coldStartErr = "engine-start-failed: initialize timed out after 15s"

func runLoop(t *testing.T, conn *scriptedConn, retry bool) (int, string, string) {
	t.Helper()
	cfg := &Config{Prefix: "agent_scratch:"}
	env := &Envelope{ID: "req-1", From: "claude-test", To: "codex-test", Kind: "request"}
	var stdout, stderr bytes.Buffer
	code, err := dispatchLoop(conn, cfg, env, time.Now().Add(2*time.Second), retry, &stdout, &stderr)
	if err != nil {
		t.Fatalf("dispatchLoop: %v", err)
	}
	return code, stdout.String(), stderr.String()
}

func TestRetryEngineStartFlakeRetriesOnceAndSucceeds(t *testing.T) {
	// The 5x-observed cold-start flake: first dispatch dies with
	// engine-start-failed initialize-timeout, an immediate re-dispatch works.
	conn := &scriptedConn{replyFor: func(attempt int, envID string) string {
		if attempt == 1 {
			return notOkReply(envID, coldStartErr)
		}
		return okReply(envID)
	}}
	code, stdout, stderr := runLoop(t, conn, true)
	if code != 0 {
		t.Fatalf("exit code=%d want 0 after retry", code)
	}
	if len(conn.dispatches) != 2 {
		t.Fatalf("dispatches=%d want 2 (original + one retry)", len(conn.dispatches))
	}
	if conn.dispatches[0] == conn.dispatches[1] {
		t.Errorf("retry must use a FRESH envelope id; both were %q", conn.dispatches[0])
	}
	if got := strings.Count(stderr, "[retry]"); got != 1 {
		t.Errorf("[retry] stderr lines=%d want exactly 1; stderr=%q", got, stderr)
	}
	if strings.Count(stdout, "\"ok\"") != 1 || strings.Contains(stdout, "engine-start-failed") {
		t.Errorf("stdout must carry ONLY the final payload; got %q", stdout)
	}
}

func TestRetryEngineStartBoundedToOnce(t *testing.T) {
	// Both attempts flake: exactly one retry, then the failure is surfaced.
	conn := &scriptedConn{replyFor: func(attempt int, envID string) string {
		return notOkReply(envID, coldStartErr)
	}}
	code, stdout, _ := runLoop(t, conn, true)
	if code != 1 {
		t.Fatalf("exit code=%d want 1", code)
	}
	if len(conn.dispatches) != 2 {
		t.Fatalf("dispatches=%d want 2 (retry is bounded to ONCE)", len(conn.dispatches))
	}
	if !strings.Contains(stdout, "engine-start-failed") {
		t.Errorf("final failed payload must reach stdout; got %q", stdout)
	}
}

func TestNoRetryWhenFlagOff(t *testing.T) {
	conn := &scriptedConn{replyFor: func(attempt int, envID string) string {
		return notOkReply(envID, coldStartErr)
	}}
	code, stdout, stderr := runLoop(t, conn, false)
	if code != 1 {
		t.Fatalf("exit code=%d want 1", code)
	}
	if len(conn.dispatches) != 1 {
		t.Fatalf("dispatches=%d want 1 (no retry without the flag)", len(conn.dispatches))
	}
	if strings.Contains(stderr, "[retry]") {
		t.Errorf("no [retry] line expected; stderr=%q", stderr)
	}
	if !strings.Contains(stdout, "engine-start-failed") {
		t.Errorf("failed payload must reach stdout; got %q", stdout)
	}
}

func TestNonRetryableErrorNotRetried(t *testing.T) {
	// Only the observed-transient shape retries; a busy seat (or any other
	// bridge error) surfaces immediately even with the flag on.
	conn := &scriptedConn{replyFor: func(attempt int, envID string) string {
		return notOkReply(envID, "bridge busy with task abc-123")
	}}
	code, _, stderr := runLoop(t, conn, true)
	if code != 1 {
		t.Fatalf("exit code=%d want 1", code)
	}
	if len(conn.dispatches) != 1 {
		t.Fatalf("dispatches=%d want 1 (non-retryable error)", len(conn.dispatches))
	}
	if strings.Contains(stderr, "[retry]") {
		t.Errorf("no [retry] line expected; stderr=%q", stderr)
	}
}

func TestRetryableEngineStartPredicate(t *testing.T) {
	cases := []struct {
		err  string
		want bool
	}{
		{coldStartErr, true},
		{"engine-start-failed: initialize timed out after 30s", true},
		{"engine-start-failed: spawn failed: no such file", false}, // not known-transient
		{"bridge busy with task x", false},
		{"thread-affinity-miss thread=t1", false},
		{"", false},
	}
	for _, c := range cases {
		if got := retryableEngineStart(c.err); got != c.want {
			t.Errorf("retryableEngineStart(%q)=%v want %v", c.err, got, c.want)
		}
	}
}

func TestClassifyReplyParsesError(t *testing.T) {
	raw := []byte(`{"kind":"reply","in_reply_to":"req-1","payload":{"ok":false,"error":"engine-start-failed: initialize timed out after 15s"}}`)
	class, payload := classifyReply(raw, "req-1")
	if class != classMine {
		t.Fatalf("class=%v want classMine", class)
	}
	if payload.Error != "engine-start-failed: initialize timed out after 15s" {
		t.Errorf("payload.Error=%q not parsed", payload.Error)
	}
}
