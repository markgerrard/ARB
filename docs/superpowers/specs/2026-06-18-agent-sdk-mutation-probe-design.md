# Spec — agent-sdk mutation probe (PATH-2 de-risking)

**Status:** design, approved 2026-06-18; revised after tri-model spec panel (codex/agy/cold-Opus,
unanimous SPEC-NEEDS-CHANGES — false-PASS + spike-first + honest-linkage findings folded in).
**Owner:** warm Opus. **Builds on:** `docs/decisions/m3-judgment-seat.md` (PATH 2, deferred).

## Goal & success bar

Prove that **`claude-agent-sdk`** (the Claude Code harness a future `agent-sdk` engine would wrap) can
drive **M3 / Kimi / GLM-5.2** through a **multi-step mutation task** (read → implement → self-verify) in
a throwaway git repo, with the result judged by a **held-out test the model never sees**. Deliverable:
a **per-model verdict** (PASS / PARTIAL / FAIL) with evidence.

**What a PASS means (scoped honestly — panel P1):** "claude-agent-sdk can drive this model to a
*genuine* code mutation in a clean cwd." It **green-lights writing an engine build-spike**, NOT the
finished engine. It explicitly does **not** retire these residual risks (they belong to the engine
build): bridge tool-*mediation* (the SDK runs tools itself — can the bridge intercept/permission-gate
its writes? the D1 mini-agent failure mode), worktree-isolated dispatch, the completion/commit gate,
and progress/steer/interrupt/stop/`TurnResult` plumbing (`src/agent_redis_bridge/engines/base.py:32`).

## Model matrix

| Model | Key (gitignored env var) | Endpoint / model-id |
|---|---|---|
| MiniMax-M3 | `AGENT_SDK_MINIMAX_KEY` | `https://api.minimax.io/anthropic`, `MiniMax-M3` (from `AnthropicNormalizer`) |
| Kimi (kimi-for-coding) | `AGENT_SDK_KIMI_KEY` | endpoint + model-id + auth-header shape **resolved by the Stage-0 spike** |
| GLM-5.2 | `AGENT_SDK_GLM_KEY` | endpoint + model-id + auth-header shape **resolved by the Stage-0 spike** |

- **Qwen-plus dropped** (no key). Keys live ONLY in `envs/agent-sdk-models-dev.env` (gitignored, 600),
  read by env-var name, never printed/committed. **Provided via chat 2026-06-18 → rotate after testing.**

## Stage 0 — go/no-go spike (BEFORE building the mutation probe) — panel P0

The whole probe rests on one unproven assumption: that `claude-agent-sdk` can be pointed at a
non-Anthropic `base_url` + key **per model** and get real tool-use. `ANTHROPIC_BASE_URL` is
**per-process, not per-call**, and `claude-agent-sdk` spawns the Claude Code CLI under the hood — so a
single-process per-model loop may be impossible, and auth-header shape / model-id validation differ per
vendor. Retire this cheaply first:

- For each model, in an **isolated subprocess** with its own env (`ANTHROPIC_BASE_URL` + key), invoke
  claude-agent-sdk on a one-shot **read-only** task: "read `READING.txt` and quote line 1." Confirm:
  the SDK drives the model, a tool call fires, the content is right.
- Record per vendor: base_url, model-id, auth-header shape, whether a header/model-name **translation
  shim** is needed (e.g. `Bearer` vs `x-api-key`), and whether per-model isolation requires a fresh
  subprocess (expected: yes).
- **Gate:** only vendors that pass Stage 0 enter the mutation matrix; the rest are recorded
  `FAIL(endpoint/auth)` with the error. Run M3 first (known-good) so a Kimi/GLM failure is
  unambiguously their endpoint, not the harness.
- Also note (for the engine, not to fix here): **does the SDK expose a tool-execution hook the bridge
  could mediate, or does it run tools internally?** (the D1 question). Record the observation.

## Architecture & components (mutation probe, post-spike)

Each model runs in its **own subprocess** with a scoped env (no global env mutation → no race — panel P2).

1. **`models.py`** — `ModelSpec{name, base_url, key_env, model_id, auth_style}` list + a loader that
   reads keys from the env (clear error if a key var is unset). No secrets in source.
