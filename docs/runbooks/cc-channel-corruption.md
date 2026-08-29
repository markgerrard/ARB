# Claude Code Channel-Corruption Runbook

A diagnosis-and-recovery runbook for the Claude Code **tool-result delivery channel** corruption regression. It records the symptoms, the pin that fixes it, the verify-via-artifact discipline that contains it, and a one-minute canary (`scripts/cc-channel-probe`) to detect recurrence.

---

## When to reach for this

Pull this runbook out when you see any of:

- Git SHAs, line counts, or file contents in tool results that **disagree with what you can independently re-derive** from disk / `git`.
- Duplicated or truncated lines in tool output, especially on the **tail of a parallel tool batch**.
- Cancelled parallel tool calls being read back as **user denials**.
- A "push"/"commit" reported as done that `git rev-parse` / `git ls-remote` does not confirm.
- General "the agent seems confidently wrong about what just happened" behaviour after a CLI upgrade.

Don't reach for it for ordinary logic bugs, flaky tests, or network errors — those have honest error envelopes. This is specifically about the channel **lying about its own output**.

---

## What the bug is

Claude Code **2.1.154 onward** (when streaming tool execution became always-on) shipped a regression in which the tool-result channel itself corrupts. It can:

- fabricate plausible-but-wrong output (wrong git SHAs, duplicated lines),
- misread cancelled parallel-batch tool calls as user denials,
- produce **false reassurance** as readily as false alarm.

The dangerous property: a session running on the corrupted channel **cannot self-diagnose**, because its self-report travels over the same channel. A plain "read it back" confirms nothing when the read-back path is the thing lying.

Known-bad band observed: **2.1.154 – 2.1.158**. Known-good pin: **2.1.153** (below the regression, above the 2.1.139 Agent-View floor).

---

## The fix: pin to 2.1.153

Native-installer layout. Hold the active symlink at 2.1.153 even though newer binaries may sit in `versions/`:

```sh
claude --version            # confirm what is ACTIVE, not what is installed
ls -la ~/.local/bin/claude  # symlink should point at .../versions/2.1.153
```

### Apply the pin (disable auto-updater, then downgrade)

If you are currently on a known-bad version, run this to disable the auto-updater and drop back to 2.1.153:

```sh
# disable auto-updater (persist DISABLE_AUTOUPDATER=1 in settings.json)
mkdir -p ~/.claude
python3 - <<'PY'
import json, os
p = os.path.expanduser("~/.claude/settings.json")
data = {}
if os.path.exists(p):
    data = json.load(open(p))
data.setdefault("env", {})["DISABLE_AUTOUPDATER"] = "1"
json.dump(data, open(p, "w"), indent=2)
PY

# downgrade to the known-good pin
claude install 2.1.153

# verify the active version
claude --version
```

Then launch Claude Code with the model pinned explicitly (model selection is server-side, independent of the CLI version):

```sh
claude --model claude-opus-4-8
```

Pin is held by all of: the symlink target, `autoUpdates: false` + `autoUpdatesProtectedForNative: true` in `~/.claude.json`, the `DISABLE_AUTOUPDATER=1` env var (now persisted in `~/.claude/settings.json` by the step above), and the explicit `--model claude-opus-4-8` flag at launch.

### Confirm the pin is *stable*, not just *set*

The failure mode here is silent: a supervisor re-bumps the symlink across a restart and you don't notice until symptoms return. Run `claude --version` at the **start of your next session or two**. Once it has survived a couple of restarts at 2.1.153, you can stop thinking about it.

---

## Containment discipline (use even when pinned — good hygiene regardless)

These are the habits that keep work correct when channel reliability is in question:

1. **Confirm via an independent execution artifact, never the channel's echo of itself.** Ground truth is files on disk, `git log` / `git rev-parse` / `git ls-remote`, file hashes — not the channel's restatement of a command, and not the model's own narration.
2. **Two confirmations = two *independent* commands**, not the same command twice. A garbled channel can return the same wrong answer twice.
3. **One irreversible action at a time** while suspect — no speculative fan-out of edits/commits/pushes in one block.
4. **No parallel tool batching while suspect** — corruption clusters on the tail of parallel batches. One tool call per message.
5. **Inter-agent (bridge) results must be file-backed** — read the report FILE the worker wrote and re-derive its commit SHA via `git -C <worktree> rev-parse HEAD`; never trust the stdout reply payload.

---

## The canary: `scripts/cc-channel-probe`

A fast regression detector. Run it any time you suspect channel corruption, and at the start of a session after a CLI version change.

```sh
./scripts/cc-channel-probe; echo "EXIT=$?"
```

It reports the active version (warning if in the 2.1.154–2.1.158 band) and runs three machine-checkable channel tests, each cross-checked against on-disk ground truth:

1. **Sequential ordering** — `SEQ-1..20` once each, in order.
2. **File-vs-stdout** — 50 lines written to disk vs 50 emitted; `tail` matches.
3. **`cat` read-back** — 100 lines, no duplicates, correct first/last line.

`EXIT=1` / **RED** is **definitive** — a sequential round-trip failed, the channel is clearly broken, do not resume.

`EXIT=0` / **GREEN is weak evidence, not a clean bill of health.** The bug's primary signature is corruption on the **tail of parallel tool batches**, and it is **intermittent**. This script runs in one sequential subshell and runs each check once, so it never exercises the failure mode — a GREEN here has been observed on a known-bad version in another session. Green means only "sequential round-trips work". To actually clear the channel you must run the **Parallel-batch trial** below.

