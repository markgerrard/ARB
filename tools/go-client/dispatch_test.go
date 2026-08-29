package main

import (
	"bytes"
	"strings"
	"testing"
	"time"
)

// starvingConn fakes a bus where an orphan sibling reply cycles through the
// shared inbox forever: BLPOP always returns instantly, never times out.
type starvingConn struct {
	orphan  string
	rpushed int
	polls   int
}

func (s *starvingConn) DoBlocking(blockSeconds int, args ...string) (interface{}, error) {
	return []interface{}{"inbox", s.orphan}, nil
}

func (s *starvingConn) Do(args ...string) (interface{}, error) {
	switch args[0] {
	case "RPUSH":
		s.rpushed++
		return int64(1), nil
	case "HGETALL":
		s.polls++
		return []interface{}{
			"task_id", "task-1",
			"seat_id", "codex-bridge-wedge",
			"stalled_at", "2026-07-08T08:39:22+01:00",
		}, nil
	}
	return nil, nil
}

func TestStallLineDespiteOrphanStarvation(t *testing.T) {
	// A stale sibling reply cycling through the shared inbox means BLPOP
	// returns instantly forever. The status poll must fire on its own time
	// cadence anyway, or the [stall] stderr channel is silent for the whole
	// dispatch (live wedge gate 2026-07-08: 53s stall window, zero lines,
	// two orphan replies on the orchestrator inbox).
	old := statusPollEvery
	statusPollEvery = 20 * time.Millisecond
	defer func() { statusPollEvery = old }()

	conn := &starvingConn{orphan: `{"kind":"reply","in_reply_to":"someone-else","payload":{"ok":true}}`}
	cfg := &Config{Prefix: "agent_scratch:"}
	env := &Envelope{ID: "task-1", From: "claude-test", To: "codex-test"}
	var stderr bytes.Buffer

	code, _, err := waitForReply(conn, cfg, env, time.Now().Add(450*time.Millisecond), &stderr)
	if err != nil {
		t.Fatalf("waitForReply: %v", err)
	}
	if code != 124 {
		t.Fatalf("exit code=%d want 124 (timeout)", code)
	}
	if got := strings.Count(stderr.String(), "[stall]"); got != 1 {
		t.Fatalf("[stall] lines=%d want exactly 1 (episode dedup); stderr=%q", got, stderr.String())
	}
	if conn.rpushed == 0 {
		t.Errorf("orphan reply must be re-queued, not consumed")
	}
}

func TestClassifyReplyMine(t *testing.T) {
	raw := []byte(`{"kind":"reply","in_reply_to":"req-1","payload":{"ok":true,"result":"done"}}`)
	class, payload := classifyReply(raw, "req-1")
	if class != classMine {
		t.Fatalf("class=%v want classMine", class)
	}
	if payload == nil || !payload.Ok {
		t.Errorf("payload.ok not parsed: %+v", payload)
	}
}

func TestClassifyReplyMineNotOk(t *testing.T) {
	raw := []byte(`{"kind":"reply","in_reply_to":"req-1","payload":{"ok":false,"error":"boom"}}`)
	class, payload := classifyReply(raw, "req-1")
	if class != classMine {
		t.Fatalf("class=%v want classMine", class)
	}
	if payload.Ok {
		t.Errorf("payload.ok should be false")
	}
}

func TestClassifyReplyNotifyDropped(t *testing.T) {
	// a notify (activity-stream chatter) must be classified as drop, even if it
	// carries our id somewhere — strict filter on kind.
	raw := []byte(`{"kind":"notify","payload":{"data":{"task_id":"req-1"}}}`)
	class, _ := classifyReply(raw, "req-1")
	if class != classNotify {
		t.Errorf("class=%v want classNotify", class)
	}
}

func TestClassifyReplyOtherSibling(t *testing.T) {
	// a reply addressed to a DIFFERENT in_reply_to belongs to a sibling dispatcher
	// sharing this inbox — must be re-queued, not consumed.
	raw := []byte(`{"kind":"reply","in_reply_to":"other-req","payload":{"ok":true}}`)
	class, _ := classifyReply(raw, "req-1")
	if class != classOther {
		t.Errorf("class=%v want classOther", class)
	}
}

func TestClassifyReplyGarbageIsOther(t *testing.T) {
	// unparseable / unexpected -> treat as 'other' (re-queue, don't crash, don't consume).
	class, _ := classifyReply([]byte(`not json`), "req-1")
	if class != classOther {
		t.Errorf("class=%v want classOther for garbage", class)
	}
}