2. **`fixture/`** — a small source tree copied fresh per model into a temp dir, `git init` + baseline
   commit **before the model runs** (else the diff check is vacuous — panel P2). It contains: a stubbed
   function with a precise docstring contract, and a **visible `test_contract.py`** (the failing test the
   model works against). The model's task: **"implement the function so `test_contract.py` passes" —
   it may edit ONLY the implementation file.**
3. **`held_out/`** — a **hidden** test suite (`test_heldout.py`) that exercises the same contract on
   inputs the model never sees, kept OUTSIDE the model's repo. This is the real PASS authority.
4. **`probe.py`** — per model: fresh temp repo ← `fixture/`; baseline commit; subprocess with scoped
   env; invoke claude-agent-sdk (Write/Edit/Bash enabled, `cwd`=temp repo, per-model timeout); capture
   the tool-use trace.
5. **Verifier (anti-false-PASS — panel P0)** — after the run, in a **clean checkout of `fixture/`**,
   overlay ONLY the model's implementation file, then run BOTH `test_contract.py` (pristine, original)
   AND `held_out/test_heldout.py`. **PASS** = both suites green in the clean checkout AND the model's
   repo shows the impl file changed AND `test_contract.py` byte-unchanged in the model's repo.
   **FAIL/PARTIAL** otherwise — specifically FAIL if: impl unchanged, `test_contract.py` was modified
   (gaming), held-out tests red (hardcoded to visible cases), or any file outside the impl was touched.
   This makes a PASS mean "the function is correct on held-out inputs," not "pytest exited 0."
6. **Report** — per-model table + notes (tool calls, both test results, errors), written to a results
   doc. **Secret-free, enforced (see below).**

### Data flow
`models.py` → Stage-0 gate → for each passing model (own subprocess): fresh repo ← `fixture/` →
baseline commit → claude-agent-sdk(task) → trace → verifier (clean checkout + impl overlay + contract
+ held-out) → verdict → aggregate → report.

## Error handling

- Missing key env-var → clear startup error naming the var. Auth/endpoint/SDK-spawn failure → FAIL with
  captured (scrubbed) error; continue to next model. Per-model timeout → FAIL(timeout).
- "No tool calls / no diff / test-file-modified" → never PASS (deny-proof lesson: absence/ gaming ≠
  success).

## Secret handling (tested invariant — panel P1/P2)

Keys reach the model's subprocess env (Bash is enabled, so a model *could* `echo $KEY`). Therefore:
**every captured stream (tool-use trace, stderr, the results doc) is scrubbed by exact key value AND
known key-env-var names before it is printed or persisted.** A **canary-secret unit test** seeds a
sentinel value through the capture path and asserts it is absent from stdout, trace, and results — so
"secret-free" is verified, not asserted. Each model runs in its own subprocess so env never leaks
across models.

## Testing

The probe is a test; its rigor is the held-out verifier, not self-report. The **verifier logic gets
unit tests** including the adversarial cases that would otherwise false-PASS: (a) model weakened/edited
`test_contract.py` → FAIL; (b) model hardcoded visible cases, held-out red → FAIL; (c) impl unchanged →
FAIL; (d) genuine impl → PASS. Plus the canary-secret-absence test. The harness itself is otherwise
throwaway-grade (committed for reuse, not over-tested).

## Deliverable

`tools/agent-sdk-probe/` — committed, **secret-free**: `models.py`, `probe.py`, `fixture/`,
`held_out/`, `spike.py` (Stage 0), `README.md`, and the verifier+canary unit tests. Plus a results
writeup (`docs/agent-sdk-probe-results.md`) with the per-model verdict + resolved endpoints/model-ids/
auth shapes + the D1 tool-mediation observation.

## Out of scope (YAGNI)

No bridge integration; no `agent-sdk` engine skeleton / `ENGINE_TO_TOOL` entry; no progress/steer/
stream plumbing; no raw-`anthropic` fallback (approach A only — a fallback would let an SDK failure
masquerade as success); no task *suite* (one fixed task); no Qwen; no translation-proxy unless the
Stage-0 spike proves one is required (then it's a recorded finding, not a silent build).

## Decision linkage

If a model PASSES (genuine held-out mutation), it green-lights **writing an `agent-sdk` engine
build-spike** (its own spec→plan→build), whose first job is to retire the residual risks named in the
Goal section (esp. the D1 tool-mediation question — can the bridge supervise the SDK's writes, or must
the seat be jailed at the OS/filesystem level). Per-model FAILs become recorded limits.
