# FABA round contract (v5) — shared instruction surface

<!-- ONE surface to maintain: both FABA forms bind to this file.
     SDK form: faba_launch.py composes it into bootstrap_template.md at the
     CONTRACT marker before rendering (invariant sha covers the composition).
     Subagent form: run_probe_round.py embeds it verbatim in the dispatch
     brief for the faba-round agent. Edit HERE, never in the wrappers. -->

## Contract

1. Read `round-input.json` in your workspace. It carries the round pointers:
   the subject artefact id, a one-line `subject_summary` of what that subject
   IS, the round number, the prior decision record id, and the round task. If
   arb-memory read tools are available to you, fetch artefacts by id;
   otherwise work from any materialised copies listed in `round-input.json`.
   If `round-input.json` carries a `must_carry_ids` list, your findings table
   MUST contain every id in it. That list is the prior record's still-open
   findings PLUS any it marked CLOSED whose `reopen-if` scope a code change has
   since touched — those are REOPENED: re-examine them from scratch, do not
   copy the prior closure. (Absent the list, carry the prior record's open
   findings as before.)
2. Perform the round task. Verify claims by running commands and reading real
   state — never by trusting prose. Every closure or verdict you record must
   cite reproducible evidence: the command, its exit code, and the
   commit/snapshot it ran against.
3. Write your decision record to `decision-record.md` in your workspace,
   following the schema below.
4. You do NOT publish the record and hold no bus credentials: the parent
   publishes `decision-record.md` after your round ends and gates on the
   store's own receipt. Your job ends at a schema-valid record in the
   workspace.
5. End your FINAL message with exactly one line (nothing after it):
   `FABA_EXIT {"record_artefact_id": "<id>", "status": "ok|failed", "recommendation": "<one line>"}`
6. Your stop may be blocked by a record gate (a message naming schema problems
   with your decision record). The gate is authoritative: fix exactly the
   problems it names in `decision-record.md`, then finish again with the
   FABA_EXIT line. Do not argue with the gate.

## Decision record schema (v0, prototype)

```markdown
# FABA decision record — round {N}
Subject: <artefact id> | Prior record: <id or none> | Status: ok|failed
Basis: <the commit you verified against — `git rev-parse HEAD` of the repo whose
paths your reopen-if scopes name; write `none` if this round has no code basis.
A successor uses it to auto-detect which of your closed findings a later change
reopened, so record it accurately.>

**What the subject IS:** <one line for a zero-context successor — start from
round-input's `subject_summary`, improve it if the round taught you more>

## Round task
<the task as given>

## Findings
| id | severity | status | evidence (command, exit code, ref) | reopen-if |
(open findings carried forward from the prior record first, then new ones;
 a closure with no command+exit evidence is INVALID — leave it open instead.
 reopen-if states, per CLOSED finding, WHAT tree change reopens it: default `*`
 = the whole subject subtree — the conservative choice; narrow to a pathspec
 ONLY when your evidence shows the finding is isolated. Open findings leave the
 reopen-if cell blank — they cannot be reopened.)

## Recommendation
<one line: what the parent should do next>

## Open items
<what the next round's instance must pick up>
```

## Rails

- No git commit, push, merge, rebase, or deploy. No writes outside your
  workspace. Irreversible actions are recommendations in your record, never
  actions you take.
- Do not fabricate evidence. A claim without a command and exit code is a
  claim, not evidence.
- A tool that is unavailable or denied is DATA, not a failure: record the
  exact outcome as a finding and continue the round.
- If you cannot complete the round, still write a `decision-record.md` with
  status `failed` and what blocked you — the parent publishes it for the audit
  trail (the round still counts as failed), and a round with NO record is
  indistinguishable from a crash and forces a re-dispatch.
