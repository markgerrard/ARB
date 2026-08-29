# Session handoff — Open Interpreter vs Pi bake-off

Snapshot for continuing on the Mac Mini. All repository work described here is committed on
`feat/openinterpreter-pi-bakeoff`. No scored benchmark dispatch has run.

## Resume

```sh
cd /Users/<user>/AgentRedisBridge
git fetch origin
git switch feat/openinterpreter-pi-bakeoff
git pull --ff-only
tmux new -s oi-bakeoff
```

Read these before acting:

1. `AGENTS.md`
2. `skills/using-agent-bridge/SKILL.md`
3. `docs/pipeline-operating-manual.md`
4. `docs/superpowers/specs/2026-07-13-openinterpreter-pi-isolated-bakeoff-design.md`
5. The design review brief for this bake-off (panel feedback on the initial design)

Workflow B was explicitly selected. Mark selected the inline warm orchestrator to author the initial
design, spec, and plan drafts. The design is not approved yet; spec and plan have not been authored.

## Current decision and scope

The experiment is two pairwise comparisons, never a four-seat leaderboard:

- GLM-5.2: Pi versus Open Interpreter `zcode`.
- Kimi K2.7: Pi versus Open Interpreter `kimi-cli`.

The design now requires 128 scored cells: two pairs, two arms, eight Implementor Bench tasks, four
repetitions. It requires a controller-only evidence clone plus a disposable clone, linked worktree,
daemon identity, state home, tool boundary, and engine process per cell. It also requires a separate
scorer sandbox and quarantined Git importer. Section 6 of the design lists fourteen readiness
blockers. Do not start scored work until every blocker is evidenced green.

MiniMax, OpenCode Go, reviewer ranking, automatic routing, production daemons, and cross-model
GLM-vs-Kimi claims remain out of scope.

## Open Interpreter findings already verified

- Local Open Interpreter version was `0.0.21` at `/Users/<user>/.local/bin/interpreter`.
- The binary exposes `interpreter app-server --listen stdio://` even though some docs show a
  standalone `interpreter-app-server` binary.
- A direct subclass of the current Codex adapter completed the initialize/thread/turn handshake.
- Every adapter-used JSON-RPC schema compared byte-identically with local Codex; Open Interpreter
  adds provider/model/harness schemas.
- Headless harness config works for `harness = "zcode"` and `harness = "kimi-cli"`.
- Live exact-response probes passed for GLM-5.2 under zcode and Kimi `k2p7` under kimi-cli.
- The current Codex adapter drops the concrete `turn.error.message` when `turn/completed` has failed;
  the new engine must fix that path.
- The intended implementation is a thin `InterpreterEngine(CodexEngine)`, not a copied adapter.

## Audited design panels

All four rounds are reconcile-closed with `outcome=emitted`:

| Round | Run ID | Certifying result |
|---|---|---|
| initial | `panel-oi-pi-design-20260713T200405Z-e7860d` | Codex, GLM, and agy all `needs-changes` |
| r1 | `panel-oi-pi-design-r1-20260713T201310Z-b4be47` | Codex `approve`; GLM and agy `needs-changes` |
| r2 | `panel-oi-pi-design-r2-20260713T202018Z-ea529c` | Codex and agy `needs-changes`; GLM `timed-out` |
| r3 | `panel-oi-pi-design-r3-20260713T203132Z-c76c24` | Codex `approve`; agy `needs-changes`; GLM `timed-out` |

The current fold at commit `e534d4e` addresses r3 by:

- splitting tool-plane secret/network-denial checks from the engine-control-plane egress check;
- requiring identical canonical mount paths so a linked worktree's `gitdir:` pointer resolves;
- treating the entire cell Git directory as hostile;
- importing only a validated bundle through a no-network Git importer sandbox with hooks, external
  helpers, non-local protocols, and ambient config disabled.

That fold was reviewed on the Mac Mini in r4 under
`panel-oi-pi-design-r4-20260714T032024Z-919665`; audit-close returned `outcome=emitted`.
Codex and agy independently returned `needs-changes/P1`. GLM was operator-cancelled for excessive
latency and recorded honestly as `timed-out`; its Pi SDK seat was stopped and GLM is excluded from
subsequent panels for now. A project-local Grok ACP reviewer was exact-probed successfully and stood
up as `grok-agentredisbridge-mini-oi-r4-review` to restore a decorrelated static-review lens. The next
action is to verify and fold the surviving r4 P1s, then re-panel the changed design with the available
Codex, agy, and Grok seats. Do not call the design approved while the r4 P1s survive.

## GLM reviewer failure and replacement

Do not use `pi-rpc` for the next panel. Two fresh `pi-rpc` GLM review dispatches repeated the same
wedge:

- normal read/search tool progress;
- then extended thinking with no further event;
- provider socket disappeared;
- Pi process remained at 0% CPU, blocked in `kevent`;
- bridge cancellation also failed to complete, so the ephemeral daemon had to be terminated.

Use `pi-sdk`. It initially failed because the local SDK host dependency link was absent. The supported
repair was run successfully:

