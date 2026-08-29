# ARB-B2 — serve the close-discipline block to Pi-orchestrated warm turns

**Status:** draft for panel review. Author is a cold seat with no prior context; nothing is
implemented. Slice is wiring, not protocol: the served bytes do not change.

**Filing:** `docs/BACKLOG.md:1249-1313` (CD-1) and backlog item ARB-B2. Served copy:
`prompts/arb-close-discipline.md` — 592 bytes, sha256
`a0b1019265490e761bdcc7057de50478456a53c77e5cfff30aff3ed768f2aa82` (measured this session at
`7e0785e9`; matches the value recorded at `docs/BACKLOG.md:1306`). Three live bullets at
`prompts/arb-close-discipline.md:3-10`; staged bullets deliberately unserved
(`prompts/arb-close-discipline.NOTES.md:14-43`). **This slice promotes no bullet.**

## 1. What is actually wired today

| Route | Mechanism | Serves the block? |
|---|---|---|
| Claude Code CLI seat | `claudeya()` in `~/.zshrc:14-25` → `claude --append-system-prompt-file ~/.claude/arb-close-discipline.md` (`:22-24`), guarded by a readability test that refuses (`:16-21`) | **yes** |
| Pi orchestrator TUI (the warm orchestrating seat) | pi launched with `--append-system-prompt "$ROLE_PROFILE"` on the worker hosts (`scripts/pi-project-b-a-console:12,35`, `-b-console:12,35`, `-1-console:8,22`); the interactive orchestrator's launch line is not in this repo | **no** |
| `/arb-orch` wizard | `pi.sendUserMessage(kickoff)` (PiExtensions `extensions/arb-orch-wizard.ts:1699-1700`, body built at `:1145-1150`) | **no — and cannot** |
| Warm `arb_agent` children | in-process Pi SDK `createAgentSession` (PiExtensions `extensions/arb-subagents/runner.ts:187-195`) with `noContextFiles: true` / `noPromptTemplates: true` (`:161-168`); `baseSessionOptions` (`:179,:188`) is never populated by `index.ts:140` | **no** |
| Cold bridge engine seats | `scripts/arb-orch-panel:483-573` → `scripts/dispatch-dev` → bridge; role profile only, `src/agent_redis_bridge/bridge.py:556-568` → `:3579-3582` → `<system_guidance>` wrap at `src/agent_redis_bridge/protocol.py:44-52` | **no** |

Two facts that constrain everything below:

- **The wizard is the wrong injection point.** Its only session-injection API is
  `pi.sendUserMessage` (`arb-orch-wizard.ts:1700`) — first-user-message tier, which
  `prompts/arb-close-discipline.NOTES.md:64-67` rejects by name as the tier that already failed
  against this pathology. The backlog's own framing ("the wizard … has to inject") does not
  survive contact with the code.
- **`BRIDGE_ROOT` in the wizard is hardcoded to `/Users/<user>/<workspace>`**
  (`arb-orch-wizard.ts:27`). That checkout is at `732ba79d`, **210 commits behind ARB `dev`**, and
  has **no `prompts/` directory at all** (`prompts/` landed at `afef94c8`). A repo-path injection
  resolved against the wizard's root dangles *today*. The dangling-path case is not hypothetical.

## 2. Entry points, and what this slice covers

1. **Pi orchestrator session (interactive, mac-mini/MBP)** — **COVERED.**
2. **`pi-project-b-{a,b,1}` warm console seats** — **v2: MOVED TO THE NEGATIVE CONTROL (r1 cold-Opus
   P1-1, orchestrator-verified).** O-3 is resolved against coverage: these consoles run
   `roles/team-seat.md`, whose line 3 defines the seat as "a team-member seat … taking direction
   from a coordination lead", and they BLPOP a work inbox (`pi-project-b-a-supervisor:41,88`) — worker
   tier by the same `docs/BACKLOG.md:1289-1292` rule that excludes engine seats. v1 would have
   injected close discipline into seats with no part in a close. They join A5 as negative
   controls; the slice covers the orchestrator session only.
