# Design — kimi runtime-surface probe

Status: **DESIGN, approved to build** (Mark, 2026-07-17) · Author: warm Opus · Arc: kimi seat → sandbox pivot

## 1. Why this exists

The arc pivoted (Mark, 2026-07-17) from "stand up a read-only plan-mode kimi seat" to **"sandbox kimi
like codex, then give it real tools."** codex's reviewer seats are not trusted — they are *sandboxed*
(`bridge.py:3019`, `--sandbox` defaults to `workspace-write`). kimi has **no sandbox flag**, and the
behaviour matrix (`27f7465`) proved `auto`/`yolo` write **out-of-cwd with zero asks** ⇒ a worktree is
not containment. So the boundary must come from the OS.

**This probe does not author the profile.** It produces the *observed surface* the profile will be
authored against. That ordering is the arc's most expensive lesson (`1ef7016`,
`[[characterize-before-designing-external-behaviour]]`): when the object is an external system's
behaviour, the facts are obtainable only by execution, and no amount of review produces them.

### The finding that makes this non-optional

Under a naive deny-default profile authored from reasoning, **`git` died** on something no one would
predict from first principles — not the repo, not the network, but **Xcode's toolchain cache**:

```
git: warning: confstr() failed with code 5: couldn't get path of DARWIN_USER_TEMP_DIR; using /tmp
git: error: couldn't create cache file '/tmp/xcrun_db-peLo9w8q' (errno=Operation not permitted)
```

Author from reasoning and `git diff` breaks in a way that reads as *the seat* being broken, and the
tempting fix is loosening the profile until it works. That is how deny-default rots into allow-most.

### Corroborating evidence: the one existing profile in the org does not work

ARB's `bench/implbench/harness/sandbox.py` is a **READ-ONLY reference** (a parallel codex session owns
it; do not edit — see §8). Its live Seatbelt path is a placeholder (`test_sandbox_live.py` is literally
`pytest.fail("live_bakeoff is reserved for Task 14")`), i.e. **authored but never live-proven**.
Rendering its allow-set faithfully and executing it (2026-07-17, macOS 26.5.2):

```
$ /usr/bin/sandbox-exec -f arblike.sb /bin/echo hi
sandbox-exec: execvp() of '/bin/echo' failed: Operation not permitted    (rc=71)
$ /usr/bin/sandbox-exec -p '(version 1)(allow default)' /bin/echo hi
hi                                                                       (rc=0)
```

It allows `file-read*` on worktree/home/runtime and nothing else — no `/usr/lib`, no dyld, no
`/System` — so it **cannot exec a binary at all**. The `(allow default)` control passing proves the
harness is valid, not vacuous.

**Conclusion: no empirically-grounded macOS allow-set exists in this org.** Mine ARB's file for
*patterns* (deny-default rendering, pinned template digests, `verify_launch_spec` recomputing
invariants before crossing the boundary, endpoint allowlisting, env scrubbing, the `is_symlink()`
guard — all genuinely good). Do **not** mine it for the allow-set: that is a hypothesis, and this
probe is what would have falsified it.

## 2. Scope

**In scope:** produce an evidence table of kimi's observed file / exec / network surface under a real
review turn, with recorded bounds and omissions.

**Out of scope, explicitly:** authoring the Seatbelt profile; freezing `SandboxSpec`; the deny-proof
acceptance suite; flipping the seat to `auto`. Those are queue steps 2–6 in
`.claude/handoffs/2026-07-17-1052.md` and each has its own gate. **If the surface turns out to need so
much filesystem that the boundary stops being meaningful, that is a finding, and it sends the seat
back to `plan`.** The probe is allowed to kill the design; that is the point of running it first.

## 3. Architecture — two instruments, blind in different places

The core decision (Mark, 2026-07-17): **both, cross-checked.** Neither alone is trustworthy, and the
two of them are blind on *different axes* — which is the property that would have caught both of this
arc's scars.

| | **A — trace** | **B — climb** |
|---|---|---|
| Mechanism | `fs_usage` under sudo, filtered to the kimi process tree | iterative deny-default `sandbox-exec` |
| Subject | **one** real kimi turn: R2 brief, `auto`, inside the bootstrap profile (§5) | the **command corpus** (§3.1) harvested from `review_asks.jsonl` + A's exec capture |
| Cost | ~1 turn (~900s) | <1s per iteration |
| Yields | observed **superset** — everything touched | required **minimal set** — what actually fails |
| Blind to | need vs. incidental noise ⇒ **over-allows** by construction | anything failing **silently** ⇒ the `xcrun` class |
| n | **1 on stimulus** (§7) | 7 binaries / ~10 (binary, subcommand) pairs, spanning R1/R2/R3 cells |

