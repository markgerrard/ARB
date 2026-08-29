package main

import (
	"os"
	"os/exec"
	"runtime"
	"strings"
	"testing"
)

// The OSC-52 leak: copyToClipboard wrote a raw escape sequence to os.Stderr, which interleaves
// with bubbletea's alt-screen stdout and leaks the base64 payload onto the screen. The clipboard
// write must never go to stderr.
func TestCopyToClipboardDoesNotWriteEscapeToStderr(t *testing.T) {
	old := os.Stderr
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	os.Stderr = w
	if cmd := copyToClipboard("hello world"); cmd != nil {
		cmd() // run the tea.Cmd body
	}
	w.Close()
	os.Stderr = old

	out, _ := readAll(r)
	if strings.ContainsRune(out, '\x1b') {
		t.Fatalf("copyToClipboard leaked an escape sequence to stderr: %q", out)
	}
}

func TestCopyViaOSClipboardRoundTripsOnDarwin(t *testing.T) {
	if runtime.GOOS != "darwin" {
		t.Skip("pbcopy/pbpaste round-trip is darwin-only")
	}
	if _, err := exec.LookPath("pbpaste"); err != nil {
		t.Skip("pbpaste not available")
	}
	saved, _ := exec.Command("pbpaste").Output()
	t.Cleanup(func() {
		c := exec.Command("pbcopy")
		c.Stdin = strings.NewReader(string(saved))
		_ = c.Run()
	})

	const want = "arb-watch clipboard round-trip 12345"
	if !copyViaOSClipboard(want) {
		t.Fatal("copyViaOSClipboard returned false on darwin")
	}
	got, err := exec.Command("pbpaste").Output()
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != want {
		t.Fatalf("clipboard = %q, want %q", got, want)
	}
}

func readAll(r *os.File) (string, error) {
	var sb strings.Builder
	buf := make([]byte, 4096)
	for {
		n, err := r.Read(buf)
		sb.Write(buf[:n])
		if err != nil {
			return sb.String(), nil
		}
	}
}
