package main

import (
	"strings"
	"testing"
)

func specCfg() *Config { return &Config{From: "claude-bridge-dev", Branch: "dev"} }

func TestBuildEnvelopeOrdinaryRequestRefusesIndependentPath(t *testing.T) {
	// Slice 1d-iv: Go must not build ordinary envelopes; authority owns that path.
	_, err := buildEnvelope(dispatchSpec{To: "x", TaskArgs: []string{"t"}}, specCfg())
	if err == nil || !strings.Contains(err.Error(), "dispatch_authority") {
		t.Errorf("ordinary build must refuse with dispatch_authority pointer, got %v", err)
	}
}

func TestBuildEnvelopeWorktreeRunRefusesIndependentPath(t *testing.T) {
	_, err := buildEnvelope(dispatchSpec{
		To: "x", Operation: "worktree_run", WorktreeLease: "lease-1",
		TaskArgs: []string{"t"},
	}, specCfg())
	if err == nil || !strings.Contains(err.Error(), "dispatch_authority") {
		t.Errorf("worktree_run build must refuse, got %v", err)
	}
}

func TestBuildEnvelopeWorktreeArmOK(t *testing.T) {
	env, err := buildEnvelope(dispatchSpec{
		To: "x", Operation: "worktree_arm", Worktree: "faba-round-1",
		WorktreeBase: "abc", RunID: "round-1",
	}, specCfg())
	if err != nil {
		t.Fatal(err)
	}
	if env.Payload.Operation != "worktree_arm" || env.Payload.Worktree == nil {
		t.Fatalf("arm payload=%+v", env.Payload)
	}
	if env.Payload.Task != "" {
		t.Errorf("arm must be taskless, got task=%q", env.Payload.Task)
	}
}

func TestBuildEnvelopeWorktreeReleaseOK(t *testing.T) {
	env, err := buildEnvelope(dispatchSpec{
		To: "x", Operation: "worktree_release", WorktreeLease: "lease-1", RunID: "round-1",
	}, specCfg())
	if err != nil {
		t.Fatal(err)
	}
	if env.Payload.Operation != "worktree_release" || env.Payload.WorktreeLease != "lease-1" {
		t.Fatalf("release payload=%+v", env.Payload)
	}
}

func TestBuildEnvelopeArmRequiresWorktree(t *testing.T) {
	_, err := buildEnvelope(dispatchSpec{To: "x", Operation: "worktree_arm"}, specCfg())
	if err == nil {
		t.Fatal("arm without worktree must error")
	}
}

func TestIsOrdinaryAuthorityPath(t *testing.T) {
	if !isOrdinaryAuthorityPath("") || !isOrdinaryAuthorityPath("worktree_run") {
		t.Fatal("ordinary/run must be authority path")
	}
	if isOrdinaryAuthorityPath("worktree_arm") || isOrdinaryAuthorityPath("worktree_release") {
		t.Fatal("lifecycle must not be authority path")
	}
}
