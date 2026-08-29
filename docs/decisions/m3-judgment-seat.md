# M3 as the non-quorum judgment-tier bridge seat — spec, plan, decision log

**Goal.** Stand up MiniMax-M3 as a **read-only, investigating, non-quorum bridge seat** so the
autonomous-mode oracle's **judgment tier** has the decorrelated adjunct its admissibility bar
requires — flipping this host from `decorrelated-seat: no` (judgment-class posture work *parks*) to
`decorrelated-seat: yes` (it *delivers*). This is the delivery half of the autonomous-mode hardening
merged at `70f8ade`.

**Run mode.** Autonomous (user out). Branch-only; `main-inert` undeclared → default NOT inert →
**stage on branches, no merge, no push**; user reviews/merges on return. The run *builds* the
decorrelated seat, so it can't self-review — the **tri-model panel (codex/agy/cold-Opus)** reviews.

## The admissibility bar (the spec the seat must satisfy)
From the hardened `SKILL.md` (§ Oracle mechanism → judgment tier): a real **non-quorum bridge seat**
per `using-agent-bridge` — **not** bare-API, **not** a voting-seat model family (codex/agy/cold-Opus),
**not** a same-family substitute under park-pressure. Plus the non-negotiable **read-only** posture
(an oracle that can write is a contamination surface; breaks "independent of what it checks").

## Hard requirements
1. **M3** (the decorrelated model), on **our direct MiniMax key** (`~/.pi/agent/auth.json["minimax"]`
   / `ARB_MINIMAX_KEY`) — not OpenRouter, not a different model.
2. **Read-only, verified by DENY-PROOF** — a write attempt must be *refused*, confirmed by the
   filesystem (the file is not created), not by the agent's self-report. Config-assumption is not
   acceptance (it already bit us once — see decision log).
3. **Investigating** — can read/grep/find/ls the checkout to trace reachability (the judgment tier's
   value over a bare diff-read).
4. Real bridge seat (envelope/audit/role-profile/trusted-sender via `using-agent-bridge`).

## DECISION LOG

