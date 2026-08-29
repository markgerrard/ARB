# Session handoff — 2026-08-03 (omp-acp + opencode-acp seats)

Snapshot for resuming after a context clear. All work below is **committed and pushed** to
`origin/dev`; working tree clean at `fc4a6a75`.

## What shipped (commits, newest first)

- `fc4a6a75` fix(engines): close the tool-allowlist fail-open and the ACP dead-child hang
- `d9e43f56` feat(engines): omp-acp + opencode-acp seats, readonly gate extended to omp

Suite went from `1 failed / 3296 passed` at session start to **3344 passed / 0 failed**
(`pytest tests/ --ignore=tests/arb_warm_orch`, 11:25).

## 1. Two new ACP seat engines

`omp-acp` drives `omp acp` (oh-my-pi, a **fork of pi**); `opencode-acp` drives `opencode acp`.
Both subclass `GeminiAcpEngine`, both `experimental` in `support_tiers.py`.

**Why ACP and not the alternatives** — this was the arc's main design fork, and it was decided on
evidence, not preference. omp also speaks pi's `--mode rpc` NDJSON, and the existing `PiRpcEngine`
drives it end-to-end with a single flag removed (`--no-themes`, which omp dropped). That path was
rejected because the wire is pi's *private* protocol which omp is already extending (its `ready`
frame advertises `supportedProtocolVersions [1,2]`) while republishing daily — 17.2.4 across 571
versions against pi's 0.83.0. The pi-sdk host was rejected too: omp's SDK has moved on
(`ModelRuntime` gone, tool construction now `createTools`/`BashTool`), though it does ship a
`legacy-pi-coding-agent-shim` aliasing `@earendil-works/pi-coding-agent`.

**ACP costs nothing that matters** because omp's advantage over pi is *agent-side*, not
transport-side: **29 built-in tools against pi's 7**, plus subagents, LSP/DAP and persistent eval.
Verified through the ACP session, not assumed — `eval` ran real Python (returned SHA-256 matched an
independently computed digest) and `task` spawned a real subagent.

### Per-engine facts worth not rediscovering

| | omp-acp | opencode-acp |
|---|---|---|
| `session/set_model` | **rejected** ("Unknown ACP ext method") — model pinned via `--model` at spawn; `start_session` suppresses the base's call | supported, takes `provider/model` |
| session modes | `default` / `plan` | `build` / `plan` |
| tool allowlist | `--tools` (honoured on the `acp` path) | **none** — read-only is `plan` mode only |
| role profile | `--append-system-prompt` (honoured) ⇒ `consumes_role_profile = True` | none ⇒ bridge prepends to the task text |
| auth store | `~/.omp/agent/` (sqlite), separate from pi's `~/.pi/agent/auth.json` | `~/.local/share/opencode` |

**omp's tool vocabulary is NOT pi's.** The canonical reviewer allowlist `read,grep,find,ls` is
invalid — omp has no `find` and no `ls`; use `glob` (and `read` handles directories). Getting this
wrong used to look like a 60s wedge; see §3.

**Install:** `brew install can1357/tap/omp` (standalone). The README-recommended
`bun install -g @oh-my-pi/pi-coding-agent` route needs **bun ≥ 1.3.14** and dies with a
`SyntaxError` mid-parse on older bun. `opencode` is 100% TypeScript — there is **no `go install`**
path despite the name.

## 2. Read-only gate

`omp-acp` joined `_ALLOWLIST_ENGINES` in `readonly_gate.py`; its `--pi-tools` surface is enforced on
the ACP path (proven with a control/limited pair: control wrote a file, `--tools read,grep` could
not and reported only read/grep available). Its refusal now names omp's own 29-tool fallback rather
than pi's four.

`opencode-acp` is **deliberately excluded**: its read-only posture is the ACP `plan` mode, which the
gate cannot see, so it refuses rather than appear to certify a posture it cannot check. Mode-based
posture belongs to the `seat_posture_v` migration that module's docstring names — that is the open
design question if opencode ever needs to be a certifying reviewer.

## 3. Three defects e2e caught that unit tests would not have

1. **Permission asks were all cancelled.** The `GeminiAcpEngine` base cancels every
   `session/request_permission`, so both agents' first real dispatch returned
   `stopReason=cancelled` with no tool results. New `TurnPolicyPermissionMixin` in `engines/_acp.py`
   grants only inside a `trusted` turn via the panel-reviewed `_select_allow_option`, and denies
   outside a turn, on a stale session, or when no allow option is offered (grok-acp's GROK-1
   D2/D3b floor applied to this base).
2. **Bad spawn flags looked like a hang.** omp exits rc=2 instantly on a bad `--tools`, but the base
   had no liveness check, so it reported `initialize timed out after 60s`. Fixed twice over: an
   omp-local pre-flight (`omp --tools … --version`, uses omp's own validator so custom/MCP tool
   names are never false-refused) and the general base fix in §4.
3. **Tool-allowlist fail-open** — see §4.

## 4. The two backlog items (both now SHIPPED, entries updated in `docs/BACKLOG.md`)

**Allowlist fail-open.** The guard was `pi_tools and len(pi_tools.strip()) > 0 and not parsed`:
for `"   "` the value is truthy but `.strip()` is falsy, so it was skipped and the engine spawned
with the FULL toolset — failing open on exactly the typo it existed to catch. Shared
`parse_tool_allowlist` in `engines/base.py` now backs pi-sdk, pi-rpc and omp-acp
(`csv not in (None, "")`). **pi-rpc was worse than filed:** no guard at all, and `"   "` satisfied
`if not self.pi_tools`, so a full-tools seat read as tool-restricted to its own policy check.

