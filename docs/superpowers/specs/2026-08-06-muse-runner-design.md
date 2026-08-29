# Design — `muse_runner.py`: a cold-process / warm-session seat runner

**Repo pin:** `feat/muse-runner-spec` off `dev` @ `6568a3fc`. Every `file:line` below resolves in
this tree; they do **not** resolve on `worktree-warm-orch-testdrive`, which is 21 commits behind and
lacks `ReasoningDelta` entirely.

**Grounded in:** ARB Memory artefact `findings-muse-code-seat-probe-20260806` **v3** (content_hash
`ec29b1d2…49827`, published and hydrate-verified 2026-08-07), measured against
`Muse Code 0.1.0 (0.1.0-R708.1)`. **Cite v3.** v2 (`27e66753…e149e`) corrected v1
(`737adfdc…1fb80`) in four places against a live capture; v3 then withdrew one of v2's own
corrections and narrowed one gap. All three versions are retained. That note is the evidence; this
note is the design. Where they disagree, the note wins and this file is wrong — the tie-break is
unqualified again as of v3.

> **Why the tie-break briefly needed a carve-out, kept because the failure is instructive.** v2 §2
> asserted that the captured flag list had "no long `--worktree`" and that the point was unresolved.
> That was false, and v1 had it right. This design note "corrected" v1 from a lossy flag capture, and
> **v2 then adopted that correction back into the evidence record** — so both documents agreed, and
> their agreement was worthless, because it was one source counted twice. G5 disproved it on
> 2026-08-07; artefact v3 withdrew it at source, which is why no carve-out is needed now.
>
> The transferable part: an evidence record is only independent while it is *independently sourced*.
> Once a consumer's inference is written back into the artefact it cites, the citation becomes
> circular and the tie-break rule turns into an error amplifier. That is the same shared-wrong-source
> failure v3's §0 documents for C3/C4, recurring one level up — in the provenance chain rather than
> in the test suite. **Never write a consumer's inference back into the artefact it cites.**

**Scope:** phase 1 of three — the runner only. Approvals (P2) and seat parity (P3) are specified as
successors with gates between, not as out-of-scope hand-waves. See §8.

**Status:** design, not implementation. No code written. No spec obligations discharged.

---

## 0. Provenance of every claim below

Three evidence grades, kept apart. This section exists because a prior costing in this repo shipped
a provenance table with an unrun command in it, and the convention that caught it is now standing
(`the remote-observer design note (not included in this repository)` §9, §10).

| Grade | What it means | Where used |
|---|---|---|
| **verified-here** | Command run in this session, output seen | §1 CLI surface, all `file:line` citations |
| **inherited** | From the findings artefact, re-run **not** attempted | §2 event map, §3 continuity |
| **hypothesis** | Design intent, **not** measured — each carries a gate in §7 | §4 interrupt, §5 system prompt, `ReasoningDelta` |

**Verified-here this session** (no `muse exec`, therefore no API spend):

```
muse --version                 -> Muse Code 0.1.0 (0.1.0-R708.1)
which muse                     -> /Users/<user>/.local/bin/muse  (33118 bytes)
muse --help                    -> subcommand + top-level flag list
muse exec --help               -> full flag list, reproduced in §1
muse exec --help | grep -icE '--system|--instruction|base instruction'  -> 0
```

**Drift from the findings note, recorded rather than smoothed over:** the note gives the launcher as
33118 bytes dated `6 Aug 04:44`; it is the same 33118 bytes but dated `6 Aug 05:19` here. Size and
version string match, so this is a re-install or touch, not a new build — but a binary whose mtime
moved once in a day is not a stable target, which is the argument for §6's live tier.

---

## 1. The CLI surface this design commits to (verified-here)

`muse exec` full flag list, captured this session:

