# ARB `/learn` — TDD implementation plan (v2 — plan-panel absorbed)

**Spec:** `docs/superpowers/specs/2026-07-07-arb-learn-SPEC.md` (authoritative — on conflict,
spec wins). Worker: codex, isolated worktree, strict red→green per task. Tests:
`.venv/bin/python -m pytest tests/test_learn_intake.py -q` (no DB needed — every external
effect goes through injected/faked callables, mirroring `tests/test_wiki_refresh.py`).

## Task order

0. **CLI surface pinned first** (agy P1): argparse subcommands exactly —
   `propose <source-file> [--target skill|arb|project] [--force] [--from-workflow HANDOFF SHA --repo PATH]`,
   `evaluate <learn-id> [--retry]`, `promote <learn-id> [--override] [--i-am-mark]`,
   `resolve <learn-id> {approve|reject} --reason TEXT`. A help-text test pins the surface.
1. **Ids + header** — `mint_id(source_bytes, first_line)` (`learn-<slug>-<hash8>`, slug
   sanitize ≤40), `next_force_id` (`-r<N>` ordinal), `parse_header`/`render_header` (first
   line = single JSON object: status, target, proposer, supersedes, referent; body after).
   Tests incl. malformed-header fail-loud. Dedupe input = header-STRIPPED body (agy P1:
   comparing raw content would match every proposal on JSON-header boilerplate).
2. **Transport helpers** — `_run_read(sql)` builds the ssh→`docker compose exec -T memory
   python3 -` command with `capture_output=True`; inline script prints payload between
   `ARB_JSON_BEGIN`/`ARB_JSON_END`; parser extracts ONLY between sentinels (test with noisy
   banner-laden stdout). `list_proposals()` / `get_status(id)` on the spec's DISTINCT ON SQL
   (injected run_fn in tests).
3. **Write + visibility barrier** — `store_version(...)` reuses the wiki `_run_store` shape
   (artefact + ALWAYS a replacement artefact_index hint, metadata incl. learn_proposal,
   status, target), then `wait_visible(id, version, *, sleep_fn, get_status_fn)`: poll until
   the WRITTEN VERSION NUMBER is visible, 10×2s, fail-loud naming the WriteLoop. Injectable
   sleep/get_status so tests are real, not vacuous (agy P1): assert it polls N times, checks
   the version (not mere id existence), and raises on never-visible.
4. **Terminal guard + resurrection pin** — `propose`/`evaluate`/`promote`/`resolve` all
   `get_status` first. THE PIN TEST: on a `rejected` id, all four verbs refuse — and
   "refuse" is OPERATIONALIZED AS NO WRITE (GLM P1): the fake write/dispatch layers assert
   zero store calls and zero dispatches occurred, not merely that an error was printed.
5. **Dedupe** — normalized `SequenceMatcher ≥ 0.6` vs list_proposals rows; match ⇒ print id+
   status, require `--force`; memory_search advisory only — TRANSPORT PINNED (agy P1): the advisory search runs the same
   ssh inline-script path calling `store.retrieve` inside the memory container (embed env
   lives there); sentinel-framed like the other reads. Boundary tests at 0.59/0.61.
6. **Verdict parsing** — INPUT IS THE REAL ENVELOPE (codex P1, the wiki live lesson):
   every parsing test feeds `stdout=json.dumps({"result": "<seat reply>", "ok": true})` and
   asserts the inner `result` is unwrapped (reuse/port `dispatch_reply_text`) before token
   parsing; negative tests for JSON-without-result and malformed envelopes. Then:
   word-boundary uppercase tokens (`\bREJECT\b`; "rejected/rejection"
   must NOT fire), severity precedence on co-occurrence, fence extraction with
   require_fence, mapping cross-check (disagreement ⇒ eval-error), missing severity ⇒
   eval-error. Table-driven tests for every branch.
7. **Outcome precedence** — spec's strict order: REJECT > any-error(⇒eval-error, retryable)
   > NEEDS-MARK > unanimous WORTH-BUILDING. Tests: REJECT+timeout ⇒ rejected;
   WORTH×2+timeout ⇒ eval-error; WORTH×2+NEEDS ⇒ needs-mark; WORTH×3 ⇒ eval-approved.
8. **`evaluate`** — dispatch trio with PINNED target-ids (codex-bridge-dev, agy-bridge-dev,
   pi-sdk-bridge-dev-glm), shared run-id, --audit-panel; decision from the three synchronous
   reply envelopes only; eval brief embeds top-3 wiki-* citations + orchestrator-supplied
   proposals digest; outcome version + hint + barrier; drift rate printed every run, computed over the last 10
   substantive evals ORDERED BY the outcome version's created_at (agy P1: chronology pinned
   by test with out-of-order fixture rows). EVAL BRIEF CONTRACT PINNED (codex P1): a test
   asserts the dispatched task text instructs seats to emit exactly one uppercase domain
   token and the fenced vote block with stance+severity — the parser's input contract is
   stated where seats read it.
9. **`promote` / `resolve`** — promote: eval-approved only; drift refusal (display-only <5
   samples, warn ≥40%, refuse ≥60% w/o --override); `--target project` needs `--i-am-mark`;
   emits inert brief file (path printed) and writes `promoted`. resolve: needs-mark only,
   `--reason` required, writes eval-approved|rejected.
10. **`--from-workflow`** — requires `<handoff> <sha> --repo <path>`; handoff exists;
    `git -C <path> cat-file -e <sha>`; all three recorded. Tests incl. wrong-repo SHA fails.
11. **`main()` round trips** (faked subprocess): propose→evaluate(REJECT)→propose-again
    refused; propose→evaluate(3×WORTH)→promote emits brief; evaluate with one timeout ⇒
    eval-error then `--retry` succeeds; drift-refusal path.
12. **`scripts/arb-learn`** thin entrypoint + CHANGELOG entry.

## Definition of done (worker)

All tests green; wiki + arb_memory suites untouched and green. New files only:
`src/agent_redis_bridge/learn_intake.py`, `scripts/arb-learn`, `tests/test_learn_intake.py`,
CHANGELOG. Commit in the worktree; do NOT merge or push.

## Orchestrator-side after merge (not the worker's)

Live gate: one real external proposal end-to-end (expect `rejected` + reasons + live
resurrection re-attack of that id); one `--from-workflow` proposal promoted only with Mark's
nod; drift display sanity.
