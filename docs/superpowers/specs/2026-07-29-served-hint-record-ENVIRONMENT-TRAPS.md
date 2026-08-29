# Served-hint build — environment traps ("first things to rule out")

Diagnosed 2026-07-29 during S1/S2. These are **environment** faults, not code defects. Each one
cost real time or produced a misleading result. Check these before diagnosing anything else.

---

## 1. `ARB_MEMORY_DSN` in socket form silently breaks per-role connection tests

**Symptom.** `tests/arb_memory/test_eval_grants.py` fails with a psycopg parse error:

```
psycopg.ProgrammingError: invalid connection option "postgresql:/arb_memory_test?options"
```

Note the **single** slash — the DSN went in as `postgresql:///arb_memory_test`.

**Cause.** `_mcp_dsn()` (`test_eval_grants.py:64-75`) splits the DSN with `urlsplit`, adds an
`options=-csearch_path=…` query parameter, and reassembles it. A socket-form URI
(`postgresql:///db`, empty netloc) does not survive that round-trip.

**Fix.** Use the host form:

```bash
export ARB_MEMORY_DSN="postgresql://localhost:5432/arb_memory_test"
```

**Do NOT use `postgresql:///arb_memory_test`** even though it connects fine for ordinary tests —
that is exactly what makes it a trap. Plain fixture tests pass; only the tests that build a
*derived* connection string fail, and they fail with a parse error that looks like a code bug.

## 2. `test_mcp_role_has_no_audit_or_eval_access` fails even with a correct DSN — PRE-EXISTING

**Symptom.** With the host-form DSN, one test still fails:

```
AssertionError: assert 'mark' == 'arbmem_mcp'
```

**Proven pre-existing, not caused by this slice.** Verified 2026-07-29 by reverting
`src/arb_memory/` and `tests/arb_memory/` to base commit `a91a6408` (zero `hint_read` tables
present, confirmed by grep) in a venv-bearing worktree and re-running: **identical failure**. So it
predates S1 and S2 both.

**Cause.** `_mcp_dsn()` derives the MCP connection by string-replacing `arb_memory:` with
`arbmem_mcp:` in the DSN. A DSN with no username has nothing to replace, so the test connects as the
OS user (`mark`) instead of the MCP role. It needs either `ARB_MEMORY_MCP_DSN` set explicitly, or a
DSN shaped `postgresql://arb_memory:…@host/db`, **and** those roles provisioned with LOGIN.

**Standing decision: out of scope for this build.** Provisioning the MCP role chain is unrelated to
the served-hint slice. It is recorded here as a known-red baseline so it is never mistaken for a
regression. **Do not chase it** — that is the harness-refinement trap the BUILD-CHARTER forbids.

## 3. `[grok-stderr] worker quit with fatal: … Auth(AuthorizationRequired)` is noise

**Symptom.** Every grok turn-start logs several of these, referencing
`mcp.slack.com/.well-known/oauth-protected-resource`.

**Cause.** Grok's Slack MCP server failing OAuth. It is unrelated to the dispatched task.

**Evidence it is harmless.** S1 and S2 both completed cleanly, committed, and returned `ok:true`
through a continuous stream of these errors.

**Still worth ruling out first** if a later increment fails in a way that looks like a tool or
transport problem — the phrase "worker quit with fatal" reads far worse than it is.

## 4. A hand-made `git worktree add` has no `.venv`; only bridge-created ones are mirrored

**Symptom.** `timeout: failed to run command '.venv/bin/python': No such file or directory`.

**Cause.** The bridge's `_link_base_venv` (`bridge.py:1805`) mirrors the base checkout's `.venv`
into worktrees **it** creates via `--worktree`. A worktree you create yourself with
`git worktree add` gets no venv, so it cannot run the suite at all.

**Consequence for baselines.** To run a suite at some historical commit, do it *inside a
bridge-created worktree* by checking the old files out there (`git checkout <base> -- src/ tests/`)
and restoring afterwards — do not create a bare worktree and expect to run tests in it.

## 5. Never check files out in a worktree while a run is in flight there

Self-inflicted 2026-07-29: a `git checkout <base> -- src/ tests/` was run in `shr-s2` while a
full-suite run was executing in that same worktree. The run's result was unusable and had to be
discarded and repeated. Same rule as the bridge's own "do not edit a seat's workdir mid-dispatch"
(it trips `worktree_escape`) — it applies to your own test runs too.

## 6. A reviewer seat's env leaks into the dispatch — two real cases, both self-inflicted

Diagnosed 2026-07-29 during the pre-merge review panel. **Both were introduced by the seat
launcher, and both changed what a reviewer observed.** The orchestrator then told the reviewer to
trust an environment it had itself contaminated.

### 6a. `PYTHONPATH` in the daemon env resolves the WRONG checkout

**Symptom.** A bridge reviewer working in its own worktree ran `.venv/bin/python -m pytest` and
`arb_memory` resolved to the **parent checkout**, not the worktree. Caught by the seat, which
overrode with `PYTHONPATH=src` and said so; had it not, it would have silently reviewed code that
was not under review.

**Cause.** The launcher exported `PYTHONPATH=<repo>/src` into the daemon environment (copied from
the "run the daemon as `PYTHONPATH=src .venv/bin/python -m agent_redis_bridge`" recipe). It is
inherited by the dispatched engine, and an explicit `PYTHONPATH` **precedes** the worktree's
mirrored editable `.pth` on `sys.path`.

**Fix. Do not set `PYTHONPATH` when launching a seat.** The base venv's editable install already
resolves both packages, and `_link_base_venv` rewrites the `.pth` into each fresh worktree so the
mirrored interpreter imports the WORKTREE's checkout — which is the whole point of the mirror.
Verify with `env -u PYTHONPATH .venv/bin/python -c "import arb_memory; print(arb_memory.__file__)"`.