### 3.1 The corpus, and why it is not the command list

`review_asks.jsonl` holds **38 `Bash` asks / 29 distinct command strings**. Both numbers are traps.

**The ask payload truncates at ~50 chars — 19 of 38 are cut off**, and the truncated ones are exactly
the *compound* chains. The full text is **unrecoverable**: kimi's ACP server truncates before emitting,
and the payload carries no `rawInput` field. Verbatim:

```
"text": "Requesting approval to Running: git log --oneline --follow -- src/agent_redis_brid…"
```

This does not sink instrument B, for a reason that must be stated rather than assumed: **a profile
does not constrain command lines — it constrains which binaries exec and which paths they touch.** A
chain `git log && git status` runs each part as **its own process**, so its surface is exactly the
**union of its parts'**. The `&&`-chaining the truncation destroys therefore contributes **no surface**
beyond the individual invocations. So the corpus is the distinct **(binary, subcommand)** pairs, which
survive in every ask's **untruncated prefix**.

Decomposed (2026-07-17):

| binary | subcommands seen | n |
|---|---|---|
| `git` | `log` (29), `show` (3), `status` (2), `branch` (1) | 35 |
| `echo`, `head`, `pwd`, `date`, `ls` | — | 8 |
| `python3` | via `PYTHONPATH=src python3 - <<'EOF'` (heredoc — kimi authors and executes Python) | 1 |

**Read what is ABSENT: no `rg`, and not one `git diff`.** kimi reached for `git log`/`git show`
instead. Anyone authoring this profile from first principles would have allowed `rg` and `git diff` and
possibly not `python3` — which is §1's argument in miniature.

**The truncation gap is covered by A, not by reconstruction.** Hand-completing the 19 truncated
commands would be authoring from reasoning — the exact failure this probe exists to prevent. Instead
**instrument A's `fs_usage -f exec` capture supplies the execs actually performed**, and B's corpus is
`untruncated prefixes ∪ A's observed execs`.

**This couples the instruments — declare it.** A and B are no longer independent on *stimulus*; A
feeds B its command list. They remain independent on **derivation mechanism** (trace vs. deny-climb),
which is where the cross-check's value lives. The delta in §3 is still meaningful; the shared-stimulus
coupling is a **recorded limitation**, not a silent one.

**The delta between A and B is the deliverable**, not either table alone:

- **In A, not in B** — kimi touches it but does not need it. Candidate to deny. This is where
  fs_usage-only would have over-allowed.
- **In B, not in A** — one of the instruments is lying. Per this arc's scars, read this row first.
- **In both** — the load-bearing set, and the only rows that should reach `SandboxSpec` without
  further argument.

