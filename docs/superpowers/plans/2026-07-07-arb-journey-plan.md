# ARB `/journey` — TDD implementation plan (v2 — plan-panel absorbed)

**Spec:** `docs/superpowers/specs/2026-07-07-arb-journey-SPEC.md` (authoritative contract —
on any conflict, the spec wins). Worker: codex, isolated worktree, strict red→green per task.
Run tests with `ARB_MEMORY_DSN=postgresql://arb_memory:$ARB_LOCAL_PG_PASSWORD@localhost:5544/arb_memory
.venv/bin/python -m pytest tests/arb_memory/test_journey_export.py -q` (pgvector postgres on
5544 already runs locally; the scratch-schema fixture pattern is in
`tests/arb_memory/conftest.py`).

## Task order (each: failing test first, minimal green, no scope creep)

1. **`extract_refs`** — new `src/arb_memory/journey_export.py`. Tests: bare art-id with word
   boundaries (no substring leak), backticked wiki ids, self-ref dropped, dangling counted,
   NULL/None/binary ⇒ `(set(), 0)`.
2. **`node_title` / `node_kind`** — tests per spec tables incl. headingless + binary ⇒ id.
3. **`build_snapshot(conn)`** — pure assembly, no commit. Tests on scratch DB: node fields
   (tags from live anchored hints only; free hints id/created_at/tags with NO text key at
   all), edges, degrees, counts (incl. isolated + dangling). Golden edge-set fixture test.
4. **`_verify_refs` + verify plumbing** — independent scanner (must not import/call
   extract_refs — add a test asserting the function bodies are distinct code paths, e.g.
   `_verify_refs.__code__ is not extract_refs.__code__` plus behavioral divergence fixture).
   Corruption test: write snapshot, mutate one edge, verify exits nonzero.
5. **`main()`** — argparse (`--verify-only [path]`, `--snapshot-dir` override for tests),
   REPEATABLE READ txn ownership, atomic write (tmp+os.replace), stale-not-wrong exit for
   verify-only. `main()`-level tests with the scratch DB (no mocks).
6. **pyproject entry** `arb-journey-export = "arb_memory.journey_export:main"`.
7. **Visibility routes** — `GET /journey` + `GET /journey/graph.json` in `visibility.py`
   using `request_authenticated` (session-or-token). Route tests: 401 JSON (no redirect) for
   graph.json; 302→/login vs 401 for the page per login_enabled; **503 with the exact spec
   error body when no snapshot**; `test_visibility_grants` untouched and green.
8. **`journey.html`** — single self-contained file (inline JS/CSS, no CDN): force layout,
   table lens with filters, detail panel with `memory_get` pointer, prominent generated_at.
   Keep the JS dependency-free (hand-rolled force sim is fine at ~170 nodes).
9. **compose** — top-level `volumes: arb_journey:`; mounts memory(rw)/visibility(ro);
   `ARB_JOURNEY_SNAPSHOT_DIR` in both services. PINNED BY TEST (plan-panel P1): a test
   parses `deploy/docker-compose.yml` (yaml is already a test dep) and asserts the volume
   exists, both mounts with correct rw/ro modes, and the env var in both services — the
   deploy-topology failure class stays dead.
10. **Playwright check** — dev-dep, CI-required/local-skippable: rendered node count ==
    snapshot count; known hub clickable; renders behind real auth.

## Definition of done (worker)

All new tests green + full `tests/arb_memory/` green with the local DSN + `tests/` wiki suite
untouched. No changes outside: `journey_export.py`, `visibility.py` (routes only),
`static/journey.html`, `deploy/docker-compose.yml`, `pyproject.toml`, tests, CHANGELOG entry.
Commit in the worktree with a descriptive message; do NOT merge or push.

## Orchestrator-side after merge (not the worker's)

Deploy: rebuild memory+visibility on prod, run `arb-journey-export`, `--verify-only` green,
pane loads via gateway, spot-check a hub against memory_get. Wire into the vault-export cron.