**ACP dead-child hang.** `_dead_child_error` / `_await_or_detect_death` in `gemini_acp.py`, used by
both the handshake and turn loops. Liveness is consulted **only** when the queue comes up empty,
then a 0.5s grace drain — so "answered then exited" stays healthy. The turn loop was included beyond
the filed scope: the same defect there burned the whole turn timeout (up to an hour) versus ~5.5s.
The filing's claim that six adapters were exposed was **wrong** and is corrected in place —
grok/cursor/devin/cline carry their own ACP implementations and already checked `poll()`; the gap
was the `GeminiAcpEngine` subclass family only.

## 5. Host-portability fix (unrelated, required to get the suite green)

`tests/test_close_discipline_serving.py` hardcoded `/opt/homebrew/lib/node_modules/...` while pi is
installed under `~/.npm-global/...`, so A5(ii) hard-failed on a host where the SDK was present all
along. Now resolves via `npm root -g` plus a prefix sweep, override `ARB_PI_SDK_ENTRY`. PiExtensions
paths derive from `PIEXT_ROOT` (override `ARB_PIEXT_ROOT`), default unchanged.

**Do not "fix" these guards into `pytest.skip`.** They `pytest.fail` on purpose; turning an
unreachable dependency into a skip is the skip-green shape ARB-B9 was filed against.

## 6. Running a live e2e dispatch (the four gates that bit, in order)

Free-form task strings are gone (Slice 1d-iv). The working sequence:

1. Seat must register with a nonblank **`BRIDGE_WORKER_VANTAGE`** or publish fails
   `blank/missing worker_vantage`.
2. `arb-memory-harness-publish --target-agent-id <seat> --brief <md>` → receipt JSON. The brief needs
   a `# ` title, a `## Assumptions` JSON block (`{"items": []}` is a valid no-precondition claim),
   and body text.
3. The receipt is bound to the seat's **registration_generation** — restart the seat and you must
   republish, or you get `receipt registration_generation mismatch`.
4. `agent-dispatch` needs the **venv on PATH** or `dispatch_authority` dies with
   `ModuleNotFoundError: No module named 'redis'`.

**Seats launched with plain `nohup &` get frozen/reaped** — they register, then die with
`consumer-ownership-lost … current=missing`. This is NOT engine-specific: a control seat on the
untouched `pi-rpc` engine died identically. Use fork+`setsid`.

## 7. Verified live (not just unit-tested)

Both seats registered on the live bus and served real dispatches through
`dispatch_authority → Redis → ACP → agent → tools → reply`. The read-only omp seat passed a
**filesystem-checked write deny-proof**: told to create a file, it reported only `read`/`grep`/`glob`
available and no file was created. All four read-only-gate branches demonstrated live (unset
surface, valid subset, write-bearing surface, non-allowlist engine).

Seats at handoff time were ad-hoc (scratchpad `setsid` script, **not launchd**) — they do not survive
a reboot. Standing them up as proper launchd seats is unstarted work.

## 8. Open / next

- **`omp-acp` tier is `experimental`.** Promotion is a tier decision (owner-set). It has a live
  filesystem-checked deny-proof but no deny-proof *artifact*.
- **Launchd seat definitions** for `omp-bridge-dev` / `opencode-bridge-dev` — not started. Catalogue
  rows exist in `docs/runbooks/bridge-seat-catalogue.md`.
- **opencode has only the `opencode` provider** configured on this host (7 free models; smoke used
  `opencode/big-pickle`). `opencode auth login` for Anthropic/others. omp is on OpenRouter (412
  models) from its own store.
- **`omp acp` emits `session_update_unknown` (`sessionUpdate: "tool_call"`)** alongside the mapped
  `command_started`/`command_finished`. Nothing is lost, but the base's normalizer does not map
  omp's variant — worth decoding before certifying.
- **`steer` is unsupported** on the ACP base (`pi_rpc.py` has it). Real capability gap for omp-acp
  versus a pi-rpc seat.

## 9. Arc-closure constitution sweep (CLAUDE.md rail 2)

**Track (a) — decisions with NO authority trail (true crossings):**

- **Two feature/fix commits landed on `dev` without the tri-model review workflow.** `CHANGELOG.md`'s
  own header states "each feature ships through the tri-model review workflow (codex + agy-print +
  cold-Opus, per Workflow B) before merge". This arc ran **no review panel** — design forks were
  surfaced to the operator conversationally and each push was operator-directed, but the standing
  Workflow B rail was not executed. Flagged, not excused: if these engines are to be promoted past
  `experimental`, a panel over `d9e43f56..fc4a6a75` is the missing step.
- **Scope extension beyond a filed backlog item.** The ACP liveness fix was filed for `initialize`;
  it was also applied to the turn loop. Same defect class, larger blast radius than filed, decided
  by the orchestrator on proportionality grounds.

**Track (b) — decisions riding STANDING RAILS (listed to prevent drift-by-accumulation):**

- **`readonly_gate` admission of `omp-acp`** — operator-instructed explicitly ("extend the readonly
  gate for omp"), so it has a trail; recorded here because *which engines may be certified read-only*
  is a posture question, not a routine edit.
- **`opencode-acp` exclusion from the gate** — orchestrator's call by omission from that instruction,
  reasoned from the gate's own docstring (mode-based posture belongs to `seat_posture_v`).
- **Support-tier classification** of both engines as `experimental` — the conservative default;
  promotion explicitly left to the owner.
- **No writes to `CLAUDE.md` / `AGENTS.md` / `pipeline-operating-manual` / `skills/`** this arc
  (swept; clean). **No merges to `dev`** — both commits arrived by rebase.
- **No `memory_store` calls** this arc.
