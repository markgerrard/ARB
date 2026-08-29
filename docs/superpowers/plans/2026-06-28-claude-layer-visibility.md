# Claude-layer Visibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the warm Claude orchestrator and each cold-Opus reviewer appear as first-class, live, watchable seats in ARB Visibility, symmetric with the bridge seats, by tailing the Claude transcript files and teeing the bridge event contract.

**Architecture:** A standalone `claude-tail` daemon tails the warm session `.jsonl` and each cold-Opus subagent `.output`, maps each line to the bridge event contract (`command_*`/`model_text`/`model_thinking`) plus **synthetic `task_started`/`task_finished`** lifecycle events, redacts, and tees to `events:live` (roster + boundaries) and `arbmem:trace` (full transcript) via a tee helper extracted from `Bridge`. Correlation rides in the prompt (cold-Opus marker) + a `SessionStart` hook (warm). No backend gateway change; small arb-watch UI changes; containerised like the seat-host.

**Tech Stack:** Python 3.11+ (the bridge package), `redis-py`, the existing `redact()` + `RedisConfig`; Go (arb-watch); Docker (deploy). Reference design: `docs/superpowers/specs/2026-06-28-claude-layer-visibility-design.md`.

## Global Constraints

- **No backend gateway change** — emit the exact `events:live` field set `{run_id, task_id, seat_id, orchestrator, event_type, sent_at, data}` the gateway already consumes (`src/arb_memory/visibility.py`).
- **Roster keys on `task_id`** (`visibility.py:435`); state derives **only** from `task_started`/`task_finished`/`task_continuing`/`vote` (`visibility.py:109 _reduce_seat`, `:88 _is_stale`). The tailer MUST emit synthetic lifecycle events.
- **Redact before tee, both sinks** — `events:live` is NOT redacted today; apply `redact()` (content + tool_name) at the tailer. Route `model_text`/`model_thinking` to `arbmem:trace` ONLY; lifecycle/command boundaries to both.
- **Offsets in Redis**, never a local file (container mounts are read-only). At-least-once; advance offset only after publish ack; reset to 0 on file shrink (rotation).
- **Fail loud, container-visible** — unknown `.jsonl` shape → `drift_error` event + non-zero exit past a threshold, never emit garbage.
- **Zero token cost** — read files + write Redis only; never invoke an LLM.
- All new Python lives under `src/agent_redis_bridge/claude_tail/`; tests under `tests/claude_tail/`.

---

## File Structure

- `src/agent_redis_bridge/visibility_tee.py` — **NEW.** Standalone `live_tee()` / `trace_tee()` helpers (redis handle + prefix + fields), extracted from `Bridge._tee_live_event` / `_capture`. Reused by both `Bridge` and the tailer.
- `src/agent_redis_bridge/claude_tail/mapper.py` — **NEW.** Pure: a parsed `.jsonl` line → list of bridge events; the drop set; format self-check.
- `src/agent_redis_bridge/claude_tail/identity.py` — **NEW.** Pure: marker parsing + seat identity (`task_id`/`seat_id`/`orchestrator`/`run_id`) for warm vs cold.
- `src/agent_redis_bridge/claude_tail/lifecycle.py` — **NEW.** Pure: synthetic `task_started`/`task_finished` emission rules.
- `src/agent_redis_bridge/claude_tail/offset.py` — **NEW.** Redis offset store (get/commit, rotation reset).
- `src/agent_redis_bridge/claude_tail/tailer.py` — **NEW.** One transcript's tail loop: read-from-offset → parse → map+lifecycle → redact+route → tee → commit.
- `src/agent_redis_bridge/claude_tail/service.py` — **NEW.** Discover transcripts (registry + dir watch), run a tailer per transcript concurrently; `python -m agent_redis_bridge.claude_tail`.
- `src/agent_redis_bridge/bridge.py` — **MODIFY.** `_tee_live_event` / `_capture` call the extracted helpers (no behaviour change).
- `scripts/claude-tail-hooks/` — **NEW.** `session_start.py` / `session_end.py` Claude Code hook scripts (register session + mirror cold-Opus `.output`).
- `tools/arb-watch-go/model.go` — **MODIFY.** `agentOf()` learns `opus`/`claude`; isolated orchestrator section.
- `deploy/claude-tail/` — **NEW.** Dockerfile + compose for the daemon.

---

## Task 1: Extract the tee helper from `Bridge`

