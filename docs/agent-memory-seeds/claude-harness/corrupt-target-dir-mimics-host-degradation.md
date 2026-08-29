---
name: corrupt-target-dir-mimics-host-degradation
description: Corrupt cargo target/ artifacts caused mass test event-timeouts that mimicked host degradation (S4 incident 2026-07-16); attribution must include worktree/target state as a variable
metadata: 
  node_type: memory
  type: feedback
  originSessionId: d9f054cc-ff55-4a3e-9fee-68a5305bb571
---

During the codex bg-wake S4 gate (2026-07-16), mass "timeout waiting for event" failures (200-589 per run, worsening with run length) were misattributed to host degradation (pmset hang) and a reboot was done. The real cause: corrupt/stale build artifacts in one worktree's cargo `target/` dir (suite binaries built mid-incident at 06:15; same cargo fingerprint name as healthy builds but divergent size; zero-byte .rmeta residue). Proof: 2×2 swap — same commit + same cwd + healthy `CARGO_TARGET_DIR` = clean; luna target = dirty×3; bisect/base = clean×7.

**Why:** "mass-failure = host" pattern-matches convincingly (individually-passing tests, broad module spread), but a per-worktree carrier produces the identical signature. The pre-reboot diagnosis never ran a base control.

**How to apply:**
- A mass-failure gate means HOST *only if a base control in the SAME environment also mass-fails*. Always run ABAB base/branch controls before blaming host or branch.
- Add **worktree/target state** to the attribution variable list: same commit in a fresh worktree (with helper binaries built — `cargo build --bin codex`, else ~50 CLI tests fast-fail NotFound and de-load the host, masking contention diseases) discriminates content vs state.
- Cheap cure test: `CARGO_TARGET_DIR=<healthy target>` swap without touching anything.
- Remedy: quarantine (`mv target target.diseased-<date>`), rebuild fresh.
- Red herring catalog: nextest without `RUST_MIN_STACK=8MiB` (justfile sets it) deterministically stack-overflows tokio workers on this repo — unrelated to any real failure.
- Full record: `panels/impl-s4-bulk-failure-attribution.md` in codex-arb-artifacts. Related: [[mac-mini-disk-and-rust-env]].