```
--allow-workspace-switch --api-key-stdin --base-url --context-compaction-hard-threshold
--context-compaction-soft-threshold --context-compaction-strategy --disable-approval
--disable-sandbox --disable-shell --disable-web-tools --disable-write --enable-shell-tool
--image --json --max-model-steps --max-tool-output-bytes --model --no-foreign-personal-context
--no-parallel-tool-calls --no-session-log --parallel-tool-calls --preset --prompt-file
--provider --reasoning-effort --sandbox-network --session-id --subagent-worktree-isolation
--trust-workspace --user-input-auto-resolve --workspace --worktree-base --worktree-existing
--yolo -h -w
```

The runner uses exactly these and no others:

| Flag | Why |
|---|---|
| `--json` | the JSONL envelope; the entire parse contract |
| `--session-id <UUID>` | cross-process continuity (§3) |
| `--prompt-file <PATH>` | brief passed as a file — avoids argv quoting, which is a recorded failure class in this repo (`\n` shell-quoting, see `skills/using-agent-bridge`) |
| `--workspace <PATH>` | roots the policy-gated workspace tools |
| `--model`, `--reasoning-effort` | per-seat pinning, matching the per-seat effort work already on `dev` |
| `--max-model-steps`, `--max-tool-output-bytes` | turn bounding |
| `--user-input-auto-resolve` | headless: offer `request_user_input` and auto-cancel |

**One correction to the findings note's §2 table**, verified-here:

1. The approvals-disabling flag is `--disable-approval`, singular. P2 must not assume
   `--disable-tool-approvals`.

**~~Correction 2 (`--worktree` has no long form)~~ — WITHDRAWN by gate G5, 2026-08-07. The note was
right and this file was wrong**, exactly as the header's tie-break rule provides. The full
`muse exec --help` (85 lines, read in full rather than grepped) contains, verbatim:

```
  -w, --worktree [<MODE>]
          Session git worktree: off|create|existing
```

**Root cause of the false correction, reproduced.** The captured flag list above (lines 52–59) was
extracted by a pattern anchored on `^\s+--`. Help lines that carry a short alias begin `  -w, --…`
and `  -h, --…`, so the anchor never matched them and both long forms were dropped. The proof is
already visible in the capture itself: it lists `-h` and `-w` but neither `--help` nor `--worktree`
— and those are the *only* two flags in the whole surface with short forms. A flag list containing
`-h` but not `--help` is self-evidently incomplete; that internal inconsistency was the free tell
and it was missed.

> **Standing lesson.** A mechanically-produced capture is not ground truth — it carries the defects
> of the extractor. This file downgraded a correct external claim on the authority of a lossy grep.
> Where a capture and a source-of-truth disagree, re-read the source, do not correct the source.

**Consequence for the runner.** `--worktree` is real and usable, but P1 still encodes no worktree
flag — that scope decision was never contingent on this ambiguity. Two facts to carry into P2/P3:

- The value is **optional** (`[<MODE>]`, clap's optional-value form). Bare `-w` is legal, so
  `--worktree create` risks binding `create` as the positional `[PROMPT]`. Use the unambiguous
  `--worktree=create` / `-w=create` form if it is ever encoded.
- `--worktree-base` and `--worktree-existing` are separate flags that qualify the `create` and
  `existing` modes respectively; they do not replace `--worktree`.

**Not used, deliberately:** `--yolo`, `--disable-sandbox`, `--disable-approval`, `--trust-workspace`.
Each widens the blast radius of a dispatched worker, and P1 has no evidence-gated story for any of
them. P2 owns approvals; until then the runner takes Muse's defaults, including
`--sandbox-network proxy-only`.

---

## 2. Where this sits, and what it is not

**It is not an ACP adapter.** The findings note's §6 verdict, restated because a prior report framed
it the other way: the bridge does not consume its engines over ACP. `acp_server.py` speaks ACP
**outward** to buzz; `codex_stdio.py` wraps codex's own protocol **inward**. `muse_runner.py` sits in
the inward slot beside `codex_runner.py`, `grok_runner.py`, `pi_runner.py`.

**The contract is structural, not declared — this is a live hazard.** `acp_server.serve` takes
`runner: Any` (`acp_server.py:78`), and `AcpServer.__init__` likewise (`:203`). There is no
`Protocol`, no ABC, no `@runtime_checkable`. **Nothing will type-check `MuseRunner`.** Conformance
is by having the right method names, and a missing method fails at call time in a live channel.

The shape to conform to, from `WarmOrchRunner` (`runner.py:83`):

| Method | Line | P1 obligation |
|---|---|---|
| `connect()` | `runner.py:168` | establish session identity, spawn nothing |
| `disconnect()` | `runner.py:174` | reap any live child; keep session-id |
| `interrupt()` | `runner.py:180` | §4 |
| `stream_turn(text) -> AsyncIterator[TurnEvent]` | `runner.py:197` | §2.2 |
| `turn(text) -> str` | `runner.py:226` | drain `stream_turn`, join `TextDelta` only |

### 2.1 Two units, split on purpose

| Unit | Depends on | Tested by |
|---|---|---|
| `parse_envelope(line: str) -> TurnEvent \| None` — pure | nothing | offline fixtures, **zero** muse calls |
| `MuseRunner` — spawn, reap, signal, session-id | `parse_envelope` | live tier, binary-gated (§6) |

All Muse-specific knowledge lives in the pure function; it never touches a process. That is what
makes the budget in §6 achievable — the expensive surface is deliberately thin. It also answers the
"can you change the internals without breaking consumers" test: the parser's consumers see only
`TurnEvent`, which is the vocabulary the other three runners already emit.

### 2.2 `stream_turn` shape

```
stream_turn(text):
    await connect()                       # idempotent; ensures session-id exists
    write text -> tmp prompt file
    spawn: muse exec --json --session-id <sid> --prompt-file <tmp> [--workspace …]
    async for line in child.stdout:
        if not line.startswith("{"): continue      # preamble, §2.3
        ev = parse_envelope(line)
        if ev is not None: yield ev
    await child.wait()
    evaluate turn outcome from run.terminal.* ONLY   # §5 trap 1
```

### 2.3 The preamble

`muse exec` prints a non-JSON line (`muse: workspace root: …`) to stdout before the JSONL
(inherited, findings §7). The parser skips any line not starting with `{`. This is a **skip**, not a
parse error — treating it as malformed would make every successful turn log an error.

---

## 3. The structural difference: cold-process, warm-session

Muse holds no process between turns. Continuity is a persisted session UUID: two independent
`muse exec` invocations sharing `--session-id` carry context (inherited, findings §3 — the
`PLUM-4271` recall). Every existing runner holds a process open, so this model has no precedent here.

### 3.1 The session-id inversion, and why it is a simplification

`runner.py:180-196` documents a real coupling for the Claude runner: the SDK hands back the
session-id at the *end* of a turn, so `_persist_session_id` runs inside `stream_turn`
(`runner.py:224`) and **a caller that abandons the generator early never advances the channel**. The
docstring is explicit that `turn()` is safe because it drains, and the ACP server is not.

**That coupling does not reproduce under Muse, and the design must not copy it by reflex.** We mint
the UUID ourselves, so it is known *before* any process exists:

> **`MuseRunner.connect()` persists the session-id immediately, before the first turn.**

Continuity therefore survives a crash, a kill, an abandoned generator, and an interrupted turn.
Persistence is no longer coupled to turn completion at all.

The persistence mechanism is already in the tree and is reused, not reinvented:
`_session_id_path` (`runner.py:100`), `_load_session_id` (`runner.py:103`), `_persist_session_id`
(`runner.py:110`).

**`connect()` semantics:** load persisted id; if absent, mint `uuid4()` lowercased and persist.
Idempotent — `stream_turn` calls it unconditionally, mirroring `runner.py:198`.

---

## 4. `interrupt()` — hypothesis, gated

**Specified behaviour:** SIGTERM the live child, await exit, spawn nothing. **The session-id is
retained, not rotated.**

**This is a hypothesis.** The findings note (§3, §5 gap 2) flags it untested: killing mid-turn may
leave the session unresumable. It is written as intent because a seat that cannot be cancelled is of
doubtful use to the bridge, and the alternative — rotating the id on every interrupt — would silently
discard conversation context, which is a worse failure because the caller cannot see it.

**Gate G1 (§7) must pass before P1 closes.** If it fails, the fallback is rotate-on-interrupt with
the context loss surfaced as an explicit event, not swallowed.

No `SIGKILL` escalation is specified in P1: whether Muse cleans up its session log on SIGTERM is
unmeasured, and adding a kill path before G1 runs would be guessing at a failure mode we have not
seen. G1 measures it.

---

## 5. Event mapping, and the three traps

### 5.1 The map (inherited, findings §4)

| Muse `payload_type` | Key fields | `turn_events` |
|---|---|---|
| `run.output.delta` | `text` | `TextDelta` (`turn_events.py:36`) |
| `task.lifecycle.proposed` | `task_kind: "tool.bash"` | `ToolCallStarted` (`:57`) |
| `task.lifecycle.scheduled` | `idempotency_key: "tool:call_…"` | stable `tool_call_id` |
| `tool.result` | `call_id`, `correlation_facts{tool_name, outcome}` | `ToolCallCompleted` (`:71`) |
| `run.terminal.completed` | `terminal`, `text`, `reason` | turn close |
| `task.lifecycle.output` | `chunk` (`command`, `exit_code`, …) | tool output detail |

`ToolCallCompleted.status` maps from `correlation_facts.outcome`. `turn_events.py:73` requires that
a failing tool stay distinguishable — *"collapsing both into completed would make a failing tool
invisible"* — and `outcome` carries a real verdict, so the requirement is satisfiable rather than
merely asserted.

### 5.2 Trap 1 — `task.lifecycle.failed` does **not** mean the turn failed

The echo provider completes a run (`terminal: completed`) while emitting **two**
`task.lifecycle.failed` events with `reason: "invalid run configuration: provider does not support
base instructions"` (inherited, findings §4).

> **Rule: turn outcome derives from `run.terminal.*` only.** `task.lifecycle.failed` is recorded as
> detail and never determines the turn result.

A naive adapter reports a false failure on every echo run. This rule is what makes the free echo
provider usable as a smoke test at all (§6).

### 5.3 Trap 2 — `task.lifecycle.status` is **not** reasoning

It is provider-retry telemetry: `"opening meta model stream attempt 1/10"`, with
`details.facets[].kind = external_attempt|producer`.

> **Rule: `task.lifecycle.status` never maps to `ReasoningDelta`.**

### 5.4 Trap 3 — the system-prompt seam, which this repo has already paid for once

`acp_server.py:238-255` records the codex incident in its own words: a runner that *has* somewhere to
put a system prompt but does not expose `apply_system_prompt` **silently drops the client's composed
prompt**, and *"the failure mode is a seat that works perfectly and says nothing in the channel."*
The adopt call is `getattr(self.runner, "apply_system_prompt", None)` at `acp_server.py:265`, which
no-ops when absent. That docstring also records that an earlier revision asserted codex had no
system-prompt concept at all, that this was wrong, and that it cost a live-test round.

**Muse almost certainly has such a seam** — its own echo error names *"base instructions"* — and
**verified-here, `muse exec` has no flag for it**: `grep -icE '--system|--instruction|base instruction'`
over the full exec help returns `0`, and the §1 flag list confirms. The candidates are the top-level
`--agents <JSON>` ("supply one ephemeral agent-definition overlay") and `--preset <NAME>`, neither of
which appears in exec's flag list — so whether a top-level flag even composes with the `exec`
subcommand is **unresolved**.

> **P1 obligation: `MuseRunner` MUST define `apply_system_prompt`.** If G2 shows the seam is not
> reachable from `exec`, the method MUST raise or log loudly rather than no-op silently — the whole
> point of the codex incident is that the quiet path is the expensive one.

### 5.5 `ReasoningDelta` — mapped speculatively, marked untested

`ReasoningDelta` exists at `turn_events.py:43` and its docstring states the stake: it maps to
`agent_thought_chunk` on the ACP wire, and *"without this event the panel's thought view is
structurally empty"* for that seat.

The findings note probed only `--reasoning-effort minimal` and saw no reasoning delta, so this is
**untested, not absent** (§5 gap 1). Muse advertises `none|minimal|low|medium|high|xhigh|ultra`.

> P1 maps any payload carrying a reasoning-shaped stream to `ReasoningDelta`, **and G3 must identify
> the payload_type or record that none exists at `high`.** Until G3, no claim is made either way.

---

## 6. Testing, and the budget

Two tiers, matching the repo's existing convention (`test_grok_acp.py` alongside
`test_grok_acp_e2e.py`; `test_pi_rpc.py` alongside `test_pi_rpc_e2e.py`).

