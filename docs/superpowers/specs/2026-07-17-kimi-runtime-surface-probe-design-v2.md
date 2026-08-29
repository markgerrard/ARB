# Design v2 — kimi runtime-surface probe: **A discovers, B proves minimality**

Status: **DESIGN, pre-panel** · Author: warm Opus · Arc: kimi seat → sandbox pivot
**Supersedes `2026-07-17-kimi-runtime-surface-probe-design.md` (`5ec9abb`), which was BLOCKED by its
own certifying quorum** (`RID=panel-surface-probe-design-20260717T101246Z-44229f`, decision `block`,
close `outcome=emitted`). Re-architecture directed by Mark, 2026-07-17.

## 1. Why v1 was blocked — the defects this design exists to not repeat

v1 proposed: **A** = `fs_usage` trace of one live turn; **B** = deny-default climb over a *command
corpus harvested from historical ACP ask payloads*. Both certifying seats blocked it. Every finding
below was **re-verified by execution** before being accepted (a panel P0 is a candidate, not a
verdict — and one of the five agy findings was verified **false**, see §9).

| # | Defect | Status |
|---|---|---|
| 1 | **B's premise is broken.** Its corpus came from ACP ask payloads that truncate at ~50 chars, unrecoverably (no `rawInput`). Proxies discard the argv/env/cwd/heredoc that *determine* path surface. | **VERIFIED** |
| 2 | **v1's own fix was incoherent.** v1 said "A's exec capture covers the truncation gap." A traces **one new turn** — it cannot recover what kimi ran in *historical* cells. And `fs_usage -f exec` yields **only the binary path, no argv** (executed: rows read `execve /usr/bin/stat`, no arguments). | **VERIFIED** |
| 3 | **v1's controls were false-green.** Exit-code-only. Executed under a real deny: `git show --stat HEAD \| head -60` → **rc=0 with EMPTY stdout**. `set -o pipefail` → rc=127. v1 would have scored a command that produced *nothing* as passing. | **VERIFIED** |
| 4 | **v1's bootstrap profile would have poisoned instrument A.** kimi writes session state to `~/.kimi-code/sessions/…` — **20 occurrences at `da4c2d2`** (`git show da4c2d2:…/review_asks.jsonl \| grep -c 'kimi-code/sessions'`). v1 allowed writes to worktree+TMPDIR **only** ⇒ the traced turn runs **degraded** and A measures a *bootstrap-conditioned* surface **while looking complete**. Fixture-masks-reality. | **VERIFIED** |
| 5 | **Network was in scope and never instrumented.** | **VERIFIED** (`fs_usage -f network` works — §4) |
| 6 | **v1's corpus counts were not reproducible from the committed repo.** Working tree: 38 Bash asks / 82 lines. Committed `da4c2d2`: **30 / 71**. The probe harness was *appending to the file while v1 cited it*. | **VERIFIED** |
| 7 | **v1 claimed the corpus "spans R1/R2/R3".** It spans **R1+R2**. R3 has still not executed (`review_results.jsonl`: R1×5, R2×6, run in flight). | **VERIFIED — the claim was FALSE** |

**The lesson (defect 6 is the sharpest):** v1's numbers disagreed with sol's because the reviewer
worktrees pinned committed bytes while a live run appended to the author's copy. **Neither party
miscounted — they read different bytes.** Any evidentiary claim in this design MUST cite a
**committed SHA**, never a working-tree path.

> **This rule was violated by this document, in its first draft, by its author.** The §1 row 4 figure
> was written as "21 occurrences" — a working-tree snapshot of the still-growing file (now 24; the
> committed value is **20**). Caught by applying the rule to the document that introduces it. Recorded
> because it is the whole point: **knowing a rule confers no immunity, and only mechanical
> re-derivation from committed bytes does.** The probe harness MUST therefore generate its own
> inventory from a named SHA and **fail loudly if a documented total disagrees with the parser** —
> discipline is not sufficient here, and this paragraph is the evidence.

