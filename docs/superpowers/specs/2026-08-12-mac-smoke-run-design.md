# Mac smoke run — a behavioural corpus for the primitives the Mac seat depends on

**Status: DESIGN, not implemented.** Nothing in this document has been built. Where it
describes behaviour of the current tree, that behaviour was measured on 2026-08-11/12 on
`mini-dev` and the measurement is cited; where it describes the proposed check, it is a
proposal.

## The gap this closes

`tests/test_script_portability.py` (landed 2026-08-11, `7e70d277` + `65418f4c`) lints shell
scripts and doc code blocks for two **textual** hazards: `chmod <mode> --`, and Linux-only
absolute paths `/bin/{grep,sed,awk}`. It runs on Linux, which is where these slip through.

It cannot catch a string that is *textually correct* and *behaviourally different* on macOS.
The proving case is the advice the docs themselves carried until `65418f4c`:

> Use `$(command -v grep)` resolved at launch.

`command -v` on a shell **function** returns the function name, not a path. Claude Code aliases
`grep` to a function, so that form resolves to precisely the thing it was written to bypass.
Measured on `mini-dev`: `command -v grep` → `grep`; `command grep --version` →
`grep (BSD grep, GNU compatible) 2.6.0-FreeBSD`. No static lint can distinguish those, because
the text is right and the behaviour is wrong.

That is the class this design covers, and the reason it must run on a Mac.

## The defect that motivates it, found while scoping

`scripts/agent-inbox-watcher:84` and `scripts/codex-inbox-once:40` both call:

```sh
digest=$(sha256sum "${source}" | awk '{print $1}')
```

bare and unguarded, inside `preserve()` — the reject-preservation path. **`sha256sum` is not
part of a stock macOS install** (macOS ships `shasum`). `scripts/arb-pi-orch:48-49` guards the
identical call with `command -v sha256sum`, so the repo already knows the pattern; two scripts
do not use it.

This is invisible three ways at once, which is why it is the right motivating case:

1. **Invisible on this Mac** — `/sbin/sha256sum` is installed on `mini-dev` (non-stock), so the
   call succeeds here. "It works on my Mac" is not evidence.
2. **Invisible to the Linux lint** — on Linux `sha256sum` exists and the string is unremarkable.
3. **Invisible to a naive Mac check** — a corpus asserting "the binary is present" passes on
   this host for the same reason (1) does.

Scope was measured, not assumed. `stat -c` appears twice and is **already correctly guarded**
(`stat -f%m … || stat -c%Y …` in `scripts/claude-hooks/{handoff-hint,context-nudge}.sh`).
`date -d`, `sed -i`, `readlink -f` and `split -` have **zero** real invocations. The exposure is
narrow and specific.

> **A cautionary measurement, recorded because it shaped the design.** A first pass harvested
> external binaries by word frequency and reported **31 `timeout` invocations**. The true count
> of bare `timeout` commands is **zero** — every hit was the word in a comment, a `--turn-timeout`
> flag, a `TURN_TIMEOUT` variable, or a Python string. A grep count is not a defect count. This
> is why the coverage guard below is designed defensively rather than trusted.

## Architecture

One pytest module, `tests/test_macos_primitives.py`, module-level
`pytest.mark.skipif(sys.platform != "darwin")`. No bus, no credentials, no seats, no network:
`tmp_path` and subprocesses only, so it runs on any Mac in seconds.

Division of labour with the existing lint is explicit, because conflating the two is how the
`command -v grep` advice survived review:

| | `test_script_portability.py` (existing) | `test_macos_primitives.py` (new) |
|---|---|---|
| runs on | any platform, including Linux | macOS only |
| catches | **textual** — literal paths, `chmod --` | **behavioural** — right text, wrong behaviour |
| answers | "is the call site written safely?" | "does the safe form actually work here?" |

`sha256sum` splits across both and needs both: the **lint** asserts every call site is guarded
(catchable on Linux, protects contributors without a Mac); the **corpus** asserts the guarded
fallback `shasum -a 256` really produces an identical digest on macOS.

## The corpus — three assertion classes

Each test asserts **the repo's assumption**, not a general platform fact, so it fails when the
repo's dependence breaks rather than when Apple changes something unused.

**Class 1 — availability.** For binaries not guaranteed by a stock macOS. The assertion is
deliberately **not** "the binary exists": that is the check this host passes while a clean Mac
fails. Instead assert a stock-guaranteed equivalent exists and agrees byte-for-byte with the
non-stock one over a fixture. Today: `sha256sum` vs `shasum -a 256`.

**Class 2 — flag and behaviour divergence.** Assert the BSD reality *and* that the form the repo
uses works. All four measured on `mini-dev`, 2026-08-11:

| form | measured result |
|---|---|
| `chmod 600 -- f` | `chmod: --: No such file or directory` |
| `sed -i 's/x/y/' f` | rejected; BSD requires `-i ''` |
| `stat -c '%s' f` | rejected; BSD requires `-f` |
| `date -d '2026-01-01'` | rejected; BSD requires `-v`/`-j -f` |
| `readlink -f f` | **works** on this macOS |

**Class 3 — shell resolution.** The class that produced the broken advice: `command -v grep`
returns a function name, not a path; `command grep` reaches a real binary; BSD grep supports
`--line-buffered`, which every Monitor invocation depends on. Measured: `command grep` resolves
to `/usr/bin/grep` on macOS (BSD grep) and `--line-buffered` is accepted — so `65418f4c`'s fix
is correct on macOS as well as on the Linux host it was written on.

## The coverage guard, and where it lives

The guard is **static**, so it must **not** live in the Darwin-only module — there it would never
fire for anyone working on Linux, who are exactly the contributors who introduce these bugs.

- The corpus exports `COVERED`: the set of primitives it has behavioural tests for.
- The lint imports `COVERED` and asserts every external binary in command position in tracked
  shell scripts is in `COVERED ∪ ALLOWLIST`.

One source of truth, two enforcement points. A Linux contributor adding a new binary dependency
is told to add a macOS behaviour test for it without needing a Mac.

**Extractor rules, designed against the 31-vs-0 measurement above:**

- only files whose shebang is `sh`/`bash` — the false hits came largely from Python scripts
- strip comments before matching
- require command position: line start, or following `|`, `||`, `&&`, `;`, `(`, `$(`
- exclude shell builtins, keywords, and functions defined in the same file
- `ALLOWLIST` is a dict with a **reason string per entry**, reviewed in the diff

A false positive costs one triage line. A false negative is silent. The guard errs loud, on
purpose.

## Failure semantics

On Linux the corpus skips wholesale. That is intended — but it means a green Linux suite says
nothing about macOS, **and** an empty corpus would also be green. It therefore mirrors the guard
the lint already carries (`test_there_are_shell_scripts_to_lint`: *"a glob that silently matches
nothing always passes"*): assert the scanned script set is non-empty and that `COVERED` is
non-empty, so a corpus covering nothing is loud rather than invisible.

## Proving the check can fail

A mutation sidecar per touched pair, with mutations reaching three different mechanisms:

1. strip the `command -v sha256sum` guard from `arb-pi-orch` → lint red
2. drop an entry from `COVERED` → coverage guard red
3. invert one behavioural assertion → corpus red

**Every mutation must change the file's byte length.** On 2026-08-11 a zero-size-delta mutation
(`return EXIT_ERROR` → `return EXIT_ALARM`, both ten characters, file 9514 bytes either side)
produced a gate that passed once and then refused three times as `SURVIVOR`; the mutated code
never reached the interpreter. CPython invalidates cached bytecode on `(mtime, size)`, and a
same-size rewrite inside one mtime second is the case that check cannot distinguish. The
mechanism is inferred rather than proven — it could not be forced on demand — but the correlation
was exact: of eight mutations, the one with zero delta was the only one ever seen to survive.
See `8eca6e4f`.

## Sequencing constraint

The two unguarded `sha256sum` call sites are a **live defect**. Landing the guarded-call-site
check before fixing them makes the lint red on arrival. Order:

1. Fix `scripts/agent-inbox-watcher:84` and `scripts/codex-inbox-once:40` to use the
   `command -v` pattern `arb-pi-orch` already demonstrates.
2. Land the corpus, the `COVERED` export, and the coverage guard.

Step 1 touches the peer's and prod's files (`f5fe3d86`, `cb1c109f`), so it likely wants routing
to them rather than being done unilaterally here.

## Explicitly out of scope

- **Integration-level Mac testing** (arming a real watcher, live dispatch round-trips). Rejected
  during scoping: it needs a bus, credentials and live seats, so it fails for reasons unrelated
  to portability, and a noisy gate gets ignored.
- **Re-enabling CI.** `.github/workflows/ci.yml.disabled` is disabled; this design assumes no CI
  and relies on the corpus running whenever anyone runs the suite on a Mac.
- **Fixing the `sha256sum` call sites.** A prerequisite (above), not part of this work.

## What this design does not establish

It catches primitives the repo *uses today* and binds the corpus to the tree so new dependencies
are noticed. It does **not** establish that macOS behaves identically to Linux anywhere else, and
a green corpus is not a claim that the Mac seat is correct — only that the specific assumptions
enumerated in `COVERED` hold.
