# CDX-1 V1 protocol probe — raw wire captures (codex-cli 0.142.5, 2026-07-08)

Design gate V1 of `docs/superpowers/specs/2026-07-08-cdx1-approval-handling-design.md`:
drive the real `codex app-server` (stdio) with `approvalPolicy: "on-request"` and a task
that must run a command, and pin the approval wire shapes + the fail-closed claim
empirically. Each `run-*.jsonl` is the complete bidirectional message log
(`{"ts", "dir": "in"|"out"|"summary", "msg"}`); the task asked codex to `touch` a
sentinel file, so "did the command execute" is a filesystem fact, not an inference.

## Findings (each cited to its run)

1. **Sandbox gates the ask, not just approvalPolicy** (run-A, first attempt with
   `workspace-write`): an in-sandbox command under `on-request` runs with NO approval
   request at all. Asks fire only for commands the sandbox refuses (`read-only` +
   `touch` reliably produces one). Consequence: the CDX-1 hang needs
   non-bypass + non-trusted + a sandbox-exceeding command — narrower than the audit
   implied, but the same P0 when it hits.
2. **Method name confirmed**: `item/commandExecution/requestApproval` (runs A/B/C).
   Legacy `execCommandApproval`/`applyPatchApproval` never observed on 0.142.5; the
   impl still answers them defensively.
3. **The request carries its own decision vocabulary**: `params.availableDecisions`
   (observed: `"accept"`, `{"acceptWithExecpolicyAmendment": ...}`, `"cancel"`), plus
   `command`, `cwd`, `reason`, `commandActions`. The impl mirrors cursor's
   option-picker: choose `cancel` to deny, `accept` for the trusted-drift allow.
4. **Unlisted decision strings coerce to non-acceptance** (run-A: replied
   `{"decision": "denied"}` — not a listed decision — sentinel NOT created,
   `serverRequest/resolved` emitted, `item/completed` with no execution, turn
   completed normally). Deny-and-continue is confirmed viable.
5. **Error response ⇒ NO execution** (run-B: replied JSON-RPC `-32601` — sentinel NOT
   created, turn completed normally). **The load-bearing fail-closed claim holds on
   this binary**; the design's deny-all-unknowns escalation contingency stays dormant.
6. **Canonical deny pinned** (run-C): `{"decision": "cancel"}` → resolved, no
   execution, clean completion.
7. **Server-initiated requests use their own id namespace starting at 0** (all runs),
   overlapping client ids — the CDX-4 per-side id guard is load-bearing for any
   mid-turn client `request()`.

Probe script: reproduced from the session scratchpad; the artifact (these JSONL logs)
is the evidence, the script is trivial to re-derive from `codex.py`'s handshake.