**Tier 1 — offline, zero spend.** `tests/test_muse_runner.py` drives `parse_envelope` and the
`MuseRunner` state machine against committed JSONL fixtures under `tests/fixtures/muse/`. This is the
bulk of the suite and never invokes `muse`.

**Tier 2 — live, binary-gated.** `tests/test_muse_runner_e2e.py`, skipped when
`shutil.which("muse") is None`. It re-derives the fixtures and asserts the envelope *shape* still
matches. This tier exists because `0.1.0-R708.1` is early software whose launcher mtime already
moved once in a day; without it, nothing detects Muse changing its envelope.

**Fixtures to capture once and commit:** an echo-provider run (free); one `bash` tool call; a
two-turn session resume; an interrupted turn (G1).

### 6.1 Budget

Owner-set ceiling: **$10**. Standard-tier pricing for `muse-spark-1.2`, supplied by the owner:
**cached input $0.15 / input $1.25 / output $4.25 per 1M tokens.**

Pessimistic per-turn assumption — 20k input, 5k output, no cache:

```
input   20,000 × $1.25/1M = $0.025
output   5,000 × $4.25/1M = $0.02125
                            ---------
per turn                    ≈ $0.0462
$10 ceiling                 ≈ 216 turns   (10 / 0.04625 = 216.2, rounded DOWN)
```