3. **Warm `arb_agent` in-process children** — **NOT COVERED, deliberate.** Negative control.
4. **Cold bridge engine seats (panel/dispatch)** — **NOT COVERED, deliberate.** Worker tier per
   `docs/BACKLOG.md:1289-1292`: "a worker told to await a close-consumer reconcile it has no part
   in is being taught a claim it cannot act on." Negative control.
5. **Claude Code CLI seats** — already covered; untouched, but pinned by the byte-identity test.

## 3. Fork 1 — injection mechanism

**Recommend: a repo-side pi launcher that passes a SECOND `--append-system-prompt` carrying the
close-discipline file, guarded claudeya-style; plus an assert-only in-session detector.**

Grounding: `pi --help` documents `--append-system-prompt <text>` as "Append text **or file
contents** to the system prompt (**can be used multiple times**)". So close discipline rides its
own append, orthogonal to the role profile, with no concatenation and no coupling — and the
existing consoles already pass a *path* as that flag's value (`scripts/pi-project-b-a-console:35`),
which is why they work. New script `scripts/arb-pi-orch` (modelled on `pi-project-b-a-console`) resolves
`<checkout>/prompts/arb-close-discipline.md` from its own `$0` location — the repo knows its own
checkout, per `docs/BACKLOG.md:1271-1275` — and the three consoles gain the same second flag.

Detector (second, small part): a PiExtensions hook `pi.on("before_agent_start", …)` that **reads
`event.systemPrompt`, asserts the three live-bullet anchors are present, and emits a loud error +
`deliverAs: "steer"` notice when they are not**. It never injects. Precedent for the hook shape is
live: `extensions/pi-memory/index.ts:472-490` returns `{ systemPrompt: event.systemPrompt + … }`.

**Rejected — inject at `before_agent_start` instead of at launch.** Tempting (per-turn re-read, no
restart to promote a bullet, reaches sessions launched by hand) and it is append-not-replace per
`NOTES.md:70-72`. It fails on the decisive property: **the hook cannot refuse.** A handler that
throws is caught at pi-coding-agent `dist/core/extensions/runner.js:857-866`; the runner
`emitError`s and the turn proceeds with the unmodified system prompt. An injector that cannot
refuse is a silent downgrade by construction — the exact defect ARB-B2 exists to remove. Hence
inject where refusal is possible (before `exec`), detect where it is not.

**Rejected — reuse the `BRIDGE_ROLE_PROFILE_FILE` channel.** A false friend, on four counts:
(a) it reaches **engine dispatch only** — i.e. exactly the worker tier this slice must not touch;
(b) role answers *what job this seat has* and is per-seat, close discipline is role-orthogonal
(`docs/BACKLOG.md:1283-1287`); (c) it is bound once at bridge boot (`bridge.py:556-568`) and
`role_profile_for_turn` drops it entirely whenever `task_override is not None`
(`bridge.py:3726-3731`), so nudge (`:3116`) and commit-message (`:3416`) turns lose it; (d) it
carries **three independent silent-downgrade sites** — `load_role_profile` logs a warning and
returns `None` (`bridge.py:4950-4964`), `pi_rpc.py:163-171` is commented "*Silent fail*", and
`pi_sdk.py:141-148` swallows `OSError` to `None`. Adopting that channel would inherit all three.

**Rejected — `--append-system-prompt-file` pass-through.** There is no Claude CLI anywhere on the
orchestrated route to pass it to: the wizard sends a user message into a live pi session
(`arb-orch-wizard.ts:1700`) and warm children are in-process SDK sessions
(`arb-subagents/runner.ts:187-195`). The flag is real (`claude --help`, and `~/.zshrc:23` uses it)
but it is the CLI route's mechanism, not this one's.

## 4. Fork 2 — failure mode on a missing/unreadable file

**Recommend: refuse to launch. Exit `78` (`EX_CONFIG`), stderr line
`arb-pi-orch: close-discipline-unreadable path=<abs> — refusing to launch`, and do not `exec` pi.**

Justification. The "maximally bluffable configuration" caution (`NOTES.md:22-25`) is about serving
a bullet that asserts machinery which does not exist; it does not argue for launching without the
block. The risk here is the inverse and worse: `docs/BACKLOG.md:1262-1269` — an orchestrating seat
making acceptance and close decisions "while looking entirely normal". Launch-plus-loud-log is
precisely the shape of the three role-profile sites above, whose warnings land in a daemon log
nobody reads; `NOTES.md:90-93` records that the CLI route chose refusal for exactly this reason.

