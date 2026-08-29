package main

import (
	"bytes"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strings"
	"time"
)

type replyClass int

const (
	classOther  replyClass = iota // garbage / sibling reply -> re-queue, don't consume
	classMine                     // kind==reply && in_reply_to==myID -> the reply we want
	classNotify                   // activity-stream chatter -> drop
)

type replyPayload struct {
	Ok    bool
	Error string          // the payload's error field (drives the retry discriminator)
	Raw   json.RawMessage // the payload object, printed verbatim to stdout
}

// classifyReply applies the strict reply filter (mirror agent-dispatch:376-398):
// our reply only when kind==reply AND in_reply_to==myReqID; notify is dropped;
// anything else (a sibling dispatcher's reply, or unparseable) is re-queued.
func classifyReply(raw []byte, myReqID string) (replyClass, *replyPayload) {
	var env struct {
		Kind      string          `json:"kind"`
		InReplyTo string          `json:"in_reply_to"`
		Payload   json.RawMessage `json:"payload"`
	}
	if err := json.Unmarshal(raw, &env); err != nil {
		return classOther, nil
	}
	if env.Kind == "notify" {
		return classNotify, nil
	}
	if env.Kind == "reply" && env.InReplyTo == myReqID {
		var pp struct {
			Ok    bool   `json:"ok"`
			Error string `json:"error"`
		}
		_ = json.Unmarshal(env.Payload, &pp)
		return classMine, &replyPayload{Ok: pp.Ok, Error: pp.Error, Raw: env.Payload}
	}
	return classOther, nil
}

// redisConn is the slice of *Client the wait loop needs; tests fake it.
type redisConn interface {
	Do(args ...string) (interface{}, error)
	DoBlocking(blockSeconds int, args ...string) (interface{}, error)
}

// dispatch sends the envelope and waits for the matching reply. Returns the exit
// code (0 ok, 1 not-ok, 124 timeout) and prints the reply payload to stdout — the
// same contract as the Python agent-dispatch. With retryEngineStart, a reply
// matching the known-transient engine cold-start flake is re-dispatched ONCE
// (fresh envelope id, same overall deadline) before the failure is surfaced.
func dispatch(cfg *Config, env *Envelope, timeout time.Duration, retryEngineStart bool) (int, error) {
	c, err := dial(cfg)
	if err != nil {
		return 1, err
	}
	defer c.Close()
	return dispatchLoop(c, cfg, env, time.Now().Add(timeout), retryEngineStart, os.Stdout, os.Stderr)
}

// retryableEngineStart matches the ONLY engine-start failure shape observed to be
// transient (cold-start initialize timeout; retry-once succeeded 5/5 — BACKLOG
// DSP-1). Other engine-start-failed causes (bad config, missing binary, auth) are
// not known-transient and must surface immediately.
func retryableEngineStart(errStr string) bool {
	return strings.HasPrefix(errStr, "engine-start-failed:") && strings.Contains(errStr, "initialize timed out")
}

// dispatchLoop enqueues the envelope and waits for its reply, retrying the
// cold-start flake at most once. Only the FINAL payload is printed to stdout
// (callers parse stdout as a single JSON document); the retry note goes to
// stderr alongside the task-id/[stall] channel.
func dispatchLoop(c redisConn, cfg *Config, env *Envelope, deadline time.Time, retryEngineStart bool, stdout, stderr io.Writer) (int, error) {
	for {
		msg, err := env.Marshal()
		if err != nil {
			return 1, err
		}
		toInbox := cfg.inboxKey(env.To)

		// Observable queue: write status=queued BEFORE the RPUSH so the bridge can't
		// pop+mark-running before this lands (mirror agent-dispatch:362-369).
		qd := "0"
		if n, e := c.Do("LLEN", toInbox); e == nil {
			if v, ok := n.(int64); ok {
				qd = fmt.Sprint(v)
			}
		}
		_, _ = c.Do("HSET", cfg.statusKey(env.ID), "task_id", env.ID, "state", "queued", "queue_depth", qd, "enqueued_at", nowISO())
		_, _ = c.Do("EXPIRE", cfg.statusKey(env.ID), "604800")

		// Stable "task-id: <uuid>" on stderr for backgrounded callers / ctl.
		fmt.Fprintf(stderr, "task-id: %s\n", env.ID)

		if _, err := c.Do("RPUSH", toInbox, string(msg)); err != nil {
			return 1, fmt.Errorf("RPUSH: %w", err)
		}

		code, payload, err := waitForReply(c, cfg, env, deadline, stderr)
		if err != nil {
			return 1, err
		}
		if code == 1 && retryEngineStart && payload != nil && retryableEngineStart(payload.Error) && time.Now().Before(deadline) {
			retryEngineStart = false // bounded to ONCE
			fmt.Fprintf(stderr, "[retry] %s — known cold-start flake, re-dispatching once (DSP-1)\n", payload.Error)
			env.ID = newUUID() // fresh task identity: status keys / reply filter / audit are id-keyed
			env.SentAt = nowISO()
			continue
		}
		if payload != nil {
			fmt.Fprintln(stdout, prettyJSON(payload.Raw))
		}
		return code, nil
	}
}

