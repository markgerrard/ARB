package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// authorityArgs builds the python3 -m dispatch_cli argv for ordinary enqueue.
// Production symbol: authorityEnqueue and tests both call this — no mirrored copy.
func authorityArgs(spec dispatchSpec, cfg *Config, envFile string, dryRun bool) ([]string, error) {
	if spec.ArtefactID == "" || spec.Version < 1 || spec.ReceiptPath == "" || spec.BriefPath == "" {
		return nil, fmt.Errorf(
			"ordinary request/worktree_run requires --artefact-id --version --receipt --brief " +
				"(Slice 1d-iv: enqueue only via dispatch_authority)",
		)
	}
	args := []string{
		"-m", "agent_redis_bridge.dispatch_cli",
		"--caller-identity", "go",
		"--target-agent-id", spec.To,
		"--sender", cfg.From,
		"--branch", cfg.Branch,
		"--run-id", spec.RunID,
		"--artefact-id", spec.ArtefactID,
		"--version", strconv.Itoa(spec.Version),
		"--receipt", spec.ReceiptPath,
		"--brief", spec.BriefPath,
	}
	if envFile != "" {
		args = append(args, "--env-file", envFile)
	}
	if dryRun {
		args = append(args, "--dry-run")
	}
	if spec.Operation == "worktree_run" {
		args = append(args, "--operation", "worktree_run", "--worktree-lease", spec.WorktreeLease)
	}
	// Worktree is a typed payload key — route via --worktree-json, not extra.
	// (extra_payload-injected typed keys are dropped by the authority guard).
	if spec.Operation == "" && spec.Worktree != "" {
		cleanup := spec.WorktreeCleanup
		if cleanup == "" {
			cleanup = "keep"
		}
		wt := map[string]string{"name": spec.Worktree, "cleanup": cleanup}
		if spec.WorktreeBase != "" {
			wt["base_ref"] = spec.WorktreeBase
		}
		wtJSON, mErr := json.Marshal(wt)
		if mErr != nil {
			return nil, mErr
		}
		args = append(args, "--worktree-json", string(wtJSON))
	}
	// Extra payload fields the bridge still consumes (claim/lane stay outside task).
	extra := map[string]any{}
	if len(spec.Artifacts) > 0 {
		extra["expected_artifacts"] = spec.Artifacts
	}
	if len(spec.Allowed) > 0 {
		extra["allowed_paths"] = spec.Allowed
	}
	if spec.CommitMsg != "" {
		extra["commit_message"] = spec.CommitMsg
	}
	if spec.Structured {
		extra["expect_structured"] = true
	}
	if spec.Fresh {
		extra["fresh_context"] = true
	}
	if spec.ThreadID != "" {
		extra["thread_id"] = spec.ThreadID
	}
	if spec.ForkThread != "" {
		extra["fork_from_thread_id"] = spec.ForkThread
	}
	if spec.AuditPanel {
		extra["audit_vote_expected"] = true
	}
	if spec.PanelInputLocked {
		extra["panel_input_locked"] = true
	}
	if spec.ReasoningEffort != "" {
		extra["reasoning_effort"] = spec.ReasoningEffort
	}
	if spec.TurnTimeout != nil {
		extra["turn_timeout"] = *spec.TurnTimeout
	}
	if len(extra) > 0 {
		b, mErr := json.Marshal(extra)
		if mErr != nil {
			return nil, mErr
		}
		args = append(args, "--extra-payload-json", string(b))
	}
	return args, nil
}

// authorityPrepare builds the exec.Cmd authorityEnqueue will run — argv via
// authorityArgs, child env with the publish credential stripped. Tests assert
// on this production command, not a reconstructed copy.
func authorityPrepare(spec dispatchSpec, cfg *Config, envFile string, dryRun bool, baseEnv []string) (*exec.Cmd, error) {
	args, err := authorityArgs(spec, cfg, envFile, dryRun)
	if err != nil {
		return nil, err
	}
	repoRoot, err := findRepoRoot()
	if err != nil {
		return nil, err
	}
	cmd := exec.Command("python3", args...)
	cmd.Dir = repoRoot
	// Non-FABA: strip publish credential.
	cmd.Env = filterEnv(baseEnv, "ARB_MEMORY_REDIS_URL")
	cmd.Env = append(cmd.Env, "PYTHONPATH="+filepath.Join(repoRoot, "src"))
	return cmd, nil
}

