package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func baseValid() *Config {
	return &Config{Host: "127.0.0.1", Port: "6379", DB: "12", Prefix: "agent_scratch:",
		From: "claude-bridge-dev", Branch: "dev"}
}

func TestValidateOK(t *testing.T) {
	if err := baseValid().validate(); err != nil {
		t.Errorf("valid config rejected: %v", err)
	}
}

func TestValidateRequiresFrom(t *testing.T) {
	c := baseValid()
	c.From = ""
	if err := c.validate(); err == nil || !strings.Contains(err.Error(), "from") {
		t.Errorf("empty From must error about 'from', got %v", err)
	}
}

func TestValidateRequiresNonEmptyBranch(t *testing.T) {
	// detached HEAD yields "" -> the bridge would reject envelope-invalid invalid-branch;
	// guard it at the client edge instead.
	c := baseValid()
	c.Branch = "   "
	if err := c.validate(); err == nil || !strings.Contains(err.Error(), "branch") {
		t.Errorf("blank Branch must error about 'branch', got %v", err)
	}
}

func TestValidateRequiresBus(t *testing.T) {
	c := baseValid()
	c.Host = ""
	if err := c.validate(); err == nil || !strings.Contains(err.Error(), "REDIS") {
		t.Errorf("empty Host must error about REDIS host, got %v", err)
	}
}

func TestLoadEnvFileFillsMissingOnly(t *testing.T) {
	// an env file supplies values; explicit (already-set) fields are NOT overwritten.
	dir := t.TempDir()
	envFile := filepath.Join(dir, "seat.env")
	os.WriteFile(envFile, []byte("AGENT_REDIS_HOST=10.0.0.5\nAGENT_REDIS_PORT=6380\n# comment\nAGENT_REDIS_DB=3\nexport AGENT_REDIS_PREFIX=pfx:\n"), 0o600)

	c := &Config{Host: "already-set"} // pre-set host must win over the file
	if err := c.loadEnvFile(envFile); err != nil {
		t.Fatal(err)
	}
	if c.Host != "already-set" {
		t.Errorf("env file overwrote a pre-set field: Host=%q", c.Host)
	}
	if c.Port != "6380" || c.DB != "3" || c.Prefix != "pfx:" {
		t.Errorf("env file did not fill missing fields: %+v", c)
	}
}

func TestTLSPrecedenceFirstSourceWins(t *testing.T) {
	// fill-missing for the TLS bool: a process-env explicit "0" must NOT be flipped
	// on by a later env-file "1" (a plain bool can't record "explicitly off", so the
	// first source that provides AGENT_REDIS_TLS must win).
	c := &Config{}
	c.applyEnv(func(k string) string { // process env: TLS off
		if k == "AGENT_REDIS_TLS" {
			return "0"
		}
		return ""
	})
	c.applyEnv(func(k string) string { // env file: TLS on
		if k == "AGENT_REDIS_TLS" {
			return "1"
		}
		return ""
	})
	if c.TLS {
		t.Errorf("env-file TLS=1 overrode process-env TLS=0; want TLS off (fill-missing)")
	}
}

func TestLoadEnvFileMissingIsError(t *testing.T) {
	c := &Config{}
	if err := c.loadEnvFile("/no/such/file.env"); err == nil {
		t.Errorf("missing env file must error (existence-checked guard)")
	}
}