Rounded down deliberately: rounding a headroom figure up overstates the budget, which is the
direction that gets a cap breached.

P1 needs roughly 15–25 live turns to capture fixtures and run G1–G3. **The P1 live ceiling is set at
60 turns (≈ $2.77 pessimistic)** — generous against need, comfortable against cap. Session resume
should hit the $0.15 cached-input rate, so real cost is expected well below this; that expectation is
**not** an estimate to cite, it is a reason the ceiling is safe.

> **Obligation: the first live capture MUST check whether the `--json` envelope carries token
> usage.** The findings note does not mention one. If usage is present, record actuals and replace
> the pessimistic arithmetic with measurement. If absent, count turns and say so — do not convert
> turns to dollars without a rate confirmed against a real bill.

`--no-session-log` is available and is **not** used: the session log is where G1's resumability is
observable.

---

## 7. Gates — P1 does not close until these run

Each has a pass/fail predicate. None may be closed by assertion. Per
`docs/defect-classes/refusal-is-ambient-assert-the-code.md`, each asserts a **specific** outcome, not
a bare success.

| ID | Question | Probe | Pass |
|---|---|---|---|
| ~~**G1**~~ | ~~Does a killed turn leave the session resumable?~~ | **RUN 2026-08-07 — PASSED.** See §12 | interrupt() retaining the session id is now licensed by measurement, not hypothesis |
| **G2** | Is the system-prompt seam reachable from `exec`? (§5.4) | try `--agents <JSON>` with the `exec` subcommand; observe whether instructions take effect | seam identified, or recorded unreachable and `apply_system_prompt` made loud. **Flag topology now measured free — see below; only the behavioural half still costs a turn** |
| **G3** | Does a reasoning stream exist at higher effort? (§5.5) | one turn at `--reasoning-effort high`, capture full JSONL | reasoning `payload_type` named, or "none at `high`" recorded with the capture as evidence |
| **G4** | Do two concurrent `exec`s on one session-id corrupt it? (findings §5 gap 3) | two overlapping execs, same id, then a recall turn | either safe, or the runner must serialise turns per session — **P1 assumes serialised and does not test concurrency support** |
| ~~**G5**~~ | ~~What is `-w`'s long form? (§1)~~ | **RUN 2026-08-07 — PASSED, no API call.** Full 85-line `muse exec --help` read | **`-w, --worktree [<MODE>]`** — the long form exists. §1's contrary "correction" is withdrawn there, with the extractor bug that caused it reproduced |
| **G6** | Does `tool.result.call_id` equal `task.lifecycle.scheduled.idempotency_key` minus its `tool:` prefix? | one live turn containing a `bash` tool call; capture full JSONL; compare the two ids | ids join. **Fail** ⇒ `ToolCallCompleted` never matches its `ToolCallStarted`, and **nothing raises** — buzz's idle deadline (`turn_events.py:60-62`) silently never resets |

