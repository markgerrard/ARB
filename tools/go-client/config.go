package main

import (
	"bufio"
	"errors"
	"fmt"
	"os"
	"strings"
)

// Config holds the resolved Redis connection + sender identity. Resolution order
// mirrors the Python tool: explicit flags / process env first, then an --env-file
// fills only what's still missing.
type Config struct {
	Host, Port, DB, Prefix string
	TLS                    bool
	tlsSet                 bool // first source providing AGENT_REDIS_TLS wins (fill-missing)
	User, Password         string
	From, Branch           string
}

// validate enforces the structural guards the design calls for — turning the shell
// recipe's silent footguns into loud, typed errors.
func (c *Config) validate() error {
	if strings.TrimSpace(c.From) == "" {
		return errors.New("--from (or FROM_AGENT_ID) is required: the bridge's sender policy only trusts specific ids; no silent legacy default")
	}
	if strings.TrimSpace(c.Branch) == "" {
		return errors.New("--branch is required and must be non-empty (a detached HEAD yields \"\", which the bridge rejects as envelope-invalid invalid-branch)")
	}
	if c.Host == "" || c.Port == "" {
		return errors.New("AGENT_REDIS_HOST and AGENT_REDIS_PORT are required (the bus is unresolved)")
	}
	return nil
}

// applyEnv fills ONLY missing fields from a getter (process env or env-file map).
func (c *Config) applyEnv(get func(string) string) {
	setIf := func(dst *string, key string) {
		if *dst == "" {
			if v := get(key); v != "" {
				*dst = v
			}
		}
	}
	setIf(&c.Host, "AGENT_REDIS_HOST")
	setIf(&c.Port, "AGENT_REDIS_PORT")
	setIf(&c.DB, "AGENT_REDIS_DB")
	setIf(&c.Prefix, "AGENT_REDIS_PREFIX")
	setIf(&c.User, "AGENT_REDIS_USER")
	setIf(&c.Password, "AGENT_REDIS_PASSWORD")
	if !c.tlsSet {
		// A plain bool can't record "explicitly off", so the FIRST source that
		// provides AGENT_REDIS_TLS wins — matching the Python ${AGENT_REDIS_TLS:-…}
		// precedence (process env before env file), so an env-file "1" cannot flip
		// on a process-env "0".
		if v := get("AGENT_REDIS_TLS"); v != "" {
			switch v {
			case "1", "true", "True", "yes":
				c.TLS = true
			}
			c.tlsSet = true
		}
	}
	setIf(&c.From, "FROM_AGENT_ID")
	setIf(&c.Branch, "BRANCH")
}

// loadEnvFile fills missing fields from a KEY=VALUE file. The file MUST exist
// (existence-checked guard); a missing env file is a loud error, not a silent skip.
func (c *Config) loadEnvFile(path string) error {
	f, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("env file: %w", err)
	}
	defer f.Close()
	vals := map[string]string{}
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		line = strings.TrimPrefix(line, "export ")
		k, v, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		vals[strings.TrimSpace(k)] = strings.Trim(strings.TrimSpace(v), `"'`)
	}
	if err := sc.Err(); err != nil {
		return err
	}
	c.applyEnv(func(k string) string { return vals[k] })
	return nil
}

// --- key helpers (mirror redis_io.py RedisConfig) ---

func (c *Config) inboxKey(agentID string) string { return c.Prefix + "agent:" + agentID + ":inbox" }
func (c *Config) statusKey(taskID string) string { return c.Prefix + "task:" + taskID + ":status" }
func (c *Config) resultKey(taskID string) string { return c.Prefix + "task:" + taskID + ":result" }