// statusPollEvery is the cadence of the task-status poll behind the [stall]
// stderr channel. Package-level so the starvation test can shrink it.
var statusPollEvery = 5 * time.Second

// waitForReply BLPOPs the sender's inbox until deadline for the reply matching
// env.ID, surfacing stall episodes from the task status hash on stderr. Returns
// the exit code (0 ok, 1 not-ok, 124 timeout) plus the matched payload (nil on
// timeout) — printing is the caller's job, so a retrying caller can suppress a
// transient failure payload from stdout.
//
// The status poll runs on a time cadence INDEPENDENT of BLPOP outcomes: when
// an orphan sibling reply (or notify chatter) cycles through the shared inbox,
// BLPOP returns instantly on every iteration and never times out — a
// poll-only-on-timeout shape starves the [stall] channel for the entire
// dispatch (observed live 2026-07-08: 53s stall window, zero lines).
func waitForReply(c redisConn, cfg *Config, env *Envelope, deadline time.Time, stderr io.Writer) (int, *replyPayload, error) {
	fromInbox := cfg.inboxKey(env.From)
	stallState := stallPrintState{}
	nextPoll := time.Now().Add(statusPollEvery)
	for time.Now().Before(deadline) {
		if !time.Now().Before(nextPoll) {
			status, ok := pollTaskStatus(c, cfg, env.ID)
			maybePrintStallLineOK(stderr, &stallState, status, ok)
			nextPoll = time.Now().Add(statusPollEvery)
		}
		reply, err := c.DoBlocking(5, "BLPOP", fromInbox, "5")
		if err != nil {
			return 1, nil, fmt.Errorf("BLPOP: %w", err)
		}
		if reply == nil {
			continue // BLPOP server-side timeout; next iteration polls if due
		}
		arr, ok := reply.([]interface{})
		if !ok || len(arr) != 2 {
			continue
		}
		raw, _ := arr[1].(string)
		switch class, payload := classifyReply([]byte(raw), env.ID); class {
		case classMine:
			if payload.Ok {
				return 0, payload, nil
			}
			return 1, payload, nil
		case classNotify:
			continue
		default: // classOther: sibling dispatcher's reply -> re-queue
			// Back off before re-queuing: a single orphaned sibling reply is returned
			// by BLPOP immediately, so without a brake this becomes a hard spin that
			// hammers Redis for the whole timeout (Python's per-iteration redis-cli
			// fork was an accidental ~ms brake; we add an explicit one).
			_, _ = c.Do("RPUSH", fromInbox, raw)
			time.Sleep(100 * time.Millisecond)
		}
	}
	fmt.Fprintf(stderr, "timed out waiting for reply to %s\n", env.ID)
	return 124, nil, nil
}

// prettyJSON re-indents the reply payload to multi-line, matching the Python tool's
// `jq '.payload'` output shape. Falls back to the raw bytes if it isn't valid JSON.
func prettyJSON(raw json.RawMessage) string {
	var buf bytes.Buffer
	if err := json.Indent(&buf, raw, "", "  "); err != nil {
		return string(raw)
	}
	return buf.String()
}

// nowISO renders the current time like Python's `date -Iseconds` (offset, seconds).
func nowISO() string { return time.Now().Format("2006-01-02T15:04:05-07:00") }

// newUUID returns a random UUIDv4 (no external dependency).
func newUUID() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	b[6] = (b[6] & 0x0f) | 0x40 // version 4
	b[8] = (b[8] & 0x3f) | 0x80 // variant 10
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}
