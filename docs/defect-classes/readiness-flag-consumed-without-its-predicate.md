# The readiness flag crossed a boundary and lost its predicate

**The class where every party is honest and the gate still certifies something nobody proved.**
A producer runs a real, executed readiness check, and records the result as a flag. A consumer
downstream reads the flag and treats it as proof of *the property the consumer cares about*. But a
boolean carries no predicate: the producer knows what it proved, the consumer sees only `True`, and
the flag's **name** is the only thing that travels between them. When the producer's predicate and
the consumer's requirement differ — most dangerously in **which actor** had to be capable — the gate
passes on evidence about something else.

**A flag is not evidence. It is a citation to evidence, and citations go stale silently.**

## Why it survives every local check

Nothing in the chain is wrong on its own terms, which is why review does not catch it:

- **The producer's proof is genuine and executed.** Not a mock, not an assumption — a real check
  that really ran. Auditing the producer finds nothing to fix.
- **The producer's prose is accurate.** It says what it measured. It does not claim the stronger
  property; it simply has no way to stop a reader from inferring it.
- **The consumer's prose may also be accurate.** It can honestly say the subject *advertises* the
  flag — and still be positioned in a gate where PASS is read as "this prerequisite holds".
- **The overclaim lives in neither file.** It lives in the *join*: in the reader's assumption that
  a flag named for a capability attests that capability. There is no line of code to point at, so
  a line-by-line review of either side comes back clean.
- **The name does the lying.** `brief_hydrate=v1` reads as "this seat hydrates". Its producer's
  contract is "this host's console script is locatable and exits 0". Names travel; contracts don't.

## The detection move

**For every flag your gate consumes, open the code that SETS it and read the predicate it actually
proves — then ask whether that predicate is about the same actor, at the same time, under the same
conditions as the thing you are gating.** Three axes, and the actor axis is the one that hides:

1. **Which actor?** Host, process, engine, model, network path — a readiness proof run by the host
   says nothing about what the engine will do on a turn.
2. **At what time?** Registration-time versus dispatch-time. A flag set at startup describes a
   world that may not exist when you read it.
3. **Under what conditions?** The producer's env, tool ceiling, and configuration may not be the
   ones in force when the capability is exercised.

If the answer to any of these differs, the flag is *necessary, not sufficient*, and the gate must
either obtain the missing evidence itself or downgrade its claim to what the flag supports.

**Corollary for producers:** name flags after the predicate, not the aspiration —
`hydrate_console_script_ok` cannot be misread the way `brief_hydrate` can.

## Proving instance — `brief_hydrate=v1`, seat-preflight's hydration gate

> **Naming note.** This section describes the check as it stood **at discovery**, when it was called
> `target-hydration` / `check_target_hydration`. It was renamed to `target-advertises-hydrate` on
> 2026-08-11 (the third remedy below). The old name is kept in the narrative because the name *is*
> the defect here — renaming it in the account would delete the evidence.

The producer is honest. `prove_brief_hydrate_readiness`
(`src/agent_redis_bridge/brief_hydrate_ready.py:156-166`) documents itself precisely:

> Return True only after executed console-script/policy/prompt/receipt/cleanup checks. …
> Step 1 proves the console script is locatable and executes (exit 0), not merely that
> `arb_memory.brief_hydrate` is importable.

That is a **host** predicate, and the bridge records it as a flag at
`src/agent_redis_bridge/bridge.py:983` (`brief_hydrate = "v1" if self.brief_hydrate_ready else ""`),
published into the registry at `bridge.py:1000`.

The consumer reads the flag and nothing else. `check_target_hydration`
(`scripts/seat-preflight:746`) passes a seat when its registry entry carries `brief_hydrate == "v1"`,
and runs in the `ref-required` and `legacy-removal` enablement checklists
(`scripts/seat-preflight:1395-1399`) — the gates an operator consults *before* turning those flags on.

**The predicate the gate needs is about a different actor.** Hydration on a live dispatch is
performed by the **engine**, on its turn, and the bridge refuses the whole turn when the receipt is
absent — `bridge.py:3804-3808`, "Receipt is required; a successful model reply cannot override
missing/bad receipt", returning `brief_hydration_receipt_missing`.

