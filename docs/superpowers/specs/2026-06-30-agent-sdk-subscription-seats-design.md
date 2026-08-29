# Agent-SDK subscription seats (Claude-on-plan) — design

> Status: **design, APPROVED TO BUILD** (2026-06-30), panel-reviewed (see below). Spike proven (branch
> `spike/agent-sdk-subscription-opus`, commit `61e0fa9`; haiku-4.5 ran on the plan, granular streaming,
> 27/27 vendor-lane tests green) — but the spike bypassed load-bearing paths; **do not certify on it.**
> Memory: `[[agent-sdk-subscription-seat]]`.

## Panel review outcome (2026-06-30)

Reviewed by the canonical quorum — **codex (GPT-5.5) + cold-Opus + agy-print (Gemini) + pi-GLM** — all
returned **SOUND_WITH_CHANGES** (architecture right; build-blocked by fixable changes, no redesign).
Reports collected as dispatch replies (no in-repo write → review hygiene held). Pre-build fixes folded:

- **PB-0 (P0, unanimous + warm-Opus verified) — `subscription_env` must ZERO shadow keys, not omit.**
  The SDK merges parent `os.environ` *before* `options.env` (`subprocess_cli.py:431-436`), so an omitted
  key survives and an inherited `ANTHROPIC_API_KEY` wins by precedence → silent API-key billing. Fix:
  (a) **overwrite** shadow keys to `""` via the existing **prefix sweep** (`SENSITIVE_PREFIXES`), not the
  enumerated set, preserving `CLAUDE_CODE_OAUTH_TOKEN`/`CLAUDE_CONFIG_DIR`; (b) fail-loud guard inspects
  the **final merged env** (inherited ∪ overlay) before connect; (c) **empirical** deny-proof — inject
  `ANTHROPIC_API_KEY` into `os.environ`, assert it's neutralized AND the seat transacts on the plan, not
  shape-only. (The spike passed only because this host has no `ANTHROPIC_API_KEY` — fixture-masks-reality.)
- **PB-1 (P1, codex, verified) — `build_engine` rejects empty `key_env`.** `bridge.py:2389` raises
  "provider key missing" before the subscription branch. Skip provider-key loading for `spec.subscription`;
  require `CLAUDE_CODE_OAUTH_TOKEN` (or a usable login) instead. Add bridge-integration test for
  `--engine agent-sdk --model opus-4.8` with no `AGENT_SDK_*` key.
- **PB-2 (P1, all 4) — reviewer must be COLD.** Engine resumes `last_session_id` (`agent_sdk.py:146`); a
  standing reviewer that resumes is warm-masquerading-as-cold and launders decorrelation. Reviewer seat
  runs fresh-context (`oneshot`/`resume=None`) as an **asserted invariant**; implementors may resume.
- **PB-3 (P1, cold-Opus + pi-GLM, verified) — OAuth-token scrub is vendor-shaped.** Scrub var-names key
  off `auth_var()` = `ANTHROPIC_API_KEY`, never `CLAUDE_CODE_OAUTH_TOKEN` (`agent_sdk.py:131,459,463`), so
  the token has weaker log/tee scrubbing than vendor keys → can leak into the eval/visibility tee. Branch
  the scrub set on `spec.subscription` to include `CLAUDE_CODE_OAUTH_TOKEN` + the literal value, in the
  session store, `_handle_stderr`, and `_scrub_payload`. Test: injected token never appears in a captured
  payload/stderr line.
- **PB-4 (P1, cold-Opus + pi-GLM) — the codex→seat→verdict round-trip is UNPROVEN and is the whole point.**
  Promote open-question to a **gating E2E acceptance test**: a codex orchestrator dispatches a real review
  to the opus-4.8 seat over the bus and consumes the verdict, asserting it came from the plan-authed seat.
- **PB-5 (P1, agy) — per-seat `CLAUDE_CONFIG_DIR`.** Concurrent subscription seats sharing `~/.claude`
  race on the CLI's SQLite state. Give each seat an isolated config dir; the OAuth token (env) bypasses
  keychain so isolation doesn't break auth.
- **PB-6 (operator decisions, 2026-06-30):** certifier rule → **audit-detect** (record invoking
  orchestrator identity/model per run; post-hoc flag bridge-Opus-fired-in-a-CC-Opus-run; surface, not
  block). haiku Bash → **off by default** (add per-task with justification). Concurrency → **1 Opus / 2
  implementors** semaphore per bridge process; a cross-process Redis cap is future hardening if these
  seats scale across processes/hosts.
- **Audit-detect visibility limit:** the bridge can always record the sender id from the dispatch envelope.
  It records the orchestrator model only when the dispatch payload explicitly self-declares
  `orchestrator_model`; if absent or inaccurate, the double-Opus heuristic can false-negative. The flag is
  surface-not-block, so misclassification is a wrong audit marker, not a containment decision.
