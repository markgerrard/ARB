package main

import (
	"errors"
	"strings"
)

// dispatchSpec is the resolved CLI input for one dispatch (testable, no flag/IO).
type dispatchSpec struct {
	To, RunID                                       string
	Operation, WorktreeLease                        string
	LeaseTTL                                        *int
	Worktree, WorktreeBase, WorktreeCleanup         string
	ThreadID, ForkThread, CommitMsg                 string
	ReasoningEffort                                 string
	TurnTimeout                                     *int
	AuditPanel, PanelInputLocked, Structured, Fresh bool
	Artifacts, Allowed                              []string
	TaskArgs                                        []string
	// Slice 1d-iv pre-minted source (ordinary request / worktree_run).
	ArtefactID, ReceiptPath, BriefPath string
	Version                            int
}

// isLifecycleOp is true for taskless worktree_arm / worktree_release — the only
// ordinary-request-adjacent shapes Go still builds and enqueues itself.
func isLifecycleOp(op string) bool {
	return op == "worktree_arm" || op == "worktree_release"
}

// isOrdinaryAuthorityPath is true when enqueue must go through dispatch_authority.
func isOrdinaryAuthorityPath(op string) bool {
	return op == "" || op == "worktree_run"
}

// buildEnvelope turns the resolved spec + identity into a request envelope for
// lifecycle ops only. Ordinary request / worktree_run are refused here — they
// must go through the Python dispatch_authority CLI (see authorityEnqueue).
func buildEnvelope(spec dispatchSpec, cfg *Config) (*Envelope, error) {
	if isOrdinaryAuthorityPath(spec.Operation) {
		return nil, errors.New(
			"ordinary request/worktree_run must use dispatch_authority " +
				"(--artefact-id --version --receipt --brief); " +
				"Go no longer builds independent ordinary envelopes (Slice 1d-iv)",
		)
	}
	if spec.AuditPanel && spec.RunID == "" {
		// Python hard-errors this combo (agent-dispatch:166-169): a panel dispatch
		// with no run_id records zero audit AND zero eval, silently.
		return nil, errors.New("--audit-panel requires --run-id (a panel dispatch with no run_id gets zero audit AND zero eval, silently)")
	}
	if spec.Operation != "" {
		if spec.ForkThread != "" || spec.CommitMsg != "" || spec.ReasoningEffort != "" ||
			spec.AuditPanel || spec.PanelInputLocked || spec.Structured || spec.Fresh ||
			len(spec.Artifacts) != 0 || len(spec.Allowed) != 0 {
			return nil, errors.New("worktree operation mode rejects ordinary turn-dispatch fields")
		}
		payload := Payload{Operation: spec.Operation, RunID: spec.RunID}
		switch spec.Operation {
		case "worktree_arm":
			if spec.Worktree == "" {
				return nil, errors.New("--operation worktree_arm requires --worktree")
			}
			if spec.WorktreeLease != "" || strings.TrimSpace(strings.Join(spec.TaskArgs, " ")) != "" || spec.ThreadID != "" {
				return nil, errors.New("worktree_arm does not accept a task, --worktree-lease, or --thread-id")
			}
			cleanup := spec.WorktreeCleanup
			if cleanup == "" {
				cleanup = "keep"
			}
			if cleanup != "keep" || spec.TurnTimeout != nil {
				return nil, errors.New("worktree_arm requires cleanup keep and rejects --turn-timeout")
			}
			payload.Worktree = &Worktree{Name: spec.Worktree, Cleanup: cleanup, BaseRef: spec.WorktreeBase}
			if spec.LeaseTTL != nil && *spec.LeaseTTL <= 0 {
				return nil, errors.New("--lease-ttl must be a positive integer")
			}
			payload.LeaseTTL = spec.LeaseTTL
		case "worktree_release":
			if spec.WorktreeLease == "" {
				return nil, errors.New("--operation worktree_release requires --worktree-lease")
			}
			if spec.Worktree != "" || spec.WorktreeBase != "" || spec.WorktreeCleanup != "" ||
				strings.TrimSpace(strings.Join(spec.TaskArgs, " ")) != "" || spec.ThreadID != "" ||
				spec.LeaseTTL != nil || spec.TurnTimeout != nil {
				return nil, errors.New("worktree_release accepts only --worktree-lease and --run-id")
			}
			payload.WorktreeLease = spec.WorktreeLease
		default:
			return nil, errors.New("--operation must be worktree_arm, worktree_run, or worktree_release")
		}
		return &Envelope{ID: newUUID(), From: cfg.From, Branch: cfg.Branch, To: spec.To,
			Kind: "request", SentAt: nowISO(), Payload: payload, RunID: spec.RunID}, nil
	}
	return nil, errors.New("unreachable: ordinary path refused above")
}
