# Workdir mutated while a run is in flight (voided/contaminated result)

**Status: SECOND LOGGED OCCURRENCE — promoted.** The accretion threshold (a bullet enters the
served block only on repeated LOGGED failure) was met by the two occurrences below; the
tree-provenance bullet was co-signed by the owner 2026-08-01 and promoted to
`prompts/arb-close-discipline.md` in the same commit that landed the enforcing machinery,
`scripts/tree-provenance-run` (per the serving artefact's sequencing rule: never ship a bullet
asserting machinery that is not live).

## The class

A test run, dispatch, or measurement executes in a working tree that something — usually the
operator's own hand — mutates before it finishes. The result is not merely noisy: it describes
a tree that never existed as a coherent state, so it is **void**, whatever its color. The class
is vicious for two reasons: the mutation is usually well-intentioned concurrent work, not an
error anyone notices; and the contaminated result *looks exactly like a normal result* — green,
red, or flaky — so nothing prompts a second look. Prose rules ("don't edit a workdir mid-run")
logged after the first occurrence did not prevent the second.

## Detection move

For any suite/measurement result, ask: *what proves the tree at finish was the tree at start?*
If nothing does, the result is [U] regardless of outcome. Mechanically: run suites under
`scripts/tree-provenance-run` (records HEAD + a tree digest — `git status --porcelain -uall`
bytes plus `git diff HEAD --binary` bytes, sha256'd — at start, re-checks at finish, stamps the
result, exits 97 VOID on change or on an unverifiable finish — assert the OK stamp, not a bare
green). Policy half: a background run gets its own **disposable worktree pinned to the commit
under test — one worktree, one writer**; the wrapper detects the violation at the endpoints,
the worktree policy is the operational control that avoids it.

## Occurrence 1 — benchmark contamination (2026-07-24)

During an 8-dispatch seat benchmark, edits to the seat's workdir while dispatches were in
flight contaminated the result set in a *shape-shifting* way: in-flight tasks failed their gate
as `dirty_uncommitted` (the gate diffs against task-START state, so the operator's edits were
blamed on the seat), while tasks starting after the edit silently baselined the dirt as
`no_changes_clean` — a mixed result set that read as seat flakiness, not contamination.
Missing-not-at-random data in a small-N comparison. Recorded:
`docs/measurement-principles.md` § P3; symptom rows in `docs/fragments/failure-shapes.md`.

## Occurrence 2 — the voided shr-s2 suite run (2026-07-29, FABA S2)

A `git checkout <base> -- src/ tests/` was executed in worktree `shr-s2` while a background
full-suite run was executing in that same worktree. The run's result was unusable and had to be
discarded and repeated. Same rule as the bridge's own "do not edit a seat's workdir
mid-dispatch" (which trips `worktree_escape`) — it applies to one's own test runs too.
Recorded: `docs/superpowers/specs/2026-07-29-served-hint-record-ENVIRONMENT-TRAPS.md` § 5.
This occurrence filed ARB-B1, which built the wrapper.

## Honest limits

The wrapper's check is **endpoint equality, not continuous observation**: a mutation fully
reverted before the finish snapshot is invisible (pinned as a documented-limit test in
`tests/test_tree_provenance_run.py`; continuous certainty would need filesystem watching,
deliberately out of scope). Untracked files contribute their **paths** to the digest, not
their content — an edit to an already-untracked file's content is invisible, while tracked
content changes are caught via the diff term. And the stamp certifies that the endpoints
matched, not command honesty (in-process test code is trusted, as in every gate in this
family). These limits were sharpened by the four-seat review panel of 2026-08-01, which found
the original status-line-only hash blind to dirty-tracked re-mutation and to additions under
an untracked directory — both now caught and pinned by dedicated VOID tests.