**Files:**
- Create: `src/agent_redis_bridge/visibility_tee.py`
- Modify: `src/agent_redis_bridge/bridge.py` (`_tee_live_event` ~1897, `_capture` transcript path)
- Test: `tests/claude_tail/test_visibility_tee.py`

**Interfaces:**
- Produces: `live_tee(redis, prefix, *, run_id, task_id, seat_id, orchestrator, event_type, sent_at, data, maxlen, ttl)` and `trace_tee(redis, prefix, *, run_id, task_id, seat_id, orchestrator, event, data, seq, turn_index, maxlen, redactor=redact, content_cap=256*1024)` — both build the exact field dict + XADD to `<prefix>events:live` / `<prefix>arbmem:trace`; `live_tee` returns the entry id, `trace_tee` returns the entry id OR `None` when the flusher's drop rules apply (missing `kind`/`item_id`, empty text for text-bearing kinds). **`trace_tee` carries the `TranscriptFlusher` value contract structurally** — `redactor` applied to content + tool_name, `content_cap` + truncation marker, `turn_index` an explicit param (NOT read from `data`, which collapses turns). Prefer extracting the shared value-processing from `TranscriptFlusher._write` so both producers share one contract. No `Bridge` dependency. *(Panel-corrected from a thinner first cut — see spec § "T1 build correction".)*

- [ ] **Step 1: Write the failing test** — a helper-emitted live event has the exact field set a bridge seat produces.

```python
# tests/claude_tail/test_visibility_tee.py
import json
from agent_redis_bridge.visibility_tee import live_tee

class RecRedis:
    def __init__(self): self.xadds=[]
    def xadd(self, key, fields, **kw): self.xadds.append((key, fields, kw)); return b"1-0"

def test_live_tee_emits_exact_events_live_fields():
    r = RecRedis()
    live_tee(r, "agent_scratch:", run_id="run-1", task_id="t1", seat_id="codex-1",
             orchestrator="claude-bridge-dev", event_type="task_started", sent_at="2026-06-28T00:00:00+00:00",
             data={"task_id":"t1"}, maxlen=500, ttl=60)
    key, fields, kw = r.xadds[0]
    assert key == "agent_scratch:events:live"
    assert fields == {"run_id":"run-1","task_id":"t1","seat_id":"codex-1",
                      "orchestrator":"claude-bridge-dev","event_type":"task_started",
                      "sent_at":"2026-06-28T00:00:00+00:00","data":json.dumps({"task_id":"t1"},separators=(",",":"))}
    assert kw == {"maxlen":500,"ttl":60}
```

- [ ] **Step 2: Run it — expect FAIL** `PYTHONPATH=src .venv/bin/python -m pytest tests/claude_tail/test_visibility_tee.py -q` → ImportError / no `visibility_tee`.

- [ ] **Step 3: Read the current `Bridge._tee_live_event` (bridge.py ~1897-1933) and the transcript `_capture`/flusher field-build; copy the EXACT field assembly (key order, `json.dumps(data, separators=(",",":"))`) into the new module** as free functions taking `(redis, prefix, …)`. Keep the local (sync xadd) path; the remote/flusher path stays in `Bridge` (the tailer uses the sync helper or its own flusher instance).

```python
# src/agent_redis_bridge/visibility_tee.py
import json
def live_tee(redis, prefix, *, run_id, task_id, seat_id, orchestrator, event_type, sent_at, data, maxlen, ttl):
    fields = {"run_id": run_id, "task_id": task_id, "seat_id": seat_id or "",
              "orchestrator": orchestrator or "", "event_type": event_type, "sent_at": sent_at,
              "data": json.dumps(data, separators=(",", ":"))}
    return redis.xadd(f"{prefix}events:live", fields, maxlen=maxlen, ttl=ttl)
# trace_tee analogous, XADD to f"{prefix}arbmem:trace" with the flusher's field shape (read transcript_flusher.py:_write)
```

- [ ] **Step 4: Run test — expect PASS.**

- [ ] **Step 5: Refactor `Bridge._tee_live_event` (local path) to call `live_tee(...)`; run the bridge tee tests** `pytest tests/test_async_tees_marking.py tests/test_bridge_turn_heartbeat.py -q` → all PASS (no behaviour change).

- [ ] **Step 6: Commit** `git add -A && git commit -m "refactor: extract standalone live_tee/trace_tee from Bridge"`

---

## Task 2: Transcript line → bridge events (mapper) + format self-check