- **P2s folded:** specify subscription `key` = OAuth value used *only* for scrubbing (never `auth_var`
  injection; assert no `auth_var` key set); token-absent fail-loud at construction; state explicitly that
  **Bash + OAuth = exfiltratable** (gate governs tool-name not Bash args → containment rests on per-seat
  revoke + mistakes-not-malice, NOT structural — don't overclaim); reword "per-seat token bounds blast
  radius" → bounds *revocation*, not quota/exfil; add a `SEAT_ENABLED` kill switch (ARB-Email pattern);
  keep the `--bare` assert.

## Purpose

Stand up **standing bridge seats that run Claude models on Mark's Claude subscription** (plan OAuth, not
an Anthropic API key), over the existing `agent_sdk` engine (`claude_agent_sdk`): an **Opus-4.8 cold
reviewer** and **Sonnet-4.6 / Haiku-4.5 implementor** seats.

**The motivating use case is a non-Claude-Code orchestrator — specifically codex.** When Claude Code is
the orchestrator it spawns its own cold-Opus reviewer and Claude implementors as in-session subagents
(the Agent/Task tool). **A codex orchestrator has no such subagent mechanism** — it cannot stand up a
cold-Opus seat at all. These bridge seats are how codex (or any non-Claude-Code orchestrator) gains
access to a decorrelated cold-Opus reviewer and Claude implementors, dispatched over the bus. Drawing
from the plan pool (not API $) is what makes standing seats affordable to leave running.

## Scope (this phase)

- **IN:** agent-sdk engine, subscription-OAuth lane, three seats (opus-4.8 reviewer, sonnet-4.6 +
  haiku-4.5 implementors), per-seat token provisioning, containerisation, threat-model write-up, tests.
- **OUT (deferred to a later phase):** the **pi** harness on-plan. pi-ai v0.79.9 natively supports
  Claude subscription OAuth (`anthropic_oauth`/`claudecode` provider, browser login → `auth.json`;
  proven by Mark logging in 2026-06-30) — a real future option, but **agent-sdk first for BOTH reviewer
  and implementor roles** (Mark's call). Recorded so the panel doesn't re-open it.

## Decisions (resolved before panel)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Harness | **agent-sdk only** this phase (`claude_agent_sdk`); pi deferred |
| 2 | Auth | **Subscription OAuth**, per-seat `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN` (1-yr token, mountable secret). No API key. Draws from plan usage pool. |
| 3 | Cred-wipe | **Optional**, via `ModelSpec.subscription` flag. Default `False` → vendor seats keep the existing cred-wipe sandbox unchanged. `True` → `subscription_env()` (inverse of `isolated_env`): preserve OAuth, drop only shadow keys. |
| 4 | Seats | opus-4.8 (reviewer), sonnet-4.6 + haiku-4.5 (implementors) |
| 5 | Policy basis | Help-article "Agent SDK / third-party-app usage draws from your plan" (June-15 separate-billing pause in effect). Solo/own-use = sanctioned "ordinary use"; the legal-doc "use API key / on behalf of users" caution targets reselling/multi-user, N/A here. |
| 6 | Bare mode | **Forbidden** — `--bare` ignores `CLAUDE_CODE_OAUTH_TOKEN`. SDK-client path is not bare; add a guard/assert. |

## The subscription lane (productionize the spike)

Spike diff (to promote from throwaway):
- `ModelSpec.subscription: bool = False` (the optional-wipe lever).
- `subscription_env(*, base)` — keep inherited env incl. `CLAUDE_CODE_OAUTH_TOKEN` + logged-in
  `CLAUDE_CONFIG_DIR`; drop `SUBSCRIPTION_SHADOW_KEYS` (the `ANTHROPIC_*` key/base-url + `AGENT_SDK_*`
  vars that would shadow the OAuth).
- `AgentSdkEngine._build_options` branches on `spec.subscription`: subscription lane uses no throwaway
  config_dir, no vendor key; session-store scrub var = `CLAUDE_CODE_OAUTH_TOKEN`.
- Model specs `opus-4.8` / `sonnet-4.6` / `haiku-4.5` (subscription=True, real model_ids).

To finish for prod (panel to confirm):
- Update `test_agent_sdk_models.py::test_three_logical_models_with_short_slugs` (now 6 models) — make it
  assert the vendor set AND the subscription set explicitly, not a bare count.
- Dispatch/`--engine`/`--model` wiring: confirm the bridge can launch a seat with `--model opus-4.8`
  and that role-profile + ceiling flow through.
- A **fail-loud assert** if a subscription seat starts with any shadow key present in its final env
  (deny-proof the "API key silently shadows the plan" footgun) and if `--bare` is set.

## Auth / credential provisioning

- **Per-seat token.** Each seat gets its own `setup-token` (independent revocation; no coupling to the
  host's interactive login; matches Mark's existing multi-headless-server pattern). One account → pooled
  plan usage limits across all seats.
- **Cross-platform (verified, `claude_agent_sdk/_internal/session_resume.py:340-359`):** precedence
  `ANTHROPIC_API_KEY` > `CLAUDE_CODE_OAUTH_TOKEN` > config-dir/keychain. macOS=Keychain; Linux=plaintext
  `~/.claude/.credentials.json` (`/home/claude/...`); **`CLAUDE_CODE_OAUTH_TOKEN` bypasses both** → the
  OS-agnostic container path.
- **Secret handling:** token in an `envs/*.env` (0600, gitignored) like other creds; piped to hosts,
  never echoed. Rotation: 1-yr expiry → calendar a regen; document revoke-in-account-settings.

## Seat family (roles, ceilings)

| Seat | Model | Role | Tool ceiling (proposal — panel to set) |
|---|---|---|---|
| reviewer | opus-4.8 | cold reviewer | read-only (Read/Grep/Glob/LS + local-memory read MCP); **no mutation** |
| implementor-s | sonnet-4.6 | implementor | read + write/edit + Bash (trusted policy) |
| implementor-h | haiku-4.5 | implementor | read + write/edit + Bash (cheap/mechanical) |

**Certifying — RESOLVED by the use case.** The bridge Opus seat exists for orchestrators that *can't*
spawn a cold-Opus subagent (codex). In a **codex-orchestrated run it is the sole Opus voice**, so it
**can certify** — `[[opus-cannot-certify-twice]]` (at most ONE Opus seat per certifying quorum) is
satisfied, not violated. The rule it must still honour is a *usage* rule, not a code constraint: **do not
run the bridge Opus seat AND a cold-Opus subagent as two certifiers in the same run** (that would be two
Opus = one voice, double-counted). So: Claude-Code orchestrator → use its native cold-Opus subagent;
codex/other orchestrator → use this bridge seat as the single Opus certifier. The seat itself is
capability-identical either way; the constraint lives in the orchestration recipe.

## Containment / threat model

A subscription seat is **credentialed-by-design**: it holds plan OAuth and can act as Mark against
Anthropic — the deliberate inverse of the bridge cred-wipe decorrelation guarantee
(`[[arb-threat-model-recalibration]]`, `[[structural-not-configurational-containment]]`). Mitigations:
- **Per-seat token** bounds blast radius (revoke one without touching the host or other seats).
- **`can_use_tool` gate + tool ceiling** remain the real guard on what a turn can DO (unchanged from the
  vendor lane; the reviewer is read-only by ceiling).
- The flag defaults OFF, so this posture is opt-in per seat, never accidental.
- Fail-loud if a shadow key leaks into a subscription seat's env (prevents silent API-key billing AND
  prevents a vendor key masquerading as plan auth).