```sh
tools/pi-sdk-host/install.sh
cd tools/pi-sdk-host
node -e "Promise.all([import('@earendil-works/pi-coding-agent'),import('@earendil-works/pi-ai')]).then(()=>console.log('OK'))"
node --test deps.test.mjs
```

This linked the globally installed Pi `0.80.3`; imports and both dependency tests passed. A live ARB
exact-response probe through `pi-sdk` with `zai/glm-5.2` also passed. Re-run the install script on the
Mac Mini because `node_modules` is machine-local and ignored by Git.

Start the next GLM reviewer with a new role/agent ID, a fresh empty `PI_CODING_AGENT_DIR`, and
`--engine pi-sdk --model zai/glm-5.2 --pi-tools read,grep,find,ls`. Auth material must come from the
machine's private Pi store or environment and must never enter a brief, argv, Git, or audit payload.

## r4 operational sequence

1. Confirm the VPN is connected and both buses work:

   ```sh
   redis-cli -n 12 PING
   set -a
   source ~/.config/arb-memory/redis-memory.env
   set +a
   .venv/bin/python - <<'PY'
   import os, redis
   print(redis.from_url(os.environ["ARB_MEMORY_REDIS_URL"]).ping())
   PY
   ```

2. Stand up ephemeral project-local reviewers only: Codex, Pi SDK GLM, and agy/Gemini. Use
   `roles/reviewer.md`, fresh contexts, read-only tool ceilings, and new role suffixes. Run an exact
   auth probe through each actual engine configuration before adding it to the roster. Current
   `pi-sdk` and `agy-print` log `fresh-context-unsupported`; after probing either one, stop it and
   launch the panel reviewer under a new agent ID and empty state directory. Do not review in the
   probe's accumulated conversation.
3. Mint a new run ID. Emit the exact roster manifest first. Dispatch every bridge seat with
   `--audit-panel --run-id`, one isolated review worktree per seat, all from the current immutable
   feature-branch commit.
4. The r4 brief must verify the r3 fold in outcome and hunt for new contradictions. In particular:
   linked-worktree Git operations at canonical paths, hostile Git metadata quarantine, separate
   tool/control-plane probes, scorer confinement, effective reasoning equality, G0/G1 timeout
   attribution, and executable `seat-preflight` semantics.
5. Record absent seats honestly, but re-fire an absent certifying lens against the folded artefact.
   Close only through `scripts/arb-audit-close-request`; do not announce a verdict unless it prints
   `outcome=emitted`.
6. If r4 is clean, update the design status and commit it. Then author the implementation spec,
   panel it, remediate/re-panel, author the executable plan, and panel that. Implementation starts
   only after those Workflow B stages are clean.

The certifying authored-stage quorum is Codex/GPT + Pi/GLM + agy/Gemini. Cold Opus remains
non-certifying for these Anthropic-authored stages. If resuming in Claude Code with a native cold
Opus subagent available, include it as the non-certifying cold lens; do not substitute a same-model
Codex subagent and label it Opus.

## Credentials and safety

Kimi, MiniMax, and OpenCode Go credentials were pasted into chat earlier. Treat all of them as
exposed and rotate them before any seat preflight or scored run. No credential value is stored in
this branch or handoff. The replacement Kimi key was used only in isolated live probes and was not
persisted by this work.

No production daemon was installed or changed. All temporary review daemons and review worktrees
were stopped and removed before handoff.

## Commits on this feature branch

Newest repository commits before this handoff note:

- `54c3366` — record design-panel audit state
- `e534d4e` — quarantine untrusted Git imports
- `2c11608` — make the seat preflight executable and classify timeouts
- `8a730ec` — separate engine, tool, and scorer runtime planes
- `28c00b5` — harden per-cell isolation and evidence identity
- `4ee791f` — initial isolated bake-off design and review brief

Checks run after the design edits: `git diff --check`, `scripts/check-doc-recipes`, and
`scripts/check-doc-drift`. The doc-index checker has eleven pre-existing unindexed-doc failures; this
work did not add a reported index failure. The active checkout was clean at handoff.

## Task 13 implementation handoff — 2026-07-14

Tasks 1–12 are integrated at the base used for this worktree. Task 13 adds only its declared
integration/deny-proof tests and the three declared documentation updates. No live bakeoff,
provider calibration, pilot, scored cell, or Task 14 action was run.

Hermetic verification commands:

```sh
env -u IMPLBENCH_BATTERY_KEY .venv/bin/python -m pytest -q
PYTHONPATH=bench env -u IMPLBENCH_BATTERY_KEY .venv/bin/python -m pytest -q bench/implbench/tests
scripts/check-doc-recipes
scripts/check-doc-drift
git diff --check
```

The Task 13 tests prove the 128-cell known-good close/evidence path, all close-phase restart
points, stop and infrastructure branches, sealed-package immutability, receipt-only scored
dispatch, secret/extension/active-checkout denial, scorer confinement, and classifier deny
proofs. Credentials remain external to the repository. Any live readiness or calibration result
must be recorded only under the controller-owned evidence root by the separate Task 14 gate.