**Brief-side rail:** tell the reviewer to print `arb_memory.__file__` and confirm it is inside its
own worktree *before* trusting any run, rather than asserting the environment is correct.

### 6b. `AGENT_BRIDGE_CODEX_DISABLE_AUTO_MEMORY=1` reddens four hermetically-unsealed argv tests

**Symptom.** A reviewer seat measured the full `tests/arb_memory/` suite at **9 failed / 1024
passed / 1 skipped** where the orchestrator (and a second reviewer) measured **8 / 1025 / 1**. Four
of the extra reds were `tests/arb_memory/test_local_memory_injection_codex.py` argv tests.

**Cause.** The var is legitimate seat hygiene — it keeps a reviewer seat COLD so it does not inherit
the operator's interactive `auto_memory` config. But `engines/codex.py` appends
`["-c", "features.auto_memory=false"]` when it is set, and those tests clear only
`ARB_MEMORY_LOCAL_MCP` before asserting the **entire argv** equals
`["codex", "app-server", "--listen", "stdio://"]`. They are not hermetic against it.

**Reproduce:**
```
AGENT_BRIDGE_CODEX_DISABLE_AUTO_MEMORY=1 pytest tests/arb_memory/test_local_memory_injection_codex.py -q
  -> 4 failed, 1 passed
env -u AGENT_BRIDGE_CODEX_DISABLE_AUTO_MEMORY pytest ... -q
  -> 5 passed
```

**Not a regression from any served-hint change** — the blobs for `engines/codex.py`,
`test_local_memory_injection_codex.py` and `test_lane_writer.py` are byte-identical at `a91a6408`
and `680d41eb`. The test-hermeticity defect belongs to whoever owns those tests; it is recorded, not
chased (anti-recursion guard).

### The generalisable rule

**A known-red baseline is a property of an environment, not of a commit.** "8 failures" was
reproducible — a second reviewer hit it exactly — but only under the same env and DSN. Handing a
baseline to a reviewer as a *fact* invites it to file environment noise as a regression, or worse,
to dismiss a real regression as expected. State the baseline **with the environment that produced
it**, and ask the reviewer to re-derive it rather than adopt it. `test_lane_writer`'s three
failures are the sharp case: order- and DSN-dependent, they fired for one reviewer and not the
other, on identical bytes.

## 7. An **unset** `ARB_MEMORY_DSN` turns the suite green by deleting most of it — and the DSN is host-specific

Diagnosed 2026-07-29, post-merge, while confirming the slice still passed on `dev`.

**Symptom.** On merged `dev`, clean shell:

```
pytest tests/arb_memory/ -k hint_read -q
  -> 13 passed, 63 skipped, 978 deselected      (exit 0)
```

The recorded baseline for that exact selector is **76 passed, 0 skipped**. Same commit, same bytes.
63 of the 76 tests simply did not run — and the summary line still opens with the word *passed*.

**Cause.** The database fixtures in `tests/arb_memory/conftest.py` — `scratch` and
`empty_schema_conn`, plus `conn_factory`, which takes `scratch` as a parameter — begin with
`pytest.skip("no ARB_MEMORY_DSN")` when the variable is unset. Every test that touches PostgreSQL
evaporates. Nothing fails, so nothing draws attention.

**Why this is worse than trap 1.** Trap 1 is the *wrong* DSN and it fails loudly, with a psycopg
parse error. This is the *absent* DSN and it succeeds quietly. A wrong DSN announces itself; a
missing one does not. It is also the mirror image of §6's lesson: there, a red baseline was at risk
of being read as an expected green — here, a hollow green is read as a real one.

**The multi-host clause — this is why it bites here.** This repo is checked out on more than one
server, and `ARB_MEMORY_DSN` is **host-specific**, so there is no correct value to write down once.
`docs/runbooks/arb-memory-seat-e2e.md` and `…-artefact-audit-e2e.md` both document
`postgresql://arb_memory:$ARB_LOCAL_PG_PASSWORD@127.0.0.1:5544/arb_memory`; the dev Mac runs PostgreSQL on
**5432**, with `arb_memory_test` owned by the OS user and no `arb_memory` role password in play:

```bash
# this host (dev Mac), verified 2026-07-29 — NOT portable to another server
export ARB_MEMORY_DSN="postgresql://user@127.0.0.1:5432/arb_memory_test"
```

Neither value is "the" DSN. **Derive it on the host you are standing on**, never copy it from a
runbook, a handoff, or another host's brief:

```bash
lsof -nP -iTCP -sTCP:LISTEN | grep -i postgres   # the port this host actually listens on
psql -lqt | cut -d'|' -f1,2                      # the database and its owner
```

Trap 1 still applies on every host: use the **host form**, never `postgresql:///db`.

**The rail. A pass count is not a baseline — a pass *and skip* count is.** "76 passed, 0 skipped"
catches this the moment it regresses; "76 passed" cannot, because the failing state is also
`76 passed`. Record both numbers everywhere a baseline is stated, and treat a rising skip count as a
red result, not a quiet one. Self-check:

```bash
pytest tests/arb_memory/ -k hint_read -q -rs | grep -c 'no ARB_MEMORY_DSN'   # must be 0
```

**Brief-side rail.** Require a dispatched seat to print its resolved DSN host/port **and** its skip
count before any suite result is trusted — the same shape as §6a's `arb_memory.__file__` check,
extended from the interpreter to the database. A seat that reports only "N passed" has not told you
whether the tests ran.
