// Command go-client is a stateless Go client edge for the AgentRedisBridge: dispatch
// work into the fleet and read task status/result over the envelope/Redis contract,
// from any host, with zero clone/venv. Typed flags replace the fragile shell recipe —
// the \n / backtick task-body trap is impossible by construction (argv string, never
// shell-interpolated), --from is required (no silent legacy default), --branch is
// non-empty-guarded (no detached-HEAD invalid-branch).
//
// Usage:
//
//	go-client dispatch --to <agent-id> --from <id> --branch <b> [--env-file f] \
//	    {--run-id r | --adhoc} --artefact-id ID --version N --receipt PATH --brief PATH \
//	    [--audit-panel] [--worktree name ...] [--timeout secs] [--dry-run-envelope]
//	go-client dispatch ... --run-id r --operation worktree_arm --worktree name [--worktree-base oid] [--lease-ttl secs]
//	go-client dispatch ... --run-id r --operation worktree_run --worktree-lease id \
//	    --artefact-id ID --version N --receipt PATH --brief PATH
//	go-client dispatch ... --run-id r --operation worktree_release --worktree-lease id
//
// Ordinary request/worktree_run enqueue via Python dispatch_authority (Slice 1d-iv).
// Lifecycle arm/release remain Go-built taskless envelopes.
// --run-id/--adhoc is required unless --dry-run-envelope (which never touches the bus).
//
//	go-client status --to <task-id> [--env-file f]
//	go-client result --to <task-id> [--env-file f]
package main

import (
	"flag"
	"fmt"
	"os"
	"time"
)

type stringSlice []string

func (s *stringSlice) String() string     { return fmt.Sprint(*s) }
func (s *stringSlice) Set(v string) error { *s = append(*s, v); return nil }

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: go-client {dispatch|status|result} ...")
		os.Exit(2)
	}
	switch os.Args[1] {
	case "dispatch":
		os.Exit(runDispatch(os.Args[2:]))
	case "status", "result":
		os.Exit(runCtl(os.Args[1], os.Args[2:]))
	default:
		fmt.Fprintf(os.Stderr, "unknown subcommand %q (want dispatch|status|result)\n", os.Args[1])
		os.Exit(2)
	}
}

func newConfigFromFlags(fs *flag.FlagSet, envFile string) (*Config, error) {
	cfg := &Config{}
	cfg.applyEnv(os.Getenv) // process env first
	if envFile != "" {
		if err := cfg.loadEnvFile(envFile); err != nil { // then fill missing from file
			return nil, err
		}
	}
	return cfg, nil
}

