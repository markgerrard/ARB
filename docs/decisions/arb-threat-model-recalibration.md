# ARB threat-model recalibration — mistakes, not a malicious orchestrator

**Date.** 2026-06-19. **Decider.** Mark (warm-Opus orchestration session).

## Decision

ARB's gate enforcement should be sized to **catch mistakes**, not to **resist an adversary**.
For ARB's actual operating context — **solo, on Mark's own infrastructure, orchestrated by his own
warm-Opus** — there is **no malicious orchestrator** in the threat model. Nobody is forging reviewer
reports or fabricating which-model-answered to sneak bad code past the gate. So the right enforcement
is *mistake-catching*; *adversary-resisting* machinery is over-engineering until ARB is operated by a
party that isn't trusted.

## What this means for the backlog

- **#6 (gate authenticates reviewer_reports) + #14 (bridge immutable `from→task` ledger):
  productization-era — NOT deferred-and-named, just not needed now.** Both defend against an
  *adversarial orchestrator* (report-forgery / which-model-answered fabrication). That adversary does
  not exist for solo use. Build them **only if** ARB becomes multi-party, is operated by someone Mark
  doesn't trust, or ships as a product others run. Reviewer-attestation is additionally *leaky by
  construction* for the in-process Agent-tool path (the orchestrator can read/alter anything an
  in-process subagent writes; cryptographic signing can't hold a secret the orchestrator can't read),
  so even building it wouldn't cleanly cover the cold-Opus certifier.
- **#5 (gate enforces author-non-quorum) + #10 (spec-panel flags load-bearing opens): known minor
  polish.** Cheap accident-guards, but the accident-level checks that *mattered* already shipped —
  model-swap / seat-identity / scribe-exclusion (#13) and the fixture-masks-reality discipline + Tier-1
  reliability (#8/#9/#11). Optional; do them if convenient, skip without loss.

## Why (the discipline turned on the backlog itself)

Match enforcement to **verified stakes**. ARB's solo stakes are *"I might make a mistake moving
fast,"* not *"an adversary is subverting my gate."* The same threat-model honesty that makes ARB good
(name the limit, don't ship a fake guarantee) says: *don't build a defense for a threat you don't
have.* Building adversary-grade authentication for accident-grade stakes is the
tool-building-becomes-the-work trap. ARB is **trustworthy enough to use now** — that was the win
condition, not an empty backlog. See `quorum-decision-taxonomy.md` and the bridge-protocol §6a
standing checks (the *accident-level* bars that are worth keeping).

## Settled companion fact (don't re-chase)

Agent SDK on a Claude **subscription draws from the plan** (Anthropic's June-15 metered-credit change
is *paused*). The free, plan-funded, context-isolated Claude path in this setup is the **in-process
Agent tool** (cold-Opus and the Haiku scribe run there free). The bridge's agent-sdk engine requires a
vendor **key by its own design** — `isolated_env` (`skills/.../agent_sdk_models.py`) deliberately wipes
inherited `ANTHROPIC_*` creds; that cred-wipe **is** the decorrelation guarantee (a bridge seat can't
silently be the orchestrator's own subscription session). "Decorrelated bridge-Claude" therefore
inherently needs a separate key. This is **not** an Anthropic constraint and is **not** a config bug —
do not "fix" it.
