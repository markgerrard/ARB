# ARB Visibility — Slice 4b-tui Implementation Plan (terminal agent pane: `arb-watch`)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development / executing-plans. Checkbox steps.

**Goal:** A terminal agent pane `arb-watch` — pick an orchestrator, see its live seats in a list, arrow-key/click a seat to watch its timeline — consuming 4a's frozen SSE contract via `httpx`, rendered with `textual`.

**Architecture:** A new `src/arb_memory/watch/` package: a pure `reducer.py` (COPY of 4a's `_reduce_seat`, not extracted) + `sse_client.py` (an `httpx` async SSE reader + frame parser) + `app.py` (a `textual` App: a `DataTable` of seats + a `RichLog` timeline, fed by an async worker). A console-script `arb-watch` + a `textual` optional extra in `pyproject.toml`. The reducer is pinned to 4a's by a contract test over recorded fixtures (guards drift since it's a copy).

**Tech Stack:** `textual` (NEW dep, optional extra), `httpx` (declared dep), asyncio. Spec: `docs/superpowers/specs/2026-06-25-arb-observability-slice4b-design.md` (v2). Build in worktree `arb-vis-4b-tui`.

## Global Constraints
- **Does NOT touch `src/arb_memory/visibility.py`** (that's the web track's file) and does NOT extract 4a's `_reduce_seat` — it COPIES it into `watch/reducer.py`. A contract test pins the copy to 4a's behavior over shared fixtures.
- **Auth:** token via `--token` flag or `ARB_VISIBILITY_TOKEN` env; sent as `Authorization: Bearer` on every httpx call (httpx CAN set headers, unlike the browser).
- **Last-Event-ID:** track manually; resend as the `Last-Event-ID` header on reconnect, **only ids matching `^\d+-\d+$`** (skip synthetic `backfill-N`/`stale-X` → they'd 500 the gateway). Backoff-reconnect on stream close.
- **textual + httpx go in an optional extra** (`[project.optional-dependencies]` `visibility`/`watch`) + an `arb-watch` console script — core installs don't pull textual.
- Files this track owns: `src/arb_memory/watch/*` (new), `pyproject.toml` (the extra + console script), tests. Disjoint from the web track.

### Test harness
```bash
cd /Users/<user>/<workspace> && export PYTHONPATH="$(pwd):$(pwd)/src"
/Users/<user>/<workspace>/.venv/bin/pip install -e '.[visibility]'   # plan-panel P2: install the EXTRA (exercises the declared textual>=0.50 constraint), not bare `pip install textual`
set -a; . envs/arb-memory-dev.env; set +a; export ARB_MEMORY_REDIS_URL=redis://127.0.0.1:6379/15
PYTEST=/Users/<user>/<workspace>/.venv/bin/pytest
```

---

### Task 1: Packaging — `textual` optional extra + `arb-watch` console script + install

**Files:** Modify `pyproject.toml` (`[project.optional-dependencies]` add `visibility = ["textual>=0.50", "httpx>=0.27"]`; `[project.scripts]` add `arb-watch = "arb_memory.watch.app:main"`). Create `src/arb_memory/watch/__init__.py`.

- [ ] **Step 1:** Read `pyproject.toml`'s existing `[project.optional-dependencies]` + `[project.scripts]` (if any) to mirror the style. Add the `visibility` extra + the `arb-watch` script entry pointing at `arb_memory.watch.app:main`.
- [ ] **Step 2:** Create the empty `watch/` package; `pip install textual` into the venv.
- [ ] **Step 3:** Verify `/Users/<user>/<workspace>/.venv/bin/python -c "import textual, arb_memory.watch"` succeeds. Commit `chore(visibility-tui): add textual optional extra + arb-watch console script`.

---

### Task 2: Copied reducer + contract test (pin to 4a)

**Files:** Create `src/arb_memory/watch/reducer.py` (copy of 4a `_reduce_seat`), `tests/arb_memory/test_watch_reducer.py`.

**Interfaces:** `reduce_seat(state: dict, entry: dict) -> dict` — byte-equivalent behavior to `arb_memory.visibility._reduce_seat`.

- [ ] **Step 1: Failing contract test** — for a set of entries (task_started/continuing/finished{ok,!ok}/vote/stale), assert `watch.reducer.reduce_seat(...)` produces the SAME state as `arb_memory.visibility._reduce_seat(...)` (import both; the test IS the drift guard). Include the terminal-stays-terminal-on-late-vote case.
- [ ] **Step 2: Run → FAIL** (module absent).
- [ ] **Step 3:** Copy 4a's `_reduce_seat` body into `watch/reducer.py` as `reduce_seat` (+ STALE_GRACE_S). Keep it a pure function.
- [ ] **Step 4: Run → PASS.** Commit `feat(visibility-tui): copied seat reducer + contract test pinning it to 4a`.

---

### Task 3: httpx async SSE client + frame parser

**Files:** Create `src/arb_memory/watch/sse_client.py`, `tests/arb_memory/test_watch_sse_client.py`.

**Interfaces:** `parse_frames(buffer:str) -> tuple[list[dict], str]` (complete frames + tail; skip `:` comments). `async stream(url, token, last_id=None) -> async-iterator[dict]` (httpx stream; sets Bearer + valid Last-Event-ID; backoff-reconnect).

- [ ] **Step 1: Failing tests** — `parse_frames` on a multi-frame buffer (incl. a `: ping` comment + a partial trailing frame) returns the complete frames + the tail; only `^\d+-\d+$` ids are surfaced as resumable.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** `parse_frames` + `stream` (httpx.AsyncClient.stream, `aiter_text`, accumulate, parse, yield; set `Authorization` + `Last-Event-ID` only if id matches `^\d+-\d+$`). Integration test: against a real visibility app (httpx ASGITransport for /orchestrators is fine; for the SSE use the same throwaway-redis + seeded-token pattern as 4a's tests, reading a few frames then breaking).
- [ ] **Step 4: Run → PASS.** Commit `feat(visibility-tui): httpx async SSE client + frame parser (Bearer + safe Last-Event-ID)`.

---

### Task 4: textual app (`arb-watch`) + smoke test

**Files:** Create `src/arb_memory/watch/app.py`, `tests/arb_memory/test_watch_app.py`.

**Interfaces:** `main()` (console entry: parses `--base-url`, `--token`/env, optional `--orchestrator`); a `WatchApp(textual.App)` with a seats `DataTable` + timeline `RichLog`, fed by a `@work` async worker running `sse_client.stream`.

- [ ] **Step 1: Failing smoke test** — using textual's test harness (`async with WatchApp(...).run_test() as pilot:`), feed a fixture SSE stream (monkeypatch `sse_client.stream` to yield recorded entries) and assert the seats DataTable gains both seats + selecting one populates the timeline. (No real network.)
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** `WatchApp`: on mount, fetch `/orchestrators` (or use `--orchestrator`), start the orchestrator SSE worker → upsert rows into the DataTable via `reduce_seat`; on row-select, start a seat SSE worker → append timeline lines; arrow keys move the cursor (DataTable default). `main()` wires args/env → `WatchApp(...).run()`.
- [ ] **Step 4: Run → PASS** (the smoke test; manual `arb-watch` visual check noted, not automated).
- [ ] **Step 5: Commit** `feat(visibility-tui): arb-watch textual app (seat table + timeline) + smoke test`.

### Plan-panel folds (P2)
- **Fixtures namespaced** to avoid collision with the web track's `tests/fixtures/sse_web_orchestrator.txt`: the
  TUI uses `tests/fixtures/watch_*.txt` (or inlines them). The two tracks must not write the same fixture path.
- **Smoke test (T4)** must `await pilot.pause()` after feeding the fixture stream before asserting the DataTable —
  the `@work` async worker populates it asynchronously, else the test flakes.
- **SSE reconnect (T3)** gets an explicit test: a stream that drops mid-way reconnects and resumes (with a valid
  `^\d+-\d+$` Last-Event-ID), and a synthetic last-id is NOT sent.
- **Bridge-tee E2E:** the design's "real bridge tee" E2E is already proven for bridge→gateway by 4a's
  `tests/e2e_visibility_roundtrip.py`; the TUI consumes the same SSE, so T3's integration test (real gateway +
  seeded token + throwaway-redis frames) suffices. Note this coverage boundary in the report rather than
  duplicating the bridge-tee harness.

## Self-Review
Spec coverage: textual optional extra + console script → T1; copied reducer + drift-guard contract test → T2; httpx SSE client + safe Last-Event-ID → T3; textual app + smoke test → T4. Decisions honored (textual in extra, Bearer header, copy-not-extract). Disjoint from web track (no `visibility.py` edit; only `pyproject.toml` overlaps and that's tui-only). Placeholder risk: the textual smoke test depends on textual's `run_test` pilot — if that proves flaky, fall back to unit-testing the worker's reduce loop directly + a manual visual check (note in report). The reducer copy is the one DRY compromise the panel explicitly endorsed (mirror-not-extract to keep tracks disjoint + 4a frozen).
```