**Why the axes matter.** The trace is n=1 on *stimulus* (Mark's scope call: R2 only). The climb runs
the whole command corpus and never invokes kimi at all — so it is **broad on exactly the axis the
trace is narrow on**. Neither instrument's weakness is covered by making the other bigger; they are
covered by each other.

## 4. Data flow

```
review_asks.jsonl ──harvest──▶ untruncated prefixes ──decompose──▶ (binary, subcommand) pairs
  (38 Bash asks,                                                    git log/show/status/branch,
   19 TRUNCATED)                                                    ls echo head pwd date python3
                                                                            │
bootstrap profile (§5)                                                      │
        │                                                                   ▼
        ▼                                                     [B] climb: deny-default + iterate
[A] kimi turn (R2, auto) under fs_usage                          ▲                  │
        │                                                        └──────────────────┘
        ├──▶ observed superset (file touches)                   add allow on `Operation not permitted`
        │                                                                   │
        └──▶ observed execs ─────────────────────────────────────▶ (covers the truncation gap)
                                                                            │
                    observed superset ◀───── delta ─────▶ minimal set ◀─────┘
                                              │
                                              ▼
                          kimi surface table (deliverable)
```

## 5. Bootstrap profile (approved, Mark 2026-07-17)

The trace runs kimi in `auto` — which the matrix proved writes out-of-cwd with zero asks — *before* we
have a profile. Chicken-and-egg, resolved by a deliberately coarse but real boundary:

- **read:** broad (we are measuring reads, not constraining them yet)
- **write:** worktree + a scoped TMPDIR **only**
- **network:** the API endpoint **only**
- **paths:** `/private/tmp`, never `/tmp` — see §6

This is **containment against the mistake threat model** (`[[arb-threat-model-recalibration]]`: the
threat is mistakes, not a malicious orchestrator). It is explicitly **not minimal** — minimality is
what the probe is measuring. The bootstrap profile is an instrument, and it does not enter
`SandboxSpec`.

## 6. Known mechanism traps (already paid for — do not re-discover)

| Trap | Fact |
|---|---|
| `(trace "/path")` | **Silently no-ops on macOS 26.5.2.** Profile parses, runs, emits **no file**. Dead end; this is *why* instrument A is fs_usage. |
| `(subpath "/tmp/…")` | `/tmp` is a **symlink** to `/private/tmp`; Seatbelt matches the **resolved** path ⇒ matches nothing ⇒ denies everything **including intended allows**. Fails closed (safe) but reads as "sandbox broken" and tempts loosening. Always `/private/tmp`. |
| `Popen(user=…)` (ARB's `_os_launcher`) | **setuid requires root.** Our seats run as `mark`. Not reusable. |
| `fs_usage` | **Requires root.** Passwordless sudo verified available 2026-07-17 — no need for Mark's hands. Read-only observation; mutates nothing. |
| SIP | **Enabled.** Would block tracing platform binaries, but `kimi` lives in `~/.kimi-code/bin/kimi` and is not SIP-protected. |

## 7. Controls — a green run without these proves nothing

1. **Adversarial self-test (instrument B).** Every climb probe also runs **UNSANDBOXED and MUST
   SUCCEED**. A probe that fails both sandboxed and unsandboxed measures the probe, not the boundary
   (`[[vacuously-green-guard-fail-loud]]`, `[[deny-proofs-need-adversarial-verification]]`). **This is
   the control that catches the ARB-profile failure mode** (§1) — a profile so broken it cannot exec.

2. **Minimality proof (instrument B).** For each allow in the derived set: **drop it, require the
   corpus to break.** Cheap on <1s proxies. Converts "minimal" from an assertion into a claim with
   evidence. **An allow that can be dropped with nothing breaking was never load-bearing and does not
   enter `SandboxSpec`.**

3. **Version stamping (both).** Every row carries `sw_vers` (**26.5.2 / 25F84**) and `kimi --version`
   (**0.26.0**). Seatbelt semantics have shifted across releases, and per Mark's standing caution the
   deny-proof is a **standing pre-panel check, not a one-time gate** — re-run after OS updates. **A
   profile that silently stopped denying is worse than no profile** (the fixture-masks-reality shape).
   Note honestly: kimi is a **hosted** model, so `0.26.0` is the *binary* version and **not the
   behaviour version** — the stamp records what is knowable, not what we want.

## 8. Recorded bounds and omissions (stated up front, not discovered later)

- **The trace is n=1 on stimulus.** Mark's scope call: R2 only. The profile will be fitted to one
  review's path through the tree. Mitigated (not eliminated) by instrument B's corpus breadth (§3.1),
  which spans R1/R2/R3 cells and never invokes kimi at all.
  **Accepted and recorded, not unnoticed.**
- **The corpus is a LOWER BOUND, twice over.** (a) Every ask was *denied* (`plan` + `cancelled`), so
  kimi never received command output and never walked further down the tree — the corpus is the
  **prefix** of the real surface, and under `auto` kimi goes further. (b) **19 of 38 ask payloads are
  textually truncated at ~50 chars and unrecoverable** (§3.1). **Instrument A exists to catch exactly
  this**, and it is why A supplies B's exec list rather than B reconstructing it.
- **A and B share a stimulus source** (§3.1): A's exec capture feeds B's corpus. They remain
  independent on *derivation mechanism*, which is where the cross-check's value lives — but the
  coupling is real and recorded.
- **The corpus contains no `rg` and no `git diff`** (§3.1). This is an observation about what kimi
  *asked for under denial*, not a claim that it never uses them.
- **`kimi` is a native arm64 Mach-O binary** (`~/.kimi-code/bin/kimi`), not a Node script ⇒ no
  interpreter runtime underneath ⇒ the allow-set is smaller than a scripted CLI's would be.
- **One turn is a sample, not a distribution** (matrix §9). This probe does not establish variance in
  the surface across turns.
- **DO NOT edit `/Volumes/<workspace>/repos/ARB/bench/implbench/harness/sandbox.py`** — a parallel codex
  session owns it (Task 14 in flight). READ-ONLY reference.

## 9. Deliverable

A kimi surface table, in the same shape as the behaviour matrix
(`27f7465`): executed cells, verbatim evidence, rails, **recorded omissions**. Plus the probe source
under `docs/superpowers/probes/2026-07-17-kimi-sandbox/`.

**It feeds queue step 2 — freeze `SandboxSpec` against this table** — and nothing else. Seats/cells/
panels reference the **spec**, never a mechanism; that keeps the OS choice out of design→panel→spec
entirely, so only a *contract* change forces a respec, and the Landlock backend later becomes an
implementation of a certified contract rather than a second security arc.