### D1 — mini-agent-acp REJECTED (deny-proof failed). [resolved]
First attempt used the existing `mini-agent-acp` engine (MiniMax's Mini-Agent). **Deny-proof FAILED:**
the smoke test's write attempt *succeeded* — `write_file` created `/tmp/m3-deny-proof.txt`
(filesystem-confirmed). Root cause: mini-agent runs its **own internal tools**; the bridge only
mediates `session/request_permission`, which `write_file` does **not** use, so the bridge's
cancel-all gate never sees the write. mini-agent's config can't separate read from write
(`enable_file_tools` is read+write bundled; no approval/read-only mode — only `enable_*` booleans).
Options were toolless-mini-agent (read-only by construction but no investigation) or abandon. **Chose
abandon** — the user steered to "M3 with pi with our key" / "anthropic agent sdk with m3 key", both of
which give read-only **with** investigation. mini-agent daemon torn down; its key-bearing
`~/.mini-agent/config.yaml` removed. *Lesson recorded in memory: deny-proof, never config-assume.*

### D2 — evaluate TWO paths, build+test both, recommend (user picks at morning). [open → this run]
Per user direction ("explore both paths"). Final choice surfaced for morning review, not picked
silently.

## The two paths

### PATH 1 — pi-rpc + minimax/MiniMax-M3 + `--pi-tools read,grep,find,ls`
A **documented, zero-new-code** bridge pattern (`.env.pi-dev.example`):
`--engine pi-rpc --model minimax/MiniMax-M3 --pi-tools read,grep,find,ls --role-profile-file roles/judgment-oracle.md`.
- **Our key by construction:** pi reads `~/.pi/agent/auth.json` (the same file the eval normalizer
  pulls the minimax key from) → drives `minimax/MiniMax-M3` directly, not OpenRouter.
- **Read-only is BRIDGE-ENFORCED:** `--pi-tools` is an allowlist (`read,grep,find,ls`) — write/edit/bash
  are not in it. Stronger than engine-config; the bridge gates the tool surface.
- **Investigates:** read/grep/find/ls are present.
- Registers `pi-agentredisbridge-dev` (or a role-suffixed id). Real bridge seat.
- Verified prerequisites: pi 0.79.3 installed; auth.json has minimax key; bridge supports the flags.

### PATH 2 — Anthropic Agent SDK driving M3 (read-only tool allowlist)
A **new bridge engine** (codex TDD). Drives M3 via the Anthropic-compatible `/anthropic` endpoint with
the MiniMax key, exposing a **read-only tool allowlist** (no write tool defined → read-only by
construction). Exact SDK form determined during the run (probe `claude-agent-sdk`'s base_url/model
override; fall back to an `anthropic`-SDK tool-use loop if the Claude Code SDK can't target M3).
- Read-only by **construction** (only read tools defined — absence-based, the strongest form).
- Investigates via the defined read tools.
- New code → codex TDD + tri-model review.

## Read-only enforcement comparison (the crux)
| | PATH 1 (pi) | PATH 2 (agent-sdk) |
|---|---|---|
| Mechanism | bridge `--pi-tools` allowlist (enforced) | only read tools defined (by construction) |
| New code | none (documented pattern) | a new engine (build + review) |
| Investigation | read/grep/find/ls | the read tools we define |
| Key | our minimax key via pi auth | our minimax key via SDK base_url |

## Comparison criteria (decide the recommendation)
Read-only **deny-proof pass** (mandatory gate for both); investigation quality on a real smoke;
maintenance surface (zero-code vs new engine); fidelity (M3-in-a-real-harness); fit to the
admissibility bar.

## Plan (autonomous pipeline)
- **Stage 0 (this doc) → panel** the spec/plan (tri-model design).
- **Stage 2 — codex TDD:** PATH 2 engine on a worktree (tests first). PATH 1 is config standup (no
  codex) + its tests are the deny-proof + functional smoke.
- **Stage 3–4 — tri-model review** of PATH 2 code + the design; remediate (loop 3 usual / 5 max).
- **Stage 5 — stage both on branches** (no merge/push; main-inert default).
- **Test both:** deny-proof (write refused, filesystem-confirmed) + functional smoke (investigates +
  emits a sensible ORACLE-CLEAN/FLAG verdict).
- **Decision doc:** PATH 1 vs PATH 2 with evidence + recommendation → user's morning call.

## Credential handling
Use the existing MiniMax key (`~/.pi/agent/auth.json["minimax"]` / `ARB_MINIMAX_KEY`); never echo it.
Pooled across normalizer + judgment seat (one key on the plan) — unexpected MiniMax rate-limiting
would be the shared key, documented, not a bug.

## Parked for morning review
- **Final PATH 1 vs PATH 2 choice** — built+tested+recommended; user picks.
- **Persistent daemon install** (systemd/launchd) — a *deployment* (irreversible-signature) = park.
  This run tests with **ephemeral daemons killed after**; it does not install a persistent unit.

## DESIGN PANEL (tri-model, unanimous: PROCEED-WITH-CHANGES)
codex + agy + cold-Opus, independent. Reports `/tmp/m3-design-panel/report-*.md`. Consensus:
- **PATH 2 is dominated — do NOT build it.** Decorrelation comes from the *model* (M3), not the harness;
  PATH 2 drives the *same model, same key* → **zero added decorrelation**, plus a new engine
  (maintenance), a flakier harness, unproven tool-use fidelity, and — decisive — its **bare-API
  fallback is inadmissible by the skill's own bar** (`SKILL.md:291` red-flag; the "anthropic-SDK loop
  against M3" literally *is* the existing `AnthropicNormalizer`). Build PATH 2 only if PATH 1 can't hold.
- **PATH 2 *is* technically reachable** (live probe returned `PROBE-OK` — claude-agent-sdk drove M3 via
  `ANTHROPIC_BASE_URL` override) but that was **chat-only**; tool-use fidelity through the CLI against
  MiniMax's `/anthropic` is unproven. "Explored, viable-for-chat, not worth building" is the outcome.
- **PATH 1 is the seat.** Corrections applied: (a) read-only is **pi-enforced allowlist launched by the
  bridge**, NOT "bridge-enforced" (the bridge forwards `--tools`; it doesn't mediate calls) → the
  **deny-proof is the authority**; (b) prefer `pi-sdk` over `pi-rpc` for robustness + correct
  tool-parsing (the `pi_rpc.py:371` degenerate-string bug — does NOT bite our clean
  `read,grep,find,ls` value); (c) the deny-proof must be **multi-vector + whole-surface +
  tool-surface**, not a single write_file (the mini-agent D1 lesson).
- **cold-Opus's strongest steer:** the repo *already* has the absence-by-construction answer — the
  `ContainerDispatcher` write-deny jail (`tools/eval/confinement/`, Option C, proven vs an escaping
  seat). Running the seat jailed makes read-only **absence-by-construction**, not by-policy. Flagged as
  the stronger follow-on (a real build) — parked.

## RESULTS (autonomous run)

### PATH 1 — STOOD UP + CERTIFIED ✅ (the recommended seat)
- Seat `pi-agentredisbridge-dev-minimax-m3` online (id carries harness+model — see D3): `--engine
  pi-rpc --model minimax/MiniMax-M3 --role minimax-m3 --pi-tools read,grep,find,ls
  --role-profile-file roles/judgment-oracle.md`. (pi-sdk attempted but its Node SDK
  package `@earendil-works/pi-coding-agent` isn't symlinked on this host — `tools/pi-sdk-host/install.sh`
  needed; **follow-on**. pi-rpc is sound here: our `--tools` value is clean so the `:371` bug doesn't apply.)
- **Functional smoke PASS:** read `README.md` ✓, grep across `skills/` ✓ (investigates).
- **Hardened deny-proof PASS** (multi-vector + whole-surface + tool-surface):
  - Reported tool surface = **exactly `read, grep, find, ls`** ("no write/edit/bash/mkdir/touch/patch —
    the complete set") → `--pi-tools` is **exclusive, not additive**.
  - All 5 write vectors (write_file, bash-redirect `echo>`, `find -exec touch`, `mkdir`, repo-edit) →
    **UNAVAILABLE**.
  - Filesystem truth: all 4 sentinels **absent**; repo dirty-state hash **unchanged** (before==after).
- **Admissibility bar: satisfied.** Real non-quorum bridge seat (M3, off-quorum), our direct MiniMax
  key (pi `auth.json`, not OpenRouter), not bare-API, read-only verified. Flips `decorrelated-seat:
  yes` on this host — by-policy-verified-by-deny-proof (jail = stronger follow-on).

### PATH 2 — EXPLORED, NOT BUILT (parked for your decision)
claude-agent-sdk 0.2.103 installed; live probe drove M3 (`PROBE-OK`). Not built: panel-unanimous it's
dominated (same model/key, zero added decorrelation, inadmissible fallback, unproven tool-use). The
"build PATH 2 engine?" decision is **yours** — recommendation: don't (use PATH 1).

## PARKED FOR MORNING REVIEW (decisions surfaced, not taken)
1. **Build PATH 2 engine?** — panel rec: NO (dominated). I explored it (probe works), did not build.
2. **Jail the seat for absence-by-construction read-only?** — cold-Opus's stronger posture (run the seat
   in the `ContainerDispatcher` write-deny jail). A real follow-on build; current seat is by-policy
   verified-by-deny-proof (sufficient, but jail is stronger). Parked.
3. **Switch pi-rpc → pi-sdk?** — panel-preferred for robustness; needs `tools/pi-sdk-host/install.sh`
   to symlink the SDK. Doesn't change read-only (deny-proof is the authority). Parked.
4. **Persistent daemon install (systemd/launchd)?** — a deployment = irreversible-signature = park.
   This run used an **ephemeral** pi-rpc daemon (kill when done); no persistent unit installed.

## Artifacts
- `roles/judgment-oracle.md` — read-only [J]-class role profile.
- `tools/seat_deny_proof/` — codex-TDD deny-proof classifier (branch `feat/m3-deny-proof-harness`,
  tri-model reviewed) — reusable certifier for this + future judgment seats.
- Deny-proof evidence: `/tmp/m3-denymatrix.out` (+ this log).

### Deny-proof harness — tri-model reviewed + remediated
codex+agy+cold-Opus (reports `/tmp/m3-harness-review/`): unanimous **P0** — `classify_deny_proof`
returned PASS on an *empty* vector list (no write attempt = silent certification). **Fixed**
(`feat/m3-deny-proof-harness` @ `1a1822f`): empty vectors → INCONCLUSIVE (after the failure checks,
so a write-tool-exposing surface still FAILs); +robust import; +invalid-outcome test; 10→14 tests
green. **Adjudicated** cold-Opus's P1 (all-`unavailable` → require ≥1 `refused`) as NOT-taken with a
pinning test: an all-unavailable set + exclusive surface-PASS is by-construction read-only (the real
PATH-1 shape) — surface-check is the disambiguator; forcing a refusal would reject the strongest
posture. Documented for your override if you disagree.

## Branches (staged, NOT merged, NOT pushed)
- `feat/m3-judgment-seat` — role profile + this decision log.
- `feat/m3-deny-proof-harness` (@ `1a1822f`, off `feat/m3-judgment-seat`) — the deny-proof classifier.

Status: PATH 1 certified (M3, our key, read-only deny-proof PASS, investigates); PATH 2 explored
(drives M3) + parked (dominated); harness built (codex TDD) + tri-model reviewed + remediated.
Ephemeral pi-rpc daemon used for testing (no persistent unit installed). Branch-only; your
review/merge on return. See "PARKED FOR MORNING REVIEW" above for the 4 decisions awaiting you.

## D3 — DECISION (user, on return): GO WITH PI. [resolved]
PATH 1 (pi) is the judgment seat. Both paths were **live-verified to work** (not just from memory):
- **pi:** live read dispatch → `# Agent Redis Bridge`; read-only by **tool-absence** (write tool not
  in the surface — `UNAVAILABLE`).
- **agent-sdk:** drives M3 with real **tool-use** (`TOOL_USES: ['Read']`, correct content) and denies
  writes by **policy** (M3 requested `Write`; harness blocked → file absent). Effective, but
  read-only by *denial* not *absence* — the weaker posture. pi wins for a read-only oracle.

### agent-id convention: combine harness + model (pi runs several)
A bare `pi-<project>-<workspace>` collides when pi runs multiple models (kimi, qwen, …). So the id
carries the **model** via `--role <model>` (the bridge's id-suffix mechanism, `derive_agent_id`;
independent of `--role-profile-file`, which still sets behavior). The judgment seat is now:

```
pi-agentredisbridge-dev-minimax-m3
```
launched with: `--engine pi-rpc --model minimax/MiniMax-M3 --role minimax-m3 --pi-tools
read,grep,find,ls --role-profile-file roles/judgment-oracle.md`. Future pi model-seats follow
`pi-<project>-<workspace>-<model>` (e.g. `…-kimi-k2`, `…-qwen3`). If the *same* model serves multiple
roles under pi, extend the suffix to `<model>-<role>` (≤16 chars, lowercase/digits/hyphens).

### PATH 2 (agent-sdk) — NOT waste; the future MUTATION harness
Reframed per the forward plan: agent-sdk *is* Claude Code → **stronger for mutations**; pi's edge is
**custom/own tools**. For this *read-only* seat pi dominates, but for future **mutation-heavy** work
with Chinese models + claude.ai, the agent-sdk harness is the right tool. The probe already proved it
**drives M3 with tool-use + key override**, so the groundwork is done — when mutation work needs it,
build the agent-sdk bridge engine properly (codex-TDD + tri-model review), don't re-derive. That id
would be `agent-sdk-<project>-<workspace>-<model>` (a new engine + `ENGINE_TO_TOOL` entry).

### Tri-model review coverage (for the record)
Design of both paths: reviewed (design panel, unanimous). Deny-proof harness: reviewed + remediated.
PATH 1 pi seat: **no impl code** (config) → certified by the live deny-proof, not a code review.
PATH 2 agent-sdk engine: **not reviewed because not built** (parked) — review it when it's built for
mutation work.

## INTEGRATION (2026-06-17): merged to `main`
Both branches integrated: `main` fast-forwarded to `feat/m3-judgment-seat` (role profile + this doc),
then the two `feat/m3-deny-proof-harness` classifier commits cherry-picked on top (the older 102-line
copy of this doc on that branch was *not* brought across — it would have regressed this log). The four
PARKED decisions above remain open for follow-on: (1) build PATH 2 — rec NO; (2) jail the seat
(absence-by-construction); (3) pi-rpc → pi-sdk (needs `tools/pi-sdk-host/install.sh`); (4) persistent
launchd install. The certified seat ran ephemerally during the build; relaunch it to make the judgment
tier live.

## D4 — GLM-5.2 sibling judge seat: pi-sdk, NOT agent-sdk (2026-06-21) [resolved]
A second read-only judge seat — **GLM-5.2** — was added as a decorrelated sibling to the M3 seat
(same read-only `judgment-oracle` shape), giving the review panel a 5th independent model. It runs on
the **`pi-sdk`** engine with `--model zai/glm-5.2`; agent-id derives to
`pi-sdk-<project>-<workspace>-glm` (e.g. `pi-sdk-bridge-dev-glm`).

**Why pi-sdk and not agent-sdk (the load-bearing finding).** GLM-5.2 was first wired through the
`agent-sdk` engine (PATH 2's home), pointing at z.ai's Anthropic-compatible endpoint
`https://api.z.ai/api/anthropic`. Every real dispatch hung. Root cause — isolated by direct `curl`,
not assumed: that endpoint's time-to-first-token scales steeply with input size (13 input tokens →
~2.7s; 214 → ~9.2s), and agent-sdk sends the **full Claude Code system prompt + all six tool schemas**
(thousands of input tokens), so a real agentic request stalls past the dispatch timeout — the seat's
child sits idle on an established connection waiting for the first token. This is **not** a seat bug; it
is a z.ai latency characteristic of that endpoint at that prompt size. (Separate, smaller bug on the
same path: the agent-sdk lane model code must be plain `glm-5.2`, **never** `glm-5.2[1m]` — z.ai 400s
"Unknown Model" on the suffix and agent-sdk retries the 400 in a loop.)

**pi-sdk dodges it** because pi's built-in `zai` provider targets z.ai's **Coding-Plan** endpoint
`https://api.z.ai/api/coding/paas/v4` (OpenAI-compatible) and sends pi's lean prompt. Proven
2026-06-21: trivial dispatch ~10s; a real review-sized agentic task (read a ~390-line file, cite
line numbers) ~121s with a sharp, correct finding. `@earendil-works/pi-coding-agent` ≥ **0.79.9** has
`zai/glm-5.2` natively in its catalog (1M context), so **no** custom `~/.pi/agent/models.json` entry is
needed; on older pi, `--model zai/glm-5.1` resolves to the same server-side model. The seat reads its
z.ai key from pi's own `~/.pi/agent/auth.json` (`zai` provider), the same way the M3 seat reads minimax.

**Persistence is supervisor-agnostic.** Both judge seats are launched by the one
`scripts/agent-redis-bridge-systemd` wrapper with an instance arg (`pi-sdk-dev-minimax-m3`,
`pi-sdk-dev-glm`) plus `AGENT_*` env (model, project, workdir, env-file, read-only tool surface,
`BRIDGE_ROLE_PROFILE_FILE`). Any supervisor that keeps them alive and restarts on exit works — **launchd**
on macOS (a `~/Library/LaunchAgents/*.plist`, `KeepAlive`+`RunAtLoad`) or **systemd** on Linux (a user
unit, `Restart=always`), see [always-up-seats.md](../always-up-seats.md). The engine/model/tool config is
identical across platforms; only the supervisor manifest differs. Upgrading pi is host-level
(`npm i -g @earendil-works/pi-coding-agent@<v>` then re-run `tools/pi-sdk-host/install.sh` to refresh the
host's symlinks); a running seat keeps its in-memory pi version until restarted, so upgrades don't disturb
live seats until you cycle them.
