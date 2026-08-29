# macOS launchd seat management (D-4 recipe, field-proven 2026-07-21)

**Problem:** launchd cannot reliably exec scripts on external volumes (persistent
"Interrupted system call", exit 126) even when the same script runs fine from a shell.

**Fix — exec-through wrapper:** install `scripts/arbseat-launcher` to the internal disk
(`cp scripts/arbseat-launcher ~/bin/ && chmod +x ~/bin/arbseat-launcher`). It just
`exec`s the repo's `agent-redis-bridge-systemd`, so the repo copy stays the single source
of truth. Point plists at the wrapper.

**Plist shape (panel consensus — sol/grok/GLM consult):**
- `ProgramArguments = ["/Users/<user>/bin/arbseat-launcher", "<engine>-<workspace>-<role>"]`
  — write the array atomically (`plutil -replace ProgramArguments -json '[...]'`), never
  per-index edits; verify with a full `plutil -p`.
- `KeepAlive = {Crashed: true}` ONLY. Never `true`/`SuccessfulExit:false` — the bridge exits
  nonzero on boot-lease conflicts and those shapes thrash-restart against the lease.
- `RunAtLoad = false` — the external volume may not be mounted at login; kickstart-on-demand.

**Operational notes:**
- Bridges RETRY lease acquisition: start the launchd child first, then kill any ad-hoc
  holder; the child claims the identity when the holder's lease lapses (~90s TTL).
- A healthy ping can be a STALE holder — always check the registry timestamp is newer than
  your restart before declaring success.
- Kill daemons by ps-enumerated pid with a per-pid cmdline ownership check; never pgrep
  patterns (self-matching shells, title rewrites, cross-project collisions).
- SNAPSHOT every plist (plutil -convert json) before batch edits, and check for bespoke
  seats first: direct internal-disk invocations (e.g. some pi-sdk seats) never had the
  exec problem and must NOT be flattened onto the wrapper.

Companion decisions/incident record: codex-arb-artifacts `auto-memory/DECISIONS.md` (D-4,
D-12); playbook artefact `orchestrator-panel-arbitration-playbook` v4 addendum.
