---
name: "mac-mini-quirks"
description: "This machine's traps: BSD userland, launchd vs external volume, RUST_MIN_STACK, corrupt target/, pid-kill discipline"
metadata:
  type: reference
  origin_session_id: "seeded-by-claude-orchestrator-20260721"
  last_write_session_id: "019f89ed-be79-7782-aebf-9bd60e3e13c5"
  source_project_key: "workspace-dev-bcd89c27363b"
---

- BSD userland: no `timeout` command (use bounded loops or gtimeout); zsh does not word-split
  unquoted $vars (wrap loops in bash -c); grep is ugrep-flavored in some shells — prefer
  fixed-string -F or simple patterns.
- launchd CANNOT exec scripts on /Volumes/<workspace> (persistent EINTR/126). Seats use the
  internal-disk wrapper ~/bin/arbseat-launcher; full recipe in ARB repo
  docs/macos-launchd-seats.md. RunAtLoad stays false (volume mount order). After plist
  environment edits, reload with `launchctl bootout` → `launchctl bootstrap` (if bootstrap
  returns I/O error 5, wait around 3 seconds and retry) → `launchctl kickstart`. `kickstart -k`
  alone restarts the stale loaded definition, so new plist environment values do not apply;
  bootstrap alone leaves these no-RunAtLoad seats loaded but not running.
- codex-rs integration tests need RUST_MIN_STACK=8388608 (tokio stack overflow at default 2MiB
  in debug — not a product bug).
- Mass test timeouts ≠ host degradation: check for a corrupt worktree target/ dir first and
  attribute with controlled ABAB base/branch reruns before blaming the machine (a reboot was
  once chased for a phantom).
- Kill daemons by ps-enumerated PID with a per-pid cmdline ownership check — never pgrep
  patterns (self-matching shells, process.title rewrites, cross-project collisions). A live
  ping after a restart may be a STALE lease holder: check the registry timestamp is fresh.
- Full workspace cargo test ≈ 1 min warm but release builds ≈ 30 min; targeted suites are
  seconds — see operator-rules for the cadence.
- Every bridge-dev seat is launchd-managed (labels `com.example.arbseat.*`, `com.example.codex-bridge.bridge-dev-sol`,
  `com.example.agy-bridge.bridge-dev`, `com.example.pi-*-bridge.bridge-dev`, …) — kickstart, never
  hand-launch. The arbseat bootout→bootstrap race can leave a unit loaded-but-not-running with
  rc=0; check the pid in `launchctl list` and kickstart stragglers.
- Git origins on Mark's Macs are written HTTPS but transport over SSH via a global `insteadOf`
  rewrite (servers use SSH outright) — code that reasons about remotes must not trust remote-URL
  text on a Mac.