~~G5 is free and should run first.~~ **G5 ran 2026-08-07 and passed.** G2's remainder, G4 consume the
live budget; G1, G3 and G6 are closed (§12).

**G2's flag topology, measured free on the same `--help` read (2026-08-07).** Three facts, each a
zero-hit or exact-line grep over the full help text of both `muse --help` and `muse exec --help`:

| Flag | `muse <flag> …` (top level) | `muse exec <flag> …` | 
|---|---|---|
| `--system-prompt` | **absent** | **absent** | 
| `--agents <JSON>` | present | **absent** | 
| `--preset <NAME>` | present | present (`native-basic`, `miniswe`) |

So the seam, if it exists, is **`muse --agents '<JSON>' exec …`** — `--agents` must precede the
subcommand, because `exec` does not accept it. This narrows G2 from "is there a seam at all" to a
single behavioural question: *does a top-level `--agents` actually reach an `exec` run's model
context?* That still costs one live turn — flag acceptance is not evidence of effect, and a flag
parsed-then-ignored is precisely the failure this gate exists to catch. **Assert on observed model
behaviour (a canary instruction obeyed), never on exit code 0.**

**G6 was raised by the implementation, not by this design** — writing the mapper forced the question of how a completion finds its start, and the evidence note lists the two id fields without stating their relation. `muse_events.normalise_call_id` implements the strip and says in its own docstring that it is an assumption. It is recorded here because a gate discovered downstream is exactly the kind that gets lost between documents.

**Carried forward unresolved, not gated in P1** (findings §5, dispositions stated so none is lost):

- **gap 4, `session-message` socket bus** — unprobed; a possible second, warmer seat model. **P3.**
  If it yields a persistent process, it competes with this whole design, which is why P1 keeps the
  process-management surface thin.
- **gap 5, binary-grep + Zed-registry claims** — inherited, never re-verified. **Not load-bearing
  here**: this design does not depend on ACP absence, only on the CLI surface verified in §1.
- **gap 6, idle-deadline arithmetic** — `turn_events.py:60-62` records buzz resetting a 900s idle
  deadline on `ToolCallStarted` against an 1800s dispatch allowance. Whether per-turn process spawn
  changes that is unexamined. **P3**, and it is a seat-integration question, not a runner one.

---

## 8. Phasing

| Phase | Contents | Entry condition |
|---|---|---|
| **P1** | `muse_runner.py`, `parse_envelope`, offline + e2e tests, G1–G5 | this spec approved |
| **P2** | `muse_approvals.py` on `task.lifecycle.side_effect_intent.policy_decision`, mirroring `codex_approvals.py` | **G1–G5 all closed**, P1 merged |
| **P3** | seat config, engine registration, `session-message` bus (gap 4), idle-deadline fit (gap 6) | P2 merged |