### The one manual step the script cannot cover

A bash script runs in a subshell — it can exercise stdout and `cat`, but **not the Claude Code `Read` tool**, which only exists inside the agent loop. To complete the check, from inside a Claude Code session:

1. Run the canary (covers ordering / file-vs-stdout / `cat`).
2. Then have Claude **`Read`** the integrity fixture and **`cat`** the same file, and confirm the two outputs are byte-identical:

   ```sh
   python3 - <<'PY'
   from pathlib import Path
   Path("/tmp/cc-read-integrity.txt").write_text(
       "\n".join(f"LINE-{i:04d}" for i in range(1, 101)) + "\n")
   PY
   ```

   Read `/tmp/cc-read-integrity.txt` with the Read tool, then `cat` it. Expected: 100 lines, `LINE-0001` first, `LINE-0100` last, identical across both paths. Clean up `/tmp/cc-read-integrity.txt` afterwards.

### Parallel-batch trial — the actual diagnostic

This is the test that targets the real bug. It **must be driven from inside a Claude Code session**, not from a script: only the agent loop can issue a parallel tool batch (multiple tool_use blocks in one message), and the corruption clusters on the **tail** of such a batch. One pass proves nothing because the bug is intermittent — you run many batches and report a **rate**, not a boolean.

Procedure (have Claude run this, repeatedly):

1. Pick a batch size `N` (≥ 5; the tail is where corruption shows, so wider is better) and a trial count `T` (≥ 20; intermittent faults need volume).
2. For each trial, in a **single message**, issue `N` parallel `Bash` tool calls. Call *k* writes a unique sentinel to its own file **and** echoes it to stdout:

   ```sh
   echo "BATCH-<trial>-CALL-<k>" | tee "/tmp/cc-batch/<trial>-<k>.tok"
   ```
3. After each batch, reconcile **three independent views** and require they agree:
   - the **files on disk** (`ls /tmp/cc-batch/` — every `<trial>-<k>.tok` present, each containing its own sentinel),
   - the **stdout** each tool call reported back,
   - that **no call was reported as a denial / cancellation** (the parallel-tail misread).
4. Record failures / total. A single discrepancy — a missing file's call reported as success, a duplicated line, a call read as a user denial — is the bug caught.

**Pass criterion:** **0 faults** across all `T × N` calls — i.e. the fault *count* is zero, not the pass count (`0 faults / 120 calls` = a perfect run, every call correct). A "fault" is any of: missing file, wrong/duplicated sentinel, denial/cancellation misread, or wrong-file content. Any non-zero count ⇒ channel suspect ⇒ pin/downgrade and do not resume. Clean up `/tmp/cc-batch/` afterwards.

> Why files **and** stdout: the disk write is execution-truth; the stdout echo is what the delivery channel chose to hand back. The bug is precisely the two disagreeing, so checking only one cannot catch it.

### What "safe to resume" actually requires

All three, not any one alone:

1. `cc-channel-probe` **GREEN** (sequential round-trips OK — necessary, far from sufficient),
2. Read-tool output **==** `cat` output on the integrity fixture (agent-loop read path OK),
3. **Parallel-batch trial: 0 faults / `T × N` calls** (the primary failure mode actually exercised; fault count zero, not pass count).

GREEN on (1) alone is *not* clearance — that is the trap that greened on a known-bad version.

---

## Incident record (2026-05-31)

- **Trigger:** investigation of the 2.1.154+ streaming-tool-exec channel-corruption regression.
- **Action:** pinned CLI to **2.1.153** (newer 2.1.156–2.1.158 binaries left dormant in `versions/`, symlink held at 2.1.153; `DISABLE_AUTOUPDATER=1` + `autoUpdatesProtectedForNative: true`).
- **Verification:** ran the six-test harness probe (version, sequential ordering, file-vs-stdout, Read-tool integrity, `cat` parity, no-op flush). All six green; the ground-truth-vs-channel cross-checks (file/`cat` agreeing with stdout/Read) passed.
- **Strength of that verdict (corrected):** the sequential probe alone is **necessary but not sufficient** — a subsequent observation confirmed the sequential canary can GREEN on a known-bad version, so a GREEN there must never be read as channel clearance. The sequential run established only that the *sequential* paths were clean and the CLI is pinned to a known-good version (2.1.153, below the regression).
- **Parallel-batch trial (the actual diagnostic):** subsequently ran the full trial at **T=20 × N=6 = 120 genuine parallel tool calls**, one batch per message. **0 discrepancies** across all three independent views: stdout returned all 120 in order with zero denials/cancellations; on disk all 120 files present, each containing its own sentinel (0 missing, 0 mismatches); 120 unique / 120 total (no dupes); 0 wrong-content files. The bug's primary signature was actively exercised 20× and absent every time.
- **Verdict:** all three legs of "What safe to resume actually requires" now satisfied (canary GREEN + Read==`cat` + parallel-trial **0 faults / 120 calls**). **Safe to resume.** Scope caveat: 0 faults across 120 calls is bounded-confidence, not proof of impossibility — a fault rarer than ~1-in-120 could still hide under it; for a pinned-good version that is the expected outcome.
- **Caveat carried forward:** the model-identity claim (Opus 4.8) rests on session env, not an external artifact — the one claim not independently command-verifiable. Probably right; not *proven* the way the line-count tests are.
- **Follow-up:** confirm the pin survives a restart or two (`claude --version` at next session start), then stop tracking it. Re-run the parallel-batch trial before treating the channel as cleared on any *new* version.