**Files:**
- Create: `src/agent_redis_bridge/claude_tail/mapper.py`, `tests/claude_tail/test_mapper.py`
- Fixtures: capture a handful of real lines (see Step 1) into `tests/claude_tail/fixtures/`.

**Interfaces:**
- Produces: `map_line(obj: dict) -> list[Event]` where `Event = {"event_type": str, "data": dict}`; `IGNORE_LINE_TYPES` (the benign-drop set); raises `DriftError` on a genuinely unknown line/block type. **Empirically corrected** (real-transcript census, not a guess): the benign-ignore set is large — `{ai-title, agent-name, mode, permission-mode, system, last-prompt, queue-operation, attachment, file-history-snapshot, bridge-session}`; `user` lines whose `message.content` is a string (human prompt) or a `text` block are also benign drops; only `assistant`/`user`-with-`tool_result` map. DriftError stays for truly-unknown types only (T6 catches it → drop+count+threshold-exit), so a single new metadata type degrades gracefully rather than crashing.

- [ ] **Step 1: Capture real fixtures** from the live transcript — capture the FULL distribution, do not cherry-pick (the first cut captured 5 lines and missed 7 line types + human string/text user input, so the drift self-check fired on 100% of real sessions):
```bash
F=~/.claude/projects/-Users-mark-<workspace>/*.jsonl
# minimal stubs for the benign-drop types (they drop by `type` alone);
# real (scrubbed) shapes for the content types: assistant text/thinking/tool_use, user tool_result,
# PLUS a user string-content line and a user text-block line.
```

- [ ] **Step 2: Write failing tests** — each shape maps to the right event; drop-set returns `[]`; unknown shape raises.
```python
def test_assistant_text_maps_to_model_text():
    ev = map_line({"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"hi"}]}})
    assert ev == [{"event_type":"model_text","data":{"delta":"hi"}}]
def test_tool_use_maps_to_command_started():
    ev = map_line({"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"ls"}}]}})
    assert ev[0]["event_type"]=="command_started" and ev[0]["data"]["tool_name"]=="Bash"
def test_tool_result_maps_to_command_output_and_finished():
    ev = map_line({"type":"user","message":{"content":[{"type":"tool_result","content":"out"}]}})
    assert [e["event_type"] for e in ev]==["command_output","command_finished"]
def test_file_history_snapshot_drops():
    assert map_line({"type":"file-history-snapshot"})==[]
def test_unknown_shape_raises_drift():
    import pytest
    with pytest.raises(DriftError): map_line({"type":"assistant","message":{"content":[{"type":"NEW_BLOCK"}]}})
```
- [ ] **Step 3: Run — expect FAIL.**
- [ ] **Step 4: Implement `map_line` + `IGNORE_LINE_TYPES` + `DriftError`** (drop benign line types by `type` alone; for `assistant`/`user` iterate `message.content` blocks and map by `block["type"]`; handle `user` string content + `text` blocks as benign drops; truly-unknown line/block type → `raise DriftError`). `IGNORE_LINE_TYPES` = the empirically-observed benign set above, not the original 3. Include the fixture round-trip test over `lines.jsonl`.
- [ ] **Step 5: Run — expect PASS.**
- [ ] **Step 6: Commit** `git commit -m "feat(claude-tail): transcript-line -> bridge-event mapper + drift self-check"`

---

## Task 3: Synthetic lifecycle (task_started / task_finished)

**Files:** Create `src/agent_redis_bridge/claude_tail/lifecycle.py`, `tests/claude_tail/test_lifecycle.py`

**Interfaces:**
- Produces: `class Lifecycle: started() -> Event|None` (returns a `task_started` event once, on first call, else None); `finished() -> Event` (a `task_finished` event with `{"ok": bool}`).

- [ ] **Step 1: Failing test** — first content event yields a leading `task_started`; subsequent ones don't; `finished()` yields `task_finished`.
```python
def test_started_emits_once():
    lc = Lifecycle()
    assert lc.started()["event_type"]=="task_started"
    assert lc.started() is None
def test_finished_event():
    assert Lifecycle().finished()["event_type"]=="task_finished"
```
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** the one-shot `started` flag + `finished`.
- [ ] **Step 4: Run — PASS.**
- [ ] **Step 5: Commit** `git commit -m "feat(claude-tail): synthetic task_started/task_finished lifecycle"`

---

## Task 4: Identity + marker correlation

**Files:** Create `src/agent_redis_bridge/claude_tail/identity.py`, `tests/claude_tail/test_identity.py`

