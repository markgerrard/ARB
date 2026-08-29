# Diagnose Live-Panel Orchestrator-Forwarded Dispatch — Implementation Plan [#13]

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-architect `run_panel` so the skill never self-dispatches — the orchestrator forwards each sealed brief through an injected dispatcher to three vendor-decorrelated bridge voting seats (codex/agy/M3) plus an in-process Agent-tool scribe — and add the load-bearing checks the spec-panel required.

**Architecture:** Skill authors+seals briefs and runs the recompute gate (unchanged); `run_panel(sealed_briefs, dispatch, work_dir)` takes an injected `dispatch` fn. Voting seats route over the bridge via the real `agent-dispatch` interface; the descriptive scribe runs as an in-process cold Agent-tool subagent and is **excluded from the verdict basis by construction**. A new neutral-validator predicate anchors model-decorrelation to seat identity.

**Tech Stack:** Python 3.14 (stdlib only in skills/), pytest, the `agent-dispatch` CLI, redis dev bus (db=12).

**Spec:** `docs/superpowers/specs/2026-06-19-diagnose-live-dispatch-design.md` (design-panel + spec-panel folded).

**Plan-panel verdict (2026-06-19):** cold-Opus (stake-free) + agy + M3, all **PLAN-HOLES**, converged. The
two deny-proofs were confirmed genuine (Task 2 + Task 4 fail today); Task 8's gate-onboard guard confirmed
correct. Five buildability P0s folded: **P0-1** scribe-exclusion must apply to the gate's recompute too
(Task 5 step-3, `neutral_validators.py:~133`) else every live run self-blocks; **P0-2** roster must keep
`model` (4 call sites) — added (Task 1); **P0-3** migrate seat==role fixtures to real target_ids (Task 4
step-3c) else the corpus goes red; **P0-4** register the 3 new block reasons in `NEUTRAL_BLOCK_REASONS` +
frozen test (Task 4 step-3b); **P0-5** Task 2's argv assertion was hollow (today's `--role` value passes a
"non-flag last arg" check) → pin the positional task's identity (`json.loads(argv[-1])["role"]=="blind"`).

