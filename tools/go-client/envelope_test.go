package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// TestGoldenLifecycleByteMatch proves Go still owns lifecycle envelope bytes
// (worktree_arm / worktree_release). Ordinary goldens were retired in Slice 1d-iv
// because ordinary enqueue is authority-owned; independent Go ordinary builders
// are a tripwire failure.
func TestGoldenLifecycleByteMatch(t *testing.T) {
	const from, branch = "claude-bridge-dev", "dev"
	const codex = "codex-bridge-dev"
	cases := []struct {
		file  string
		build func(id, sentAt string) *Envelope
	}{
		{"lifecycle-worktree-arm", func(id, s string) *Envelope {
			return &Envelope{
				ID: id, From: from, Branch: branch, To: codex, Kind: "request", SentAt: s,
				Payload: Payload{
					Operation: "worktree_arm",
					Worktree:  &Worktree{Name: "faba-round-1", Cleanup: "keep", BaseRef: "abc"},
					RunID:     "round-1",
					LeaseTTL:  intPtr(3600),
				},
				RunID: "round-1",
			}
		}},
		{"lifecycle-worktree-release", func(id, s string) *Envelope {
			return &Envelope{
				ID: id, From: from, Branch: branch, To: codex, Kind: "request", SentAt: s,
				Payload: Payload{
					Operation:     "worktree_release",
					WorktreeLease: "lease-1",
					RunID:         "round-1",
				},
				RunID: "round-1",
			}
		}},
	}

	for _, c := range cases {
		t.Run(c.file, func(t *testing.T) {
			want, err := os.ReadFile(filepath.Join("testdata", "golden", c.file+".json"))
			if err != nil {
				t.Fatalf("read golden: %v", err)
			}
			want = bytes.TrimRight(want, "\n")

			var probe struct {
				ID     string `json:"id"`
				SentAt string `json:"sent_at"`
			}
			if err := json.Unmarshal(want, &probe); err != nil {
				t.Fatalf("parse golden id/sent_at: %v", err)
			}

			got, err := c.build(probe.ID, probe.SentAt).Marshal()
			if err != nil {
				t.Fatalf("marshal: %s", err)
			}
			if !bytes.Equal(got, want) {
				t.Errorf("byte mismatch vs lifecycle golden:\n got: %s\nwant: %s", got, want)
			}
		})
	}
}

func intPtr(v int) *int { return &v }

// TestNoHTMLEscaping guards ensure_ascii=False / SetEscapeHTML(false) for
// lifecycle (and any residual string fields) serialization.
func TestNoHTMLEscaping(t *testing.T) {
	e := &Envelope{ID: "x", From: "a", Branch: "b", To: "c", Kind: "request", SentAt: "t",
		Payload: Payload{Operation: "worktree_release", WorktreeLease: "lease-a < b"}}
	got, err := e.Marshal()
	if err != nil {
		t.Fatal(err)
	}
	for _, esc := range []string{`\u003c`, `\u003e`, `\u0026`} {
		if bytes.Contains(got, []byte(esc)) {
			t.Errorf("found HTML escape %q: %s", esc, got)
		}
	}
}

func TestLifecycleMarshalOmitsTask(t *testing.T) {
	e := &Envelope{ID: "x", From: "a", Branch: "b", To: "c", Kind: "request", SentAt: "t",
		Payload: Payload{Operation: "worktree_release", WorktreeLease: "lease-1"}}
	got, err := e.Marshal()
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(got, []byte(`"task"`)) {
		t.Errorf("lifecycle envelope must not carry task: %s", got)
	}
}

// TestUnescapeLineSeps covers the CHANGELOG defect class: Go's encoding/json
// escapes U+2028/U+2029 even with SetEscapeHTML(false); unescape restores the
// literal UTF-8 only when the preceding backslash run is odd (a real escape).
func TestUnescapeLineSeps(t *testing.T) {
	// UTF-8 of U+2028 / U+2029
	u2028 := []byte{0xE2, 0x80, 0xA8}
	u2029 := []byte{0xE2, 0x80, 0xA9}

	cases := []struct {
		name string
		in   []byte
		want []byte
	}{
		{
			name: "odd-run-u2028-becomes-literal",
			// Go escape of the separator character itself: `\u2028`
			in:   []byte(`hello\u2028world`),
			want: append(append([]byte("hello"), u2028...), []byte("world")...),
		},
		{
			name: "odd-run-u2029-becomes-literal",
			in:   []byte(`hello\u2029world`),
			want: append(append([]byte("hello"), u2029...), []byte("world")...),
		},
		{
			name: "escaped-backslash-then-u2028-preserved",
			// Literal backslash + text "u2028" in data is emitted as `\\u2028`
			// (even run) and must stay escaped, not become the separator.
			in:   []byte(`path\\u2028still`),
			want: []byte(`path\\u2028still`),
		},
		{
			name: "even-run-four-backslashes-preserved",
			in:   []byte(`x\\\\u2028y`),
			want: []byte(`x\\\\u2028y`),
		},
		{
			name: "odd-run-three-backslashes-unescapes",
			// `\\\u2028` = one escaped-backslash pair + real `\u2028` escape
			in:   []byte(`x\\\u2028y`),
			want: append(append([]byte(`x\\`), u2028...), 'y'),
		},
		{
			name: "no-escape-unchanged",
			in:   []byte(`plain text`),
			want: []byte(`plain text`),
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := unescapeLineSeps(c.in)
			if !bytes.Equal(got, c.want) {
				t.Errorf("unescapeLineSeps(%q)\n got: %q\nwant: %q", c.in, got, c.want)
			}
		})
	}
}

// TestMarshalEmitsLiteralLineSeparators proves the Marshal path still undoes
// Go's U+2028/U+2029 escapes on lifecycle string fields (live use of unescapeLineSeps).
func TestMarshalEmitsLiteralLineSeparators(t *testing.T) {
	// Put U+2028 inside a lease id so Marshal must pass it through unescapeLineSeps.
	lease := "lease" + string(rune(0x2028)) + "id"
	e := &Envelope{ID: "x", From: "a", Branch: "b", To: "c", Kind: "request", SentAt: "t",
		Payload: Payload{Operation: "worktree_release", WorktreeLease: lease}}
	got, err := e.Marshal()
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(got, []byte(`\u2028`)) {
		t.Errorf("Marshal re-escaped U+2028 (unescapeLineSeps not applied): %s", got)
	}
	if !bytes.Contains(got, []byte{0xE2, 0x80, 0xA8}) {
		t.Errorf("Marshal missing literal U+2028 UTF-8: %s", got)
	}
}