**Interfaces:**
- Produces: `parse_marker(first_user_text: str) -> dict|None` (`{"run_id","seat_id","orchestrator"}` from `[ARB_RUN:.. ARB_SEAT:.. ARB_ORCH:..]`, `orchestrator=""` if `ARB_ORCH` omitted, else None); `warm_identity(session_id, project, workspace) -> Identity`; `cold_identity(agent_id, session_id, marker_text) -> Identity` where `Identity={run_id,task_id,seat_id,orchestrator}`. **`cold_identity.orchestrator` comes from the marker's `ARB_ORCH` (the spawning warm orchestrator's id), NOT hardcoded** — so multiple concurrent warm sessions group their reviewers correctly (T4 panel correction).

- [ ] **Step 1: Failing tests** — the identity table from the spec (§ Identity & lifecycle):
```python
def test_warm_identity():
    i = warm_identity("sess-1","bridge","dev")
    assert i.task_id=="sess-1" and i.run_id=="sess-1" and i.seat_id=="claude-bridge-dev" and i.orchestrator=="claude-bridge-dev"
def test_cold_identity_with_marker():
    i = cold_identity("agentX","sess-1","[ARB_RUN:run-9 ARB_SEAT:cold-opus-1]")
    assert i.task_id=="agentX" and i.run_id=="run-9" and i.seat_id=="cold-opus-1" and i.orchestrator=="claude-bridge-dev"
def test_cold_marker_absent_falls_back_to_session_runid():
    i = cold_identity("agentX","sess-1", "no marker here")
    assert i.run_id=="sess-1"   # still VISIBLE (not gated out), ungrouped
    assert i.task_id=="agentX"
def test_marker_only_from_first_message_is_callers_job():
    assert parse_marker("[ARB_RUN:r ARB_SEAT:s] do the review")=={"run_id":"r","seat_id":"s"}
```
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** `parse_marker` (regex `\[ARB_RUN:(\S+)\s+ARB_SEAT:(\S+)\]`), `warm_identity`, `cold_identity` (marker → run_id/seat_id; absent → run_id=session_id, seat_id derived from agent_id). The CALLER passes only the first user message text (Task 6 enforces "first message only").
- [ ] **Step 4: Run — PASS.**
- [ ] **Step 5: Commit** `git commit -m "feat(claude-tail): seat identity + cold-Opus marker correlation"`

---

## Task 5: Redis offset store (commit-after-publish, rotation reset)

**Files:** Create `src/agent_redis_bridge/claude_tail/offset.py`, `tests/claude_tail/test_offset.py`

**Interfaces:**
- Produces: `class OffsetStore(redis, prefix): get(key) -> int; commit(key, offset) -> None`; `offset_key(path, inode) -> str`.

- [ ] **Step 1: Failing tests** — get default 0; commit then get returns it; key includes inode.
```python
def test_get_default_zero(): assert OffsetStore(RecRedis(),"p:").get("k")==0
def test_commit_then_get():
    s=OffsetStore(FakeRedis(),"p:"); s.commit("k",1234); assert s.get("k")==1234
def test_offset_key_includes_inode(): assert offset_key("/a/b.jsonl", 99)=="claude:offset:/a/b.jsonl|99"
```
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** `OffsetStore` (Redis GET/SET on `<prefix>claude:offset:<…>`), `offset_key`. (Rotation reset lives in the tail loop, Task 6 — when `file_size < stored_offset`, call `commit(key, 0)`.)
- [ ] **Step 4: Run — PASS.**
- [ ] **Step 5: Commit** `git commit -m "feat(claude-tail): Redis offset store"`

---

## Task 6: The tail loop (offset → parse → map+lifecycle → redact+route → tee → commit)

**Files:** Create `src/agent_redis_bridge/claude_tail/tailer.py`, `tests/claude_tail/test_tailer.py`

**Interfaces:**
- Consumes: `map_line`, `Lifecycle`, `Identity`, `OffsetStore`, `live_tee`/`trace_tee`, `redact`.
- Produces: `class TranscriptTailer(path, identity, offset_store, redis, prefix, redactor): poll() -> int` (reads new lines from the committed offset, emits events, commits offset; returns count). Routing: `model_text`/`model_thinking` → trace only; `task_*`/`command_*` → both. Redact `content`/`tool_name` in `data` before either tee. Marker read from the FIRST user line only.

