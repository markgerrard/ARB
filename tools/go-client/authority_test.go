package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestFilterEnvStripsPublishCredential(t *testing.T) {
	in := []string{
		"PATH=/usr/bin",
		"ARB_MEMORY_REDIS_URL=redis://memory/0",
		"AGENT_REDIS_HOST=127.0.0.1",
		"ARB_MEMORY_REDIS_URL_EXTRA=keep", // different key; must remain
	}
	got := filterEnv(in, "ARB_MEMORY_REDIS_URL")
	for _, e := range got {
		if strings.HasPrefix(e, "ARB_MEMORY_REDIS_URL=") {
			t.Fatalf("publish credential leaked into filtered env: %q", e)
		}
	}
	joined := strings.Join(got, "\n")
	if !strings.Contains(joined, "PATH=/usr/bin") {
		t.Fatalf("unrelated env dropped: %v", got)
	}
	if !strings.Contains(joined, "ARB_MEMORY_REDIS_URL_EXTRA=keep") {
		t.Fatalf("prefix-sibling key must be preserved: %v", got)
	}
	if len(got) != 3 {
		t.Fatalf("expected 3 env entries after strip, got %d: %v", len(got), got)
	}
}

func TestAuthorityEnqueueArgvAssembly(t *testing.T) {
	// Production command capture: authorityPrepare builds the real exec.Cmd
	// authorityEnqueue will run. Assert quartet flags + credential scrub on
	// that command — not on a reconstructed copy.
	spec := dispatchSpec{
		To:            "codex-bridge-dev",
		RunID:         "run-1",
		ArtefactID:    "art-brief-1",
		Version:       1,
		ReceiptPath:   "/tmp/receipt.json",
		BriefPath:     "/tmp/brief.md",
		Operation:     "worktree_run",
		WorktreeLease: "lease-1",
	}
	cfg := &Config{From: "claude-bridge-dev", Branch: "dev"}
	baseEnv := []string{
		"ARB_MEMORY_REDIS_URL=redis://leaked/0",
		"PATH=/bin",
		"AGENT_REDIS_HOST=127.0.0.1",
	}

	cmd, err := authorityPrepare(spec, cfg, "", false, baseEnv)
	if err != nil {
		t.Fatalf("authorityPrepare: %v", err)
	}

	// python3 is Args[0]; production argv follows.
	if len(cmd.Args) < 2 {
		t.Fatalf("production cmd has no argv: %v", cmd.Args)
	}
	joined := strings.Join(cmd.Args[1:], " ")
	for _, need := range []string{
		"--artefact-id art-brief-1",
		"--version 1",
		"--receipt /tmp/receipt.json",
		"--brief /tmp/brief.md",
		"--caller-identity go",
		"--operation worktree_run",
		"--worktree-lease lease-1",
	} {
		if !strings.Contains(joined, need) {
			t.Errorf("production argv missing %q; got %s", need, joined)
		}
	}

	// Captured production child env must strip publish credential.
	for _, e := range cmd.Env {
		if strings.HasPrefix(e, "ARB_MEMORY_REDIS_URL=") {
			t.Fatalf("authority child env must not carry publish credential: %v", cmd.Env)
		}
	}
	// Unrelated env from the base set must survive.
	envJoined := strings.Join(cmd.Env, "\n")
	if !strings.Contains(envJoined, "PATH=/bin") {
		t.Fatalf("unrelated base env dropped from production cmd: %v", cmd.Env)
	}

	// W3: production cmd.Dir is repo root; PYTHONPATH ends in /src.
	if cmd.Dir == "" {
		t.Fatal("production cmd.Dir must be non-empty (repo root)")
	}
	marker := filepath.Join(cmd.Dir, "src", "agent_redis_bridge", "dispatch_cli.py")
	if st, err := os.Stat(marker); err != nil || st.IsDir() {
		t.Fatalf("cmd.Dir must be repo root containing dispatch_cli.py; dir=%q err=%v", cmd.Dir, err)
	}
	pyPathFound := false
	for _, e := range cmd.Env {
		if strings.HasPrefix(e, "PYTHONPATH=") {
			pyPathFound = true
			val := strings.TrimPrefix(e, "PYTHONPATH=")
			if !strings.HasSuffix(val, "/src") && !strings.HasSuffix(val, string(filepath.Separator)+"src") {
				t.Errorf("PYTHONPATH must end in /src; got %q", val)
			}
			if !strings.Contains(val, cmd.Dir) {
				t.Errorf("PYTHONPATH must include repo root %q; got %q", cmd.Dir, val)
			}
		}
	}
	if !pyPathFound {
		t.Fatal("production cmd.Env must carry PYTHONPATH")
	}

	// W2(c): ordinary path with Worktree set (Operation == "") emits --worktree-json
	// on the captured production argv, with name/cleanup/base_ref fields.
	specWT := dispatchSpec{
		To:              "codex-bridge-dev",
		RunID:           "run-wt-1",
		ArtefactID:      "art-brief-1",
		Version:         1,
		ReceiptPath:     "/tmp/receipt.json",
		BriefPath:       "/tmp/brief.md",
		Operation:       "",
		Worktree:        "iso-wt-1",
		WorktreeBase:    "origin/dev",
		WorktreeCleanup: "keep",
		Artifacts:       []string{"report.md"},
	}
	cmdWT, err := authorityPrepare(specWT, cfg, "", false, baseEnv)
	if err != nil {
		t.Fatalf("authorityPrepare(worktree): %v", err)
	}
	argsWT := cmdWT.Args[1:]
	joinedWT := strings.Join(argsWT, " ")
	if !strings.Contains(joinedWT, "--worktree-json") {
		t.Fatalf("production argv missing --worktree-json; got %s", joinedWT)
	}
	// Extract --worktree-json value and assert fields.
	var wtRaw string
	for i, a := range argsWT {
		if a == "--worktree-json" {
			if i+1 >= len(argsWT) {
				t.Fatal("--worktree-json present without value")
			}
			wtRaw = argsWT[i+1]
			break
		}
	}
	if wtRaw == "" {
		t.Fatalf("--worktree-json value empty; argv=%v", argsWT)
	}
	var wt map[string]string
	if err := json.Unmarshal([]byte(wtRaw), &wt); err != nil {
		t.Fatalf("worktree-json not valid JSON: %v raw=%q", err, wtRaw)
	}
	if wt["name"] != "iso-wt-1" {
		t.Errorf("worktree name: want iso-wt-1 got %q", wt["name"])
	}
	if wt["cleanup"] != "keep" {
		t.Errorf("worktree cleanup: want keep got %q", wt["cleanup"])
	}
	if wt["base_ref"] != "origin/dev" {
		t.Errorf("worktree base_ref: want origin/dev got %q", wt["base_ref"])
	}
	// Must NOT pack worktree into --extra-payload-json.
	// foundExtra is required: if emission vanishes the loop body never runs and
	// a vacuous pass would hide expected_artifacts being dropped (rem4 P2-3).
	foundExtra := false
	for i, a := range argsWT {
		if a == "--extra-payload-json" {
			foundExtra = true
			if i+1 >= len(argsWT) {
				t.Fatal("--extra-payload-json present without value")
			}
			var extra map[string]any
			if err := json.Unmarshal([]byte(argsWT[i+1]), &extra); err != nil {
				t.Fatalf("extra-payload-json invalid: %v", err)
			}
			if _, ok := extra["worktree"]; ok {
				t.Fatalf("worktree must not be packed into extra-payload-json; got %v", extra)
			}
			// Non-typed fields still ride EXTRA.
			arts, _ := extra["expected_artifacts"].([]any)
			if len(arts) != 1 || arts[0] != "report.md" {
				t.Errorf("expected_artifacts should remain in extra; got %v", extra)
			}
		}
	}
	if !foundExtra {
		t.Fatalf("--extra-payload-json missing (spec carries expected_artifacts); argv=%v", argsWT)
	}
}

func TestAuthorityArgsRequiresQuartet(t *testing.T) {
	cfg := &Config{From: "claude-bridge-dev", Branch: "dev"}
	_, err := authorityArgs(dispatchSpec{To: "x"}, cfg, "", false)
	if err == nil {
		t.Fatal("expected quartet gate error, got nil")
	}
	if !strings.Contains(err.Error(), "--artefact-id") {
		t.Fatalf("error should name quartet flags, got: %v", err)
	}
}