The refusal must assert a **specific** code, not a bare non-zero: a pi launcher can exit non-zero
for a dozen ambient reasons (missing `PI_BIN`, `PATH`, auth), so a test asserting "did not launch"
would stay green with the guard deleted. `78` plus the stderr token is the falsifiable form.

Cost accepted: an unattended restart on a host whose checkout lacks the file will not come back up.
That is the correct outcome — `/Users/<user>/<workspace>` is that host today (§1), and a seat that
comes back up without close discipline is the failure being paid for.

The detector's failure mode is fixed by the runtime, not chosen: it can only be loud
(`runner.js:857-866`). It emits an extension error and a steer message naming
`close-discipline-absent`; it never blocks.

**The guard is not optional hygiene — pi FAILS OPEN, verified in bytes (v2, r1 cold-Opus P1-2;
orchestrator re-read the same lines).** `resolvePromptInput`
(`/opt/homebrew/lib/node_modules/@earendil-works/pi-coding-agent/dist/core/resource-loader.js:15-29`)
does `if (existsSync(input)) { read } ... return input` — on a missing file it returns **the path
string itself**, with no error and no warning. The seat then launches with the literal text
`/Users/<user>/<workspace>/prompts/arb-close-discipline.md` occupying its close-discipline
system-prompt slot: a seat that looks configured, reports no fault, and carries no discipline.
This is not hypothetical — `/Users/<user>/<workspace>` is 210 commits behind dev with no `prompts/`
directory (§1), so **every console launched there today would already be serving the path
string** if the flag were added unguarded. Consequence for §3's roster: the flag must never be
added to a launcher without the exit-78 guard in the same commit, and A6 (below) asserts the
path-string outcome cannot reach a served prompt.

## 5. Fork 3 — provable byte-identity between the two routes

**Recommend: a repo test `tests/test_close_discipline_serving.py`, plus a composition-time hash in
the launcher's log line.** Four assertions, three of which can fail independently:

1. **Cross-route equality.** Resolve route A (`~/.claude/arb-close-discipline.md`, following the
   symlink) and route B (the launcher's resolver, imported, not re-implemented); assert
   `sha256(A) == sha256(B)`. Route A is a host file, so use the bounded external-path convention
   already in this repo — `tests/test_arb_orch_panel_subagents.py:272-302` (`grep -Fq` with a
   `timeout`, `pytest.mark.skipif` on unreachable, plus a test that the reachability probe itself
   is bounded). A skipped test is a green test, so the skip reason must name the missing host file.
2. **Content oracle.** Equality alone is vacuous — both routes read one file, so a truncated file
   passes. Assert the served bytes contain all three live anchors: `"proposal, not a close"`,
   `"no polish exemption"`, `"pinning test"` (`prompts/arb-close-discipline.md:3,5,8`).
3. **Staged-bullet absence.** Assert `"uploaded_by allowlist"` and `"randomised [E] claim"`
   (`NOTES.md:37-38`) are **absent** from the served file. This is the served ⊆ artefact guard
   (`NOTES.md:27-28`) as a check rather than a convention.
4. **Launcher self-report — INTENT ONLY, and labelled as such (v2 correction, r1 cold-Opus P1-3).**
   The launcher logs `close-discipline-intent sha256=<hex> bytes=<n>` before `exec`. This measures
   what the launcher *read*, not what pi *served*: pi re-reads the file at session load and on
   every `reload()`, so the two can diverge. Keep it for launch-record forensics; do not cite it
   as served-bytes evidence.
5. **Served-bytes oracle (new in v2 — this is the one that satisfies "verify produced bytes, not
   intent").** The `before_agent_start` detector receives the fully assembled system prompt
   (`types.d.ts:520-521` documents `systemPrompt` as "the fully assembled system prompt string"),
   so it emits `close-discipline-served sha256=<hex> bytes=<n> anchors=<3/3>` computed over
   `event.systemPrompt` — and emits `close-discipline-absent` when the anchors are missing OR when
   the assembled prompt contains the literal prompt-file path (the fail-open shape from §4). The
   detector cannot block (`runner.js:857-866`), so this is an observation channel, not a gate; the
   gate is exit 78 at launch. Host-scoped: the detector lives in PiExtensions, so no ARB test can
   prove it is installed — that acceptance leg is explicitly host-side (O-6).

Rejected: pinning the hash as a test constant. It would make every legitimate bullet promotion a
test edit, which trains the reflex of editing the oracle to match the artefact.

## 6. Acceptance criteria

Each criterion names an asserted value and the mutant that must kill it.

- **A1 — resolver.** `scripts/arb-pi-orch --print-prompt-path` exits `0` and prints an absolute
  path ending `/prompts/arb-close-discipline.md` that is readable and non-empty. Mutant: point the
  resolver at `arb-close-discipline.NOTES.md` → A4 and A5 fail.
- **A2 — dangling path refuses loudly.** With `prompts/` renamed away, the launcher exits **`78`**,
  stderr matches `close-discipline-unreadable path=`, and pi is **not** executed — proven by a stub
  `PI_BIN` that writes `$TMPDIR/pi-launched.marker`; assert the marker is **absent**. Mutant:
  delete the guard → the marker appears and the exit code is pi's, not `78`.
- **A3 — positive, live.** A pi orchestrator started through the launcher answers *"what does your
  close discipline say about polished claims?"* with the no-polish-exemption content, and a
  sentinel token placed in a scratch copy round-trips. This is the CLI route's own evidence bar
  (`docs/BACKLOG.md:1299-1303`); the sentinel run uses a scratch copy so the served file is never
  mutated in place.
- **A4 — byte-identity, SPLIT IN v3 (r2v F1: the v2 form was skip-green).** The v2 criterion let a
  MISSING route A (the Claude symlink) skip the only equality assertion while the anchor and
  staged-absence assertions still passed against route B — the suite went green with one of the two
  claimed routes absent. v3 splits it:
  - **A4-hermetic** (runs everywhere, never skips): assertions 2–3 (content anchors, staged-bullet
    absence) against route B, plus equality against a route-A *fixture* checked into the repo.
    Mutant: append one byte to the route-B copy → fails.
  - **A4-host** (deployed-host closure, NON-SKIPPING): resolve the real
    `~/.claude/arb-close-discipline.md`; **a missing or unreadable route A is a FAILURE, not a
    skip**, with the message naming the absent path. Assert `sha256(A) == sha256(B)` and
    `bytes(A) == bytes(B)`. Mutant: `mv` the symlink aside → A4-host fails (verify this by doing it
    and restoring). This is the criterion that closes the slice on a host; A4-hermetic alone never
    closes it.
- **A5 — negative control (must NOT receive the block). (ii) and (iii) REWRITTEN in v2 — sol, grok
  and cold-Opus independently found (ii) vacuous: it asserted on the child TASK-TEMPLATE surface,
  while injection would arrive via the SYSTEM-PROMPT composition path, so its named mutant
  (`roles/team-seat.md`) sits on no child code path and could never kill the test.**
  (i) A cold panel seat dispatched via `scripts/arb-orch-panel`: assert the `<system_guidance>`
  block produced by `protocol.build_task_prompt` (`src/agent_redis_bridge/protocol.py:44-52`)
  contains **0** occurrences of the sentinel and of `"proposal, not a close"`. Mutant: add the
  block to the role-profile path → fails.
  (ii) A warm `arb_agent` child — **SEAM CORRECTED IN v3 (r2v F2; orchestrator re-verified against
  the installed SDK).** v2 named a top-level `appendSystemPrompt` / `baseSessionOptions` key that
  **does not exist**: `CreateAgentSessionOptions` (`dist/core/sdk.d.ts:10-53`) has no such field —
  it accepts `resourceLoader` (`:47`). The append source lives on the LOADER
  (`DefaultResourceLoader.appendSystemPromptSource`, `resource-loader.js:91,:137`, resolved at
  `:332-341`), and the session builds the prompt from `loader.getAppendSystemPrompt()`
  (`agent-session.js:724-739`). PiExtensions constructs the child loader with no append source
  (`arb-subagents/runner.ts:158-170`) and never populates `baseSessionOptions`
  (`arb-subagents/index.ts:136-140`).
  So: construct (or fake) the child `DefaultResourceLoader` exactly as `runner.ts` does, resolve it,
  and assert its resolved append sources — and the resulting child system prompt — contain **zero**
  close-discipline bytes. Mutant: pass `appendSystemPrompt: <prompt path>` into that child loader →
  the test must fail. A top-level-key assertion is explicitly NOT acceptable: Pi ignores unknown top
  level properties, so such a test can kill its own mutant while a real regression injected through
  the loader stays green.
  (iii) The three `pi-project-b-{a,b,1}` consoles (moved here in v2, §2): assert their launch lines carry
  **no** close-discipline flag. Mutant: add the flag to one console → fails.
- **A6 — detector, including the fail-open shape (extended in v2).** (a) Launch pi *without* the
  launcher; the detector emits `close-discipline-absent` exactly once for that turn. Launch *with*
  it; the detector emits `close-discipline-served sha256=… anchors=3/3` and no absent event.
  Mutant: empty the detector's anchor list → the without-launcher case goes quiet and the test
  fails. (b) **Path-string case (new, r1 cold-Opus P1-2):** with the flag pointing at a
  NON-EXISTENT path, pi's `resolvePromptInput` returns the path string, so the assembled
  `event.systemPrompt` contains the literal path and none of the three anchors — the detector must
  emit `close-discipline-absent`, and the served-sha line must NOT be emitted. Mutant: have the
  detector match on "the flag was present" rather than on anchors → (b) goes quiet, which is
  exactly the silent downgrade.
  (c) **Digest oracle (new in v3, r2v F3).** v2 asserted only that a sha-SHAPED line appeared with
  `anchors=3/3` — a detector emitting a constant 64-hex digest and a wrong byte count satisfied
  both stated cases. So: capture the detector's exact `event.systemPrompt`, compute its sha256 and
  byte length INDEPENDENTLY in the test, and assert both equal the emitted `sha256=` and `bytes=`
  fields. Mutants: (i) emit a constant digest → fails; (ii) emit `bytes` off by one → fails. Note
  this digest covers the FULL assembled system prompt and therefore must NOT be compared against
  A7's 592-byte artefact hash — they are different objects, and conflating them is how a
  wrong-but-plausible oracle gets written. Host-scoped acceptance (O-6): this runs where PiExtensions is
  installed and is reported as host evidence, never claimed from the ARB checkout.
- **A7 — no bullet promoted.** `git diff prompts/arb-close-discipline.md` is empty across the whole
  slice; the served sha256 after the slice equals
  `a0b1019265490e761bdcc7057de50478456a53c77e5cfff30aff3ed768f2aa82`.

Harness check before trusting any of the above: assert the new test module reports a non-zero
collected count. `no tests ran in 0.00s` is a broken harness, not a baseline. No `docs/index.json`
entry is required for this note — `docs/superpowers/specs` is collection-exempt
(`scripts/doc_index_lib.py:21-26`).

## 7. Deployment

Fleet restart / rollout to the mac-mini and the worker hosts is owner-gated and out of scope; this
slice lands the launcher, the detector, and the tests only.

## 8. OPEN

- **O-1.** Which checkout the mac-mini Pi orchestrator runs from. The wizard's hardcoded
  `/Users/<user>/<workspace>` (`arb-orch-wizard.ts:27`) has no `prompts/` and is 210 commits behind
  `dev` — either that clone is refreshed, or the launcher must resolve a different root. Blocks A3.
- **O-2.** The interactive Pi orchestrator's actual launch command line is not in this repo, so
  "add a second flag" is a recommendation the repo cannot verify. Owner must either adopt
  `scripts/arb-pi-orch` as the launch path or state the real one.
- **O-3.** Are `pi-project-b-{a,b,1}` orchestrator-tier or worker-tier? They run `roles/team-seat.md`
  (`pi-project-b-a-console:12`). If worker-tier they belong in the A5 negative control instead.
- **O-4.** Whether pi joins repeated `--append-system-prompt` values in flag order, deduplicates,
  or last-wins. The help text says repeatable; ordering is unverified and A4's cross-route equality
  does not test it. Needs a one-command probe before implementation.
- **O-5.** Excluding warm `arb_agent` children (§2.3) is this author's reading of
  `docs/BACKLOG.md:1289-1292`. A protected `review` child (`arb-subagents/protected.ts:57-62`) is
  arguably a certifying seat rather than a worker. Owner call; if included, the hook is
  `baseSessionOptions` at `arb-subagents/runner.ts:179,188` (unused today) and the key is likely
  `appendSystemPrompt` by analogy with `src/agent_redis_bridge/engines/pi_sdk.py:283-284` —
  **unverified**.
- **O-6.** The detector lives in PiExtensions, outside this repo, so no ARB test can prove it is
  installed. Global load is via `~/.pi/agent/settings.json` `packages` → the PiExtensions checkout.
  A6 therefore verifies behaviour on this host only, and that scope limit must ride with the claim.

## 9. v2 changelog (fold of panel r1 — closed
needs-changes; votes: cold-Opus block/P1, sol needs-changes/P1, grok needs-changes/P1,
agy approve/none)

- **§2 roster corrected:** the three `pi-project-b-{a,b,1}` consoles are WORKER tier
  (`roles/team-seat.md:3` + inbox BLPOP at `pi-project-b-a-supervisor:41,88`) and move from covered to
  negative control A5(iii). O-3 resolved (cold-Opus P1-1; orchestrator re-read `team-seat.md:1-5`).
- **§4: pi fails OPEN, verified in bytes** — `resource-loader.js:15-29` `resolvePromptInput`
  returns the *path string* when the file is absent, so an unguarded flag serves the path as
  prompt text. Stated with its live instance (the 210-commit-stale <workspace> checkout) and pinned
  by new criterion A6(b) (cold-Opus P1-2).
- **§5: launch-log sha relabelled `close-discipline-intent`** and a served-bytes oracle added as
  assertion 5 — the detector hashes `event.systemPrompt` (documented as the fully assembled prompt
  at `types.d.ts:520-521`), which is the only channel that observes produced bytes rather than
  launcher intent (cold-Opus P1-3, sol's third gap).
- **A5(ii) rewritten** — v1 asserted on the child task-template surface with a mutant on no child
  code path: vacuous, found independently by sol, grok and cold-Opus. Now targets the
  `appendSystemPrompt` composition surface, with the key name explicitly UNVERIFIED and a standing
  instruction to report the reach limit rather than substitute a weaker surface.
- **A6 extended** with the path-string case and its mutant; host-scoped acceptance restated.
- O-4 resolved from bytes (repeated `--append-system-prompt`: order preserved, `"\n\n"` join, no
  dedup, no truncation — cold-Opus). O-3 resolved above. O-1/O-2/O-6 remain host-acceptance legs;
  O-5 (protected warm `review` children) remains an owner call, default exclusion.


## 10. v3 changelog (fold of r2v — sol block/P1)

- **A4 split (F1)** — v2 remained skip-green: a missing Claude symlink skipped the only equality
  assertion while the rest passed against route B. v3 separates A4-hermetic (always runs, uses a
  repo fixture) from A4-host (NON-SKIPPING; a missing/unreadable route A is a failure), and only
  A4-host closes the slice on a host.
- **A5(ii) seam corrected (F2)** — v2 named a top-level `appendSystemPrompt`/`baseSessionOptions`
  key that does not exist in the installed SDK (orchestrator confirmed: `sdk.d.ts:47` has
  `resourceLoader`, no append field). The real seam is `DefaultResourceLoader`'s
  `appendSystemPromptSource`, resolved into the prompt at `agent-session.js:724-739`. The mutant
  must inject through the child loader; a top-level-key assertion is explicitly disallowed because
  Pi ignores unknown properties, making such a test self-satisfying.
- **A6(c) added (F3)** — the detector's `sha256=`/`bytes=` now need an independent oracle: the test
  recomputes both from the captured `event.systemPrompt` and compares, with constant-digest and
  off-by-one-length mutants. Explicitly warns against comparing this digest to A7's artefact hash.
- Unchanged and still binding: §4's pi fail-open guard (exit 78), the worker-tier negative controls
  (project-b consoles, engine seats, warm children), and A7's no-bullet-promoted pin.