- [ ] **Step 1: Failing integration-ish test** (fake redis + a temp `.jsonl`): writing a tool_use line then polling emits a `task_started` (first) + a `command_started` to events:live, and the offset advances; a secret in tool input is redacted.
```python
def test_poll_emits_lifecycle_and_routes_and_redacts(tmp_path):
    f = tmp_path/"s.jsonl"
    f.write_text(json.dumps({"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"cat .env; SECRET=sk-abc"}}]}})+"\n")
    live, trace = RecRedis(), RecRedis()  # or one RecRedis keyed by stream
    t = TranscriptTailer(str(f), warm_identity("sess","bridge","dev"), OffsetStore(FakeRedis(),"p:"),
                         redis=live, prefix="agent_scratch:", redactor=redact)
    n = t.poll()
    assert n>=1
    types=[json.loads(fields["data"]).get("kind") or fields["event_type"] for _,fields,_ in live.xadds]
    assert "task_started" in [fields["event_type"] for _,fields,_ in live.xadds]
    assert "command_started" in [fields["event_type"] for _,fields,_ in live.xadds]
    assert "sk-abc" not in "".join(fields["data"] for _,fields,_ in live.xadds)  # redacted
def test_model_text_routes_to_trace_only(...):  # assert model_text NOT in events:live, present in arbmem:trace
def test_offset_resets_on_shrink(...):          # stored offset > size -> reset 0, re-read
```
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement `poll()`** — open file, seek to `offset_store.get(key)` (reset to 0 if `size < offset`), read complete new lines, for each: `json.loads` → on first user line `parse_marker`; `map_line` (catch `DriftError` → emit `drift_error` + bump a counter, exit non-zero past threshold); prepend `Lifecycle.started()` if first; redact `data` content/tool_name via `redactor`; route per the rule (live_tee and/or trace_tee); **commit the new offset only after the tees return**.
- [ ] **Step 4: Run — PASS.**
- [ ] **Step 5: Commit** `git commit -m "feat(claude-tail): tail loop with routing, redaction, offset commit-after-publish"`

---

## Task 7: The service (discovery + concurrent tailers + daemon)

**Files:** Create `src/agent_redis_bridge/claude_tail/service.py`, `src/agent_redis_bridge/claude_tail/__main__.py`, `tests/claude_tail/test_service.py`

**Interfaces:**
- Consumes: a registry (Redis key/file the `SessionStart` hook writes: `{session_id, transcript_path, seat_id, run_id}`) + a watch on `~/.claude/tasks/` for cold-Opus `.output`.
- Produces: `class Service: tick()` — reconcile the registry/dir into a `TranscriptTailer` per transcript; `poll()` each; emit `task_finished` when a transcript goes idle past `IDLE_FINISH_SECS` or the session deregisters.

- [ ] **Step 1: Failing test** — given a registry with one warm session + one cold `.output`, `tick()` creates two tailers and polls both; a deregistered/idle session emits `task_finished`.
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** the reconcile loop + idle→finished; `__main__.py` runs `Service` on an interval reading `AGENT_REDIS_*` / `ARB_LIVE_REDIS_URL` / `ARB_TRACE_REDIS_URL` like the bridge tees.
- [ ] **Step 4: Run — PASS.**
- [ ] **Step 5: Commit** `git commit -m "feat(claude-tail): discovery service + daemon entrypoint"`

---

## Task 8: Claude Code hooks (warm register + cold-Opus mirror)

**Files:** Create `scripts/claude-tail-hooks/session_start.py`, `session_end.py`; doc the settings.json snippet in `deploy/claude-tail/README.md`.

- [ ] **Step 1: Failing test** (`tests/claude_tail/test_hooks.py`) — `session_start` given the hook stdin JSON writes a registry entry with `{session_id, transcript_path, seat_id=claude-<project>-<ws>, run_id=session_id}`.
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** the hooks (read hook JSON on stdin; resolve project/workspace; write the registry Redis key / file; `session_end` removes it). Add a small symlink/mirror step so cold-Opus `.output` files are reachable under `~/.claude/tasks/` (agy Pitfall F-B).
- [ ] **Step 4: Run — PASS.**
- [ ] **Step 5: Commit** `git commit -m "feat(claude-tail): SessionStart/End hooks + cold-Opus output mirror"`

---

## Task 9: arb-watch — agentOf learns opus/claude

**Files:** Modify `tools/arb-watch-go/model.go` (`agentOf`), `tools/arb-watch-go/*_test.go`