## Global Constraints
- Skills are **stdlib-only** (no third-party imports in `skills/**`). Copy verbatim from the spec.
- Every voting-seat argv uses ONLY `--workspace`/`--engine`/`--target-id`/`--role` + the **positional `<task>`**. NEVER `--model`/`--ceiling`/`--work-dir` (the #7 flags).
- Verified seat identities (do NOT re-derive; `--check`-verified 2026-06-19 on `agent-redis-bridge-dev.env`, db=12):
  `blind → engine=codex, target_id=codex-bridge-dev`; `alternative → engine=agy-print, target_id=agy-bridge-dev`;
  `open → engine=pi-sdk, target_id=pi-sdk-bridge-dev-minimax-m3`; `sender=claude-bridge-dev`.
- **§4a is accident-enforced, adversary-attested** (the bus has no immutable `from→task` ledger — that is #14). The check must still **FAIL against today's `fake_dispatcher`**.
- **§4b is BY CONSTRUCTION** — the scribe's reply must be *unreachable* from the certifier/collation post-briefs (no `bus_reply_ref` path), not merely "not read."
- TDD: failing test first, run-to-fail, minimal impl, run-to-pass, commit. Each task ends green + committed.
- codex is the contributor-author and is **non-certifying** in the downstream review.

## File Structure
- `skills/diagnose/panel_constants.json` — roster pinned to the verified seats (Task 1).
- `skills/diagnose/panel.py` — `run_panel` records the originating seat; `bridge_dispatch` emits the real argv incl. positional task + returns the reply's `from` (Tasks 2, 3).
- `skills/diagnose/briefs.py` — `author_post_briefs` + `_certifier_model` exclude the scribe (Task 5).
- `skills/_diagnose_common/neutral_validators.py` — new `_decorrelation_blocks` predicate (Task 4); certifier starve fail-loud (Task 6).
- `tests/test_diagnose_live_dispatch.py` — new test module for all deny-proofs (Tasks 2–7).
- `skills/diagnose/SKILL.md` + spec §6 — honest-limit text + dispatcher contract (Task 8).

---

### Task 1: Pin the roster to the verified bridge seats

**Files:**
- Modify: `skills/diagnose/panel_constants.json`
- Test: `tests/test_diagnose_live_dispatch.py`

**Interfaces:**
- Produces: `roster` entries each with `{role, channel, engine, target_id, role_profile, vendor}`; `scribe` with `{model, channel}`; `sender`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_diagnose_live_dispatch.py
import json, pathlib
CONST = json.loads((pathlib.Path("skills/diagnose/panel_constants.json")).read_text())

def test_roster_is_three_distinct_bridge_voting_seats():
    voting = CONST["roster"]
    assert [s["role"] for s in voting] == ["blind", "alternative", "open"]
    assert all(s["channel"] == "bridge" for s in voting)
    # distinct seats AND distinct vendors (decorrelation)
    assert len({s["target_id"] for s in voting}) == 3
    assert len({s["vendor"] for s in voting}) == 3
    by_role = {s["role"]: s for s in voting}
    assert by_role["blind"]["engine"] == "codex" and by_role["blind"]["target_id"] == "codex-bridge-dev"
    assert by_role["alternative"]["engine"] == "agy-print" and by_role["alternative"]["target_id"] == "agy-bridge-dev"
    assert by_role["open"]["engine"] == "pi-sdk" and by_role["open"]["target_id"] == "pi-sdk-bridge-dev-minimax-m3"
    # `model` is REQUIRED on every entry (briefs.py:37,102 + diagnose.py:266 read seat["model"]) — plan-panel P0-2
    assert all(s.get("model") for s in voting)
    # scribe stays Agent-tool, non-voting
    assert CONST["scribe"]["channel"] == "agent-tool"
    assert CONST["sender"] == "claude-bridge-dev"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_diagnose_live_dispatch.py::test_roster_is_three_distinct_bridge_voting_seats -v`
Expected: FAIL (current roster has `open=claude-opus/agent-tool`, no `engine`/`target_id`/`vendor`/`sender`).

- [ ] **Step 3: Write the roster**

```json
{
  "roster": [
    {"role": "blind",       "model": "codex",             "channel": "bridge", "engine": "codex",    "target_id": "codex-bridge-dev",                       "role_profile": "reviewer",        "vendor": "gpt"},
    {"role": "alternative", "model": "agy",               "channel": "bridge", "engine": "agy-print", "target_id": "agy-bridge-dev",                         "role_profile": "reviewer",        "vendor": "gemini"},
    {"role": "open",        "model": "minimax/MiniMax-M3", "channel": "bridge", "engine": "pi-sdk",    "target_id": "pi-sdk-bridge-dev-minimax-m3", "role_profile": "judgment-oracle", "vendor": "minimax"}
  ],
  "scribe": {
    "model": "claude-haiku",
    "channel": "agent-tool",
    "system_prompt": "Describe only observed files, line references, command outputs, and structured facts. Do not infer causes, rank hypotheses, recommend fixes, or evaluate alternatives."
  },
  "sender": "claude-bridge-dev",
  "certifier": {"rule": "model!=author_model and not reciprocal"},
  "role_assignment": {"rule": "top-candidate-by-fixed-rank", "seed": 19062026},
  "collation_order": "by-seat-id-asc"
}
```

- [ ] **Step 4: Run test to verify it passes** — `pytest tests/test_diagnose_live_dispatch.py -k roster -v` → PASS
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(diagnose): pin live roster to verified codex/agy/M3 bridge seats [#13]"`

---

### Task 2: `bridge_dispatch` emits the real argv incl. the positional task (P0-2)

**Files:**
- Modify: `skills/diagnose/panel.py:46-75`
- Test: `tests/test_diagnose_live_dispatch.py`

**Interfaces:**
- Consumes: roster entry `{engine, target_id, role_profile}` + `sender`.
- Produces: `bridge_dispatch(target_id, engine, *, role, sender, argv_sink=None)` whose argv is `["scripts/agent-dispatch", "--workspace", "dev", "--engine", engine, "--target-id", target_id, "--role", role, task]`. `argv_sink` (test seam) captures the argv without running the subprocess.

- [ ] **Step 1: Write the failing test** (the argv deny-proof — fails against the stdin-only #7-shape code)

```python
import skills.diagnose.panel as panel

ACCEPTED = {"--workspace", "--engine", "--target-id", "--role", "--timeout", "--fresh-context"}
FORBIDDEN = {"--model", "--ceiling", "--work-dir"}

def test_bridge_argv_has_positional_task_and_only_accepted_flags():
    captured = {}
    d = panel.bridge_dispatch("codex-bridge-dev", "codex", role="reviewer", sender="claude-bridge-dev",
                              argv_sink=lambda a: captured.setdefault("argv", a))
    d({"role": "blind", "seal": "x", "brief": {"task": "find root cause"}})
    argv = captured["argv"]
    assert argv[0] == "scripts/agent-dispatch"
    flags = {a for a in argv if a.startswith("--")}
    assert flags <= ACCEPTED and not (flags & FORBIDDEN)
    # PLAN-PANEL P0-5 (M3): pin the positional task's IDENTITY, not just "non-flag string"
    # (today's argv ends with --role's VALUE "diagnose-panel", which a shape-only check passes).
    # The last element MUST be the brief JSON itself.
    assert json.loads(argv[-1])["role"] == "blind"   # last arg is the sealed brief, not a flag value
    assert "--engine" in argv and argv[argv.index("--engine") + 1] == "codex"
```

- [ ] **Step 2: Run to fail** — Expected: FAIL (current argv has no positional task; no `argv_sink`/`sender` params).
- [ ] **Step 3: Implement** — rewrite `bridge_dispatch`:

```python
def bridge_dispatch(target_id: str, engine: str, *, role: str = "reviewer",
                    sender: str = "claude-bridge-dev", argv_sink=None) -> DispatchFn:
    def dispatch(sealed_brief: dict) -> dict | None:
        task = json.dumps(sealed_brief, sort_keys=True)
        argv = ["scripts/agent-dispatch", "--workspace", "dev",
                "--engine", engine, "--target-id", target_id, "--role", role, task]
        if argv_sink is not None:
            argv_sink(argv)
            return {"model": engine, "from": target_id, "reply": ""}
        result = subprocess.run(argv, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, check=False, env={**os.environ, "AGENT_DISPATCH_SENDER": sender})
        if result.returncode != 0:
            return None
        try:
            decoded = json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"model": engine, "from": target_id, "reply": result.stdout}
        if isinstance(decoded, dict) and "reply" in decoded:
            return {"model": str(decoded.get("model", engine)),
                    "from": str(decoded.get("from", target_id)),
                    "reply": str(decoded["reply"])}
        return {"model": engine, "from": target_id, "reply": result.stdout}
    return dispatch
```
(Add `import os` at the top of `panel.py`.)

- [ ] **Step 4: Run to pass** — `pytest tests/test_diagnose_live_dispatch.py -k argv -v` → PASS
- [ ] **Step 5: Commit** — `feat(diagnose): bridge_dispatch emits real argv with positional task [#13]`

---

### Task 3: `run_panel` records the originating seat (the §4a anchor)

**Files:**
- Modify: `skills/diagnose/panel.py:29-38`
- Test: `tests/test_diagnose_live_dispatch.py`

**Interfaces:**
- Consumes: dispatcher return `{"model", "from", "reply"}` (the `from` is the reply envelope's originating seat).
- Produces: each submission carries `"seat": <originating seat from the dispatcher>`, not the role name.

- [ ] **Step 1: Write the failing test**

```python
def test_run_panel_records_originating_seat_not_role():
    briefs = [{"role": "blind", "seal": "s1"}]
    def disp(b):  # dispatcher reports the seat that answered
        return {"model": "codex", "from": "codex-bridge-dev", "reply": "obs"}
    out = panel.run_panel(briefs, disp, work_dir="/tmp/d13-rp", repo_root=".")
    sub = out["submissions"][0]
    assert sub["seat"] == "codex-bridge-dev"   # originating seat, NOT "blind"
    assert sub["role"] == "blind"
```

- [ ] **Step 2: Run to fail** — Expected: FAIL (`seat` is currently `sealed_brief["role"]`).
- [ ] **Step 3: Implement** — in `run_panel`, change the submission dict:

```python
            submissions.append(
                {
                    "role": sealed_brief["role"],
                    "seat": str(dispatched.get("from", sealed_brief["role"])),
                    "model": str(dispatched["model"]),
                    "seal": sealed_brief["seal"],
                    "bus_reply_ref": bus_reply_ref,
                    "bus_reply_sha256": hashlib.sha256(reply_text.encode("utf-8")).hexdigest(),
                }
            )
```

- [ ] **Step 4: Run to pass** → PASS
- [ ] **Step 5: Commit** — `feat(diagnose): record originating seat from reply envelope [#13]`

---

### Task 4: `_decorrelation_blocks` — the §4a load-bearing predicate

**Files:**
- Modify: `skills/_diagnose_common/neutral_validators.py` (`validate_run_record` + new private fn)
- Test: `tests/test_diagnose_live_dispatch.py`

**Interfaces:**
- Consumes: `run_record["submissions"]` (each `{role, seat, ...}`) + the committed roster (`_panel_constants()`).
- Produces: BLOCK reasons `model-mismatch` (a voting submission's `seat` ≠ its roster `target_id`) and `decorrelation-collapsed` (the three voting seats are not pairwise distinct).

- [ ] **Step 1: Write the failing test** (deny-proof: FAILS against today's fake_dispatcher seat values)

```python
from skills._diagnose_common import neutral_validators as nv

def _record(seats):  # seats: {role: seat_id}
    return {"verified": True, "submissions": [
        {"role": r, "seat": s, "seal": "x", "bus_reply_ref": "file://x",
         "bus_reply_sha256": "0"*64} for r, s in seats.items()]}

def test_decorrelation_blocks_collapsed_seats():
    # adversary collapses all three voting roles onto one seat
    blocks = nv._decorrelation_blocks(_record(
        {"blind": "codex-bridge-dev", "alternative": "codex-bridge-dev", "open": "codex-bridge-dev"}))
    assert "decorrelation-collapsed" in blocks

def test_decorrelation_blocks_seat_mismatch():
    blocks = nv._decorrelation_blocks(_record(
        {"blind": "codex-bridge-dev", "alternative": "agy-bridge-dev", "open": "WRONG-SEAT"}))
    assert "model-mismatch" in blocks

def test_decorrelation_passes_correct_roster_seats():
    blocks = nv._decorrelation_blocks(_record(
        {"blind": "codex-bridge-dev", "alternative": "agy-bridge-dev",
         "open": "pi-sdk-bridge-dev-minimax-m3"}))
    assert blocks == []

def test_decorrelation_FAILS_against_role_named_seats():
    # the fixture-masks-reality deny-proof: today's run_panel recorded seat==role.
    # Such a record MUST now block (seats are role names, not target_ids).
    blocks = nv._decorrelation_blocks(_record(
        {"blind": "blind", "alternative": "alternative", "open": "open"}))
    assert "model-mismatch" in blocks
```

- [ ] **Step 2: Run to fail** — Expected: FAIL (`_decorrelation_blocks` undefined).
- [ ] **Step 3: Implement** — add to `neutral_validators.py`:

```python
def _decorrelation_blocks(run_record: dict) -> list[str]:
    roster = {s["role"]: s for s in _panel_constants().get("roster", [])}
    subs = {s.get("role"): s for s in run_record.get("submissions", []) if s.get("role") in roster}
    if set(subs) != set(roster):          # voting panel incomplete → other predicates handle it
        return []
    reasons = []
    seats = []
    for role, seat_cfg in roster.items():
        seat = subs[role].get("seat")
        seats.append(seat)
        if seat != seat_cfg.get("target_id"):
            reasons.append("model-mismatch")
    if len(set(seats)) != len(seats):
        reasons.append("decorrelation-collapsed")
    return sorted(set(reasons))
```
And in `validate_run_record`, add: `reasons.extend(_decorrelation_blocks(run_record))`.

- [ ] **Step 3b: Register the new block reasons (plan-panel P0-4).** Add `"model-mismatch"`,
  `"decorrelation-collapsed"`, and `"certifier-starved"` (Task 6) to `NEUTRAL_BLOCK_REASONS`
  (`neutral_validators.py:17`) AND to the frozen-set assertion in `tests/test_diagnose_common.py:255` — else
  the frozen-surface test goes red.
- [ ] **Step 3c: Migrate the existing seat==role fixtures (plan-panel P0-3).** Wiring this predicate turns
  the existing corpus red because `fake_dispatcher` (`tests/test_diagnose.py:70`) and the static run-records
  (`:382,:389`) use seat==role. Fix: make `fake_dispatcher` return `"from"` = the roster `target_id` for each
  role (so `run_panel` records real seats), e.g. `{"blind": "codex-bridge-dev", "alternative":
  "agy-bridge-dev", "open": "pi-sdk-bridge-dev-minimax-m3"}[role]`; and update the static records'
  `seat` fields to the same target_ids. Re-run `tests/test_diagnose.py` — the migrated corpus stays green
  with the predicate active (and `test_panel_constants_committed_and_decorrelated` at `:229-247` passes given
  Task 1's `model` field).
- [ ] **Step 4: Run to pass** — the four `_decorrelation_blocks` tests PASS, the frozen-surface test PASS,
  AND the full `tests/test_diagnose.py` corpus stays green (fixtures migrated). `test_decorrelation_FAILS_
  against_role_named_seats` proves the check rejects yesterday's seat==role records.
- [ ] **Step 5: Commit** — `feat(diagnose): §4a seat-identity decorrelation predicate + migrate fixtures (accident-enforced) [#13]`

---

### Task 5: Exclude the scribe from the verdict basis — by construction (§4b)

**Files:**
- Modify: `skills/diagnose/briefs.py` (`author_post_briefs:50-64`, `_certifier_model:100-104`)
- Test: `tests/test_diagnose_live_dispatch.py`

**Interfaces:**
- Produces: certifier + collation post-briefs whose `sealed_submissions` contain NO `role=="scribe"` entry; `_certifier_model` never returns the scribe model.

- [ ] **Step 1: Write the failing test**

```python
from skills.diagnose import briefs

def _subs():
    return [
        {"role": "scribe", "seat": "haiku", "model": "claude-haiku", "seal": "z",
         "bus_reply_ref": "file://scribe", "bus_reply_sha256": "0"*64},
        {"role": "blind", "seat": "codex-bridge-dev", "model": "codex", "seal": "a",
         "bus_reply_ref": "file://blind", "bus_reply_sha256": "1"*64},
    ]

def test_scribe_unreachable_from_post_briefs():
    posts = briefs.author_post_briefs(briefs._load_constants() if hasattr(briefs, "_load_constants") else __import__("json").loads(__import__("pathlib").Path("skills/diagnose/panel_constants.json").read_text()), _subs(), [])
    for pb in posts:
        subs = pb["brief"].get("sealed_submissions", [])
        assert all(s["role"] != "scribe" for s in subs), "scribe leaked into a post-brief"
        assert all("scribe" not in s.get("bus_reply_ref", "") for s in subs)

def test_certifier_model_never_scribe():
    const = __import__("json").loads(__import__("pathlib").Path("skills/diagnose/panel_constants.json").read_text())
    m = briefs._certifier_model(const, [{"author_model": "codex"}])
    assert m != const["scribe"]["model"]
```

- [ ] **Step 2: Run to fail** — Expected: FAIL (scribe currently folded into post-briefs; certifier candidates include scribe).
- [ ] **Step 3: Implement (BOTH sides must agree — plan-panel P0-1)** —
  - In `briefs.py` `author_post_briefs`, filter the scribe before building `submissions`:
    ```python
    sealed_submissions = [s for s in sealed_submissions if s.get("role") != "scribe"]
    ```
    (insert as the first line of the function, before the `sorted([...])`).
  - In `briefs.py` `_certifier_model`, delete `candidates.append(constants["scribe"]["model"])` (line 103) so candidates = roster models only.
  - **In `neutral_validators.py` `_panel_blocks` — the GATE's own recompute must exclude the scribe too**, or the recomputed post-brief seal will not match the skill's (every live run self-blocks `unverified-without-panel`). Change the `expected_submissions` line (≈`:133`) from
    `expected_submissions = _canonical_submissions(submissions)` to
    `expected_submissions = _canonical_submissions([s for s in submissions if s.get("role") != "scribe"])`.
    Leave the separate scribe *pre*-brief check (≈`:120-128`) intact — the scribe is still dispatched + recorded as an audit artifact; only its post-brief inclusion is severed.

- [ ] **Step 3b: Add the both-sides-agree test** — extend `test_scribe_unreachable_from_post_briefs` (or add one) asserting a full `run_diagnose` with a scribe submission still gates GREEN (recompute matches) AND the post-briefs contain no scribe — proving the skill side and gate side agree.
- [ ] **Step 4: Run to pass** → PASS (skill + gate agree; no `unverified-without-panel` on a clean run)
- [ ] **Step 5: Commit** — `feat(diagnose): exclude scribe from verdict basis by construction, both sides (§4b) [#13]`

---

### Task 6: Certifier stable-order + starve fail-loud (§3)

**Files:**
- Modify: `skills/diagnose/briefs.py` (`_certifier_model:100-110`)
- Test: `tests/test_diagnose_live_dispatch.py`

**Interfaces:**
- Produces: `_certifier_model` selects by a stable total order (sorted model-id) and raises a typed `CertifierStarved` (caught by the panel and surfaced as a `certifier-starved` block) when no candidate ≠ author exists — NOT an uncaught `ValueError`.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from skills.diagnose import briefs

def test_certifier_starve_is_typed_not_bare():
    const = {"roster": [{"role": "blind", "model": "codex"}], "scribe": {"model": "claude-haiku"},
             "certifier": {"rule": "x"}}
    # only candidate is codex; author is codex → starved
    with pytest.raises(briefs.CertifierStarved):
        briefs._certifier_model(const, [{"author_model": "codex"}])

def test_certifier_selection_is_stable_order():
    const = {"roster": [{"role": "b", "model": "zeta"}, {"role": "a", "model": "alpha"}],
             "scribe": {"model": "claude-haiku"}, "certifier": {"rule": "x"}}
    # author=zeta → only 'alpha' qualifies; selection deterministic regardless of list order
    assert briefs._certifier_model(const, [{"author_model": "zeta"}]) == "alpha"
```

- [ ] **Step 2: Run to fail** — Expected: FAIL (`CertifierStarved` undefined; current code raises bare/returns by list order).
- [ ] **Step 3: Implement** —
  ```python
  class CertifierStarved(Exception):
      pass

  def _certifier_model(constants: dict, predicates: list[dict]) -> str:
      author_models = {p.get("author_model") for p in predicates if p.get("author_model")}
      candidates = sorted({seat["model"] for seat in constants["roster"]})  # scribe excluded; stable order
      for model in candidates:
          if model not in author_models:
              return model
      raise CertifierStarved("no roster model decorrelated from the predicate author")
  ```
  In `run_diagnose` (and any caller in the panel try/except), catch `CertifierStarved` and return a `certifier-starved` block instead of letting it propagate (replace the uncaught path at `diagnose.py:95`).

- [ ] **Step 4: Run to pass** → PASS
- [ ] **Step 5: Commit** — `feat(diagnose): certifier stable-order + typed starve fail-loud (§3) [#13]`

---

### Task 7: Goes-live contract test — non-blanket-mock (§5)

**Files:**
- Test: `tests/test_diagnose_live_dispatch.py`

**Interfaces:**
- Consumes: a thin real-interface dispatcher shim that constructs the actual argv via `bridge_dispatch(..., argv_sink=...)` and returns a canned reply — proving `run_panel` drives the real contract, NOT a blanket `run_panel` mock.

- [ ] **Step 1: Write the test**

```python
def test_run_panel_drives_real_dispatch_contract_not_blanket_mock():
    seen = []
    def shim(sealed_brief):
        captured = {}
        bd = panel.bridge_dispatch("codex-bridge-dev", "codex", role="reviewer",
                                   sender="claude-bridge-dev", argv_sink=lambda a: captured.update(argv=a))
        bd(sealed_brief)                      # exercises the REAL argv builder
        seen.append(captured["argv"])
        return {"model": "codex", "from": "codex-bridge-dev", "reply": "candidate: X"}
    out = panel.run_panel([{"role": "blind", "seal": "s"}], shim, work_dir="/tmp/d13-live", repo_root=".")
    assert out["blocking"] is None
    # the contract was actually built (positional task present), not stubbed away
    assert seen and not seen[0][-1].startswith("--") and seen[0][-1].strip()
```

- [ ] **Step 2: Run to pass** (this is a positive contract test) → PASS
- [ ] **Step 3: Commit** — `test(diagnose): goes-live contract test exercises real argv builder [#13]`

---

### Task 8: SKILL.md dispatcher contract + §6 honest limits + gate-onboard

**Files:**
- Modify: `skills/diagnose/SKILL.md` (dispatcher routing table + read-only ceiling per channel)
- Modify: spec §6 already carries the honest limits (no code) — mirror the one-paragraph dispatcher contract into SKILL.md so a context-free orchestrator forwards correctly.
- Run: gate-onboard any changed gate-relevant files; re-pin `certified_object_sha` ONLY if gate LOGIC changed (this plan touches diagnose skill + validators, not `gate.py` — confirm with `git diff --name-only` before any trust-root edit).

- [ ] **Step 1:** Add to `skills/diagnose/SKILL.md` a "Dispatcher contract" section: per-role routing (`blind → --engine codex --target-id codex-bridge-dev`; `alternative → --engine agy-print --target-id agy-bridge-dev`; `open → --engine pi-sdk --target-id pi-sdk-bridge-dev-minimax-m3`; each `--workspace dev --role <role_profile> <task>`, sender `claude-bridge-dev`; `scribe → in-process Agent-tool cold subagent`), and the read-only ceiling note (enforced on M3, attested on codex/agy).
- [ ] **Step 2:** Run the full diagnose test suite + the bridge-protocol self-gate. Expected: green. `pytest tests/test_diagnose_live_dispatch.py tests/test_diagnose.py -v`
- [ ] **Step 3:** `git diff --name-only` — confirm NO change to `skills/bridge-protocol/gate/gate.py` or `gate/schemas/*`. If unchanged, NO trust-root re-pin. If changed, STOP and surface (protected-file gate).
- [ ] **Step 4: Commit** — `docs(diagnose): SKILL.md dispatcher contract + §6 honest limits [#13]`

---

## Self-Review (run before handing to the builder)
- **Spec coverage:** §2 channels → T1/T2/T8; §3 certifier → T6; §4 roster → T1; §4a predicate → T3/T4; §4b scribe-exclusion → T5; §5 deny-proofs → T2/T4/T7; §6 honest limits → T8. ✓
- **Deny-proof present:** T2 (argv fails #7-shape), T4 (`test_decorrelation_FAILS_against_role_named_seats` fails yesterday's seat==role records / fake_dispatcher). ✓
- **No placeholder:** all argv values are the verified literals; no `<seat>` left unresolved. ✓
- **The §4a limit is named, not silently full:** Task 4 implements only accident-enforcement; adversarial `from`-fabrication is #14. The plan does NOT claim adversarial enforcement. ✓
