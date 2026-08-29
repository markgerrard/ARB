# ARB Visibility — Slice 4b-web Implementation Plan (browser agent pane)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development / executing-plans. Checkbox steps.

**Goal:** A same-origin browser agent pane served by the visibility service: pick an orchestrator, see its live seats, click one to watch its timeline — consuming 4a's frozen SSE contract over `fetch`+ReadableStream with `Authorization: Bearer`.

**Architecture:** Add two PUBLIC static routes to the existing `visibility.py` Starlette app (`GET /` → `index.html`, `GET /app.js`) — no secret in them. Vanilla JS (no framework, no build step) consumes `/orchestrators` (JSON) + `/sse/orchestrator/{id}` + `/sse/seat/{task_id}` via `fetch` streaming with a `Bearer` token from `localStorage`. The web reimplements the seat-state reducer in JS to 4a's contract; a contract test pins it to recorded SSE fixtures.

**Tech Stack:** Starlette (existing), vanilla HTML/JS, `fetch`+ReadableStream (SSE), `localStorage`. Spec: `docs/superpowers/specs/2026-06-25-arb-observability-slice4b-design.md` (v2). Build in worktree `arb-vis-4b-web`.

## Global Constraints
- **Do NOT change 4a's SSE/auth contract.** Only ADD `GET /` + `GET /app.js` static routes (public) to `visibility.py`. Data routes (`/orchestrators`, `/sse/*`) stay Bearer-gated, unchanged.
- **Static shell is PUBLIC** (no token needed to load `/` or `/app.js`); the JS supplies the `Authorization: Bearer <token>` (from `localStorage`) on the data/SSE `fetch` calls only.
- **Last-Event-ID:** `fetch` does NOT auto-resend it. The JS tracks the last id and resends it as the `Last-Event-ID` request header on reconnect — **but only ids matching `^\d+-\d+$`** (real Redis ids); never the synthetic `backfill-N`/`stale-X` ids (they'd 500 the gateway). On a synthetic/missing id, omit the header (fresh backfill).
- **JS SSE parser:** buffer partial frames across chunk boundaries; split complete frames on `\n\n`; skip comment lines (`: ping`).
- Files this track owns: `src/arb_memory/visibility.py` (static routes only), `src/arb_memory/static/index.html`, `src/arb_memory/static/app.js`, tests. Does NOT touch the reducer source or `pyproject.toml`.

### Test harness
```bash
cd /Users/<user>/<workspace> && export PYTHONPATH="$(pwd):$(pwd)/src"
set -a; . envs/arb-memory-dev.env; set +a; export ARB_MEMORY_REDIS_URL=redis://127.0.0.1:6379/15
PYTEST=/Users/<user>/<workspace>/.venv/bin/pytest
```

---

### Task 1: Public static routes serve the shell

**Files:** Modify `src/arb_memory/visibility.py` (add `GET /`, `GET /app.js`; load files from `src/arb_memory/static/`). Create `src/arb_memory/static/index.html` (minimal shell), `src/arb_memory/static/app.js` (stub). Test: `tests/arb_memory/test_visibility_web.py`.

**Interfaces:** `GET /` → 200 `text/html` (no auth); `GET /app.js` → 200 `application/javascript` (no auth). Data routes still 401 without token.

- [ ] **Step 1: Failing test**
```python
def test_index_and_appjs_public(client):
    assert client.get("/").status_code == 200 and "text/html" in client.get("/").headers["content-type"]
    assert client.get("/app.js").status_code == 200
def test_data_routes_still_gated(client):
    assert client.get("/orchestrators").status_code == 401
```
- [ ] **Step 2: Run → FAIL** (`$PYTEST tests/arb_memory/test_visibility_web.py -v`) — routes 404.
- [ ] **Step 3: Implement** — add the two routes (read the static files; serve with the right content-type; no auth guard). Mirror writer.py/visibility.py Route style; resolve the static dir via `Path(__file__).parent/"static"`. (plan-panel P2: read+return the file contents in the handler rather than relying on `FileResponse`/`StaticFiles` package-data quirks; ensure `src/arb_memory/static/*` ships as package data so the installed package serves them.)
- [ ] **Step 4: Run → PASS** (+ existing `test_visibility_auth.py` stays green — data routes unchanged).
- [ ] **Step 5: Commit** `feat(visibility-web): serve public static shell (index.html + app.js)`.

---

### Task 2: The agent-pane JS (reducer + fetch-SSE + render)

**Files:** Modify `src/arb_memory/static/index.html` (two-pane DOM + token field), `src/arb_memory/static/app.js` (the app). Test: `tests/arb_memory/test_visibility_web_contract.py` (the JS reducer vs recorded SSE fixtures — run the reducer via `node` if available, else a Python port-of-the-contract assertion; pragmatic: a JS unit test harness is out of scope, so assert the fixtures' expected reductions are documented + the app.js reducer matches them by review). 

**Interfaces:** `app.js` exposes (for testability) a pure `reduceSeat(state, entry)` mirroring 4a's `_reduce_seat`, and an SSE line-parser `parseFrames(buffer)`.

- [ ] **Step 1:** Write `index.html` — a token `<input>` (saved to `localStorage`), an orchestrator `<select>`, a left `<ul id=seats>`, a right `<pre id=timeline>`.
- [ ] **Step 2:** Write `app.js`:
  - `authHeaders()` → `{Authorization: 'Bearer '+localStorage.token}`.
  - `parseFrames(buffer)` → yields `{id,event,data}` for complete `\n\n`-terminated frames, skipping `:`-comment lines, returning the unconsumed tail.
  - `reduceSeat(state, entry)` — mirror 4a `_reduce_seat`: `task_started`/`task_continuing`→running, `task_finished`→ok?done:failed, `vote`→ (terminal stays terminal, else) voted; track last_event_ts.
  - `streamSSE(url, onEvent)` — `fetch(url, {headers: {...authHeaders(), ...(realLastId? {'Last-Event-ID': realLastId}:{})}})`, read `res.body.getReader()`, decode, `parseFrames`, dispatch; store last id only if `/^\d+-\d+$/`; on close, backoff-reconnect.
  - On load: `GET /orchestrators` → populate `<select>`; on select → `streamSSE('/sse/orchestrator/'+id)` → render seat list; on seat click → `streamSSE('/sse/seat/'+task_id)` → append timeline.
- [ ] **Step 3: EXECUTE the JS, don't mirror+review it (plan-panel P1, unanimous).** `node` IS available
  (`/opt/homebrew/bin/node`, v26). `app.js` MUST expose `reduceSeat` + `parseFrames` as pure functions loadable
  by node (ESM `export` or a guarded `globalThis`/`module.exports` so the browser still works). Create
  `tests/fixtures/sse_web_orchestrator.txt` (recorded 4a frames — namespaced to avoid TUI collision). The pytest
  (`tests/arb_memory/test_visibility_web_contract.py`): runs a tiny node harness (`node -e` or a `.mjs`) that
  imports `app.js`'s `reduceSeat`/`parseFrames`, feeds the fixture frames, prints the resulting seat states as
  JSON; the test then asserts that JSON equals the output of the REAL `arb_memory.visibility._reduce_seat` over
  the same parsed frames. So the test executes the SHIPPED JS and pins it to 4a's reducer (catches both JS drift
  and a 4a reducer change). `pytest.importorskip`-style skip ONLY if node is genuinely absent — never silently
  fall back to review.
- [ ] **Step 4: Commit** `feat(visibility-web): agent-pane app.js (reducer + fetch-SSE + two-pane render)`.

---

### Task 3: SSE proxy-buffering headers + E2E

**Files:** Modify `src/arb_memory/visibility.py` (add `Cache-Control: no-cache` + `X-Accel-Buffering: no` to the SSE StreamingResponse headers — not a contract change). Create `tests/e2e_visibility_web.py`.

- [ ] **Step 1:** Add the two headers to the SSE responses; assert via a test that `/sse/orchestrator` response carries them (with a token).
- [ ] **Step 2: E2E** (mirror `tests/e2e_visibility_roundtrip.py`): real bridge tee → `GET /` serves the shell (200, public) → `fetch`-style `GET /orchestrators` + a streamed `/sse/orchestrator/{id}` read (httpx, with token) surfaces both seats; 401 without token on data routes. Run 3× isolated.
- [ ] **Step 3: Commit** `feat(visibility-web): SSE no-buffer headers + live E2E (shell + roster)`.

## Self-Review
Spec coverage: public shell → T1; reducer+fetch-SSE+render+Last-Event-ID-filter+parser → T2; proxy headers + E2E → T3. Decisions (fetch-streaming/Bearer/localStorage, public shell, served by service) all honored. No 4a contract change (only static routes + benign SSE headers). The JS reducer "mirrors not extracts" 4a's `_reduce_seat`. Placeholder risk: the JS contract test is review-pinned (no node test runner assumed) — the Python mirror over shared fixtures is the executable guard.