- [ ] **Step 1: Failing Go test** — `agentOf("cold-opus-1")=="opus"`, `agentOf("claude-bridge-dev")=="claude"`, existing ids unchanged.
- [ ] **Step 2: Run — FAIL** `cd tools/arb-watch-go && go test ./... -run AgentOf`.
- [ ] **Step 3: Implement** the two prefixes in `agentOf` (keep existing codex/agy/pi mapping).
- [ ] **Step 4: Run — PASS.**
- [ ] **Step 5: Commit** `git commit -m "feat(arb-watch): agentOf recognizes opus/claude seats"`

---

## Task 10: arb-watch — isolated orchestrator section

**Files:** Modify `tools/arb-watch-go/model.go` (`visibleSeats` / `renderSeatTable`), tests.

**Interfaces:** Consumes the agent badge from Task 9.

- [ ] **Step 1: Failing test** — given a mix of bridge seats + a `claude-*` seat, `visibleSeats()` returns the orchestrator seat **last** (after the activity-sorted run seats), and `renderSeatTable` emits a `── orchestrator ──` divider before it.
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement** — in `visibleSeats`, partition `agentOf=="claude"` seats to the end (stable, after the activity sort); in `renderSeatTable`, insert the divider before the first orchestrator row. Keep them out of the activity-sort.
- [ ] **Step 4: Run — PASS.**
- [ ] **Step 5: Commit** `git commit -m "feat(arb-watch): isolated orchestrator section at the bottom"`

---

## Task 11: Containerise the claude-tail daemon

**Files:** Create `deploy/claude-tail/Dockerfile`, `deploy/claude-tail/docker-compose.yml`, `deploy/claude-tail/README.md`

- [ ] **Step 1: Write the Dockerfile** (python:3.x-slim + `pip install .`; entrypoint `python -m agent_redis_bridge.claude_tail`).
- [ ] **Step 2: Write the compose** — read-only bind mounts `~/.claude:/claude:ro` (+ the `~/.claude/tasks` mirror), `--user $(id -u)`, env `ARB_LIVE_REDIS_URL`/`ARB_TRACE_REDIS_URL` (+ `host.docker.internal` for local), offsets in Redis (no writable mount needed).
- [ ] **Step 3: Build + smoke** `docker build -f deploy/claude-tail/Dockerfile -t arb-claude-tail:dev .` then `docker run --rm arb-claude-tail:dev python -c "import agent_redis_bridge.claude_tail"` → import OK.
- [ ] **Step 4: README** — the settings.json hook snippet, the mounts, the bus env, the "container reads Claude dirs read-only; offsets in Redis" note.
- [ ] **Step 5: Commit** `git commit -m "feat(claude-tail): containerised daemon (read-only Claude mounts, Redis offsets)"`

---

## Task 12: Live end-to-end validation

**Files:** none (a runbook step, like the agy-print live check).

- [ ] **Step 1:** Run the daemon locally against this host's live warm session + a spawned cold-Opus reviewer (with an `[ARB_RUN:.. ARB_SEAT:..]` marker).
- [ ] **Step 2:** Assert in `events:live` (managed bus) that the warm seat + the cold-Opus seat appear with `task_started`, then `state` running, then `task_finished`; assert `arbmem:trace` carries the (redacted) model_text/thinking.
- [ ] **Step 3:** Open arb-watch: the cold-Opus reviewer groups under its panel run; the orchestrator sits in the isolated bottom section; both drill into a live transcript.
- [ ] **Step 4: Commit** a CHANGELOG entry recording the validated end-to-end result.

---

## Self-Review

- **Spec coverage:** tee-helper (T1), mapper+drift (T2), lifecycle (T3), identity+marker (T4), offset/Redis (T5), tail loop routing+redaction+commit (T6), service/discovery (T7), hooks+mirror (T8), arb-watch agentOf (T9) + isolated section (T10), container (T11), live E2E (T12). Covers every spec section.
- **Placeholder scan:** code shown for the load-bearing new modules; the refactor (T1) + arb-watch (T9/T10) tasks anchor to exact source to read (`bridge.py:1897`, `agentOf`, `visibleSeats`) since the exact lines depend on current source — the executor reads + matches, with concrete target signatures given.
- **Type consistency:** `Identity{run_id,task_id,seat_id,orchestrator}` used consistently T4→T6→T7; `Event{event_type,data}` T2→T3→T6; `OffsetStore.get/commit` T5→T6.
- **Open decisions to confirm at execution** (from the spec): multi-session scope (tail all warm sessions or one); container vs host-side for cold-Opus tail if the mirror proves unreliable; the `events:live` minimal-data policy (already decided: boundaries-only on live, full on trace).