## 2. The inversion

> **v1:** B discovers (from history), A corroborates. — *broken: history is unrecoverable.*
> **v2:** **A discovers** (from a live turn), **B proves minimality** (against live turns).

This deletes defects 1, 2 and 6 by construction: there is **no reconstruction step**, so there is
nothing for truncation to destroy, and the historical corpus carries **no evidentiary weight**.

The property Mark approved survives intact: **two mechanisms, blind on different axes, and the delta
is the deliverable.**

| | **A — discover** | **B — prove minimality** |
|---|---|---|
| Question | *What does a real review turn touch?* | *Is every allow load-bearing?* |
| Mechanism | `fs_usage` (3 modes, §4) over a live R2 `auto` turn | **class-level drop-one against LIVE turns** |
| Yields | observed superset: paths, execs, endpoints | the subset that is actually required |
| Blind to | need vs. incidental noise ⇒ over-allows | anything outside what A observed |
| Cost | ~1 turn (~900s) | ~6–8 turns (§5) |

**The delta rule, corrected.** v1 said "in A, not in B ⇒ candidate to deny." That is **wrong for the
silent-degradation class** and was flagged by two seats independently. Corrected:

- **In A and required by B** → enters `SandboxSpec`.
- **In A, not required by B** → **HOLD FOR ARGUMENT — never auto-deny.** A path can be touched,
  be load-bearing, and still not break the oracle when denied (the program degrades quietly). Each
  such row needs a named human/panel argument to deny. Denial-by-default-of-the-delta is how a
  quietly-broken profile ships.
- **Required by B, not in A** → **one of the instruments is lying.** Read this row first.

## 3. Corrected bootstrap profile (fixes defect 4)

The trace runs kimi in `auto` before a profile exists. Chicken-and-egg, resolved by a coarse-but-real
boundary — **now including the seat-state paths v1 omitted**:

- **read:** broad (we are measuring reads, not constraining them yet)
- **write:** isolated worktree · scoped TMPDIR · **`~/.kimi-code/**` (seat state/cache — the v1 omission)**
- **network:** the API endpoint only
- **paths:** `/private/tmp`, never `/tmp` (§8)

**Bootstrap self-test — REQUIRED, and it gates A's data:** the R2 `auto` turn **must reach the §6
oracle under the bootstrap profile** before any of A's output is trusted. If it does not, A is
measuring a degraded agent and its "superset" is an artifact. **This gate is what defect 4 buys** —
without it, a poisoned trace is indistinguishable from a clean one.

The bootstrap profile is an **instrument**. It does not enter `SandboxSpec`.

## 4. Instrument A — three capture modes, chosen by measurement

All three verified on macOS 26.5.2 / 25F84, 2026-07-17:

| Mode | Rate | Role | Evidence |
|---|---|---|---|
| `-f exec` (unfiltered) | **~192 lines/s** | binary set + **PATH probing** | 767 lines/4s |
| `-f network` | moderate | endpoints, sockets, **DNS** | 12,714 lines; captured `connect → private/var/run/mDNSResponder` and `socket` rows for a `curl` |
| `-f filesys` (**name-filtered** to A-exec's binary set) | **~8,200 lines/s** | the path touches | 24,507 lines/3s |
| ~~`-f filesys` unfiltered~~ | **~63,200 lines/s** | **REJECTED** | 252,895 lines / **60MB per 4s** ⇒ ~**5.1GB** and ~57M lines per 900s turn, of which **0.15%** was the target tree |

**PATH probing is real surface and nobody predicted it.** A-exec captures one `execve` **per PATH
candidate**: `/Volumes/…/.venv/bin/git`, `/Volumes/<workspace>/rust/cargo/bin/git`,
`/opt/homebrew/bin/git`, … A profile must account for the probe of every PATH entry, not just the
binary that wins.

### 4.1 Attribution is an open problem — stated, not waved at

**`fs_usage` has NO ancestry semantics.** Its man page: *"The sampled data can be limited to a list of
process IDs or commands. When a command name is given, all processes with that name will be sampled."*
Its columns carry `PROCESS NAME` (as `name.pid`) but **no PPID**. So there is no descendant selector.

This is not theoretical: **the 2026-07-17 test trace captured a parallel session's work** — `codex`
(3,952 rows), `python3.12` probing `/Volumes/<workspace>/repos/ARB/…`, `grok`, `redis-server`. A
name-filter on `git` catches **every `git` on the host**, not kimi's.

Mitigation, both parts REQUIRED, neither sufficient alone:
1. **Quiesce the host** for the trace window — no parallel bridge dispatches, no other sessions.
   Crude, effective, and *verifiable*.
2. **Measure the contamination rather than assume it away:** sample `ps -axo pid,ppid,comm`
   throughout, reconstruct the pid tree, and **report the count of rows NOT attributable to kimi's
   tree** as a first-class number in the deliverable. A trace with unexplained rows is a trace with
   unknown provenance.

### 4.2 `fs_usage` drop behaviour is UNDOCUMENTED — treat as a live risk

`man fs_usage` contains **no** drop/overflow/lost-event language. So: it is **unknown** whether it
reports drops, and **absence of a warning is not evidence of no drops**. A dropped event is a missing
path is an under-allow is a broken profile — silently. Two partial mitigations: keep volume low (the
mode choices in §4 exist substantially for this), and rely on the §2 delta ("required by B, not in A"
⇒ an instrument is lying) as the detector. **Recorded as an open risk, not a solved one.**

## 5. Instrument B — class-level drop-one against live turns

Per-allow drop-one on live turns is unaffordable (N allows × 900s). Per-allow drop-one on *proxies*
is what got v1 blocked. So B operates on **semantic classes**, each dropped against a **real turn**:

| Class | Hypothesis |
|---|---|
| repo / worktree | required |
| `.git` internals | required |
| xcrun / toolchain cache (`DARWIN_USER_TEMP_DIR`) | required — the original scar |
| `~/.kimi-code/**` seat state | required (§3) |
| dyld / `/usr/lib` / `/System` | required |
| PATH-probe directories (§4) | **unknown — the interesting one** |
| network endpoint | required |
| TMPDIR | unknown |

~6–8 classes × ~900s ≈ **1.5–2h**. Affordable, and **sound in a way proxies never were**: the
subject under test is the actual seat doing actual work.

**Each drop must make a NAMED oracle fail (§6), never merely change an exit code.**

## 6. The oracle — semantic, never exit codes (fixes defect 3)

v1's "must succeed" / "must break" were undefined, and exit codes are provably insufficient here
(defect 3, executed). Every probe is scored against a **named semantic oracle**.

**The turn oracle — `DELIVERED`:** `stopReason == end_turn` **AND** `len(text) > 200` **AND**
`mutations == 0` **AND** the review text carries `file:line` citations. This is not invented for
this design: it is the criterion `review_viability.py` already uses, and it has already discriminated
5/5 R1 and 3/3 R2 real reviews from failures.

**For any shell probe:** `set -o pipefail`, plus per-stage status, plus an expected-output assertion
recorded **from the unsandboxed control**. Never a bare `$?`.

**Two controls, because green proves nothing without them:**
1. **Adversarial self-test** — every probe runs **UNSANDBOXED and must satisfy the same oracle**. A
   probe that fails both ways measures the probe. *This is the control that catches the ARB-profile
   failure mode: its allow-set cannot exec `/bin/echo` (rc=71) while `(allow default)` passes.*
2. **Positive deny control** — deliberately block a known-required access and require the oracle to
   **go red**. Proves the checker can see a failure at all
   (`[[deny-proofs-need-adversarial-verification]]`, `[[vacuously-green-guard-fail-loud]]`).

## 7. Deliverable

A kimi surface table (not included in this copy) — executed cells, verbatim evidence,
rails, **recorded omissions**, plus the contamination count (§4.1). Probe source under
`docs/superpowers/probes/2026-07-17-kimi-sandbox/`.

Feeds queue step 2 — **freeze `SandboxSpec` against this table** — and nothing else. Seats/cells/panels
reference the **spec, never a mechanism**, so the Landlock backend later implements a certified
contract rather than opening a second security arc.

**The probe may still kill the design.** If the surface needs so much filesystem that the boundary
stops meaning anything, that is a finding, and the seat goes back to `plan`.

## 8. Mechanism traps — paid for, do not re-discover

| Trap | Fact |
|---|---|
| `(trace "…")` | **Silently no-ops on 26.5.2.** Parses, runs, emits nothing. This is *why* A is `fs_usage`. |
| `(subpath "/tmp/…")` | `/tmp` is a symlink; Seatbelt matches **resolved** paths ⇒ denies everything incl. intended allows. Use `/private/tmp`. |
| **`(subpath "/a/b")` is NOT a string prefix** | It matches `/a/b` and its contents — **not** `/a/b-c.txt`. A rule that looks like it denies can silently match nothing. (Cost an hour on 2026-07-17.) |
| **A denied exec looks like a MISSING FILE** | `deny process-exec` surfaces as `zsh: no such file or directory: /opt/homebrew/bin/git` — not a permission error. Reads as a broken PATH, invites the wrong fix. |
| Denied **file** ops DO name the path | `touch: /private/tmp/…/t2.txt: Operation not permitted` (rc=1) — reported by the failing program via errno, not by `sandbox-exec`. |
| Sandbox denials ARE in the unified log | `log show --predicate 'subsystem == "com.apple.sandbox.reporting"'` → `[violation] Sandbox: imagent(434) deny(1) mach-lookup …`. A real attribution channel if one is needed. |
| `fs_usage` | Requires root (passwordless sudo verified available). Read-only. |
| `Popen(user=…)` (ARB's `_os_launcher`) | setuid needs root; our seats run as the operator's own OS user. Not reusable. |
| SIP | Enabled; `kimi` (`~/.kimi-code/bin/kimi`, native arm64 Mach-O) is not SIP-protected. |

## 9. Recorded bounds and omissions

- **n=1 on stimulus** (Mark's scope call: R2 only). Accepted and recorded.
- **The historical ask corpus carries NO evidentiary weight in this design.** It is context. Its
  counts are not reproducible from committed bytes (defect 6) and half its commands are unrecoverable.
  **Any future citation of it must name a committed SHA.**
- **R3 has not executed.** Do not claim R1/R2/R3 breadth (defect 7).
- **`fs_usage` drop behaviour is undocumented** (§4.2) — open risk.
- **`-f exec` yields no argv** — B can never replay exact commands. v2 does not need it to.
- **Attribution has no clean mechanism** (§4.1) — mitigated by quiesce + measured contamination.
- **agy's F3 was VERIFIED FALSE.** It claimed the design "relies on the assumption that `default` mode
  is safe/inert" at **P0, Confidence: High, Alternative explanations: None.** v1 makes no such claim
  (grep returns nothing). Recorded because it is the calibration datum: **a fabricated P0 is written
  identically to a real one**, and only execution separated them.
- **grok's "1 redirection-like `>`" was VERIFIED FALSE** — zero commands contain `>`. Its F1
  conclusion stands on other grounds (pathspecs, env-prefix, heredoc).
- **DO NOT edit `/Volumes/<workspace>/repos/ARB/bench/implbench/harness/sandbox.py`** — parallel codex
  session owns it. READ-ONLY reference; its allow-set is a hypothesis (it cannot exec a binary).