func runDispatch(args []string) int {
	fs := flag.NewFlagSet("dispatch", flag.ContinueOnError)
	var (
		to            = fs.String("to", "", "target agent id (required)")
		from          = fs.String("from", "", "sender agent id (required; or FROM_AGENT_ID)")
		branch        = fs.String("branch", "", "branch (required, non-empty; or BRANCH)")
		envFile       = fs.String("env-file", "", "env file to fill missing Redis/identity config")
		timeoutSecs   = fs.Int("timeout", 1800, "seconds to wait for the reply")
		turnTimeout   = fs.Int("turn-timeout", 0, "ceiling for one task engine turn; a dispatch may contain multiple turns")
		runID         = fs.String("run-id", "", "observability run id")
		auditPanel    = fs.Bool("audit-panel", false, "mark the seat's reply for vote recording (needs run-id)")
		panelLocked   = fs.Bool("panel-input-locked", false, "lock input for a certifying task when audit recording is intentionally disabled")
		worktree      = fs.String("worktree", "", "run on a fresh worktree of this name")
		worktreeBase  = fs.String("worktree-base", "", "base ref for --worktree")
		worktreeClean = fs.String("worktree-cleanup", "", "keep|auto for --worktree")
		operation     = fs.String("operation", "", "isolated worktree_arm|worktree_run|worktree_release mode")
		worktreeLease = fs.String("worktree-lease", "", "bridge-minted worktree lease id")
		leaseTTL      = fs.Int("lease-ttl", 0, "worktree arm lease lifetime in seconds")
		threadID      = fs.String("thread-id", "", "resume this conversation thread")
		forkThread    = fs.String("fork-thread-id", "", "fork this conversation thread")
		structured    = fs.Bool("expect-structured", false, "ask for a structured reply")
		fresh         = fs.Bool("fresh-context", false, "reset engine context first")
		effort        = fs.String("effort", "", "per-dispatch reasoning effort (low|medium|high|xhigh|max|ultra; codex only)")
		commitMsg     = fs.String("commit-message", "", "orchestrator-commit message")
		dryRun        = fs.Bool("dry-run-envelope", false, "print the exact envelope bytes, do not send")
		adhoc         = fs.Bool("adhoc", false, "explicit one-off dispatch that needs no run-id label")
		retryStart    = fs.Bool("retry-engine-start", false, "re-dispatch ONCE (fresh envelope id, same deadline) on the transient engine-start cold-start flake (DSP-1)")
		artefactID    = fs.String("artefact-id", "", "pre-minted artefact id (ordinary request/worktree_run)")
		version       = fs.Int("version", 0, "pre-minted artefact version (ordinary request/worktree_run)")
		receiptPath   = fs.String("receipt", "", "path to target-bound harness publish receipt JSON")
		briefPath     = fs.String("brief", "", "path to original brief bytes (legacy hash-verified)")
	)
	var artifacts, allowed stringSlice
	fs.Var(&artifacts, "expected-artifact", "expected artifact path (repeatable)")
	fs.Var(&allowed, "allowed-path", "allowed write-path prefix (repeatable)")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	turnTimeoutSet := false
	leaseTTLSet := false
	fs.Visit(func(f *flag.Flag) {
		if f.Name == "turn-timeout" {
			turnTimeoutSet = true
		}
		if f.Name == "lease-ttl" {
			leaseTTLSet = true
		}
	})
	if turnTimeoutSet && *turnTimeout >= *timeoutSecs {
		fmt.Fprintln(os.Stderr, turnTimeoutWarning(*turnTimeout, *timeoutSecs))
	}

	// Mirror agent-dispatch's hard gate: a real dispatch needs either --run-id (panel/
	// multi-round workflow, so arb-watch shows a label and audit/vote evidence records) or
	// --adhoc (an explicit, conscious "this one-off needs no label"). --dry-run-envelope
	// never touches the bus, so it's exempt — same as the Python tool's --check/--dry-run-envelope.
	if !*dryRun && *runID == "" && !*adhoc {
		fmt.Fprintln(os.Stderr, "error: pass --run-id ID (panel/multi-round dispatch) or --adhoc (explicit one-off, no label needed)")
		fmt.Fprintln(os.Stderr, "       (or use scripts/dispatch-dev instead, which auto-defaults --run-id for you)")
		return 2
	}

	cfg, err := newConfigFromFlags(fs, *envFile)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return 2
	}
	// explicit flags override env/file
	if *from != "" {
		cfg.From = *from
	}
	if *branch != "" {
		cfg.Branch = *branch
	}
	if err := cfg.validate(); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return 2
	}
	if *to == "" {
		fmt.Fprintln(os.Stderr, "error: --to (target agent id) is required")
		return 2
	}
	spec := dispatchSpec{
		To: *to, RunID: *runID,
		Operation: *operation, WorktreeLease: *worktreeLease,
		Worktree: *worktree, WorktreeBase: *worktreeBase, WorktreeCleanup: *worktreeClean,
		ThreadID: *threadID, ForkThread: *forkThread, CommitMsg: *commitMsg,
		AuditPanel: *auditPanel, PanelInputLocked: *panelLocked, Structured: *structured, Fresh: *fresh,
		ReasoningEffort: *effort,
		TurnTimeout:     nil,
		Artifacts:       artifacts, Allowed: allowed,
		TaskArgs:     fs.Args(),
		ArtefactID:   *artefactID,
		Version:      *version,
		ReceiptPath:  *receiptPath,
		BriefPath:    *briefPath,
	}
	if leaseTTLSet {
		spec.LeaseTTL = leaseTTL
	}
	if turnTimeoutSet {
		spec.TurnTimeout = turnTimeout
	}

	// Ordinary request / worktree_run: Python dispatch_authority only.
	if isOrdinaryAuthorityPath(spec.Operation) {
		reqID, envJSON, err := authorityEnqueue(spec, cfg, *envFile, *dryRun)
		if err != nil {
			fmt.Fprintln(os.Stderr, "error:", err)
			return 2
		}
		if *dryRun {
			fmt.Println(envJSON)
			db := cfg.DB
			if db == "" {
				db = "0"
			}
			fmt.Fprintf(os.Stderr, "dry-run: NOT sent — authority dry-run for %s on %s:%s db=%s\n",
				*to, cfg.Host, cfg.Port, db)
			return 0
		}
		fmt.Fprintf(os.Stderr, "task-id: %s\n", reqID)
		_ = retryStart // cold-start retry is lifecycle/engine-path; authority path waits once
		code, err := authorityWaitReply(cfg, reqID, time.Duration(*timeoutSecs)*time.Second)
		if err != nil {
			fmt.Fprintln(os.Stderr, "error:", err)
		}
		return code
	}

	// Lifecycle arm/release: Go still builds + enqueues the taskless envelope.
	env, err := buildEnvelope(spec, cfg)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return 2
	}

	if *dryRun {
		b, err := env.Marshal()
		if err != nil {
			fmt.Fprintln(os.Stderr, "error:", err)
			return 1
		}
		fmt.Println(string(b))
		db := cfg.DB
		if db == "" {
			db = "0"
		}
		fmt.Fprintf(os.Stderr, "dry-run: NOT sent — would RPUSH to %s on %s:%s db=%s\n", cfg.inboxKey(*to), cfg.Host, cfg.Port, db)
		return 0
	}

	code, err := dispatch(cfg, env, time.Duration(*timeoutSecs)*time.Second, *retryStart)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
	}
	return code
}

func turnTimeoutWarning(turnTimeout, clientTimeout int) string {
	return fmt.Sprintf(
		"warning: --timeout %d gives up before one requested --turn-timeout %d can complete",
		clientTimeout, turnTimeout,
	)
}

func runCtl(cmd string, args []string) int {
	fs := flag.NewFlagSet(cmd, flag.ContinueOnError)
	taskID := fs.String("to", "", "task id")
	taskIDAlt := fs.String("task-id", "", "task id (alias)")
	envFile := fs.String("env-file", "", "env file")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	id := *taskID
	if id == "" {
		id = *taskIDAlt
	}
	if id == "" {
		fmt.Fprintln(os.Stderr, "error: --task-id is required")
		return 2
	}
	cfg, err := newConfigFromFlags(fs, *envFile)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return 2
	}
	if cfg.Host == "" || cfg.Port == "" {
		fmt.Fprintln(os.Stderr, "error: AGENT_REDIS_HOST/PORT required")
		return 2
	}
	var code int
	if cmd == "status" {
		code, err = ctlStatus(cfg, id)
	} else {
		code, err = ctlResult(cfg, id)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
	}
	return code
}