// authorityEnqueue shells out to the Python dispatch_authority CLI for ordinary
// request / worktree_run. Go never constructs or rpush'es those envelopes itself
// after Slice 1d-iv. Returns request_id (and for dry-run, the envelope JSON on
// stdout already printed by the CLI).
func authorityEnqueue(spec dispatchSpec, cfg *Config, envFile string, dryRun bool) (requestID string, envelopeJSON string, err error) {
	cmd, err := authorityPrepare(spec, cfg, envFile, dryRun, os.Environ())
	if err != nil {
		return "", "", err
	}
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		msg := strings.TrimSpace(stderr.String())
		if msg == "" {
			msg = err.Error()
		}
		return "", "", fmt.Errorf("dispatch_authority: %s", msg)
	}
	out := strings.TrimSpace(stdout.String())
	if dryRun {
		return "", out, nil
	}
	var meta struct {
		RequestID string `json:"request_id"`
		OK        bool   `json:"ok"`
	}
	if err := json.Unmarshal([]byte(out), &meta); err != nil {
		return "", "", fmt.Errorf("authority metadata: %w (stdout=%q)", err, out)
	}
	if meta.RequestID == "" {
		return "", "", fmt.Errorf("authority returned no request_id: %s", out)
	}
	return meta.RequestID, "", nil
}

// authorityWaitReply waits for the reply to a request_id already enqueued by the authority.
func authorityWaitReply(cfg *Config, requestID string, timeout time.Duration) (int, error) {
	// Body already enqueued by the authority; only wait on the from-inbox.
	c, err := dial(cfg)
	if err != nil {
		return 1, err
	}
	defer c.Close()
	deadline := time.Now().Add(timeout)
	fromInbox := cfg.inboxKey(cfg.From)
	for time.Now().Before(deadline) {
		remain := time.Until(deadline)
		block := 5
		if remain < 5*time.Second {
			block = int(remain.Seconds())
			if block < 1 {
				block = 1
			}
		}
		raw, err := c.DoBlocking(block, "BLPOP", fromInbox, strconv.Itoa(block))
		if err != nil || raw == nil {
			continue
		}
		// BLPOP returns [key, value]
		pair, ok := raw.([]interface{})
		var body []byte
		if ok && len(pair) == 2 {
			switch v := pair[1].(type) {
			case []byte:
				body = v
			case string:
				body = []byte(v)
			}
		}
		if body == nil {
			continue
		}
		cls, pp := classifyReply(body, requestID)
		switch cls {
		case classMine:
			fmt.Println(string(pp.Raw))
			if pp.Ok {
				return 0, nil
			}
			return 1, nil
		case classNotify:
			continue
		default:
			// Re-queue sibling replies.
			_, _ = c.Do("RPUSH", fromInbox, string(body))
		}
	}
	fmt.Fprintf(os.Stderr, "timed out waiting for reply to %s\n", requestID)
	return 124, nil
}

func findRepoRoot() (string, error) {
	// Prefer walking up from this binary's location; fall back to cwd.
	candidates := []string{}
	if exe, err := os.Executable(); err == nil {
		candidates = append(candidates, filepath.Dir(exe), filepath.Join(filepath.Dir(exe), "..", ".."))
	}
	if wd, err := os.Getwd(); err == nil {
		candidates = append(candidates, wd)
	}
	for _, c := range candidates {
		root, err := filepath.Abs(c)
		if err != nil {
			continue
		}
		// Walk up looking for src/agent_redis_bridge
		for dir := root; dir != filepath.Dir(dir); dir = filepath.Dir(dir) {
			if st, err := os.Stat(filepath.Join(dir, "src", "agent_redis_bridge", "dispatch_cli.py")); err == nil && !st.IsDir() {
				return dir, nil
			}
		}
	}
	return "", fmt.Errorf("cannot locate bridge repo root (need src/agent_redis_bridge/dispatch_cli.py)")
}

func filterEnv(env []string, dropKey string) []string {
	out := make([]string, 0, len(env))
	prefix := dropKey + "="
	for _, e := range env {
		if strings.HasPrefix(e, prefix) {
			continue
		}
		out = append(out, e)
	}
	return out
}