**The disproof was already in this repo, eleven days before the gap was noticed.**
`docs/agent-memory-seeds/claude-harness/wave1-ref-required-live.md:17-21` records that six roster
seats (`asdk-bridge-dev-{haiku45,opus48,opus5,sonnet5}`, `pi-sdk-bridge-dev-{glm,minimax-m3}`)

> advertise `brief_hydrate=v1` (in-process readiness) but structurally cannot execute the hydrate
> helper on an engine turn (`engines/agent_sdk.py` "You have no shell"), so live ref dispatch fails
> `brief_hydration_receipt_missing`.

(The shell-less engine prompt is `src/agent_redis_bridge/engines/agent_sdk.py:286`.) Mark resolved
the fork on 2026-07-29 by keeping those six out of rosters — a **roster** remedy. The **gate** that
reads the same field was never revisited, so a seat in that class **would** pass `target-hydration`
today. Recorded precisely: this is a latent gate defect demonstrated from the code and the record.
No run of `target-hydration` against one of those six has been located, so no claim is made that it
*did* pass in production.

Note where the overclaim is and is not. `check_target_hydration`'s own message says targets
*"advertise brief_hydrate=v1"* — literally true, and the author chose that verb carefully. The
defect is **positional**: an honest sentence sitting where a gate's PASS is consumed as
"prerequisite satisfied". Honest prose does not neutralize a misplaced gate.

**The repo already holds the rule it needs, one file away.** `bridge.py:296` states it for a
sibling case: *"Engine completion metadata is not an attestation."* The same sentence, applied to
registry advertisement, would have closed this. A rule known in one module is not thereby enforced
in another.

## What would actually close it

Status as of 2026-08-11: **the third is done, the first two are not.** Naming a remedy is itself a
claim (`residual-remedy-is-also-a-claim.md`), so each is marked with what was actually executed
rather than what was reasoned about:

- **NOT DONE — obtain the missing evidence**: gate on a demonstrated engine-side hydration receipt
  rather than the advertisement, at the cost of needing a live turn.
- **NOT DONE — infer the actor**: derive engine hydration capability from the effective tool ceiling
  — the axis `scripts/roster-preflight` implements as B2 and asserts in
  `test_b1_v1_alone_does_not_clear_b2`, which pins that `brief_hydrate=v1` alone must not clear the
  hydration axis. Note this axis already exists and is tested; what is missing is seat-preflight
  *consuming* it.
- **DONE 2026-08-11 — downgrade the claim**: the check is renamed `target-advertises-hydrate`
  (`scripts/seat-preflight:746`), its messages carry the new prefix, and its docstring states
  explicitly that a PASS does not establish that any target can hydrate. Operator-visible output
  changed with it.

The first is strongest and most expensive; the third was nearly free and removes the *misreading*
without touching what the gate enforces.

**What the rename does and does not buy.** A seat that advertises `brief_hydrate=v1` and cannot
hydrate still passes this check — the enablement checklists are exactly as permissive as before.
What changed is that a PASS now says what it means, so an operator reading `target-advertises-hydrate:
evidence file reports 6 roster target(s) advertise brief_hydrate=v1` is no longer invited to conclude
those targets can hydrate. **The gap is narrowed in prose, not in enforcement**, and recording it as
anything more would be this very defect class applied to its own remedy.

## Relatives

- `claim-scope-exceeds-evidence-scope.md` — the **population** face of the same generalization; there
  the evidence is about too few *members*, here about the wrong *actor*. Both are real evidence,
  wrongly quantified.
- `verification-inspected-the-wrong-object.md` — the **binding** face: there the check inspected a
  different artifact; here it inspected the right artifact, which attests a different property.
- `fake-cheaper-than-real.md` — the umbrella: a stand-in cheaper than the real component along the
  dimension that matters. A flag is the cheapest possible stand-in for a capability.
- `refusal-is-ambient-assert-the-code.md` — the mirror at the other end of the gate: there a PASS
  carries no information because refusal is ambient; here a PASS carries information about the
  wrong thing.