P2's known risk, recorded now: `side_effect_intent` carries `cancellation_handle`, observed **null**
in both of the findings note's observations. Whether it is ever populated is untested. P2 must not
design a cancellation path on a field never seen carrying a value.

---

## 9. What this design would have to be wrong about

Stated plainly, because a design that cannot say how it fails is not reviewable:

1. **If G1 fails**, interrupt becomes lossy and the seat may be unsuitable for dispatch patterns that
   cancel — which is most of them under a 1800s allowance.
2. **If gap 4's socket bus yields a persistent process**, the cold-process model is the wrong shape
   and P1's process management is wasted (though `parse_envelope` survives either way — which is the
   main reason for the §2.1 split).
3. **If Muse's envelope is unstable across builds**, tier-1 fixtures rot silently and only tier 2
   catches it. The launcher mtime moving within one day is weak evidence this is a live risk.
4. **If `--json` carries no token usage**, the $10 ceiling is enforced by turn-counting under an
   assumed rate, which is weaker than measurement and must be labelled as such wherever cited.

---

## 11. Live capture, 2026-08-06 — four corrections and two closed gates

`muse` was run for real (VPN **off** — see §11.5). Captures are committed at
`tests/fixtures/muse/echo-provider.jsonl` (23 events, free) and
`tests/fixtures/muse/real-turn-bash-high-effort.jsonl` (47 events, one bash tool call).
Both are replayed by `tests/test_muse_fixtures.py`.

### 11.1 The preamble is on **stderr**, not stdout

The probe artefact §7 says `muse exec` prints `muse: workspace root: …` to stdout before the JSONL.
Measured: **every** stdout line is JSON (`grep -cv '^{'` returns 0) and the preamble appears on
stderr. `parse_line`'s skip is therefore belt-and-braces rather than load-bearing — harmless, kept,
but §2.3's rationale was wrong about which stream it defends.

### 11.2 `task.lifecycle.*` payloads are **nested**; `run.*` and `tool.*` are flat — this was a live bug

Lifecycle fields (`task_kind`, `idempotency_key`, `reason`) live under `payload["event"]`.
`run.output.delta.text`, `run.terminal.*.terminal|text`, and `tool.result.call_id|correlation_facts`
are flat on `payload`.

The first implementation read lifecycle fields flat. Every one resolved to `None`, so `scheduled`
returned no id and **the mapper emitted no tool calls at all** — silently, because a missing key is
not an error. The offline suite was green throughout, because its hand-written envelopes were built
from the same wrong prose. Fixed by `muse_events.lifecycle_event()`.

### 11.3 Not every `scheduled` task is a tool — the second live bug

One real turn scheduled **three** tasks: one `tool.bash` and two `model.meta.response`, the latter
keyed `idempotency_key="model:<run>:<task>"`. Emitting `ToolCallStarted` per scheduled event invents
tool calls that never complete (no `tool.result`), leaving unmatched starts in buzz's activity panel
and resetting its idle deadline on model chatter. **The `tool:` prefix is the discriminator.**

### 11.4 Gates closed by this capture

| Gate | Verdict |
|---|---|
| **G6** — does `tool.result.call_id` join `scheduled.idempotency_key`? | **PASS.** `tool:call_019fd661adc677f190e67ffa4ad85302` → `call_019fd661adc677f190e67ffa4ad85302`, exact. Asserted on the raw bytes by `test_g6_prefix_rule_holds_on_the_raw_bytes`. |
| **G3** — reasoning stream at higher effort? | **NONE at `--reasoning-effort high`.** No reasoning-shaped `payload_type` in 47 events. `ReasoningDelta` stays unmapped **on evidence, not omission**; `test_no_reasoning_payload_at_high_effort` fails if a future build adds one. |

~~Still open: **G1** (interrupt resumability), **G2** (system-prompt seam), **G4** (concurrent
execs), **G5** (`-w` long form).~~