## Containerisation

- `claude` CLI in the image (`npm i -g @anthropic-ai/claude-code`); `claude_agent_sdk` in the venv.
- Inject `CLAUDE_CODE_OAUTH_TOKEN` as a mounted secret; **do not** pass `--bare`.
- launchd (mac-mini fleet) and/or compose (containers) seat definitions, modelled on the existing
  agent-sdk / pi dev-fleet seats (`[[bridge-dev-fleet-launchd]]`).
- Note: bridge/agy seats run on the **mac-mini fleet**, not the arb-prod droplet (door-only). Container
  target is for the fleet, not the MCP door host.

## Test plan

- Unit: `subscription_env` drops every shadow key + preserves OAuth/config-dir; `_build_options`
  subscription branch wires no vendor key and the right scrub var; updated model-set assertion.
- Deny-proof: a subscription seat with an injected `ANTHROPIC_API_KEY` in `os.environ` must NOT carry it
  into the seat env (and ideally fail loud), proving the plan isn't silently shadowed.
- Live (cheap): a haiku-4.5 seat one-shot on the plan (the spike, kept as a smoke test).
- Regression: full vendor-lane suite stays green (cred-wipe unchanged).

## Resolved questions (panel + operator, 2026-06-30)

1. **Tool ceilings** — reviewer (opus-4.8): `Read,Grep,Glob,LS` + auto local-memory reads, **no Bash, no
   mutation** (unanimous). implementor-s (sonnet-4.6): `Read,Grep,Glob,LS,Write,Edit,MultiEdit,Bash`.
   implementor-h (haiku-4.5): same **minus Bash by default** (add per-task) — operator decision.
2. **Token** — **per-seat** (independent revocation; quota pooled regardless).
3. **Session continuity** — reviewer **fresh-context** (PB-2); implementors may resume within a work item.
4. **Concurrency** — semaphore: **1 Opus / 2 implementors** per bridge process (operator decision) +
   `SEAT_ENABLED` kill switch. Cross-process enforcement is future hardening.
5. **Run location** — **mac-mini fleet first** (logged-in); container parity later (OAuth env bypasses keychain).
6. **Codex round-trip** — now a **gating E2E** (PB-4), not an open question.
7. **Certifier enforcement** — **audit-detect** (PB-6).

## References

- Spike: branch `spike/agent-sdk-subscription-opus` @ `61e0fa9`; harness
  `/Users/<user>/.claude/jobs/.../tmp/spike_subscription_opus.py`.
- Code: `src/agent_redis_bridge/engines/agent_sdk_models.py`, `agent_sdk.py`.
- Memory: `[[agent-sdk-subscription-seat]]`, `[[agent-sdk-engine]]`,
  `[[bridge-seat-role-bound-at-launch]]`, `[[opus-cannot-certify-twice]]`, `[[arb-threat-model-recalibration]]`.
