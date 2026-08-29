// Package envelope constructs the bridge request envelope BYTE-IDENTICALLY to the
// Python agent-dispatch tool, so a Go client edge can interoperate with the existing
// Python bridge spine over the shared Redis contract (SPEC.md).
//
// The wire format (src/agent_redis_bridge/envelope.py:118-119) is compact JSON
// (separators (",",":")) with ensure_ascii=False and INSERTION-ORDER keys (not sorted).
// Two things make Go match it:
//  1. struct field declaration order == Python's dict insertion order (verified against
//     the golden corpus in testdata/golden, captured from `agent-dispatch --dry-run-envelope`);
//  2. encoding/json HTML-escapes <, >, & by default — Python does not — so Marshal()
//     disables it (json.Encoder.SetEscapeHTML(false)).
package main

import (
	"bytes"
	"encoding/json"
)

// Worktree is the payload.worktree sub-object (bridge.py parse_worktree_spec).
// Field order: name, cleanup, base_ref (matches the dispatcher).
type Worktree struct {
	Name    string `json:"name"`
	Cleanup string `json:"cleanup,omitempty"`
	BaseRef string `json:"base_ref,omitempty"`
}

// Payload is the request payload. Field order MUST match the Python dispatcher's
// jq append sequence (agent-dispatch:305-341): task, worktree, expected_artifacts,
// allowed_paths, commit_message, expect_structured, fresh_context, thread_id,
// fork_from_thread_id, audit_vote_expected, panel_input_locked,
// reasoning_effort, turn_timeout. New fields append at the end to preserve
// byte identity with the Python dispatcher's jq insertion order.
type Payload struct {
	Task              string    `json:"task,omitempty"`
	Worktree          *Worktree `json:"worktree,omitempty"`
	ExpectedArtifacts []string  `json:"expected_artifacts,omitempty"`
	AllowedPaths      []string  `json:"allowed_paths,omitempty"`
	CommitMessage     string    `json:"commit_message,omitempty"`
	ExpectStructured  bool      `json:"expect_structured,omitempty"`
	FreshContext      bool      `json:"fresh_context,omitempty"`
	ThreadID          string    `json:"thread_id,omitempty"`
	ForkFromThreadID  string    `json:"fork_from_thread_id,omitempty"`
	AuditVoteExpected bool      `json:"audit_vote_expected,omitempty"`
	PanelInputLocked  bool      `json:"panel_input_locked,omitempty"`
	ReasoningEffort   string    `json:"reasoning_effort,omitempty"`
	TurnTimeout       *int      `json:"turn_timeout,omitempty"`
	Operation         string    `json:"operation,omitempty"`
	WorktreeLease     string    `json:"worktree_lease,omitempty"`
	LeaseTTL          *int      `json:"lease_ttl,omitempty"`
	RunID             string    `json:"run_id,omitempty"`
}

// MarshalJSON keeps operation-mode envelopes byte-identical to the bash
// dispatcher's isolated assembly branches.  Normal turn payloads retain the
// historical struct order and therefore the existing golden bytes.
func (p Payload) MarshalJSON() ([]byte, error) {
	if p.Operation == "worktree_arm" {
		return marshalPayloadValue(struct {
			Operation string    `json:"operation"`
			Worktree  *Worktree `json:"worktree"`
			RunID     string    `json:"run_id,omitempty"`
			LeaseTTL  *int      `json:"lease_ttl,omitempty"`
		}{p.Operation, p.Worktree, p.RunID, p.LeaseTTL})
	}
	if p.Operation == "worktree_run" {
		return marshalPayloadValue(struct {
			Operation     string `json:"operation"`
			Task          string `json:"task"`
			WorktreeLease string `json:"worktree_lease"`
			RunID         string `json:"run_id,omitempty"`
			ThreadID      string `json:"thread_id,omitempty"`
			TurnTimeout   *int   `json:"turn_timeout,omitempty"`
		}{p.Operation, p.Task, p.WorktreeLease, p.RunID, p.ThreadID, p.TurnTimeout})
	}
	if p.Operation == "worktree_release" {
		return marshalPayloadValue(struct {
			Operation     string `json:"operation"`
			WorktreeLease string `json:"worktree_lease"`
			RunID         string `json:"run_id,omitempty"`
		}{p.Operation, p.WorktreeLease, p.RunID})
	}
	type plainPayload Payload
	return marshalPayloadValue(plainPayload(p))
}

func marshalPayloadValue(value any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(value); err != nil {
		return nil, err
	}
	return bytes.TrimRight(buf.Bytes(), "\n"), nil
}

// Envelope is the top-level request. Field order matches the dispatcher
// (agent-dispatch:328-338): id, from, branch, to, kind, sent_at, payload, then
// run_id (top-level, AFTER payload when present).
type Envelope struct {
	ID      string  `json:"id"`
	From    string  `json:"from"`
	Branch  string  `json:"branch"`
	To      string  `json:"to"`
	Kind    string  `json:"kind"`
	SentAt  string  `json:"sent_at"`
	Payload Payload `json:"payload"`
	RunID   string  `json:"run_id,omitempty"`
}

// Marshal serializes the envelope to the EXACT wire bytes the Python tool would
// enqueue: compact, UTF-8 (no \uXXXX), no HTML escaping, no trailing newline.
func (e *Envelope) Marshal() ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(e); err != nil {
		return nil, err
	}
	// json.Encoder.Encode appends a trailing '\n'; the wire bytes have none.
	out := bytes.TrimRight(buf.Bytes(), "\n")
	// Go's encoding/json escapes U+2028 / U+2029 even with SetEscapeHTML(false);
	// Python's json.dumps(ensure_ascii=False) emits them literally. Undo the escape
	// to keep byte-identity.
	return unescapeLineSeps(out), nil
}

// unescapeLineSeps replaces Go's   /   escapes with the literal UTF-8
// characters, but ONLY when the backslash is a real escape introducer — a literal
// " " in the data is emitted by Go as "\\u2028" (an escaped backslash followed
// by text) and must be preserved. We decide by counting the preceding backslash run:
// an odd run means the final backslash escapes what follows.
func unescapeLineSeps(b []byte) []byte {
	out := make([]byte, 0, len(b))
	i := 0
	for i < len(b) {
		if b[i] != '\\' {
			out = append(out, b[i])
			i++
			continue
		}
		j := i
		for j < len(b) && b[j] == '\\' {
			j++
		}
		run := j - i
		if run%2 == 1 && j+4 < len(b)+1 && j+5 <= len(b) {
			seq := string(b[j : j+5])
			if seq == "u2028" || seq == "u2029" {
				out = append(out, b[i:i+run-1]...) // the escaped-backslash pairs
				if seq == "u2028" {
					out = append(out, 0xE2, 0x80, 0xA8) // U+2028
				} else {
					out = append(out, 0xE2, 0x80, 0xA9) // U+2029
				}
				i = j + 5
				continue
			}
		}
		out = append(out, b[i:j]...)
		i = j
	}
	return out
}