**Superseded 2026-08-07.** G1 closed by the §12 capture (PASS). **G5 closed free** — `-w`'s long form
is `--worktree`, and §1's denial of it is withdrawn there. Still open: **G2** (narrowed to one
behavioural question — the flag topology is now measured; §7) and **G4** (concurrent execs, now
*enforced* by the F2 per-session lock, so it confirms rather than protects).

### 11.5 The VPN question resolved itself

Muse was assumed US-only, which is why the whole split-tunnel thread existed. **Measured: a real
turn succeeds with ExpressVPN disconnected, exiting from a non-US location.** No VPN, no split
tunnel, and no Mac-mini ipchange-monitor patch is needed — that work is unnecessary rather than
deferred.

For the record, ExpressVPN split tunnelling *was* made to work first (allowlist mode + a rule for the
version-stamped `muse-bin-0.1.0-R708.1`, applied only after `expressvpnctl connect`, which is what
actually installs pending settings). It is now redundant, and the rule is a stale-path trap on the
next Muse upgrade — the launcher deletes old `muse-bin-*` and writes a new stamped name, so the rule
would silently stop matching. **Remove it.**

### 11.6 Payload types observed but not mapped

`runtime.command.accepted`, `session.run.linked`, `turn.input.user`, `run.lifecycle.started`,
`run.model.configured`, `task.stream.linked`, `task.lifecycle.accepted|started|completed|rejected|output`.
All fall through to `[]`, which is correct — but `task.lifecycle.completed` is worth noting: it is a
TASK completion, **not** a tool completion. Tool completion is `tool.result`, and only `tool.result`
carries the `outcome` that keeps a failing tool visible (`turn_events.py:73`).

---

## 12. Gate G1 — RUN AND PASSED, 2026-08-07; and the premise itself finally measured

Two things were measured, deliberately separated so a failure would be attributable.

### 12.1 The premise (panel finding F3)

Until this run the design's **central** claim — that two independent `muse exec` processes sharing
one `--session-id` carry context — was *inherited* from the probe artefact, re-run by nobody, and
covered by no fixture. Both committed captures were single-turn. The panel caught that the brief had
nevertheless graded it `demonstrated`.

**Measured:** turn 1 stored `PLUM-4271` and completed; turn 2, a separate process on the same
`--session-id`, returned it. Fixtures `resume-turn1-set-token.jsonl`, `resume-turn2-recall-token.jsonl`.

### 12.2 G1 — resumability across a kill

**PASS.** A turn was SIGTERMed mid-flight — `rc=143`, **no** `run.terminal.*` event, 16 events
against the usual 26 — and the next process on the same `--session-id` recalled `ZEPHYR-3390`, the
token set *before* the kill. Fixtures `killed-turn-sigterm.jsonl`, `killed-turn-then-recall.jsonl`.

So `interrupt()` retaining the session id (§4) is now licensed by measurement rather than being the
lesser-of-two-evils hypothesis it was. The rotate-on-interrupt fallback is **not** needed.

### 12.3 The first attempt was INVALID, and said so

The initial probe reported `killed=False`: the chosen task ("count to 40") finished inside the 6 s
kill delay, so the SIGTERM never fired. The harness classified that `invalid-not-killed` rather than
reading the subsequent successful recall as a G1 pass — which it would have been, wrongly, since a
turn that completed normally proves nothing about surviving a kill. Recorded because the near-miss
is the point: the recall *did* succeed, and a harness that only checked "was the token recalled"
would have closed this gate on evidence that did not bear on it.

### 12.4 Consequence worth naming

A killed turn now exits 143, which §11's F4 fix converts into `MuseTurnFailed`. That is correct for
a crash but debatable for a **deliberate** `interrupt()`, where the caller already knows. In
practice a cancelling consumer abandons the generator, so the raise never executes; a consumer that
drains after interrupting will see it. Left as-is and flagged rather than special-cased — no gate
covers it, and guessing is what §11 was written to stop.
