---
name: benchmark-harness-destroys-its-own-data
description: "Benchmarking seats by dispatch — stagger to 2-3, freeze the seat workdir, never trust the dispatcher as system of record"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: b533da0d-2e46-4c0f-9e1f-a003e170b08b
  modified: 2026-07-25T03:41:24.713Z
---

When benchmarking seats by dispatching them, the harness is not a neutral observer — it shares a
working tree, a bus, and a local port range with the thing being measured. Three ways it corrupts
its own results (all hit in one 8-dispatch run, 2026-07-24):

1. **Ephemeral-port exhaustion.** Each `agent-dispatch` spawns a fresh `redis-cli` per BLPOP poll.
   8 concurrent dispatchers over ~40 min → `Can't assign requested address`, 4 of 8 runs dead.
   **Stagger to 2–3 concurrent.**
2. **Mid-flight workdir edits.** Editing the seat's workdir during a dispatch fails that run's gate
   as `dirty_uncommitted` — the gate diffs against task-START state, so your edits are blamed on the
   seat. It is **silent for tasks that start after the edit** (they baseline the dirt as
   `no_changes_clean`), so you get a mixed result set that looks like flakiness, not contamination.
   **Freeze the workdir — no edits, commits, or test runs — until every dispatch lands.**
3. **Lost replies are unrecoverable.** The dispatcher's stdout is the only durable copy. When the
   dispatcher dies the task still ran, but the result key is reaped before you can read it.

Also: seats are `--max-parallel 1`, so size `--timeout` for **queue position** (`N × turn_timeout`),
not task length, or later dispatches exit 124 while waiting their turn.

**Why:** all three produce *non-random* missing data — contamination hits in-flight runs, port
exhaustion hits long ones, both correlate with what a benchmark deliberately does (wide fan-out,
long turns). Missing-not-at-random biases small-N comparisons and is invisible in the surviving rows.

**How to apply:** before firing, verify the seat's workdir is clean and commit to not touching it.
Fire in batches of 2–3. Capture evidence independently of the dispatcher (`--run-id` audit rows, or
have the seat write outside the repo). Always report the denominator: dispatched vs survived vs why.

Full write-up: ARB `docs/measurement-principles.md` § P3; symptom rows in
`docs/fragments/failure-shapes.md`. Related: [[round-panel-roster]], [[author-round-input-mutation]].
